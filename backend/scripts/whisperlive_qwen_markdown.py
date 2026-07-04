"""Transcribe audio with WhisperLive, organize notes with local Qwen, and write Markdown.

This script is intentionally offline after model downloads:

1. Connect to a local WhisperLive OpenVINO server.
2. Stream a local audio file as 16 kHz mono float32 frames.
3. Collect completed WhisperLive transcript segments.
4. Ask local OpenVINO Qwen on CPU to periodically turn accumulated subtitles
   into classroom notes.
5. Keep updating one Markdown file. With ``--session-id`` it is saved as
   ``data/sessions/{session_id}/structured_notes.md``; without a session it
   falls back to ``data/whisperlive_markdown`` for offline smoke tests.

When ``--post-transcript`` or ``--enable-cloud-graph`` is enabled, the script
also syncs raw WhisperLive transcript events and Markdown snapshots to the
backend. Cloud graph extraction and final session naming are handled by the
backend, not by local Qwen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app import prompts as prompt_templates
from backend.scripts.local_audio_stream_sender import (
    DEFAULT_OPENVINO_ROOT,
    DEFAULT_QWEN_MODEL,
    MEDIA_EXTENSIONS,
    SAMPLE_RATE,
    parse_json_object,
    post_json,
    resolve_backend_session_id,
    result_text,
    send_event,
    sequence_coverage,
    transcript_compare_key,
)


DEFAULT_WHISPERLIVE_HOST = "127.0.0.1"
DEFAULT_WHISPERLIVE_PORT = 9090
DEFAULT_WHISPERLIVE_MODEL = os.getenv(
    "WHISPERLIVE_MODEL",
    "OpenVINO/whisper-large-v3-turbo-fp16-ov",
)
DEFAULT_INPUT = DEFAULT_OPENVINO_ROOT / "test_video"
DEFAULT_OUTPUT_DIR = Path("data/whisperlive_markdown")
DEFAULT_SESSIONS_DIR = Path("data/sessions")
DEFAULT_MARKDOWN_TITLE = "WhisperLive 本地课堂笔记"
ENGLISH_MARKDOWN_TITLE = "WhisperLive Local Classroom Notes"
DEFAULT_WHISPER_LANGUAGE = "auto"
QWEN_DEBUG_LOG_FILENAME = "qwen_notes_debug.jsonl"
DEFAULT_QWEN_DEBUG_LOG_MAX_CHARS = 20000
ASR_HALLUCINATION_PHRASES = (
    "谢谢观看",
    "感谢观看",
    "感谢您的观看",
    "字幕由",
    "amara.org",
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "subscribe to",
)
ASR_MERGE_MAX_GAP_SECONDS = 0.9
ASR_MERGE_MAX_DURATION_SECONDS = 12.0
ASR_MERGE_MAX_WORDS = 45
DEFAULT_MAX_QWEN_SEGMENTS_PER_UPDATE = 8


@dataclass(frozen=True)
class WhisperLiveSegment:
    """One transcript segment received from WhisperLive."""

    start: float
    end: float
    text: str
    completed: bool


@dataclass(frozen=True)
class MarkdownResult:
    """Normalized Qwen markdown payload."""

    summary: list[str]
    sections: list[tuple[str, list[str]]]
    keywords: list[str]
    summary_source_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)
    bullet_source_ids: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class BackendSyncTask:
    """One asynchronous backend sync task."""

    kind: str
    payload: dict[str, Any]


def log(message: str) -> None:
    """Print one timestamped log line."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def env_flag(name: str, default: str = "1") -> bool:
    """Return a boolean-like environment flag."""
    value = os.getenv(name, default).strip().lower()
    return value not in {"0", "false", "no", "off"}


def int_env(name: str, default: int) -> int:
    """Return an integer environment value."""
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class QwenDebugLogger:
    """Session-scoped JSONL logger for local Qwen note generation diagnostics."""

    def __init__(
        self,
        path: Path | None,
        *,
        enabled: bool | None = None,
        max_text_chars: int | None = None,
    ) -> None:
        self.path = path if (env_flag("QWEN_NOTES_DEBUG_LOG") if enabled is None else enabled) else None
        self.max_text_chars = max(
            1000,
            max_text_chars
            if max_text_chars is not None
            else int_env("QWEN_NOTES_DEBUG_LOG_MAX_CHARS", DEFAULT_QWEN_DEBUG_LOG_MAX_CHARS),
        )
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """Return whether this logger writes records."""
        return self.path is not None

    def event(self, event: str, **fields: Any) -> None:
        """Append one JSONL diagnostic event."""
        if self.path is None:
            return
        payload = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
            **{key: self._safe(value) for key, value in fields.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            with self._lock:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception as exc:  # noqa: BLE001 - diagnostics must not break ASR.
            log(f"Qwen debug log write failed: {exc}")

    def _safe(self, value: Any) -> Any:
        """Convert arbitrary values into JSON-safe, size-limited data."""
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, str):
            if len(value) <= self.max_text_chars:
                return value
            return (
                value[: self.max_text_chars]
                + f"...<truncated {len(value) - self.max_text_chars} chars>"
            )
        if isinstance(value, dict):
            return {str(key): self._safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._safe(item) for item in value]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return self._safe(str(value))


def qwen_debug_log_path(output_path: Path) -> Path:
    """Return the JSONL debug log path beside the structured notes file."""
    return output_path.parent / QWEN_DEBUG_LOG_FILENAME


def qwen_debug_segments(segments: list[WhisperLiveSegment]) -> list[dict[str, Any]]:
    """Serialize transcript segments for Qwen diagnostics."""
    return [
        {
            "segment_id": whisperlive_segment_id(segment),
            "start": segment.start,
            "end": segment.end,
            "completed": segment.completed,
            "text": segment.text,
        }
        for segment in segments
    ]


def qwen_debug_result(result: MarkdownResult) -> dict[str, Any]:
    """Serialize a MarkdownResult for diagnostics."""
    return {
        "counts": markdown_result_counts(result),
        "summary": result.summary,
        "sections": [
            {"heading": heading, "bullets": bullets}
            for heading, bullets in result.sections
        ],
        "keywords": result.keywords,
        "summary_source_ids": result.summary_source_ids,
        "bullet_source_ids": {
            f"{heading_key}|{bullet_key}": source_ids
            for (heading_key, bullet_key), source_ids in result.bullet_source_ids.items()
        },
    }


def qwen_debug_dropped_items(
    raw: MarkdownResult,
    grounded: MarkdownResult,
) -> dict[str, Any]:
    """Return note items dropped by grounding."""
    grounded_summary = {markdown_merge_key(item) for item in grounded.summary}
    raw_bullets = [
        {"heading": heading, "bullet": bullet}
        for heading, bullets in raw.sections
        for bullet in bullets
    ]
    grounded_bullets = {
        (markdown_merge_key(heading), markdown_merge_key(bullet))
        for heading, bullets in grounded.sections
        for bullet in bullets
    }
    return {
        "summary": [
            item
            for item in raw.summary
            if markdown_merge_key(item) not in grounded_summary
        ],
        "bullets": [
            item
            for item in raw_bullets
            if (
                markdown_merge_key(str(item["heading"])),
                markdown_merge_key(str(item["bullet"])),
            )
            not in grounded_bullets
        ],
        "keywords": [
            item
            for item in raw.keywords
            if markdown_merge_key(item)
            not in {markdown_merge_key(keyword) for keyword in grounded.keywords}
        ],
    }


def markdown_result_counts(result: MarkdownResult) -> dict[str, int]:
    """Return compact counts for Qwen Markdown diagnostics."""
    return {
        "summary": len(result.summary),
        "sections": len(result.sections),
        "bullets": sum(len(bullets) for _, bullets in result.sections),
        "keywords": len(result.keywords),
    }


def format_markdown_result_counts(result: MarkdownResult) -> str:
    """Format MarkdownResult counts as one log-friendly string."""
    counts = markdown_result_counts(result)
    return (
        f"summary={counts['summary']} sections={counts['sections']} "
        f"bullets={counts['bullets']} keywords={counts['keywords']}"
    )


def whisperlive_segment_id(segment: WhisperLiveSegment) -> str:
    """Create a stable transcript.segment ID for a WhisperLive segment."""
    start = int(round(segment.start * 1000))
    end = int(round(segment.end * 1000))
    digest = hashlib.sha1(  # noqa: S324 - stable local id, not security.
        f"{segment.start:.3f}|{segment.end:.3f}|{segment.text}".encode("utf-8")
    ).hexdigest()[:8]
    return f"seg_whisperlive_{start}_{end}_{digest}"


def qwen_segment_fingerprint(segment: WhisperLiveSegment) -> tuple[float, float, str]:
    """Create the stable in-memory fingerprint used by Qwen batching."""
    return (round(segment.start, 2), round(segment.end, 2), segment.text)


def transcript_payload(segment: WhisperLiveSegment) -> dict[str, Any]:
    """Convert one WhisperLive segment to the existing transcript.segment payload."""
    return {
        "segment_id": whisperlive_segment_id(segment),
        "start_ts": segment.start,
        "end_ts": segment.end,
        "text": segment.text,
        "speaker": "teacher",
        "confidence": None,
        "is_final": bool(segment.completed),
        "source": "whisperlive_openvino",
        "skip_realtime_extraction": True,
    }


def source_segment_payloads(segments: list[WhisperLiveSegment]) -> list[dict[str, Any]]:
    """Build source segment objects for notes-driven graph extraction."""
    return [
        {
            "segment_id": whisperlive_segment_id(segment),
            "start_ts": segment.start,
            "end_ts": segment.end,
            "text": segment.text,
        }
        for segment in segments
        if segment.text.strip()
    ]


