"""Stream a local ALSA microphone to WhisperLive and EDU-Mate.

This is the live microphone counterpart of ``whisperlive_qwen_markdown.py``:

1. Pick an ALSA input device, preferring USB microphones.
2. Use ffmpeg to capture 16 kHz mono float32 PCM from the microphone.
3. Send the PCM stream to a local WhisperLive OpenVINO websocket server.
4. POST completed ``transcript.segment`` events to the current EDU-Mate session.
5. Optionally keep the same Qwen structured-notes and cloud graph flow alive.

The microphone discovery and ffmpeg supervision intentionally mirror the
``/home/edu-mate_user/Desktop/camera/mic_camera_recorder.py`` project, while
the backend sync is kept identical to the existing WhisperLive file pipeline.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.scripts.local_audio_stream_sender import (  # noqa: E402
    DEFAULT_QWEN_MODEL,
    SAMPLE_RATE,
    get_json,
    post_json,
    resolve_backend_session_id,
)
from backend.scripts.whisperlive_qwen_markdown import (  # noqa: E402
    DEFAULT_OUTPUT_DIR,
    DEFAULT_SESSIONS_DIR,
    DEFAULT_WHISPER_LANGUAGE,
    DEFAULT_WHISPERLIVE_HOST,
    DEFAULT_WHISPERLIVE_MODEL,
    DEFAULT_WHISPERLIVE_PORT,
    DEFAULT_MAX_QWEN_SEGMENTS_PER_UPDATE,
    BackendSyncer,
    PeriodicMarkdownUpdater,
    QwenDebugLogger,
    QwenMarkdownPolisher,
    WhisperLiveFileClient,
    WhisperLiveSegment,
    log,
    make_markdown_output_path,
    parse_domain_terms,
    qwen_debug_log_path,
    transcript_payload,
)


DEFAULT_AUDIO_DEVICE = "auto"
DEFAULT_MICROPHONE_SOURCE = Path("microphone")


def require_program(name: str) -> str:
    """Return an executable path or raise a clear CLI error."""
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Missing required program `{name}`. Install it first.")
    return path


def parse_arecord_devices(output: str) -> list[tuple[str, str]]:
    """Parse ``arecord -l`` output into ``(label, plughw:X,Y)`` pairs."""
    pattern = re.compile(
        r"^card\s+(?P<card>\d+):\s+(?P<card_name>.+?),\s+device\s+"
        r"(?P<device>\d+):\s+(?P<device_name>.+?)\s+\[",
        re.IGNORECASE,
    )
    devices: list[tuple[str, str]] = []
    for line in output.splitlines():
        match = pattern.search(line.strip())
        if not match:
            continue
        card = match.group("card")
        device = match.group("device")
        label = f"{match.group('card_name')} {match.group('device_name')}"
        devices.append((label, f"plughw:{card},{device}"))
    return devices


def choose_audio_device(devices: list[tuple[str, str]]) -> str:
    """Prefer USB/microphone-like ALSA devices, then fall back to the first one."""
    for label, device in devices:
        lowered = label.lower()
        if any(word in lowered for word in ("usb", "mic", "microphone")):
            return device
    return devices[0][1] if devices else "default"


def discover_audio_device() -> str:
    """Pick a sensible ALSA input device, matching the Desktop recorder behavior."""
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "default"
    return choose_audio_device(parse_arecord_devices(result.stdout))


def resolve_or_wait_backend_session_id(
    *,
    requested_session_id: str,
    base_url: str,
    request_timeout: float,
    create_if_missing: bool,
    wait_for_session: bool,
    poll_interval: float,
    wait_timeout: float,
) -> str:
    """Resolve a recording session, optionally waiting for the frontend to start it."""
    started_at = time.monotonic()
    last_notice_at = 0.0
    while True:
        try:
            return resolve_backend_session_id(
                requested_session_id=requested_session_id,
                base_url=base_url,
                timeout=request_timeout,
                create_if_missing=create_if_missing,
            )
        except Exception as exc:  # noqa: BLE001 - CLI should keep waiting on transient backend/session misses.
            if not wait_for_session:
                raise

            elapsed = time.monotonic() - started_at
            if wait_timeout > 0 and elapsed >= wait_timeout:
                raise RuntimeError(
                    "Timed out waiting for a frontend-created recording session."
                ) from exc

            now = time.monotonic()
            if now - last_notice_at >= 5.0:
                log(
                    "Waiting for a frontend-created recording session before "
                    f"starting microphone capture: {exc}"
                )
                last_notice_at = now
            time.sleep(max(0.2, poll_interval))


class RecordingSessionMonitor:
    """Poll backend session status and stop live capture when classroom ends."""

    def __init__(
        self,
        *,
        base_url: str,
        session_id: str,
        http_timeout: float,
        poll_interval: float,
        stop_event: threading.Event,
        enabled: bool,
    ) -> None:
        self.base_url = base_url
        self.session_id = session_id
        self.http_timeout = http_timeout
        self.poll_interval = max(0.5, poll_interval)
        self.stop_event = stop_event
        self.enabled = bool(enabled and session_id)
        self._recording = True
        self._thread: threading.Thread | None = None
        self._last_warning = ""

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._thread.join(timeout=3.0)
        self._thread = None

    def is_recording(self) -> bool:
        return bool(self._recording and not self.stop_event.is_set())

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                payload = get_json(
                    self.base_url,
                    f"/sessions/{self.session_id}",
                    timeout=self.http_timeout,
                )
                status = str(payload.get("status") or "").strip()
                if status and status != "recording":
                    self._recording = False
                    log(
                        "Recording session ended; stopping microphone capture "
                        f"for session={self.session_id} status={status}"
                    )
                    self.stop_event.set()
                    return
            except Exception as exc:  # noqa: BLE001 - transient backend outages should not stop capture.
                warning = str(exc)
                if warning != self._last_warning:
                    log(f"Recording session status check failed: {warning}")
                    self._last_warning = warning
            time.sleep(self.poll_interval)


def ffmpeg_microphone_command(
    *,
    ffmpeg: str,
    audio_device: str,
    sample_rate: int,
) -> list[str]:
    """Build the ffmpeg command that captures microphone PCM for WhisperLive."""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "alsa",
        "-thread_queue_size",
        "1024",
        "-i",
        audio_device,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "f32le",
        "pipe:1",
    ]


def iter_microphone_packets(
    *,
    ffmpeg: str,
    audio_device: str,
    packet_seconds: float,
    max_audio_seconds: float,
    sample_rate: int,
    stop_event: threading.Event,
) -> Iterable[bytes]:
    """Yield live microphone float32 PCM packets from ffmpeg stdout."""
    if packet_seconds <= 0:
        raise ValueError("packet_seconds must be positive")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")

    packet_bytes = max(1, int(packet_seconds * sample_rate)) * 4
    max_packets = (
        int(max_audio_seconds / packet_seconds)
        if max_audio_seconds and max_audio_seconds > 0
        else None
    )
    command = ffmpeg_microphone_command(
        ffmpeg=ffmpeg,
        audio_device=audio_device,
        sample_rate=sample_rate,
    )
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stderr_lines: list[str] = []
    stderr_thread = threading.Thread(
        target=_drain_stderr,
        args=(process.stderr, stderr_lines),
        daemon=True,
    )
    stderr_thread.start()

    packet_count = 0
    stopped_by_limit = False
    stopped_by_event = False
    try:
        while True:
            if stop_event.is_set():
                stopped_by_event = True
                break
            if max_packets is not None and packet_count >= max_packets:
                stopped_by_limit = True
                break
            packet = process.stdout.read(packet_bytes)
            if not packet:
                break
            packet_count += 1
            yield packet
    finally:
        stop_was_requested = stopped_by_event or stop_event.is_set()
        stop_event.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process.stdout:
            process.stdout.close()
        stderr_thread.join(timeout=1)

    return_code = process.poll()
    if return_code and not stopped_by_limit and not stop_was_requested:
        detail = "\n".join(stderr_lines[-5:]).strip()
        raise RuntimeError(
            f"ffmpeg microphone capture failed with code {return_code}: {detail}"
        )


def _drain_stderr(stream: Any, lines: list[str]) -> None:
    """Drain ffmpeg stderr so the process cannot block on a full pipe."""
    try:
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                lines.append(text)
                if len(lines) > 50:
                    del lines[: len(lines) - 50]
    finally:
        try:
            stream.close()
        except OSError:
            pass


class WhisperLiveMicrophoneClient(WhisperLiveFileClient):
    """WhisperLive websocket client whose audio source is a live microphone."""

    def transcribe_microphone(
        self,
        *,
        ffmpeg: str,
        audio_device: str,
        packet_seconds: float,
        max_audio_seconds: float,
        sample_rate: int,
        tail_wait: float,
        stop_event: threading.Event,
    ) -> list[WhisperLiveSegment]:
        """Stream live microphone audio until Ctrl+C or ``max_audio_seconds``."""
        ws = self.websocket_module.create_connection(self.url, timeout=self.connect_timeout)
        receiver = threading.Thread(target=self._receive_loop, args=(ws,), daemon=True)
        receiver.start()
        sent_packets = 0
        started_at = time.time()
        try:
            self._send_options(ws)
            self._wait_for_ready()
            log(f"Microphone capture started: device={audio_device}, sample_rate={sample_rate}")
            for packet in iter_microphone_packets(
                ffmpeg=ffmpeg,
                audio_device=audio_device,
                packet_seconds=packet_seconds,
                max_audio_seconds=max_audio_seconds,
                sample_rate=sample_rate,
                stop_event=stop_event,
            ):
                ws.send_binary(packet)
                sent_packets += 1
            log(f"Sent {sent_packets} microphone packet(s) in {time.time() - started_at:.2f}s")
            self._wait_for_tail(tail_wait)
            try:
                ws.send_binary(b"END_OF_AUDIO")
            except Exception:
                pass
            time.sleep(0.5)
            return self._final_segments()
        finally:
            self._receiver_stop.set()
            stop_event.set()
            try:
                ws.close()
            except Exception:
                pass
            receiver.join(timeout=2.0)


class TranscriptPreviewSyncer:
    """Asynchronously broadcast throttled partial subtitles to the frontend."""

    def __init__(
        self,
        *,
        base_url: str,
        session_id: str,
        http_timeout: float,
        enabled: bool,
        min_interval_seconds: float,
        min_chars: int,
        should_post: Callable[[], bool] | None = None,
    ) -> None:
        self.base_url = base_url
        self.session_id = session_id
        self.http_timeout = http_timeout
        self.enabled = bool(enabled and session_id)
        self._should_post = should_post or (lambda: True)
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.min_chars = max(1, min_chars)
        self._queue: queue.Queue[WhisperLiveSegment | None] = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None
        self._last_sent_at = 0.0
        self._last_key = ""
        self.post_count = 0

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        self._queue.put(None)
        self._thread.join(timeout=5.0)
        self._thread = None

    def enqueue(self, segment: WhisperLiveSegment) -> None:
        """Queue the latest useful partial segment, replacing stale previews."""
        if not self.enabled or segment.completed or not self._should_post():
            return
        text = segment.text.strip()
        if len(text) < self.min_chars:
            return
        key = f"{segment.start:.2f}|{segment.end:.2f}|{text}"
        now = time.monotonic()
        if key == self._last_key:
            return
        if now - self._last_sent_at < self.min_interval_seconds:
            return
        self._last_key = key
        self._last_sent_at = now

        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        try:
            self._queue.put_nowait(segment)
        except queue.Full:
            pass

    def _run(self) -> None:
        while True:
            segment = self._queue.get()
            try:
                if segment is None:
                    return
                if not self._should_post():
                    continue
                self._post_preview(segment)
            except Exception as exc:  # noqa: BLE001
                if "Session is not recording" in str(exc):
                    self.enabled = False
                    log("Transcript preview sync stopped: session is not recording.")
                else:
                    log(f"Backend sync transcript preview failed: {exc}")
            finally:
                self._queue.task_done()

    def _post_preview(self, segment: WhisperLiveSegment) -> None:
        payload = transcript_payload(segment)
        payload["is_final"] = False
        post_json(
            self.base_url,
            "/events/transcript-preview",
            {
                "session_id": self.session_id,
                "payload": payload,
            },
            timeout=self.http_timeout,
        )
        self.post_count += 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Stream an ALSA microphone to WhisperLive and EDU-Mate."
    )
    parser.add_argument("--server", default=DEFAULT_WHISPERLIVE_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_WHISPERLIVE_PORT)
    parser.add_argument("--audio-device", default=DEFAULT_AUDIO_DEVICE)
    parser.add_argument("--sample-rate", type=int, default=SAMPLE_RATE)
    parser.add_argument("--packet-seconds", type=float, default=0.25)
    parser.add_argument(
        "--max-audio-seconds",
        type=float,
        default=0.0,
        help="Maximum live microphone duration. Use 0 for unlimited until Ctrl+C.",
    )
    parser.add_argument("--tail-wait", type=float, default=8.0)
    parser.add_argument("--connect-timeout", type=float, default=300.0)
    parser.add_argument("--whisperlive-model", default=DEFAULT_WHISPERLIVE_MODEL)
    parser.add_argument("--language", default=DEFAULT_WHISPER_LANGUAGE)
    parser.add_argument("--no-vad", action="store_true")
    parser.add_argument("--send-last-n-segments", type=int, default=12)
    parser.add_argument("--no-speech-thresh", type=float, default=0.3)
    parser.add_argument("--same-output-threshold", type=int, default=8)
    parser.add_argument("--backend-url", default=os.getenv("BACKEND_URL", "http://127.0.0.1:8000"))
    parser.add_argument(
        "--session-id",
        default="auto",
        help=(
            "Existing recording session_id, or auto to attach to/create the newest "
            "recording session."
        ),
    )
    parser.add_argument(
        "--no-create-session",
        action="store_true",
        help="Do not create a backend test session when no recording session exists.",
    )
    parser.add_argument(
        "--wait-for-session",
        action="store_true",
        help="Wait until the frontend starts a recording session before capturing audio.",
    )
    parser.add_argument(
        "--session-poll-interval",
        type=float,
        default=2.0,
        help="Seconds between recording-session checks when --wait-for-session is used.",
    )
    parser.add_argument(
        "--session-wait-timeout",
        type=float,
        default=0.0,
        help="Maximum seconds to wait for a session. Use 0 to wait forever.",
    )
    parser.add_argument(
        "--stop-when-session-ended",
        action="store_true",
        help="Stop the current microphone capture when the bound session is no longer recording.",
    )
    parser.add_argument(
        "--session-status-interval",
        type=float,
        default=2.0,
        help="Seconds between session status checks when --stop-when-session-ended is used.",
    )
    parser.add_argument(
        "--no-post-transcript",
        action="store_true",
        help="Do not POST completed transcript.segment events to the backend.",
    )
    parser.add_argument(
        "--no-preview-partials",
        action="store_true",
        help="Disable low-latency partial subtitle previews in the frontend.",
    )
    parser.add_argument(
        "--partial-preview-interval",
        type=float,
        default=0.75,
        help="Minimum seconds between frontend partial subtitle preview updates.",
    )
    parser.add_argument(
        "--partial-min-chars",
        type=int,
        default=3,
        help="Minimum partial subtitle length before previewing it in the frontend.",
    )
    parser.add_argument(
        "--enable-cloud-graph",
        action="store_true",
        help="POST Qwen Markdown snapshots to the cloud knowledge-tree agent.",
    )
    parser.add_argument(
        "--no-qwen-notes",
        action="store_true",
        help="Only stream/post ASR subtitles; skip local Qwen Markdown notes.",
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--sessions-dir", default=str(DEFAULT_SESSIONS_DIR))
    parser.add_argument("--qwen-model", default=str(DEFAULT_QWEN_MODEL))
    parser.add_argument("--qwen-device", default=os.getenv("QWEN_DEVICE", "CPU"))
    parser.add_argument("--qwen-tokens", type=int, default=900)
    parser.add_argument(
        "--max-qwen-segments-per-update",
        type=int,
        default=int(os.getenv("QWEN_MAX_SEGMENTS_PER_UPDATE", DEFAULT_MAX_QWEN_SEGMENTS_PER_UPDATE)),
    )
    parser.add_argument("--update-every-seconds", type=float, default=30.0)
    parser.add_argument("--subtitle-update-every-seconds", type=float, default=5.0)
    parser.add_argument("--min-update-segments", type=int, default=2)
    parser.add_argument("--graph-update-every-seconds", type=float, default=60.0)
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument("--domain-terms", default=os.getenv("DOMAIN_TERMS", ""))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = parse_args(argv or sys.argv[1:])
    ffmpeg = require_program("ffmpeg")
    audio_device = (
        discover_audio_device()
        if args.audio_device.strip().lower() == "auto"
        else args.audio_device.strip()
    )
    log(f"WhisperLive server: {args.server}:{args.port}")
    log(f"WhisperLive model: {args.whisperlive_model}")
    log(f"Audio device: {audio_device}")

    post_transcript = not args.no_post_transcript or bool(args.enable_cloud_graph)
    sync_enabled = post_transcript or bool(args.enable_cloud_graph)
    session_id = args.session_id.strip()
    if sync_enabled:
        session_id = resolve_or_wait_backend_session_id(
            requested_session_id=session_id or "auto",
            base_url=args.backend_url,
            request_timeout=args.http_timeout,
            create_if_missing=not args.no_create_session,
            wait_for_session=bool(args.wait_for_session),
            poll_interval=args.session_poll_interval,
            wait_timeout=args.session_wait_timeout,
        )

    stop_event = threading.Event()
    session_monitor = RecordingSessionMonitor(
        base_url=args.backend_url,
        session_id=session_id,
        http_timeout=args.http_timeout,
        poll_interval=args.session_status_interval,
        stop_event=stop_event,
        enabled=bool(sync_enabled and args.stop_when_session_ended),
    )
    session_monitor.start()

    syncer = BackendSyncer(
        base_url=args.backend_url,
        session_id=session_id,
        http_timeout=args.http_timeout,
        post_transcript=post_transcript,
        enable_cloud_graph=bool(args.enable_cloud_graph and not args.no_qwen_notes),
        graph_update_every_seconds=args.graph_update_every_seconds,
        should_post=session_monitor.is_recording,
    )
    syncer.start()
    if syncer.enabled:
        log(
            "Backend sync enabled: "
            f"transcript={post_transcript}, cloud_graph={syncer.enable_cloud_graph}, "
            f"url={args.backend_url}, session={session_id}"
        )
    preview_syncer = TranscriptPreviewSyncer(
        base_url=args.backend_url,
        session_id=session_id,
        http_timeout=args.http_timeout,
        enabled=bool(post_transcript and not args.no_preview_partials),
        min_interval_seconds=args.partial_preview_interval,
        min_chars=args.partial_min_chars,
        should_post=session_monitor.is_recording,
    )
    preview_syncer.start()
    if preview_syncer.enabled:
        log(
            "Partial subtitle previews enabled: "
            f"interval={args.partial_preview_interval:.2f}s, "
            f"min_chars={max(1, args.partial_min_chars)}"
        )

    client = WhisperLiveMicrophoneClient(
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
        on_partial_segment=preview_syncer.enqueue,
    )

    updater: PeriodicMarkdownUpdater | None = None
    if not args.no_qwen_notes:
        output_path = make_markdown_output_path(
            Path(args.output_dir),
            DEFAULT_MICROPHONE_SOURCE,
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
            input_path=DEFAULT_MICROPHONE_SOURCE,
            whisper_model=args.whisperlive_model,
            domain_terms=domain_terms,
            max_new_tokens=args.qwen_tokens,
            update_every_seconds=max(0.0, args.update_every_seconds),
            min_update_segments=max(1, args.min_update_segments),
            subtitle_update_every_seconds=max(0.0, args.subtitle_update_every_seconds),
            max_qwen_segments_per_update=max(1, args.max_qwen_segments_per_update),
            on_markdown_update=syncer.enqueue_notes_update,
            debug_logger=debug_logger,
        )
        log(f"Markdown output: {output_path}")
        updater.start()
    else:
        log("Qwen Markdown notes disabled for this microphone run.")

    segments: list[WhisperLiveSegment] = []
    try:
        segments = client.transcribe_microphone(
            ffmpeg=ffmpeg,
            audio_device=audio_device,
            packet_seconds=args.packet_seconds,
            max_audio_seconds=args.max_audio_seconds,
            sample_rate=args.sample_rate,
            tail_wait=args.tail_wait,
            stop_event=stop_event,
        )
    except KeyboardInterrupt:
        log("Stopping microphone stream...")
        stop_event.set()
        segments = client.snapshot_segments(completed_only=True)
    except Exception as exc:
        log(f"Microphone stream stopped before clean finalization: {exc}")
        session_monitor.stop()
        segments = client.snapshot_segments(completed_only=True)
        if updater is not None:
            try:
                final_output_path = updater.stop_and_flush(segments)
                if final_output_path:
                    log(f"Final Markdown ready after microphone stop: {final_output_path}")
            except Exception as flush_exc:  # noqa: BLE001
                log(f"Final Markdown flush failed after microphone stop: {flush_exc}")
        preview_syncer.stop()
        syncer.stop()
        raise

    session_monitor.stop()
    if updater is not None:
        try:
            final_output_path = updater.stop_and_flush(segments)
            if final_output_path:
                log(f"Final Markdown ready: {final_output_path}")
        except Exception as exc:  # noqa: BLE001
            log(f"Final Markdown flush failed: {exc}")
    preview_syncer.stop()
    syncer.stop()
    if syncer.enabled:
        log(
            "Backend sync finished: "
            f"transcripts={syncer.transcript_post_count}, "
            f"previews={preview_syncer.post_count}, "
            f"notes={syncer.notes_post_count}"
        )
    log(f"Collected {len(segments)} completed transcript segment(s)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