class BackendSyncer:
    """Asynchronously sync WhisperLive transcripts and Markdown notes to EDU-Mate."""

    def __init__(
        self,
        *,
        base_url: str,
        session_id: str,
        http_timeout: float,
        post_transcript: bool,
        enable_cloud_graph: bool,
        graph_update_every_seconds: float,
        should_post: Callable[[], bool] | None = None,
    ) -> None:
        self.base_url = base_url
        self.session_id = session_id
        self.http_timeout = http_timeout
        self.post_transcript = post_transcript
        self.enable_cloud_graph = enable_cloud_graph
        self.graph_update_every_seconds = graph_update_every_seconds
        self._should_post = should_post or (lambda: True)
        self._transcript_queue: queue.Queue[BackendSyncTask | None] = queue.Queue()
        self._notes_queue: queue.Queue[BackendSyncTask | None] = queue.Queue()
        self._transcript_thread: threading.Thread | None = None
        self._notes_thread: threading.Thread | None = None
        self._posted_transcript_ids: set[str] = set()
        self._posted_markdown_hashes: set[str] = set()
        self._last_graph_update_at = 0.0
        self.transcript_post_count = 0
        self.notes_post_count = 0

    @property
    def enabled(self) -> bool:
        """Return true when any backend sync behavior is enabled."""
        return bool(
            self.session_id
            and (
                self.post_transcript
                or self.enable_cloud_graph
            )
        )

    def start(self) -> None:
        """Start the worker thread when backend sync is enabled."""
        if not self.enabled:
            return
        if self.post_transcript and self._transcript_thread is None:
            self._transcript_thread = threading.Thread(
                target=self._run_queue,
                args=("transcript", self._transcript_queue),
                daemon=True,
            )
            self._transcript_thread.start()
        if self.enable_cloud_graph and self._notes_thread is None:
            self._notes_thread = threading.Thread(
                target=self._run_queue,
                args=("notes", self._notes_queue),
                daemon=True,
            )
            self._notes_thread.start()

    def stop(self) -> None:
        """Drain queued sync tasks and stop the worker."""
        if self._transcript_thread is not None:
            self._transcript_queue.join()
            self._transcript_queue.put(None)
            self._transcript_thread.join(timeout=10.0)
            self._transcript_thread = None
        if self._notes_thread is not None:
            self._notes_queue.join()
            self._notes_queue.put(None)
            self._notes_thread.join(timeout=10.0)
            self._notes_thread = None

    def enqueue_transcript(self, segment: WhisperLiveSegment) -> None:
        """Queue a final transcript segment for ``POST /events``."""
        if (
            not self.enabled
            or not self.post_transcript
            or not segment.completed
            or not self._should_post()
        ):
            return
        payload = transcript_payload(segment)
        segment_id = str(payload["segment_id"])
        if segment_id in self._posted_transcript_ids:
            return
        self._posted_transcript_ids.add(segment_id)
        self._transcript_queue.put(BackendSyncTask(kind="transcript", payload=payload))

    def enqueue_notes_update(
        self,
        markdown: str,
        segments: list[WhisperLiveSegment],
        update_status: str,
        sequence: int,
        output_path: Path,
        recent_segments: list[WhisperLiveSegment] | None = None,
    ) -> None:
        """Queue one Markdown notes snapshot for cloud graph extraction."""
        if not self.enabled or not self.enable_cloud_graph:
            return
        if update_status != "final" and not self._should_post():
            return
        markdown_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        if markdown_hash in self._posted_markdown_hashes:
            log(f"Skip duplicate notes snapshot hash={markdown_hash[:10]}")
            return

        now = time.monotonic()
        should_throttle = (
            update_status != "final"
            and self.graph_update_every_seconds > 0
            and self._last_graph_update_at > 0
            and now - self._last_graph_update_at < self.graph_update_every_seconds
        )
        if should_throttle:
            wait_left = self.graph_update_every_seconds - (now - self._last_graph_update_at)
            log(
                "Throttle notes graph update "
                f"seq={sequence} status={update_status}; "
                f"wait_left={max(0.0, wait_left):.1f}s"
            )
            return

        self._posted_markdown_hashes.add(markdown_hash)
        self._last_graph_update_at = now
        snapshot_id = f"notes_{sequence:06d}_{update_status}"
        focused_segments = recent_segments or segments
        log(
            "Queue notes graph update "
            f"{snapshot_id}: markdown_chars={len(markdown)} "
            f"source_segments={len(segments)} recent_segments={len(focused_segments)}"
        )
        self._notes_queue.put(
            BackendSyncTask(
                kind="notes",
                payload={
                    "session_id": self.session_id,
                    "snapshot_id": snapshot_id,
                    "sequence": sequence,
                    "markdown": markdown,
                    "markdown_hash": markdown_hash,
                    "source_segments": source_segment_payloads(segments),
                    "recent_source_segments": source_segment_payloads(focused_segments),
                    "update_status": update_status,
                    "output_path": str(output_path),
                },
            )
        )

    def _run_queue(
        self,
        worker_name: str,
        task_queue: queue.Queue[BackendSyncTask | None],
    ) -> None:
        while True:
            task = task_queue.get()
            try:
                if task is None:
                    return
                if task.kind == "transcript":
                    if not self._should_post():
                        continue
                    self._post_transcript(task.payload)
                elif task.kind == "notes":
                    if task.payload.get("update_status") != "final" and not self._should_post():
                        continue
                    self._post_notes(task.payload)
            except Exception as exc:  # noqa: BLE001
                log(f"Backend sync {worker_name} failed: {exc}")
                if task and task.kind == "notes":
                    self._posted_markdown_hashes.discard(str(task.payload.get("markdown_hash", "")))
                if task and task.kind == "transcript":
                    self._posted_transcript_ids.discard(str(task.payload.get("segment_id", "")))
            finally:
                task_queue.task_done()

    def _post_transcript(self, payload: dict[str, Any]) -> None:
        send_event(
            base_url=self.base_url,
            session_id=self.session_id,
            event_type="transcript.segment",
            payload=payload,
            timeout=self.http_timeout,
        )
        self.transcript_post_count += 1

    def _post_notes(self, payload: dict[str, Any]) -> None:
        started_at = time.monotonic()
        response = post_json(
            self.base_url,
            "/agent/knowledge-tree/update-from-notes",
            {key: value for key, value in payload.items() if key != "output_path"},
            timeout=self.http_timeout,
        )
        elapsed = time.monotonic() - started_at
        self.notes_post_count += 1
        warnings = response.get("warnings") or []
        log(
            "POST notes snapshot "
            f"{payload['snapshot_id']} from {payload['output_path']} -> "
            f"{response.get('status')} ops={response.get('graph_patch_operations', 0)} "
            f"metadata_updated={response.get('session_metadata_updated', False)} "
            f"elapsed={elapsed:.2f}s warnings={warnings}"
        )


def find_media(path: Path) -> Path:
    """Resolve a media file from a direct file path or directory."""
    expanded = path.expanduser()
    if expanded.is_file():
        return expanded
    if not expanded.exists():
        raise FileNotFoundError(f"Input path does not exist: {expanded}")
    candidates = sorted(
        item
        for item in expanded.rglob("*")
        if item.is_file() and item.suffix.lower() in MEDIA_EXTENSIONS
    )
    if not candidates:
        raise FileNotFoundError(f"No media file found under: {expanded}")
    return candidates[0]


def iter_audio_packets(
    input_path: Path,
    *,
    packet_seconds: float,
    max_audio_seconds: float,
    sample_rate: int = SAMPLE_RATE,
) -> Iterable[bytes]:
    """Decode media with ffmpeg and yield float32 mono audio packets."""
    if packet_seconds <= 0:
        raise ValueError("packet_seconds must be positive")

    import numpy as np  # noqa: PLC0415

    packet_samples = max(1, int(packet_seconds * sample_rate))
    packet_bytes = packet_samples * 4
    max_samples = (
        int(max_audio_seconds * sample_rate)
        if max_audio_seconds and max_audio_seconds > 0
        else None
    )
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(input_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "f32le",
        "pipe:1",
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    emitted_samples = 0
    stopped_early = False
    try:
        while True:
            if max_samples is not None:
                remaining_samples = max_samples - emitted_samples
                if remaining_samples <= 0:
                    stopped_early = True
                    break
                read_bytes = min(packet_bytes, remaining_samples * 4)
            else:
                read_bytes = packet_bytes

            raw = process.stdout.read(read_bytes)
            if not raw:
                break
            audio = np.frombuffer(raw, dtype=np.float32).copy()
            if audio.size == 0:
                break
            np.nan_to_num(audio, copy=False)
            np.clip(audio, -1.0, 1.0, out=audio)
            emitted_samples += int(audio.size)
            yield audio.tobytes()
    finally:
        if stopped_early and process.poll() is None:
            process.terminate()
        if process.stdout:
            process.stdout.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        return_code = process.wait()

    if return_code != 0 and not stopped_early:
        raise RuntimeError(f"ffmpeg failed for {input_path}: {stderr.strip()}")


class WhisperLiveFileClient:
    """Minimal file client for WhisperLive's websocket protocol."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        model: str,
        language: str | None,
        use_vad: bool,
        send_last_n_segments: int,
        no_speech_thresh: float,
        same_output_threshold: int,
        connect_timeout: float,
        on_completed_segment: Callable[[WhisperLiveSegment], None] | None = None,
        on_partial_segment: Callable[[WhisperLiveSegment], None] | None = None,
    ) -> None:
        try:
            import websocket  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "websocket-client is missing. Run: scripts/dev.sh install-whisperlive"
            ) from exc

        self.websocket_module = websocket
        self.url = f"ws://{host}:{port}"
        self.uid = str(uuid.uuid4())
        self.model = model
        self.language = normalize_whisper_language(language)
        self.use_vad = use_vad
        self.send_last_n_segments = send_last_n_segments
        self.no_speech_thresh = no_speech_thresh
        self.same_output_threshold = same_output_threshold
        self.connect_timeout = connect_timeout
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.segments: list[WhisperLiveSegment] = []
        self._segments_lock = threading.Lock()
        self._seen_completed: set[tuple[str, str, str]] = set()
        self._pending_completed: WhisperLiveSegment | None = None
        self._receiver_error: Exception | None = None
        self._receiver_stop = threading.Event()
        self.on_completed_segment = on_completed_segment
        self.on_partial_segment = on_partial_segment

    def transcribe_file(
        self,
        input_path: Path,
        *,
        packet_seconds: float,
        max_audio_seconds: float,
        send_realtime: bool,
        tail_wait: float,
    ) -> list[WhisperLiveSegment]:
        """Stream one local file and return collected transcript segments."""
        ws = self.websocket_module.create_connection(self.url, timeout=self.connect_timeout)
        receiver = threading.Thread(target=self._receive_loop, args=(ws,), daemon=True)
        receiver.start()
        try:
            self._send_options(ws)
            self._wait_for_ready()
            sent_packets = 0
            start = time.time()
            for packet in iter_audio_packets(
                input_path,
                packet_seconds=packet_seconds,
                max_audio_seconds=max_audio_seconds,
            ):
                ws.send_binary(packet)
                sent_packets += 1
                if send_realtime:
                    time.sleep(packet_seconds)
            log(f"Sent {sent_packets} audio packet(s) in {time.time() - start:.2f}s")
            self._wait_for_tail(tail_wait)
            ws.send_binary(b"END_OF_AUDIO")
            time.sleep(0.5)
            return self._final_segments()
        finally:
            self._receiver_stop.set()
            try:
                ws.close()
            except Exception:
                pass
            receiver.join(timeout=2.0)

    def _send_options(self, ws: Any) -> None:
        ws.send(
            json.dumps(
                {
                    "uid": self.uid,
                    "language": self.language,
                    "task": "transcribe",
                    "model": self.model,
                    "use_vad": self.use_vad,
                    "send_last_n_segments": self.send_last_n_segments,
                    "no_speech_thresh": self.no_speech_thresh,
                    "clip_audio": False,
                    "same_output_threshold": self.same_output_threshold,
                    "enable_translation": False,
                    "target_language": None,
                    "hotwords": None,
                    "enable_diarization": False,
                    "max_speakers": 1,
                    "word_timestamps": False,
                },
                ensure_ascii=False,
            )
        )

    def _receive_loop(self, ws: Any) -> None:
        while not self._receiver_stop.is_set():
            try:
                raw = ws.recv()
            except Exception as exc:  # noqa: BLE001
                self._receiver_error = exc
                return
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if message.get("uid") != self.uid:
                continue
            self.messages.put(message)
            for segment in parse_whisperlive_segments(message):
                self._remember_segment(segment)

    def _remember_segment(self, segment: WhisperLiveSegment) -> None:
        completed_to_notify: list[WhisperLiveSegment] = []
        partial_to_notify: WhisperLiveSegment | None = None
        with self._segments_lock:
            if segment.completed:
                key = (f"{segment.start:.3f}", f"{segment.end:.3f}", segment.text)
                if key in self._seen_completed:
                    return
                self._seen_completed.add(key)
                completed_to_notify = self._remember_completed_segment_locked(segment)
            elif not self.segments or self.segments[-1].completed:
                self.segments.append(segment)
                partial_to_notify = segment
            else:
                self.segments[-1] = segment
                partial_to_notify = segment

        if self.on_completed_segment is not None:
            for completed_segment in completed_to_notify:
                self.on_completed_segment(completed_segment)
        if partial_to_notify is not None and self.on_partial_segment is not None:
            self.on_partial_segment(partial_to_notify)

    def _remember_completed_segment_locked(
        self,
        segment: WhisperLiveSegment,
    ) -> list[WhisperLiveSegment]:
        """Store one completed segment, merging tiny adjacent final fragments."""
        to_notify: list[WhisperLiveSegment] = []

        if self._pending_completed is not None and should_merge_asr_segments(
            self._pending_completed,
            segment,
        ):
            merged = merge_asr_segments(self._pending_completed, segment)
            self._replace_pending_completed_locked(merged)
            self._pending_completed = merged
            if should_flush_completed_segment(merged):
                to_notify.extend(self._flush_pending_completed_locked())
            return to_notify

        to_notify.extend(self._flush_pending_completed_locked())
        self.segments.append(segment)
        self._pending_completed = segment
        if should_flush_completed_segment(segment):
            to_notify.extend(self._flush_pending_completed_locked())
        return to_notify

    def _replace_pending_completed_locked(self, merged: WhisperLiveSegment) -> None:
        """Replace the pending completed segment in the collected segment list."""
        pending = self._pending_completed
        if pending is None:
            self.segments.append(merged)
            return
        for index in range(len(self.segments) - 1, -1, -1):
            if self.segments[index] == pending:
                self.segments[index] = merged
                return
        self.segments.append(merged)

    def _flush_pending_completed_locked(self) -> list[WhisperLiveSegment]:
        """Return the pending completed segment and mark it ready for callback."""
        if self._pending_completed is None:
            return []
        pending = self._pending_completed
        self._pending_completed = None
        return [pending]

    def _flush_pending_completed(self) -> None:
        """Flush a final segment that was delayed while waiting for possible merge."""
        with self._segments_lock:
            pending_segments = self._flush_pending_completed_locked()
        if self.on_completed_segment is not None:
            for segment in pending_segments:
                self.on_completed_segment(segment)

    def _wait_for_ready(self) -> None:
        deadline = time.time() + self.connect_timeout
        while time.time() < deadline:
            try:
                message = self.messages.get(timeout=0.2)
            except queue.Empty:
                if self._receiver_error:
                    raise RuntimeError(f"WhisperLive receiver failed: {self._receiver_error}")
                continue
            if message.get("status") == "ERROR":
                raise RuntimeError(f"WhisperLive error: {message.get('message')}")
            if message.get("message") == "SERVER_READY":
                log(f"WhisperLive ready with backend {message.get('backend')}")
                return
        raise TimeoutError("Timed out waiting for WhisperLive SERVER_READY")

    def _wait_for_tail(self, tail_wait: float) -> None:
        deadline = time.time() + max(0.0, tail_wait)
        while time.time() < deadline:
            time.sleep(0.1)
            if self._receiver_error:
                break

    def _final_segments(self) -> list[WhisperLiveSegment]:
        self._flush_pending_completed()
        return self.snapshot_segments(completed_only=False)

    def snapshot_segments(self, *, completed_only: bool = True) -> list[WhisperLiveSegment]:
        """Return a stable copy of collected transcript segments."""
        with self._segments_lock:
            return normalize_collected_segments(
                self.segments,
                completed_only=completed_only,
            )


def parse_whisperlive_segments(message: dict[str, Any]) -> list[WhisperLiveSegment]:
    """Extract normalized segments from one WhisperLive websocket message."""
    raw_segments = message.get("segments")
    if not isinstance(raw_segments, list):
        return []
    segments: list[WhisperLiveSegment] = []
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not is_useful_asr_text(text):
            continue
        try:
            start = float(item.get("start", 0.0))
            end = float(item.get("end", start))
        except (TypeError, ValueError):
            start = 0.0
            end = 0.0
        segments.append(
            WhisperLiveSegment(
                start=start,
                end=end,
                text=text,
                completed=bool(item.get("completed", False)),
            )
        )
    return segments


def normalize_whisper_language(language: str | None) -> str | None:
    """Normalize CLI/app language values for WhisperLive.

    WhisperLive's websocket protocol accepts ``None`` to let the backend detect
    language when the selected backend supports it. Users can still force a
    language with values like ``zh``, ``en``, ``<|zh|>`` or ``zh-CN``.
    """
    if language is None:
        return None
    normalized = language.strip()
    if not normalized:
        return None
    lowered = normalized.lower()
    if lowered in {"auto", "detect", "none", "null"}:
        return None
    token_match = re.fullmatch(r"<\|([a-z]{2,3})\|>", lowered)
    if token_match:
        return token_match.group(1)
    if lowered.startswith("zh"):
        return "zh"
    if lowered.startswith("en"):
        return "en"
    return lowered


def is_useful_asr_text(text: str) -> bool:
    """Filter empty/noisy Whisper hallucinations before they reach the app."""
    stripped = text.strip()
    if not stripped:
        return False
    compact = re.sub(r"\s+", " ", stripped).strip()
    lowered = compact.lower()
    if any(phrase in lowered for phrase in ASR_HALLUCINATION_PHRASES):
        return False

    content_chars = re.findall(r"[\w\u4e00-\u9fff]", compact, flags=re.UNICODE)
    if len(content_chars) < 2:
        return False
    if _dominant_character_ratio(content_chars) >= 0.8 and len(content_chars) >= 6:
        return False
    return True


def _dominant_character_ratio(chars: list[str]) -> float:
    if not chars:
        return 1.0
    counts: dict[str, int] = {}
    for char in chars:
        counts[char.lower()] = counts.get(char.lower(), 0) + 1
    return max(counts.values()) / len(chars)


def should_merge_asr_segments(left: WhisperLiveSegment, right: WhisperLiveSegment) -> bool:
    """Return whether two adjacent completed ASR fragments should be one sentence."""
    if not left.completed or not right.completed:
        return False
    if right.start < left.start:
        return False
    gap = right.start - left.end
    if gap > ASR_MERGE_MAX_GAP_SECONDS:
        return False
    combined_duration = max(left.end, right.end) - min(left.start, right.start)
    if combined_duration > ASR_MERGE_MAX_DURATION_SECONDS:
        return False

    left_words = asr_word_count(left.text)
    right_words = asr_word_count(right.text)
    if left_words + right_words > ASR_MERGE_MAX_WORDS:
        return False

    if ends_with_sentence_terminal(left.text) and left_words >= 3:
        return False
    if right_words <= 4 and left_words > 8 and gap > 0.35:
        return False
    return (
        left_words <= 4
        or right_words <= 4
        or not ends_with_sentence_terminal(left.text)
    )


def merge_asr_segments(left: WhisperLiveSegment, right: WhisperLiveSegment) -> WhisperLiveSegment:
    """Merge two adjacent completed ASR fragments into one transcript segment."""
    return WhisperLiveSegment(
        start=min(left.start, right.start),
        end=max(left.end, right.end),
        text=join_asr_text(left.text, right.text),
        completed=True,
    )


def join_asr_text(left: str, right: str) -> str:
    """Join adjacent ASR text while preserving natural spacing around punctuation."""
    left_text = left.strip()
    right_text = right.strip()
    if not left_text:
        return right_text
    if not right_text:
        return left_text
    if re.match(r"^[,.;:!?，。！？；：]", right_text):
        return f"{left_text}{right_text}"
    return f"{left_text} {right_text}"


def should_flush_completed_segment(segment: WhisperLiveSegment) -> bool:
    """Return true when a final segment is unlikely to need the next fragment."""
    words = asr_word_count(segment.text)
    duration = max(0.0, segment.end - segment.start)
    if ends_with_sentence_terminal(segment.text) and (words >= 3 or duration >= 0.8):
        return True
    return duration >= ASR_MERGE_MAX_DURATION_SECONDS or words >= ASR_MERGE_MAX_WORDS


def ends_with_sentence_terminal(text: str) -> bool:
    return bool(re.search(r"[.!?。！？]['\")\]}»”’]*\s*$", text.strip()))


def asr_word_count(text: str) -> int:
    latin_words = re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", text)
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", text)
    return len(latin_words) + len(cjk_chars)


def overlap_seconds(left: WhisperLiveSegment, right: WhisperLiveSegment) -> float:
    """Return timestamp overlap between two transcript segments."""
    return max(0.0, min(left.end, right.end) - max(left.start, right.start))


def is_subsumed_partial(
    segment: WhisperLiveSegment,
    completed_segments: list[WhisperLiveSegment],
    *,
    threshold: float = 0.65,
) -> bool:
    """Return true when a partial segment is mostly covered by completed output."""
    if segment.completed:
        return False
    duration = max(0.01, segment.end - segment.start)
    return any(
        overlap_seconds(segment, completed) / duration >= threshold
        for completed in completed_segments
    )


def normalize_collected_segments(
    segments: list[WhisperLiveSegment],
    *,
    completed_only: bool,
) -> list[WhisperLiveSegment]:
    """Deduplicate and normalize collected WhisperLive segments."""
    clean: list[WhisperLiveSegment] = []
    completed_segments = [segment for segment in segments if segment.completed]
    seen_text_ranges: set[tuple[float, float, str]] = set()
    for segment in segments:
        if completed_only and not segment.completed:
            continue
        text = segment.text.strip()
        if not text:
            continue
        if is_subsumed_partial(segment, completed_segments):
            continue
        key = (round(segment.start, 2), round(segment.end, 2), text)
        if key in seen_text_ranges:
            continue
        seen_text_ranges.add(key)
        clean.append(
            WhisperLiveSegment(
                start=segment.start,
                end=segment.end,
                text=text,
                completed=segment.completed,
            )
        )
    return coalesce_completed_asr_segments(
        sorted(clean, key=lambda item: (item.start, item.end, item.completed))
    )


def coalesce_completed_asr_segments(
    segments: list[WhisperLiveSegment],
) -> list[WhisperLiveSegment]:
    """Merge adjacent completed fragments in a normalized segment list."""
    coalesced: list[WhisperLiveSegment] = []
    for segment in segments:
        if (
            coalesced
            and coalesced[-1].completed
            and segment.completed
            and should_merge_asr_segments(coalesced[-1], segment)
        ):
            coalesced[-1] = merge_asr_segments(coalesced[-1], segment)
        else:
            coalesced.append(segment)
    return coalesced


class QwenMarkdownPolisher:
    """Local OpenVINO Qwen markdown generator."""

    def __init__(
        self,
        *,
        model_path: Path,
        device: str,
        debug_logger: QwenDebugLogger | None = None,
    ) -> None:
        import openvino_genai as ov_genai  # noqa: PLC0415

        self.debug_logger = debug_logger
        self._generation_count = 0
        log(f"Loading Qwen markdown model: {model_path} on {device}")
        started_at = time.monotonic()
        self._debug(
            "model_load_start",
            model_path=str(model_path),
            device=device,
        )
        try:
            self.pipe = ov_genai.LLMPipeline(str(model_path), device)
        except Exception as exc:  # noqa: BLE001
            self._debug(
                "model_load_error",
                model_path=str(model_path),
                device=device,
                elapsed_seconds=time.monotonic() - started_at,
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            raise
        self._debug(
            "model_load_done",
            model_path=str(model_path),
            device=device,
            elapsed_seconds=time.monotonic() - started_at,
        )

    def generate(
        self,
        segments: list[WhisperLiveSegment],
        *,
        max_new_tokens: int,
        domain_terms: list[str],
    ) -> MarkdownResult:
        """Generate structured Markdown data from transcript segments."""
        generation_count = getattr(self, "_generation_count", 0) + 1
        self._generation_count = generation_count
        call_id = f"qwen_notes_{generation_count:06d}_{uuid.uuid4().hex[:8]}"
        prompt = build_markdown_prompt(segments, domain_terms=domain_terms)
        self._debug(
            "generate_start",
            call_id=call_id,
            max_new_tokens=max_new_tokens,
            domain_terms=domain_terms,
            prompt_language=prompt_templates.qwen_notes_prompt_language(
                [
                    {
                        "text": segment.text,
                    }
                    for segment in segments
                ]
            ),
            segment_count=len(segments),
            segments=qwen_debug_segments(segments),
            prompt_chars=len(prompt),
            prompt=prompt,
        )
        try:
            raw, payload = self._generate_parseable_payload(
                prompt,
                max_new_tokens=max_new_tokens,
                call_id=call_id,
                attempt="initial",
            )
            grounded = self._normalize_grounded_result(
                payload,
                segments,
                domain_terms,
                call_id=call_id,
                attempt="initial",
            )
            if has_structured_markdown_content(grounded):
                self._debug(
                    "generate_success",
                    call_id=call_id,
                    attempt="initial",
                    result=qwen_debug_result(grounded),
                )
                return grounded

            reason = "Qwen output did not contain usable summary or sections after grounding"
            log(
                "Qwen markdown quality check failed; retrying with stricter prompt: "
                f"{reason}; counts={format_markdown_result_counts(grounded)}"
            )
            retry_prompt = build_markdown_quality_retry_prompt(
                segments,
                domain_terms=domain_terms,
                previous_output=raw,
                reason=reason,
            )
            self._debug(
                "quality_retry_start",
                call_id=call_id,
                reason=reason,
                previous_counts=markdown_result_counts(grounded),
                retry_prompt_chars=len(retry_prompt),
                retry_prompt=retry_prompt,
            )
            retry_raw, retry_payload = self._generate_parseable_payload(
                retry_prompt,
                max_new_tokens=max_new_tokens,
                call_id=call_id,
                attempt="quality_retry",
            )
            retry_grounded = self._normalize_grounded_result(
                retry_payload,
                segments,
                domain_terms,
                call_id=call_id,
                attempt="quality_retry",
            )
            if not has_structured_markdown_content(retry_grounded):
                self._debug(
                    "quality_retry_failed",
                    call_id=call_id,
                    result=qwen_debug_result(retry_grounded),
                )
                raise RuntimeError(
                    "Qwen markdown quality check failed after retry: "
                    f"{format_markdown_result_counts(retry_grounded)}"
                )
            self._debug(
                "generate_success",
                call_id=call_id,
                attempt="quality_retry",
                result=qwen_debug_result(retry_grounded),
            )
            return retry_grounded
        except Exception as exc:  # noqa: BLE001
            self._debug(
                "generate_error",
                call_id=call_id,
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            raise

    def _generate_parseable_payload(
        self,
        prompt: str,
        *,
        max_new_tokens: int,
        call_id: str,
        attempt: str,
    ) -> tuple[str, dict[str, Any]]:
        """Generate a JSON payload, retrying once when Qwen output is truncated."""
        self._debug(
            "pipe_generate_start",
            call_id=call_id,
            attempt=attempt,
            max_new_tokens=max_new_tokens,
            prompt_chars=len(prompt),
        )
        started_at = time.monotonic()
        try:
            raw = result_text(
                self.pipe.generate(prompt, max_new_tokens=max_new_tokens, do_sample=False)
            )
        except Exception as exc:  # noqa: BLE001
            self._debug(
                "pipe_generate_error",
                call_id=call_id,
                attempt=attempt,
                elapsed_seconds=time.monotonic() - started_at,
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            raise
        self._debug(
            "pipe_generate_done",
            call_id=call_id,
            attempt=attempt,
            elapsed_seconds=time.monotonic() - started_at,
            raw_chars=len(raw),
            raw_output=raw,
        )
        try:
            payload = parse_qwen_markdown_payload(
                raw,
                max_new_tokens=max_new_tokens,
                pipe=self.pipe,
                debug_logger=self._debug_logger(),
                call_id=call_id,
                attempt=attempt,
            )
            self._debug(
                "parse_success",
                call_id=call_id,
                attempt=attempt,
                payload=payload,
            )
            return raw, payload
        except RuntimeError as exc:
            self._debug(
                "parse_error",
                call_id=call_id,
                attempt=attempt,
                error=str(exc),
            )
            if not is_truncated_qwen_json_error(exc):
                raise
            retry_tokens = expanded_qwen_json_token_budget(max_new_tokens)
            if retry_tokens <= max_new_tokens:
                raise
            log(
                "Qwen markdown JSON looked truncated; retrying generation "
                f"with max_new_tokens={retry_tokens}"
            )
            self._debug(
                "truncated_json_retry_start",
                call_id=call_id,
                attempt=attempt,
                retry_tokens=retry_tokens,
            )
            retry_started_at = time.monotonic()
            retry_raw = result_text(
                self.pipe.generate(
                    prompt,
                    max_new_tokens=retry_tokens,
                    do_sample=False,
                )
            )
            self._debug(
                "truncated_json_retry_done",
                call_id=call_id,
                attempt=attempt,
                elapsed_seconds=time.monotonic() - retry_started_at,
                raw_chars=len(retry_raw),
                raw_output=retry_raw,
            )
            retry_payload = parse_qwen_markdown_payload(
                retry_raw,
                max_new_tokens=retry_tokens,
                pipe=self.pipe,
                debug_logger=self._debug_logger(),
                call_id=call_id,
                attempt=f"{attempt}_truncated_retry",
            )
            self._debug(
                "parse_success",
                call_id=call_id,
                attempt=f"{attempt}_truncated_retry",
                payload=retry_payload,
            )
            return retry_raw, retry_payload

    def _normalize_grounded_result(
        self,
        payload: dict[str, Any],
        segments: list[WhisperLiveSegment],
        domain_terms: list[str],
        *,
        call_id: str,
        attempt: str,
    ) -> MarkdownResult:
        """Normalize one Qwen payload and enforce transcript grounding."""
        result = normalize_markdown_result(payload, segments)
        log(f"Qwen markdown raw counts: {format_markdown_result_counts(result)}")
        grounded = enforce_markdown_grounding(
            result,
            segments=segments,
            domain_terms=domain_terms,
        )
        log(f"Qwen markdown grounded counts: {format_markdown_result_counts(grounded)}")
        self._debug(
            "grounding_result",
            call_id=call_id,
            attempt=attempt,
            raw_result=qwen_debug_result(result),
            grounded_result=qwen_debug_result(grounded),
            dropped=qwen_debug_dropped_items(result, grounded),
        )
        return grounded

    def _debug_logger(self) -> QwenDebugLogger | None:
        """Return the optional debug logger for object.__new__ test instances."""
        return getattr(self, "debug_logger", None)

    def _debug(self, event: str, **fields: Any) -> None:
        """Write a Qwen debug event if a logger is attached."""
        logger = self._debug_logger()
        if logger is not None:
            logger.event(event, **fields)


def parse_qwen_markdown_payload(
    raw: str,
    *,
    max_new_tokens: int,
    pipe: Any,
    debug_logger: QwenDebugLogger | None = None,
    call_id: str = "",
    attempt: str = "",
) -> dict[str, Any]:
    """Parse Qwen notes JSON, using Qwen once more only for malformed JSON repair."""
    try:
        return parse_json_object(raw)
    except ValueError as first_error:
        if debug_logger is not None:
            debug_logger.event(
                "json_parse_failed",
                call_id=call_id,
                attempt=attempt,
                error=str(first_error),
                raw_chars=len(raw),
                raw_output=raw,
            )
        repair_tokens = expanded_qwen_json_token_budget(max_new_tokens)
        repair_prompt = build_markdown_repair_prompt(raw)
        if debug_logger is not None:
            debug_logger.event(
                "json_repair_start",
                call_id=call_id,
                attempt=attempt,
                repair_tokens=repair_tokens,
                repair_prompt_chars=len(repair_prompt),
                repair_prompt=repair_prompt,
            )
        repair_started_at = time.monotonic()
        repaired = result_text(
            pipe.generate(
                repair_prompt,
                max_new_tokens=repair_tokens,
                do_sample=False,
            )
        )
        if debug_logger is not None:
            debug_logger.event(
                "json_repair_done",
                call_id=call_id,
                attempt=attempt,
                elapsed_seconds=time.monotonic() - repair_started_at,
                repaired_chars=len(repaired),
                repaired_output=repaired,
            )
        try:
            payload = parse_json_object(repaired)
        except ValueError as exc:
            if debug_logger is not None:
                debug_logger.event(
                    "json_repair_parse_failed",
                    call_id=call_id,
                    attempt=attempt,
                    error=str(exc),
                    repaired_output=repaired,
                )
            raise RuntimeError(f"Qwen markdown JSON parse failed after repair: {exc}") from exc
        if debug_logger is not None:
            debug_logger.event(
                "json_repair_parse_success",
                call_id=call_id,
                attempt=attempt,
                payload=payload,
            )
        return payload


def expanded_qwen_json_token_budget(max_new_tokens: int) -> int:
    """Give malformed/truncated JSON repair enough room to close the object."""
    if max_new_tokens >= 4096:
        return max_new_tokens
    return min(4096, max(max_new_tokens + 512, max_new_tokens * 2))


def is_truncated_qwen_json_error(exc: BaseException) -> bool:
    """Return true for parse failures that look like incomplete JSON output."""
    text = str(exc).lower()
    return "unbalanced json object" in text or "unterminated" in text


def has_structured_markdown_content(result: MarkdownResult) -> bool:
    """Return true only when Qwen produced actual note content, not just keywords."""
    return bool(result.summary or result.sections)


def markdown_item_sources(
    source_map: dict[str, tuple[str, ...]],
    text: str,
) -> tuple[str, ...]:
    """Return source ids for one note item by its normalized text key."""
    return source_map.get(markdown_merge_key(text), ())


def markdown_bullet_sources(
    source_map: dict[tuple[str, str], tuple[str, ...]],
    heading: str,
    bullet: str,
) -> tuple[str, ...]:
    """Return source ids for one section bullet."""
    return source_map.get((markdown_merge_key(heading), markdown_merge_key(bullet)), ())


def merge_markdown_results(
    previous: MarkdownResult | None,
    update: MarkdownResult,
    *,
    max_summary_items: int = 12,
    max_keywords: int = 24,
    max_bullets_per_section: int = 8,
) -> MarkdownResult:
    """Merge a small Qwen note update into the accumulated classroom notes."""
    if previous is None:
        return update

    summary = merge_note_items(
        previous.summary,
        update.summary,
        source_maps=[previous.summary_source_ids, update.summary_source_ids],
        limit=max_summary_items,
    )
    keywords = merge_text_items(
        previous.keywords,
        update.keywords,
        limit=max_keywords,
    )

    summary_source_ids = build_merged_source_map(
        summary,
        [previous.summary_source_ids, update.summary_source_ids],
    )

    sections: list[tuple[str, list[str]]] = []
    bullet_source_ids: dict[tuple[str, str], tuple[str, ...]] = {}
    section_index: dict[str, int] = {}
    for result, (heading, bullets) in [
        *((previous, section) for section in previous.sections),
        *((update, section) for section in update.sections),
    ]:
        clean_heading = heading.strip() or "课堂重点"
        key = markdown_merge_key(clean_heading)
        if key in section_index:
            index = section_index[key]
            existing_heading, existing_bullets = sections[index]
            merged_bullets = merge_note_items(
                existing_bullets,
                bullets,
                source_maps=[bullet_source_ids_for_heading(bullet_source_ids, existing_heading), result.bullet_source_ids],
                heading=clean_heading,
                limit=max_bullets_per_section,
            )
            sections[index] = (
                existing_heading,
                merged_bullets,
            )
            bullet_source_ids.update(
                build_merged_bullet_source_map(
                    clean_heading,
                    merged_bullets,
                    [
                        bullet_source_ids_for_heading(
                            bullet_source_ids,
                            existing_heading,
                        ),
                        result.bullet_source_ids,
                    ],
                )
            )
            continue
        section_index[key] = len(sections)
        merged_bullets = merge_note_items(
            [],
            bullets,
            source_maps=[result.bullet_source_ids],
            heading=clean_heading,
            limit=max_bullets_per_section,
        )
        sections.append(
            (
                clean_heading,
                merged_bullets,
            )
        )
        bullet_source_ids.update(
            build_merged_bullet_source_map(
                clean_heading,
                merged_bullets,
                [result.bullet_source_ids],
            )
        )

    return MarkdownResult(
        summary=summary,
        sections=[(heading, bullets) for heading, bullets in sections if bullets],
        keywords=keywords,
        summary_source_ids=summary_source_ids,
        bullet_source_ids=bullet_source_ids,
    )


def merge_text_items(
    existing: list[str],
    incoming: list[str],
    *,
    limit: int,
) -> list[str]:
    """Merge text lists by normalized content while preserving order."""
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*existing, *incoming]:
        text = item.strip()
        if not text:
            continue
        key = markdown_merge_key(text)
        if key in seen:
            continue
        seen.add(key)
        merged.append(text)
        if len(merged) >= limit:
            break
    return merged


def merge_note_items(
    existing: list[str],
    incoming: list[str],
    *,
    source_maps: list[dict[Any, tuple[str, ...]]],
    limit: int,
    heading: str | None = None,
) -> list[str]:
    """Merge note text while preserving source ids in later helper maps."""
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*existing, *incoming]:
        text = item.strip()
        if not text:
            continue
        key = markdown_merge_key(text)
        if key in seen:
            continue
        seen.add(key)
        merged.append(text)
        if len(merged) >= limit:
            break
    return merged


def source_ids_for_note_key(
    text: str,
    source_maps: list[dict[str, tuple[str, ...]]],
) -> tuple[str, ...]:
    """Merge source ids for a summary item from multiple normalized maps."""
    key = markdown_merge_key(text)
    return merge_source_id_tuples([source_map.get(key, ()) for source_map in source_maps])


def source_ids_for_bullet_key(
    heading: str,
    bullet: str,
    source_maps: list[dict[Any, tuple[str, ...]]],
) -> tuple[str, ...]:
    """Merge source ids for one section bullet from multiple map shapes."""
    bullet_key = markdown_merge_key(bullet)
    heading_key = markdown_merge_key(heading)
    candidates: list[tuple[str, ...]] = []
    for source_map in source_maps:
        candidates.append(source_map.get((heading_key, bullet_key), ()))  # type: ignore[arg-type]
        candidates.append(source_map.get(bullet_key, ()))  # type: ignore[arg-type]
    return merge_source_id_tuples(candidates)


def merge_source_id_tuples(values: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
    """Merge source id tuples preserving order and removing duplicates."""
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for source_id in value:
            if source_id in seen:
                continue
            seen.add(source_id)
            merged.append(source_id)
    return tuple(merged)


def build_merged_source_map(
    items: list[str],
    source_maps: list[dict[str, tuple[str, ...]]],
) -> dict[str, tuple[str, ...]]:
    """Build a normalized summary source map for merged note items."""
    return {
        markdown_merge_key(item): source_ids_for_note_key(item, source_maps)
        for item in items
        if source_ids_for_note_key(item, source_maps)
    }


def build_merged_bullet_source_map(
    heading: str,
    bullets: list[str],
    source_maps: list[dict[Any, tuple[str, ...]]],
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Build a normalized bullet source map for merged section bullets."""
    return {
        (markdown_merge_key(heading), markdown_merge_key(bullet)): source_ids
        for bullet in bullets
        if (
            source_ids := source_ids_for_bullet_key(
                heading,
                bullet,
                source_maps,
            )
        )
    }


def bullet_source_ids_for_heading(
    source_map: dict[tuple[str, str], tuple[str, ...]],
    heading: str,
) -> dict[str, tuple[str, ...]]:
    """Return a bullet-only source map for one heading."""
    heading_key = markdown_merge_key(heading)
    return {
        bullet_key: source_ids
        for (candidate_heading, bullet_key), source_ids in source_map.items()
        if candidate_heading == heading_key
    }


def markdown_merge_key(text: str) -> str:
    """Normalize one note item for lightweight deduplication."""
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).lower()


def generate_incremental_markdown_result(
    qwen: QwenMarkdownPolisher,
    segments: list[WhisperLiveSegment],
    *,
    max_new_tokens: int,
    domain_terms: list[str],
    max_qwen_segments_per_update: int,
    debug_logger: QwenDebugLogger | None = None,
) -> MarkdownResult:
    """Generate notes in small batches and merge the Qwen results."""
    result: MarkdownResult | None = None
    batch_size = max(1, max_qwen_segments_per_update)
    if debug_logger is not None:
        debug_logger.event(
            "final_incremental_generation_start",
            segment_count=len(segments),
            batch_size=batch_size,
            max_new_tokens=max_new_tokens,
            domain_terms=domain_terms,
            segments=qwen_debug_segments(segments),
        )
    for index in range(0, len(segments), batch_size):
        batch = segments[index : index + batch_size]
        if debug_logger is not None:
            debug_logger.event(
                "final_incremental_batch_start",
                batch_index=index // batch_size + 1,
                batch_size=len(batch),
                segments=qwen_debug_segments(batch),
            )
        batch_result = qwen.generate(
            batch,
            max_new_tokens=max_new_tokens,
            domain_terms=domain_terms,
        )
        result = merge_markdown_results(result, batch_result)
        if debug_logger is not None:
            debug_logger.event(
                "final_incremental_batch_done",
                batch_index=index // batch_size + 1,
                batch_result=qwen_debug_result(batch_result),
                merged_result=qwen_debug_result(result),
            )
    if not has_structured_markdown_content(result):
        raise RuntimeError(
            "Qwen markdown result has no summary or sections after incremental generation: "
            f"{format_markdown_result_counts(result or MarkdownResult([], [], []))}"
        )
    if debug_logger is not None:
        debug_logger.event(
            "final_incremental_generation_done",
            result=qwen_debug_result(result),
        )
    return result


def build_markdown_quality_retry_prompt(
    segments: list[WhisperLiveSegment],
    *,
    domain_terms: list[str],
    previous_output: str,
    reason: str,
) -> str:
    """Ask Qwen to regenerate when JSON was valid but note content was unusable."""
    return prompt_templates.qwen_markdown_notes_quality_retry_prompt(
        segments=[
            {
                "id": whisperlive_segment_id(segment),
                "start": segment.start,
                "end": segment.end,
                "text": segment.text,
            }
            for segment in segments
        ],
        domain_terms=domain_terms,
        previous_output=previous_output,
        reason=reason,
    )


def build_markdown_prompt(
    segments: list[WhisperLiveSegment],
    *,
    domain_terms: list[str],
) -> str:
    """Build a strict JSON prompt for Qwen markdown cleanup."""
    return prompt_templates.qwen_markdown_notes_prompt(
        segments=[
            {
                "id": whisperlive_segment_id(segment),
                "start": segment.start,
                "end": segment.end,
                "text": segment.text,
            }
            for segment in segments
        ],
        domain_terms=domain_terms,
    )


def build_markdown_repair_prompt(raw_text: str) -> str:
    """Ask Qwen to repair malformed markdown JSON."""
    return prompt_templates.qwen_markdown_notes_repair_prompt(raw_text)


def normalize_markdown_result(
    payload: dict[str, Any],
    segments: list[WhisperLiveSegment],
) -> MarkdownResult:
    """Normalize Qwen JSON into MarkdownResult."""
    summary, summary_source_ids = clean_note_items(payload.get("summary"))
    keywords = clean_list(payload.get("keywords"))

    sections: list[tuple[str, list[str]]] = []
    bullet_source_ids: dict[tuple[str, str], tuple[str, ...]] = {}
    raw_sections = payload.get("sections")
    if isinstance(raw_sections, list):
        for item in raw_sections:
            if not isinstance(item, dict):
                continue
            heading = clean_scalar(item.get("heading"))
            bullets, bullet_sources = clean_note_items(item.get("bullets"))
            if heading and bullets:
                sections.append((heading, bullets))
                heading_key = markdown_merge_key(heading)
                for bullet in bullets:
                    source_ids = bullet_sources.get(markdown_merge_key(bullet), ())
                    if source_ids:
                        bullet_source_ids[(heading_key, markdown_merge_key(bullet))] = source_ids

    if not sections and summary:
        sections.append(("课堂要点", summary))
        heading_key = markdown_merge_key("课堂要点")
        for item in summary:
            source_ids = summary_source_ids.get(markdown_merge_key(item), ())
            if source_ids:
                bullet_source_ids[(heading_key, markdown_merge_key(item))] = source_ids
    return MarkdownResult(
        summary=summary,
        sections=sections,
        keywords=keywords,
        summary_source_ids=summary_source_ids,
        bullet_source_ids=bullet_source_ids,
    )


def clean_note_items(value: object) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """Normalize note items that can be strings or source-backed objects."""
    if not isinstance(value, list):
        return [], {}
    items: list[str] = []
    sources: dict[str, tuple[str, ...]] = {}
    seen: set[str] = set()
    for raw_item in value:
        text, source_ids = clean_note_item(raw_item)
        if not text:
            continue
        key = markdown_merge_key(text)
        if key in seen:
            if source_ids:
                sources[key] = merge_source_id_tuples([sources.get(key, ()), source_ids])
            continue
        seen.add(key)
        items.append(text)
        if source_ids:
            sources[key] = source_ids
    return items, sources


def clean_note_item(value: object) -> tuple[str, tuple[str, ...]]:
    """Return note text and declared transcript evidence ids."""
    if isinstance(value, dict):
        text = clean_scalar(
            value.get("text")
            or value.get("content")
            or value.get("summary")
            or value.get("bullet")
        )
        source_ids = clean_source_segment_ids(value.get("source_segment_ids"))
        return text, source_ids
    return clean_scalar(value), ()


def clean_source_segment_ids(value: object) -> tuple[str, ...]:
    """Normalize a source_segment_ids field."""
    if not isinstance(value, list):
        return ()
    source_ids: list[str] = []
    seen: set[str] = set()
    for item in value:
        source_id = clean_scalar(item)
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        source_ids.append(source_id)
    return tuple(source_ids)


def fallback_markdown_result(segments: list[WhisperLiveSegment]) -> MarkdownResult:
    """Build an empty MarkdownResult while Qwen notes are still pending."""
    return MarkdownResult(
        summary=[],
        sections=[],
        keywords=[],
    )


def combined_transcript_key(segments: list[WhisperLiveSegment]) -> str:
    """Return normalized transcript text used for grounding note items."""
    return transcript_compare_key("".join(segment.text for segment in segments))


def matched_character_count(candidate: str, source: str) -> int:
    """Return aligned character count between candidate and source."""
    matcher = SequenceMatcher(None, candidate, source, autojunk=False)
    return sum(block.size for block in matcher.get_matching_blocks())


def is_grounded_note_item(
    text: str,
    *,
    transcript_key: str,
    domain_terms: list[str],
    min_coverage: float = 0.72,
) -> bool:
    """Return true for legacy note items that do not include source ids."""
    item_key = transcript_compare_key(text)
    if not item_key or not transcript_key:
        return False
    if item_key in transcript_key:
        return True
    if len(item_key) <= 3:
        return False

    domain_keys = [transcript_compare_key(term) for term in domain_terms]
    for term_key in domain_keys:
        if term_key and term_key in item_key:
            item_key = item_key.replace(term_key, "")
    if not item_key:
        return True

    matched = matched_character_count(item_key, transcript_key)
    unmatched = len(item_key) - matched
    max_unmatched = max(3, int(len(item_key) * 0.15))
    return (
        sequence_coverage(item_key, transcript_key) >= min_coverage
        and unmatched <= max_unmatched
    )


ENGLISH_STOP_WORDS = {
    "about",
    "above",
    "after",
    "again",
    "against",
    "also",
    "and",
    "are",
    "because",
    "been",
    "before",
    "being",
    "between",
    "both",
    "can",
    "could",
    "does",
    "from",
    "has",
    "have",
    "having",
    "here",
    "into",
    "its",
    "itself",
    "more",
    "most",
    "not",
    "now",
    "onto",
    "other",
    "over",
    "such",
    "than",
    "that",
    "the",
    "their",
    "then",
    "there",
    "these",
    "they",
    "this",
    "through",
    "using",
    "when",
    "where",
    "which",
    "while",
    "with",
    "within",
    "will",
    "would",
    "you",
    "your",
}


def content_tokens(text: str) -> set[str]:
    """Return content-bearing tokens for lightweight evidence checks."""
    normalized = clean_scalar(text).lower()
    tokens: set[str] = set()
    for word in re.findall(r"[a-z0-9]+", normalized):
        if len(word) < 3 or word in ENGLISH_STOP_WORDS:
            continue
        tokens.add(word)
        if word.endswith("s") and len(word) > 4:
            tokens.add(word[:-1])
        if word.endswith("ing") and len(word) > 6:
            tokens.add(word[:-3])
        if word.endswith("ed") and len(word) > 5:
            tokens.add(word[:-2])
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        if len(chunk) == 2:
            tokens.add(chunk)
            continue
        tokens.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return tokens


def note_supported_by_evidence(
    text: str,
    *,
    evidence_text: str,
    all_transcript_key: str,
    domain_terms: list[str],
) -> bool:
    """Return true when a source-backed note item is supported by cited text."""
    item_key = transcript_compare_key(text)
    evidence_key = transcript_compare_key(evidence_text)
    if not item_key or not evidence_key:
        return False
    if item_key in evidence_key or item_key in all_transcript_key:
        return True

    note_tokens = content_tokens(text)
    evidence_tokens = content_tokens(evidence_text)
    if note_tokens:
        shared = note_tokens & evidence_tokens
        coverage = len(shared) / len(note_tokens)
        required_shared = 1 if len(note_tokens) <= 2 else 2
        if coverage >= 0.28 and len(shared) >= required_shared:
            return True

    domain_keys = [transcript_compare_key(term) for term in domain_terms]
    for term_key in domain_keys:
        if term_key and term_key in item_key and term_key in evidence_key:
            return True

    return sequence_coverage(item_key, evidence_key) >= 0.45


def segment_map_by_id(segments: list[WhisperLiveSegment]) -> dict[str, WhisperLiveSegment]:
    """Index transcript segments by the ids exposed to Qwen."""
    return {whisperlive_segment_id(segment): segment for segment in segments}


def filter_grounded_notes(
    values: list[str],
    *,
    source_map: dict[str, tuple[str, ...]],
    segment_by_id: dict[str, WhisperLiveSegment],
    transcript_key: str,
    domain_terms: list[str],
) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """Keep note items with valid transcript evidence."""
    grounded_items: list[str] = []
    grounded_sources: dict[str, tuple[str, ...]] = {}
    for item in values:
        key = markdown_merge_key(item)
        source_ids = source_map.get(key, ())
        if source_ids:
            valid_ids = tuple(source_id for source_id in source_ids if source_id in segment_by_id)
            if not valid_ids:
                continue
            evidence_text = " ".join(segment_by_id[source_id].text for source_id in valid_ids)
            if not note_supported_by_evidence(
                item,
                evidence_text=evidence_text,
                all_transcript_key=transcript_key,
                domain_terms=domain_terms,
            ):
                continue
            grounded_items.append(item)
            grounded_sources[key] = valid_ids
            continue

        if is_grounded_note_item(
            item,
            transcript_key=transcript_key,
            domain_terms=domain_terms,
        ):
            grounded_items.append(item)
    return grounded_items, grounded_sources


def filter_grounded_list(
    values: list[str],
    *,
    transcript_key: str,
    domain_terms: list[str],
) -> list[str]:
    """Keep only note items grounded in transcript text."""
    return [
        item
        for item in values
        if is_grounded_note_item(
            item,
            transcript_key=transcript_key,
            domain_terms=domain_terms,
        )
    ]


def enforce_markdown_grounding(
    result: MarkdownResult,
    *,
    segments: list[WhisperLiveSegment],
    domain_terms: list[str],
) -> MarkdownResult:
    """Drop Qwen note content that is not supported by the transcript."""
    transcript_key = combined_transcript_key(segments)
    segment_by_id = segment_map_by_id(segments)

    summary, summary_source_ids = filter_grounded_notes(
        result.summary,
        source_map=result.summary_source_ids,
        segment_by_id=segment_by_id,
        transcript_key=transcript_key,
        domain_terms=domain_terms,
    )
    keywords = filter_grounded_list(
        result.keywords,
        transcript_key=transcript_key,
        domain_terms=domain_terms,
    )

    sections: list[tuple[str, list[str]]] = []
    bullet_source_ids: dict[tuple[str, str], tuple[str, ...]] = {}
    for heading, bullets in result.sections:
        heading_key = markdown_merge_key(heading)
        bullet_sources = {
            bullet_key: source_ids
            for (candidate_heading, bullet_key), source_ids in result.bullet_source_ids.items()
            if candidate_heading == heading_key
        }
        grounded_bullets, grounded_bullet_sources = filter_grounded_notes(
            bullets,
            source_map=bullet_sources,
            segment_by_id=segment_by_id,
            transcript_key=transcript_key,
            domain_terms=domain_terms,
        )
        if grounded_bullets:
            sections.append((heading, grounded_bullets))
            for bullet in grounded_bullets:
                source_ids = grounded_bullet_sources.get(markdown_merge_key(bullet), ())
                if source_ids:
                    bullet_source_ids[(heading_key, markdown_merge_key(bullet))] = source_ids

    if result.summary and not summary:
        log("Dropped ungrounded Qwen summary items")
    if result.sections and not sections:
        log("Dropped ungrounded Qwen section bullets")

    return MarkdownResult(
        summary=summary,
        sections=sections,
        keywords=keywords,
        summary_source_ids=summary_source_ids,
        bullet_source_ids=bullet_source_ids,
    )


def render_markdown(
    result: MarkdownResult,
    *,
    source_file: Path,
    whisper_model: str,
    segments: list[WhisperLiveSegment],
    update_status: str = "final",
) -> str:
    """Render a Markdown document."""
    labels = markdown_labels(result, segments)
    lines = [
        f"# {labels['title']}",
        "",
        f"- {labels['generated_at']}：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- {labels['update_status']}：{update_status}",
        f"- {labels['audio_file']}：`{source_file}`",
        f"- {labels['whisper_model']}：`{whisper_model}`",
        f"- {labels['segment_count']}：{len(segments)}",
        "",
    ]
    if result.summary:
        lines.extend([f"## {labels['summary']}", ""])
        lines.extend(f"- {item}" for item in result.summary)
        lines.append("")
    if result.keywords:
        keyword_joiner = "、" if labels["language"] == "zh" else ", "
        lines.extend([f"## {labels['keywords']}", "", keyword_joiner.join(result.keywords), ""])
    for heading, bullets in result.sections:
        lines.extend([f"## {heading}", ""])
        lines.extend(f"- {item}" for item in bullets)
        lines.append("")
    lines.extend([f"## {labels['subtitles']}", ""])
    for segment in segments:
        status = "final" if segment.completed else "partial"
        lines.append(f"- `{segment.start:.2f}-{segment.end:.2f}` ({status}) {segment.text}")
    lines.append("")
    return "\n".join(lines)


def markdown_labels(
    result: MarkdownResult,
    segments: list[WhisperLiveSegment],
) -> dict[str, str]:
    """Return Markdown chrome labels matching the main lecture language."""
    sample = "\n".join(
        [
            *result.summary,
            *result.keywords,
            *(bullet for _, bullets in result.sections for bullet in bullets),
            *(segment.text for segment in segments),
        ]
    )
    if main_text_language(sample) == "en":
        return {
            "language": "en",
            "title": ENGLISH_MARKDOWN_TITLE,
            "generated_at": "Generated at",
            "update_status": "Update status",
            "audio_file": "Audio file",
            "whisper_model": "WhisperLive model",
            "segment_count": "Subtitle segments",
            "summary": "Summary",
            "keywords": "Keywords",
            "subtitles": "WhisperLive Subtitles",
        }
    return {
        "language": "zh",
        "title": DEFAULT_MARKDOWN_TITLE,
        "generated_at": "生成时间",
        "update_status": "更新状态",
        "audio_file": "音频文件",
        "whisper_model": "WhisperLive 模型",
        "segment_count": "字幕段数",
        "summary": "摘要",
        "keywords": "关键词",
        "subtitles": "WhisperLive 字幕",
    }


def main_text_language(text: str) -> str:
    """Infer whether note chrome should use Chinese or English."""
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    latin = sum(1 for char in text if char.isascii() and char.isalpha())
    return "en" if latin > 0 and cjk == 0 else "zh"


def make_markdown_output_path(
    output_dir: Path,
    input_path: Path,
    *,
    session_id: str = "",
    sessions_dir: Path = DEFAULT_SESSIONS_DIR,
) -> Path:
    """Build a stable output path for one streaming notes document."""
    if session_id.strip():
        return session_structured_notes_path(
            session_id.strip(),
            sessions_dir=sessions_dir,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", input_path.stem).strip("_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return output_dir / f"{timestamp}_{stem or 'audio'}_notes.md"


def session_structured_notes_path(session_id: str, *, sessions_dir: Path) -> Path:
    """Return the structured notes path inside one safe classroom session dir."""
    if not re.fullmatch(r"[0-9A-Za-z_\-]+", session_id):
        raise ValueError(f"Unsafe session_id for notes output: {session_id}")

    root = sessions_dir.resolve()
    session_dir = (root / session_id).resolve()
    if not session_dir.is_relative_to(root):
        raise ValueError(f"Unsafe session_id path: {session_id}")

    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir / "structured_notes.md"


def write_markdown(
    markdown: str,
    *,
    output_dir: Path,
    input_path: Path,
    output_path: Path | None = None,
) -> Path:
    """Write markdown to disk and return the path."""
    output_path = output_path or make_markdown_output_path(output_dir, input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    return output_path


class PeriodicMarkdownUpdater:
    """Periodically regenerate one Markdown notes file from transcript snapshots."""

    def __init__(
        self,
        *,
        qwen_factory: Callable[[], QwenMarkdownPolisher],
        snapshot_segments: Callable[[], list[WhisperLiveSegment]],
        output_path: Path,
        input_path: Path,
        whisper_model: str,
        domain_terms: list[str],
        max_new_tokens: int,
        update_every_seconds: float,
        min_update_segments: int,
        subtitle_update_every_seconds: float = 0.0,
        max_qwen_segments_per_update: int = DEFAULT_MAX_QWEN_SEGMENTS_PER_UPDATE,
        on_markdown_update: Callable[
            [
                str,
                list[WhisperLiveSegment],
                str,
                int,
                Path,
                list[WhisperLiveSegment],
            ],
            None,
        ]
        | None = None,
        debug_logger: QwenDebugLogger | None = None,
    ) -> None:
        self.qwen_factory = qwen_factory
        self.snapshot_segments = snapshot_segments
        self.output_path = output_path
        self.input_path = input_path
        self.whisper_model = whisper_model
        self.domain_terms = domain_terms
        self.max_new_tokens = max_new_tokens
        self.update_every_seconds = update_every_seconds
        self.min_update_segments = min_update_segments
        self.subtitle_update_every_seconds = subtitle_update_every_seconds
        self.max_qwen_segments_per_update = max(1, max_qwen_segments_per_update)
        self.on_markdown_update = on_markdown_update
        self.debug_logger = debug_logger
        self._qwen: QwenMarkdownPolisher | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_fingerprint: tuple[tuple[float, float, str], ...] = ()
        self._last_subtitle_fingerprint: tuple[tuple[float, float, str], ...] = ()
        self._failed_qwen_fingerprint: tuple[tuple[float, float, str], ...] = ()
        self._latest_qwen_result: MarkdownResult | None = None
        self.update_count = 0

    def start(self) -> None:
        """Start background periodic updates when enabled."""
        if self.update_every_seconds <= 0 and self.subtitle_update_every_seconds <= 0:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background updater without forcing a final write."""
        self._stop_event.set()
        if self._thread:
            self._thread.join()

    def stop_and_flush(self, final_segments: list[WhisperLiveSegment]) -> Path | None:
        """Stop the background updater and write the final notes snapshot."""
        self.stop()
        if not final_segments:
            return None
        return self.write_update(final_segments, final=True)

    def latest_result(self) -> MarkdownResult | None:
        """Return the latest Qwen notes result, if one has been generated."""
        return self._latest_qwen_result

    def write_update(
        self,
        segments: list[WhisperLiveSegment],
        *,
        final: bool,
    ) -> Path | None:
        """Regenerate the notes file from the provided segment snapshot."""
        fingerprint = self._fingerprint(segments)
        pending_segments = self._pending_segments_for_update(segments, final=final)
        self._debug(
            "updater_write_update_start",
            final=final,
            total_segments=len(segments),
            pending_segments=len(pending_segments),
            latest_qwen_ready=self._latest_qwen_result is not None,
            failed_retry_segments=len(self._failed_qwen_fingerprint),
            segments=qwen_debug_segments(segments),
            pending=qwen_debug_segments(pending_segments),
        )
        if not final and len(pending_segments) < self.min_update_segments:
            self._debug(
                "updater_write_update_skip",
                final=final,
                reason="not_enough_pending_segments",
                pending_segments=len(pending_segments),
                min_update_segments=self.min_update_segments,
            )
            return None
        if final and not pending_segments and self._latest_qwen_result is None:
            pending_segments = list(segments)
        if not final and not pending_segments:
            self._debug(
                "updater_write_update_skip",
                final=final,
                reason="no_pending_segments",
            )
            return None

        batches = self._qwen_batches_for_update(pending_segments, final=final)
        if not batches and self._latest_qwen_result is None:
            self._debug(
                "updater_write_update_skip",
                final=final,
                reason="no_batches_and_no_previous_result",
            )
            return None

        if self._qwen is None:
            self._qwen = self.qwen_factory()
        result = self._latest_qwen_result
        processed_segments: list[WhisperLiveSegment] = []
        for batch_index, batch in enumerate(batches, start=1):
            self._debug(
                "updater_batch_start",
                final=final,
                batch_index=batch_index,
                batch_count=len(batches),
                batch_size=len(batch),
                segments=qwen_debug_segments(batch),
            )
            try:
                batch_result = self._qwen.generate(
                    batch,
                    max_new_tokens=self.max_new_tokens,
                    domain_terms=self.domain_terms,
                )
            except Exception as exc:  # noqa: BLE001
                if final:
                    self._debug(
                        "updater_batch_error",
                        final=final,
                        batch_index=batch_index,
                        error=str(exc),
                        traceback=traceback.format_exc(),
                    )
                    raise
                self._remember_failed_qwen_batch(batch)
                log(
                    "Qwen markdown batch failed; will retry with later context: "
                    f"{exc}"
                )
                self._debug(
                    "updater_batch_error",
                    final=final,
                    batch_index=batch_index,
                    error=str(exc),
                    traceback=traceback.format_exc(),
                    failed_retry_segments=len(self._failed_qwen_fingerprint),
                )
                continue
            result = merge_markdown_results(result, batch_result)
            processed_segments.extend(batch)
            self._debug(
                "updater_batch_done",
                final=final,
                batch_index=batch_index,
                batch_result=qwen_debug_result(batch_result),
                merged_result=qwen_debug_result(result),
            )

        if not processed_segments and not final:
            self._debug(
                "updater_write_update_skip",
                final=final,
                reason="no_processed_segments_after_batch_errors",
            )
            return None
        if not has_structured_markdown_content(result):
            self._debug(
                "updater_write_update_error",
                final=final,
                reason="empty_structured_result",
                result=qwen_debug_result(result or MarkdownResult([], [], [])),
            )
            raise RuntimeError(
                "Qwen markdown result has no summary or sections: "
                f"{format_markdown_result_counts(result)}"
            )
        self._latest_qwen_result = result
        output_segments = self.snapshot_segments() or segments
        markdown = render_markdown(
            result,
            source_file=self.input_path,
            whisper_model=self.whisper_model,
            segments=output_segments,
            update_status="final" if final else "streaming",
        )
        output_path = write_markdown(
            markdown,
            output_dir=self.output_path.parent,
            input_path=self.input_path,
            output_path=self.output_path,
        )
        self.update_count += 1
        update_status = "final" if final else "streaming"
        log(
            f"Wrote {update_status} Markdown update "
            f"#{self.update_count} from {len(output_segments)} segment(s): {output_path}"
        )
        self._debug(
            "updater_write_update_done",
            final=final,
            update_count=self.update_count,
            output_path=output_path,
            update_status=update_status,
            processed_segments=len(processed_segments),
            result=qwen_debug_result(result),
        )
        recent_segments = processed_segments or segments[-max(5, self.min_update_segments) :]
        if self.on_markdown_update is not None:
            self.on_markdown_update(
                markdown,
                output_segments,
                update_status,
                self.update_count,
                output_path,
                recent_segments,
            )
        if final:
            self._last_fingerprint = fingerprint
        elif processed_segments:
            self._last_fingerprint = (
                *self._last_fingerprint,
                *self._fingerprint(processed_segments),
            )
            self._clear_failed_qwen_segments(processed_segments)
        return output_path

    def write_subtitle_snapshot(self, segments: list[WhisperLiveSegment]) -> Path | None:
        """Refresh the Markdown transcript section without invoking Qwen or the graph."""
        if not segments:
            self._debug(
                "subtitle_snapshot_skip",
                reason="no_segments",
            )
            return None
        fingerprint = self._fingerprint(segments)
        if fingerprint == self._last_subtitle_fingerprint:
            self._debug(
                "subtitle_snapshot_skip",
                reason="unchanged_fingerprint",
                segment_count=len(segments),
            )
            return None
        self._last_subtitle_fingerprint = fingerprint
        result = self._latest_qwen_result or fallback_markdown_result(segments)
        markdown = render_markdown(
            result,
            source_file=self.input_path,
            whisper_model=self.whisper_model,
            segments=segments,
            update_status="streaming",
        )
        output_path = write_markdown(
            markdown,
            output_dir=self.output_path.parent,
            input_path=self.input_path,
            output_path=self.output_path,
        )
        log(
            "Wrote streaming subtitle Markdown snapshot "
            f"from {len(segments)} segment(s); "
            f"qwen_notes={'ready' if self._latest_qwen_result else 'pending'}: {output_path}"
        )
        self._debug(
            "subtitle_snapshot_written",
            output_path=output_path,
            segment_count=len(segments),
            qwen_notes_ready=self._latest_qwen_result is not None,
        )
        return output_path

    def _run(self) -> None:
        next_qwen_update_at = time.monotonic()
        next_subtitle_update_at = time.monotonic()
        while not self._stop_event.wait(1.0):
            try:
                now = time.monotonic()
                segments = self.snapshot_segments()
                if (
                    self.subtitle_update_every_seconds > 0
                    and now >= next_subtitle_update_at
                ):
                    self.write_subtitle_snapshot(segments)
                    next_subtitle_update_at = now + self.subtitle_update_every_seconds

                qwen_due = (
                    self.update_every_seconds > 0
                    and len(segments) >= self.min_update_segments
                    and now >= next_qwen_update_at
                )
                if qwen_due:
                    try:
                        self.write_update(segments, final=False)
                    finally:
                        next_qwen_update_at = time.monotonic() + self.update_every_seconds
            except Exception as exc:  # noqa: BLE001
                log(f"Markdown periodic update failed: {exc}")

    @staticmethod
    def _fingerprint(
        segments: list[WhisperLiveSegment],
    ) -> tuple[tuple[float, float, str], ...]:
        return tuple(qwen_segment_fingerprint(item) for item in segments)

    def _pending_segments_for_update(
        self,
        segments: list[WhisperLiveSegment],
        *,
        final: bool,
    ) -> list[WhisperLiveSegment]:
        """Return transcript segments not yet successfully consumed by Qwen."""
        previous = set(self._last_fingerprint)
        if final:
            return [
                segment
                for segment in segments
                if (round(segment.start, 2), round(segment.end, 2), segment.text) not in previous
            ]

        failed = set(self._failed_qwen_fingerprint)
        failed_segments = [
            segment
            for segment in segments
            if (round(segment.start, 2), round(segment.end, 2), segment.text) in failed
        ]
        new_segments = [
            segment
            for segment in segments
            if (
                qwen_segment_fingerprint(segment) not in previous
                and qwen_segment_fingerprint(segment) not in failed
            )
        ]
        if failed_segments and not new_segments:
            return []
        return [*failed_segments, *new_segments]

    def _qwen_batches_for_update(
        self,
        pending_segments: list[WhisperLiveSegment],
        *,
        final: bool,
    ) -> list[list[WhisperLiveSegment]]:
        """Return small chronological batches so local Qwen can keep up."""
        if not pending_segments:
            return []
        batch_size = self.max_qwen_segments_per_update
        if not final:
            failed_count = len(self._failed_qwen_fingerprint)
            return [pending_segments[: max(batch_size, batch_size + failed_count)]]
        return [
            pending_segments[index : index + batch_size]
            for index in range(0, len(pending_segments), batch_size)
        ]

    def _remember_failed_qwen_batch(self, batch: list[WhisperLiveSegment]) -> None:
        """Remember a failed live batch without making final generation skip it."""
        existing = list(self._failed_qwen_fingerprint)
        seen = set(existing)
        for item in self._fingerprint(batch):
            if item in seen:
                continue
            seen.add(item)
            existing.append(item)
        self._failed_qwen_fingerprint = tuple(existing[-32:])

    def _clear_failed_qwen_segments(self, segments: list[WhisperLiveSegment]) -> None:
        """Remove successfully processed segments from the failed live retry set."""
        processed = set(self._fingerprint(segments))
        self._failed_qwen_fingerprint = tuple(
            item for item in self._failed_qwen_fingerprint if item not in processed
        )

    def _debug(self, event: str, **fields: Any) -> None:
        """Write a Qwen updater debug event if configured."""
        if self.debug_logger is not None:
            self.debug_logger.event(event, **fields)


def clean_scalar(value: object) -> str:
    """Normalize a scalar text value."""
    if value is None:
        return ""
    return str(value).strip()


def clean_list(value: object) -> list[str]:
    """Normalize a JSON array into unique non-empty strings."""
    if not isinstance(value, list):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = clean_scalar(item)
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def parse_domain_terms(raw_terms: str) -> list[str]:
    """Parse comma/newline separated domain terms."""
    if not raw_terms.strip():
        return []
    items = re.split(r"[,，;；\n]+", raw_terms)
    return clean_list(items)


def load_session_transcript_segments(session_dir: Path) -> list[WhisperLiveSegment]:
    """Load final transcript segments from a saved classroom timeline."""
    timeline_path = session_dir / "timeline.json"
    raw_items = json.loads(timeline_path.read_text(encoding="utf-8"))
    if not isinstance(raw_items, list):
        raise ValueError(f"Expected timeline list in {timeline_path}")

    segments: list[WhisperLiveSegment] = []
    for item in raw_items:
        if not isinstance(item, dict) or item.get("type") != "transcript":
            continue
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        if data.get("is_final") is False:
            continue
        text = str(data.get("text") or "").strip()
        if not text:
            continue
        segments.append(
            WhisperLiveSegment(
                start=float(data.get("start_ts") or item.get("ts") or 0.0),
                end=float(data.get("end_ts") or data.get("start_ts") or item.get("ts") or 0.0),
                text=text,
                completed=True,
            )
        )
    return normalize_collected_segments(segments, completed_only=True)


def finalize_session_notes(args: argparse.Namespace) -> Path:
    """Generate final structured notes for an already-saved classroom session."""
    session_id = args.finalize_session_id.strip()
    session_dir = (
        Path(args.finalize_session_dir)
        if args.finalize_session_dir
        else Path(args.sessions_dir) / session_id
    )
    if not session_id:
        raise ValueError("--finalize-session-id is required")
    if not session_dir.exists():
        raise FileNotFoundError(f"Saved session directory not found: {session_dir}")

    segments = load_session_transcript_segments(session_dir)
    if not segments:
        raise RuntimeError(f"No final transcript segments found in {session_dir}")

    domain_terms = parse_domain_terms(args.domain_terms)
    output_path = session_dir / "structured_notes.md"
    debug_logger = QwenDebugLogger(qwen_debug_log_path(output_path))
    if debug_logger.enabled:
        log(f"Qwen debug log: {debug_logger.path}")
    debug_logger.event(
        "finalize_session_start",
        session_id=session_id,
        session_dir=session_dir,
        segment_count=len(segments),
        segments=qwen_debug_segments(segments),
        qwen_model=args.qwen_model,
        qwen_device=args.qwen_device,
        qwen_tokens=args.qwen_tokens,
        max_qwen_segments_per_update=getattr(
            args,
            "max_qwen_segments_per_update",
            DEFAULT_MAX_QWEN_SEGMENTS_PER_UPDATE,
        ),
    )
    qwen = QwenMarkdownPolisher(
        model_path=Path(args.qwen_model),
        device=args.qwen_device,
        debug_logger=debug_logger,
    )
    result = generate_incremental_markdown_result(
        qwen,
        segments,
        max_new_tokens=args.qwen_tokens,
        domain_terms=domain_terms,
        max_qwen_segments_per_update=max(
            1,
            getattr(
                args,
                "max_qwen_segments_per_update",
                DEFAULT_MAX_QWEN_SEGMENTS_PER_UPDATE,
            ),
        ),
        debug_logger=debug_logger,
    )

    markdown = render_markdown(
        result,
        source_file=session_dir / "transcript.md",
        whisper_model=args.whisperlive_model,
        segments=segments,
        update_status="final",
    )
    write_markdown(
        markdown,
        output_dir=session_dir,
        input_path=session_dir / "transcript.md",
        output_path=output_path,
    )
    log(
        "Final Qwen structured notes generated "
        f"for session={session_id}: {output_path} "
        f"{format_markdown_result_counts(result)}"
    )
    debug_logger.event(
        "finalize_session_done",
        session_id=session_id,
        output_path=output_path,
        result=qwen_debug_result(result),
    )
    return output_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="WhisperLive local ASR + Qwen CPU markdown smoke."
    )
    parser.add_argument("--server", default=DEFAULT_WHISPERLIVE_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_WHISPERLIVE_PORT)
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Audio file or directory.")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=(
            "Fallback Markdown output directory when --session-id is not provided. "
            "Session runs save to data/sessions/{session_id}/structured_notes.md."
        ),
    )
    parser.add_argument(
        "--sessions-dir",
        default=str(DEFAULT_SESSIONS_DIR),
        help="Base directory for session-scoped Markdown output.",
    )
    parser.add_argument(
        "--finalize-session-id",
        default="",
        help=(
            "Generate final structured_notes.md for an already saved session "
            "from data/sessions/{session_id}/timeline.json, without ASR."
        ),
    )
    parser.add_argument(
        "--finalize-session-dir",
        default="",
        help="Optional explicit saved session directory for --finalize-session-id.",
    )
    parser.add_argument("--backend-url", default=os.getenv("BACKEND_URL", "http://127.0.0.1:8000"))
    parser.add_argument(
        "--session-id",
        default="",
        help=(
            "Existing session_id for backend transcript/graph sync. Use 'auto' "
            "or omit it with --post-transcript/--enable-cloud-graph to attach to "
            "the newest recording session, creating one if none exists."
        ),
    )
    parser.add_argument("--whisperlive-model", default=DEFAULT_WHISPERLIVE_MODEL)
    parser.add_argument("--language", default=DEFAULT_WHISPER_LANGUAGE)
    parser.add_argument("--max-audio-seconds", type=float, default=120.0)
    parser.add_argument("--packet-seconds", type=float, default=0.25)
    parser.add_argument("--fast-send", action="store_true", help="Send audio faster than realtime.")
    parser.add_argument("--tail-wait", type=float, default=8.0)
    parser.add_argument("--connect-timeout", type=float, default=300.0)
    parser.add_argument("--qwen-model", default=str(DEFAULT_QWEN_MODEL))
    parser.add_argument("--qwen-device", default=os.getenv("QWEN_DEVICE", "CPU"))
    parser.add_argument("--qwen-tokens", type=int, default=900)
    parser.add_argument(
        "--max-qwen-segments-per-update",
        type=int,
        default=int(os.getenv("QWEN_MAX_SEGMENTS_PER_UPDATE", DEFAULT_MAX_QWEN_SEGMENTS_PER_UPDATE)),
        help=(
            "Maximum new transcript segments sent to Qwen in one notes call. "
            "Small batches keep local Qwen responsive during long classes."
        ),
    )
    parser.add_argument(
        "--update-every-seconds",
        type=float,
        default=30.0,
        help="Regenerate Qwen structured Markdown notes every N seconds. Use 0 for final-only.",
    )
    parser.add_argument(
        "--subtitle-update-every-seconds",
        type=float,
        default=5.0,
        help=(
            "Refresh Markdown transcript snapshots every N seconds without invoking "
            "Qwen or cloud graph extraction. Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--min-update-segments",
        type=int,
        default=2,
        help="Minimum completed transcript segments required for a streaming Markdown update.",
    )
    parser.add_argument(
        "--post-transcript",
        action="store_true",
        help="POST completed WhisperLive transcript.segment events to the backend.",
    )
    parser.add_argument(
        "--enable-cloud-graph",
        action="store_true",
        help="POST Markdown note snapshots to the backend cloud knowledge-tree agent.",
    )
    parser.add_argument(
        "--graph-update-every-seconds",
        type=float,
        default=60.0,
        help="Minimum interval between streaming Markdown graph updates. Final updates always send.",
    )
    parser.add_argument(
        "--no-update-session-name",
        action="store_true",
        help=(
            "Deprecated compatibility flag. Session title/course are inferred "
            "by the backend cloud notes agent on final snapshots."
        ),
    )
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument(
        "--domain-terms",
        default=os.getenv("DOMAIN_TERMS", ""),
        help="Comma separated terms Qwen may use for conservative notes correction.",
    )
    parser.add_argument("--no-vad", action="store_true")
    parser.add_argument("--send-last-n-segments", type=int, default=12)
    parser.add_argument("--no-speech-thresh", type=float, default=0.45)
    parser.add_argument("--same-output-threshold", type=int, default=6)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = parse_args(argv or sys.argv[1:])
    if args.finalize_session_id.strip():
        finalize_session_notes(args)
        return 0

    input_path = find_media(Path(args.input))
    log(f"Input media: {input_path}")
    log(f"WhisperLive server: {args.server}:{args.port}")
    log(f"WhisperLive model: {args.whisperlive_model}")
    sync_enabled = bool(args.post_transcript or args.enable_cloud_graph)
    session_id = args.session_id.strip()
    needs_backend_session = sync_enabled
    if needs_backend_session:
        session_id = resolve_backend_session_id(
            requested_session_id=session_id or "auto",
            base_url=args.backend_url,
            timeout=args.http_timeout,
        )
    post_transcript = bool(args.post_transcript or args.enable_cloud_graph)
    syncer = BackendSyncer(
        base_url=args.backend_url,
        session_id=session_id,
        http_timeout=args.http_timeout,
        post_transcript=post_transcript,
        enable_cloud_graph=bool(args.enable_cloud_graph),
        graph_update_every_seconds=args.graph_update_every_seconds,
    )
    syncer.start()
    if syncer.enabled:
        log(
            "Backend sync enabled: "
            f"transcript={post_transcript}, cloud_graph={args.enable_cloud_graph}, "
            f"url={args.backend_url}, session={session_id}"
        )
    client = WhisperLiveFileClient(
        host=args.server,
        port=args.port,
        model=args.whisperlive_model,
        language=args.language,
        use_vad=not args.no_vad,
        send_last_n_segments=args.send_last_n_segments,
        no_speech_thresh=args.no_speech_thresh,
        same_output_threshold=args.same_output_threshold,
        connect_timeout=args.connect_timeout,
        on_completed_segment=syncer.enqueue_transcript,
    )
    output_path = make_markdown_output_path(
        Path(args.output_dir),
        input_path,
        session_id=session_id,
        sessions_dir=Path(args.sessions_dir),
    )
    domain_terms = parse_domain_terms(args.domain_terms)
    debug_logger = QwenDebugLogger(qwen_debug_log_path(output_path))
    if debug_logger.enabled:
        log(f"Qwen debug log: {debug_logger.path}")
    updater = PeriodicMarkdownUpdater(
        qwen_factory=lambda: QwenMarkdownPolisher(
            model_path=Path(args.qwen_model),
            device=args.qwen_device,
            debug_logger=debug_logger,
        ),
        snapshot_segments=lambda: client.snapshot_segments(completed_only=True),
        output_path=output_path,
        input_path=input_path,
        whisper_model=args.whisperlive_model,
        domain_terms=domain_terms,
        max_new_tokens=args.qwen_tokens,
        update_every_seconds=args.update_every_seconds,
        min_update_segments=max(1, args.min_update_segments),
        subtitle_update_every_seconds=max(0.0, args.subtitle_update_every_seconds),
        max_qwen_segments_per_update=max(1, args.max_qwen_segments_per_update),
        on_markdown_update=syncer.enqueue_notes_update,
        debug_logger=debug_logger,
    )
    log(f"Markdown output: {output_path}")
    if args.update_every_seconds > 0:
        log(
            "Qwen structured Markdown updates immediately after "
            f"{max(1, args.min_update_segments)} segment(s), then every "
            f"{args.update_every_seconds:.1f}s"
        )
    if args.subtitle_update_every_seconds > 0:
        log(
            "Subtitle Markdown snapshots every "
            f"{args.subtitle_update_every_seconds:.1f}s between Qwen notes updates"
        )

    segments: list[WhisperLiveSegment] = []
    updater.start()
    try:
        segments = client.transcribe_file(
            input_path,
            packet_seconds=args.packet_seconds,
            max_audio_seconds=args.max_audio_seconds,
            send_realtime=not args.fast_send,
            tail_wait=args.tail_wait,
        )
    except Exception:
        updater.stop()
        syncer.stop()
        raise
    if not segments:
        updater.stop()
        syncer.stop()
        raise RuntimeError("WhisperLive returned no transcript segments")

    log(f"Collected {len(segments)} transcript segment(s)")
    final_output_path = updater.stop_and_flush(segments)
    if final_output_path:
        log(f"Final Markdown ready: {final_output_path}")
    syncer.stop()
    if syncer.enabled:
        log(
            "Backend sync finished: "
            f"transcripts={syncer.transcript_post_count}, "
            f"notes={syncer.notes_post_count}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
