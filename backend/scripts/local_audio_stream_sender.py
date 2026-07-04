"""Stream a local audio file through OpenVINO Whisper and Qwen.

This script is a hardware-integration bridge for the current EDU-Mate backend:

1. Decode a local media file into overlapping 16 kHz mono float32 ASR windows.
2. Transcribe each window with the local OpenVINO Whisper model.
3. Commit stable transcript text and POST it as ``transcript.segment``.
4. Queue local OpenVINO Qwen knowledge extraction in the background.
5. POST valid extractions as internal/debug ``knowledge.extraction`` events so
   the existing backend and frontend graph pipeline can render them unchanged.

The script intentionally avoids importing OpenVINO at module import time. Unit
tests can import and exercise parsing/event-building helpers from the normal
backend virtualenv; OpenVINO is required only when the script is actually run.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app import prompts as prompt_templates


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_OPENVINO_ROOT = Path(os.getenv("OPENVINO_ROOT", "/home/edu-mate_user/openvino"))
DEFAULT_INPUT = DEFAULT_OPENVINO_ROOT / "test_video"
DEFAULT_WHISPER_MODEL = DEFAULT_OPENVINO_ROOT / "whisper-large-v3-turbo-int8-ov"
DEFAULT_QWEN_MODEL = DEFAULT_OPENVINO_ROOT / "qwen2.5-3b-int4"
DEFAULT_WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "GPU")
DEFAULT_QWEN_DEVICE = os.getenv("QWEN_DEVICE", "CPU")
SAMPLE_RATE = 16000
LOCAL_BACKEND_HOSTS = {"127.0.0.1", "localhost", "::1"}
LOCAL_BACKEND_OPENER = build_opener(ProxyHandler({}))
MEDIA_EXTENSIONS = {
    ".aac",
    ".avi",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".wav",
}


def open_backend_request(request: Request, *, timeout: float):
    """Open local backend requests without changing global proxy behavior."""
    host = (urlparse(request.full_url).hostname or "").lower()
    if host in LOCAL_BACKEND_HOSTS:
        return LOCAL_BACKEND_OPENER.open(request, timeout=timeout)
    return urlopen(request, timeout=timeout)


@dataclass(frozen=True)
class AudioChunk:
    """One decoded audio chunk with classroom-relative timestamps."""

    index: int
    start_ts: float
    end_ts: float
    audio: Any


@dataclass(frozen=True)
class TranscriptRecord:
    """Transcript data needed by Qwen and EDU-Mate events."""

    segment_id: str
    start_ts: float
    end_ts: float
    text: str


@dataclass(frozen=True)
class StableTranscript:
    """A transcript candidate that passed the streaming stability policy."""

    chunk: AudioChunk
    text: str


@dataclass
class StreamStats:
    """Counters returned by the streaming run."""

    transcript_count: int = 0
    extraction_count: int = 0
    warning_count: int = 0
    skipped_empty_chunks: int = 0


@dataclass(frozen=True)
class StreamConfig:
    """Runtime configuration for the local audio stream."""

    session_id: str
    base_url: str
    input_path: Path
    whisper_model: Path
    qwen_model: Path
    whisper_device: str
    qwen_device: str
    chunk_seconds: float
    max_audio_seconds: float
    delay: float
    extract_every: int
    qwen_tokens: int
    task: str
    language: str
    http_timeout: float
    end_session: bool
    final_extraction: bool
    polish_transcript: bool
    polish_tokens: int
    overlap_seconds: float
    stable_rounds: int
    stable_tail_chars: int
    async_extraction: bool
    extraction_queue_size: int


class Transcriber(Protocol):
    """Minimal interface for Whisper-backed transcription."""

    def transcribe(self, audio: Any) -> str:
        """Return transcript text for one audio chunk."""


class KnowledgeExtractor(Protocol):
    """Minimal interface for Qwen-backed knowledge extraction."""

    def extract(
        self,
        *,
        session_id: str,
        segments: list[TranscriptRecord],
        batch_index: int,
        max_new_tokens: int,
    ) -> dict[str, Any] | None:
        """Return one knowledge.extraction payload, or None to skip this batch."""


class TranscriptPolisher(Protocol):
    """Minimal interface for transcript punctuation/correction."""

    def polish(
        self,
        *,
        raw_text: str,
        previous_context: list[str],
        max_new_tokens: int,
    ) -> list[str] | None:
        """Return corrected sentence-like transcript segments, or None on failure."""


class OpenVINOWhisperTranscriber:
    """OpenVINO GenAI Whisper wrapper loaded lazily at runtime."""

    def __init__(
        self,
        *,
        model_path: Path,
        device: str,
        task: str,
        language: str = "",
    ) -> None:
        import openvino_genai as ov_genai  # noqa: PLC0415

        log(f"Loading Whisper model: {model_path} on {device}")
        self.pipe = ov_genai.WhisperPipeline(str(model_path), device)
        self.task = task
        self.language = language

    def transcribe(self, audio: Any) -> str:
        kwargs: dict[str, object] = {"task": self.task}
        if self.language:
            kwargs["language"] = self.language
        return result_text(self.pipe.generate(audio, **kwargs))


class OpenVINOQwenExtractor:
    """OpenVINO GenAI Qwen wrapper for transcript polish and extraction JSON."""

    def __init__(self, *, model_path: Path, device: str) -> None:
        import openvino_genai as ov_genai  # noqa: PLC0415

        log(f"Loading Qwen model: {model_path} on {device}")
        self.pipe = ov_genai.LLMPipeline(str(model_path), device)

    def extract(
        self,
        *,
        session_id: str,
        segments: list[TranscriptRecord],
        batch_index: int,
        max_new_tokens: int,
    ) -> dict[str, Any] | None:
        if not segments:
            return None

        prompt = build_qwen_extraction_prompt(
            session_id=session_id,
            segments=segments,
        )
        raw = self._generate(prompt, max_new_tokens=max_new_tokens)
        try:
            data = parse_json_object(raw)
        except ValueError as first_error:
            log(f"Qwen JSON parse failed for batch {batch_index}: {first_error}")
            repair_prompt = build_json_repair_prompt(raw)
            repaired = self._generate(repair_prompt, max_new_tokens=max_new_tokens)
            try:
                data = parse_json_object(repaired)
            except ValueError as second_error:
                log(f"Qwen JSON repair failed for batch {batch_index}: {second_error}")
                return None

        payload = normalize_extraction_payload(
            data,
            session_id=session_id,
            segments=segments,
            extraction_id=f"ext_local_qwen_{batch_index:04d}",
        )
        if not payload["entities"] and not payload["relations"]:
            log(f"Qwen returned no entities or relations for batch {batch_index}; skipped")
            return None
        return payload

    def polish(
        self,
        *,
        raw_text: str,
        previous_context: list[str],
        max_new_tokens: int,
    ) -> list[str] | None:
        """Conservatively add punctuation, fix obvious typos, and split sentences."""
        if not raw_text.strip():
            return []

        prompt = build_transcript_polish_prompt(
            raw_text=raw_text,
            previous_context=previous_context,
        )
        raw = self._generate(prompt, max_new_tokens=max_new_tokens)
        try:
            payload = parse_json_object(raw)
        except ValueError as first_error:
            log(f"Qwen transcript polish JSON parse failed: {first_error}")
            repaired = self._generate(
                build_transcript_polish_repair_prompt(raw),
                max_new_tokens=max_new_tokens,
            )
            try:
                payload = parse_json_object(repaired)
            except ValueError as second_error:
                log(f"Qwen transcript polish repair failed: {second_error}")
                return None

        sentences = normalize_polished_sentences(payload.get("sentences"))
        return sentences or None

    def _generate(self, prompt: str, *, max_new_tokens: int) -> str:
        return result_text(
            self.pipe.generate(prompt, max_new_tokens=max_new_tokens, do_sample=False)
        )


def log(message: str) -> None:
    """Print one timestamped log line."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def result_text(result: object) -> str:
    """Normalize common OpenVINO GenAI result shapes to plain text."""
    texts = getattr(result, "texts", None)
    if texts is not None:
        return "\n".join(str(text).strip() for text in texts if str(text).strip()).strip()
    text = getattr(result, "text", None)
    if text is not None:
        return str(text).strip()
    return str(result).strip()


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


def chunk_time_ranges(
    *,
    total_samples: int,
    chunk_samples: int,
    sample_rate: int = SAMPLE_RATE,
) -> list[tuple[int, float, float, int]]:
    """Return chunk index, start, end, and sample count for deterministic tests."""
    if total_samples <= 0:
        return []
    if chunk_samples <= 0:
        raise ValueError("chunk_samples must be positive")

    ranges: list[tuple[int, float, float, int]] = []
    offset = 0
    index = 1
    while offset < total_samples:
        count = min(chunk_samples, total_samples - offset)
        start_ts = offset / sample_rate
        end_ts = (offset + count) / sample_rate
        ranges.append((index, start_ts, end_ts, count))
        offset += count
        index += 1
    return ranges


def sliding_chunk_time_ranges(
    *,
    total_samples: int,
    chunk_samples: int,
    overlap_samples: int,
    sample_rate: int = SAMPLE_RATE,
) -> list[tuple[int, float, float, int]]:
    """Return window ranges for chunked ASR with overlap."""
    if total_samples <= 0:
        return []
    if chunk_samples <= 0:
        raise ValueError("chunk_samples must be positive")
    if overlap_samples < 0:
        raise ValueError("overlap_samples must be non-negative")
    if overlap_samples >= chunk_samples:
        raise ValueError("overlap_seconds must be smaller than chunk_seconds")

    ranges: list[tuple[int, float, float, int]] = []
    step_samples = chunk_samples - overlap_samples
    consumed = 0
    index = 1
    first = True
    tail_samples = 0
    while consumed < total_samples:
        read_samples = chunk_samples if first else step_samples
        new_samples = min(read_samples, total_samples - consumed)
        if new_samples <= 0:
            break
        start_sample = max(0, consumed - tail_samples)
        consumed += new_samples
        count = consumed - start_sample
        ranges.append(
            (
                index,
                start_sample / sample_rate,
                consumed / sample_rate,
                count,
            )
        )
        tail_samples = min(overlap_samples, count)
        first = False
        index += 1
    return ranges


def iter_audio_chunks(
    input_path: Path,
    *,
    chunk_seconds: float,
    overlap_seconds: float,
    max_audio_seconds: float,
    sample_rate: int = SAMPLE_RATE,
) -> Iterable[AudioChunk]:
    """Decode media with ffmpeg and yield float32 mono chunks."""
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be positive")
    if overlap_seconds < 0:
        raise ValueError("overlap_seconds must be non-negative")
    if overlap_seconds >= chunk_seconds:
        raise ValueError("overlap_seconds must be smaller than chunk_seconds")

    import numpy as np  # noqa: PLC0415

    chunk_samples = int(chunk_seconds * sample_rate)
    if chunk_samples <= 0:
        raise ValueError("chunk_seconds is too small")
    overlap_samples = int(overlap_seconds * sample_rate)
    step_samples = max(1, chunk_samples - overlap_samples)

    max_samples = (
        int(max_audio_seconds * sample_rate)
        if max_audio_seconds and max_audio_seconds > 0
        else None
    )
    bytes_per_sample = 4
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
    index = 1
    first = True
    tail: Any | None = None
    stopped_early = False
    try:
        while True:
            read_samples = chunk_samples if first else step_samples
            if max_samples is not None:
                remaining_samples = max_samples - emitted_samples
                if remaining_samples <= 0:
                    stopped_early = True
                    break
                read_bytes = min(read_samples, remaining_samples) * bytes_per_sample
            else:
                read_bytes = read_samples * bytes_per_sample

            raw = process.stdout.read(read_bytes)
            if not raw:
                break
            new_audio = np.frombuffer(raw, dtype=np.float32).copy()
            if new_audio.size == 0:
                break

            window_start_sample = emitted_samples - (int(tail.size) if tail is not None else 0)
            emitted_samples += int(new_audio.size)
            if tail is not None and tail.size:
                audio = np.concatenate([tail, new_audio])
            else:
                audio = new_audio
            np.nan_to_num(audio, copy=False)
            np.clip(audio, -1.0, 1.0, out=audio)

            start_ts = max(0, window_start_sample) / sample_rate
            end_ts = emitted_samples / sample_rate
            yield AudioChunk(index=index, start_ts=start_ts, end_ts=end_ts, audio=audio)
            tail = audio[-overlap_samples:].copy() if overlap_samples > 0 else None
            first = False
            index += 1
    finally:
        if stopped_early and process.poll() is None:
            process.terminate()
        if process.stdout:
            process.stdout.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        return_code = process.wait()

    if return_code != 0 and not stopped_early:
        raise RuntimeError(f"ffmpeg failed for {input_path}: {stderr.strip()}")


def segment_id_for_chunk(index: int, sentence_index: int | None = None) -> str:
    """Create a stable transcript segment ID for one audio chunk/sentence."""
    base = f"seg_local_whisper_{index:04d}"
    if sentence_index is None:
        return base
    return f"{base}_{sentence_index:02d}"


def transcript_payload(
    *,
    session_id: str,
    chunk: AudioChunk,
    text: str,
    segment_id: str | None = None,
) -> dict[str, Any]:
    """Build a transcript.segment payload."""
    return {
        "segment_id": segment_id or segment_id_for_chunk(chunk.index),
        "session_id": session_id,
        "start_ts": round(chunk.start_ts, 3),
        "end_ts": round(chunk.end_ts, 3),
        "text": text.strip(),
        "speaker": "teacher",
        "confidence": None,
        "is_final": True,
        "source": "local_whisper_openvino",
    }


def transcript_payloads_for_chunk(
    *,
    session_id: str,
    chunk: AudioChunk,
    texts: list[str],
) -> list[dict[str, Any]]:
    """Build one or more transcript payloads for one Whisper chunk."""
    clean_texts = [text.strip() for text in texts if text.strip()]
    if not clean_texts:
        return []
    if len(clean_texts) == 1:
        return [
            transcript_payload(
                session_id=session_id,
                chunk=chunk,
                text=clean_texts[0],
            )
        ]

    ranges = allocate_sentence_ranges(
        chunk_start=chunk.start_ts,
        chunk_end=chunk.end_ts,
        texts=clean_texts,
    )
    return [
        {
            **transcript_payload(
                session_id=session_id,
                chunk=AudioChunk(
                    index=chunk.index,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    audio=chunk.audio,
                ),
                text=text,
                segment_id=segment_id_for_chunk(chunk.index, sentence_index),
            ),
        }
        for sentence_index, (text, (start_ts, end_ts)) in enumerate(
            zip(clean_texts, ranges, strict=True),
            start=1,
        )
    ]


def allocate_sentence_ranges(
    *,
    chunk_start: float,
    chunk_end: float,
    texts: list[str],
) -> list[tuple[float, float]]:
    """Allocate approximate timestamps to polished sentences by text length."""
    if not texts:
        return []
    duration = max(0.0, chunk_end - chunk_start)
    weights = [max(1, len(text.strip())) for text in texts]
    total_weight = sum(weights)
    ranges: list[tuple[float, float]] = []
    cursor = chunk_start
    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            end_ts = chunk_end
        else:
            end_ts = chunk_start + duration * (sum(weights[: index + 1]) / total_weight)
        ranges.append((round(cursor, 3), round(max(cursor, end_ts), 3)))
        cursor = end_ts
    return ranges


def build_transcript_polish_prompt(
    *,
    raw_text: str,
    previous_context: list[str],
) -> str:
    """Prompt Qwen to produce readable classroom subtitle sentences."""
    return prompt_templates.transcript_polish_prompt(
        raw_text=raw_text,
        previous_context=previous_context,
    )


def build_transcript_polish_repair_prompt(raw_text: str) -> str:
    """Ask Qwen to convert a malformed polish response into the sentence schema."""
    return prompt_templates.transcript_polish_repair_prompt(raw_text)


def normalize_polished_sentences(value: object) -> list[str]:
    """Normalize Qwen polish output into non-empty sentence strings."""
    if not isinstance(value, list):
        return []
    sentences: list[str] = []
    seen: set[str] = set()
    for item in value:
        sentence = clean_text(item)
        if not sentence:
            continue
        if sentence in seen:
            continue
        seen.add(sentence)
        sentences.append(sentence)
    return sentences


def transcript_compare_key(value: object) -> str:
    """Normalize transcript text for grounding and duplicate checks."""
    return "".join(re.findall(r"[0-9A-Za-z\u4e00-\u9fff]+", clean_text(value))).lower()


def sequence_coverage(candidate: str, source: str) -> float:
    """Return how much of candidate can be aligned to source."""
    if not candidate:
        return 0.0
    matcher = SequenceMatcher(None, candidate, source, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(candidate)


def is_grounded_polished_sentence(
    *,
    sentence: str,
    raw_text: str,
    min_coverage: float = 0.72,
) -> bool:
    """Check that a polished sentence is supported by the raw Whisper text."""
    sentence_key = transcript_compare_key(sentence)
    raw_key = transcript_compare_key(raw_text)
    if not sentence_key or not raw_key:
        return False
    if sentence_key in raw_key:
        return True
    if len(sentence_key) <= 3:
        return False
    if len(sentence_key) > max(len(raw_key) + 8, int(len(raw_key) * 1.2)):
        return False
    return sequence_coverage(sentence_key, raw_key) >= min_coverage


def filter_grounded_polished_sentences(
    *,
    raw_text: str,
    sentences: list[str],
    previous_context: list[str],
) -> list[str]:
    """Keep only Qwen polish results that are grounded in the current raw text."""
    raw_key = transcript_compare_key(raw_text)
    if not raw_key:
        return []

    previous_keys = {
        transcript_compare_key(item)
        for item in previous_context[-8:]
        if len(transcript_compare_key(item)) >= 8
    }
    accepted: list[str] = []
    accepted_keys: set[str] = set()
    rejected_count = 0

    for sentence in sentences:
        sentence_key = transcript_compare_key(sentence)
        if not sentence_key:
            continue
        if sentence_key in accepted_keys:
            rejected_count += 1
            continue
        if len(sentence_key) >= 8 and sentence_key in previous_keys:
            rejected_count += 1
            log("Rejected repeated polish sentence from previous context")
            continue
        if not is_grounded_polished_sentence(sentence=sentence, raw_text=raw_text):
            rejected_count += 1
            log("Rejected ungrounded polish sentence")
            continue
        accepted.append(sentence)
        accepted_keys.add(sentence_key)

    accepted_length = sum(len(transcript_compare_key(sentence)) for sentence in accepted)
    max_accepted_length = max(len(raw_key) + 8, int(len(raw_key) * 1.2))
    if accepted and accepted_length > max_accepted_length:
        log("Rejected polish result because combined text is longer than raw transcript")
        return []
    if rejected_count:
        log(f"Rejected {rejected_count} unsafe polish sentence(s)")
    return accepted


def build_qwen_extraction_prompt(
    *,
    session_id: str,
    segments: list[TranscriptRecord],
) -> str:
    """Prompt Qwen for a strict extraction-shaped JSON object."""
    return prompt_templates.local_qwen_extraction_prompt(
        session_id=session_id,
        segments=[
            {
                "segment_id": segment.segment_id,
                "start_ts": segment.start_ts,
                "end_ts": segment.end_ts,
                "text": segment.text,
            }
            for segment in segments
        ],
    )


def build_json_repair_prompt(raw_text: str) -> str:
    """Ask Qwen to convert a malformed response into the required JSON shape."""
    return prompt_templates.local_qwen_extraction_repair_prompt(raw_text)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse one JSON object from raw LLM output."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = strip_code_fence(stripped)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        json_text = first_balanced_json_object(stripped)
        data = json.loads(json_text)

    if not isinstance(data, dict):
        raise ValueError("Qwen output must be a JSON object")
    return data


def strip_code_fence(text: str) -> str:
    """Strip a surrounding Markdown code fence when present."""
    lines = text.strip().splitlines()
    if len(lines) >= 3 and lines[0].strip().startswith("```") and lines[-1].strip().startswith("```"):
        return "\n".join(lines[1:-1]).strip()
    return text


def first_balanced_json_object(text: str) -> str:
    """Return the first balanced JSON object substring."""
    start = text.find("{")
    if start < 0:
        raise ValueError("Qwen output did not contain a JSON object")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    raise ValueError("Qwen output contained an unbalanced JSON object")


def normalize_extraction_payload(
    data: dict[str, Any],
    *,
    session_id: str,
    segments: list[TranscriptRecord],
    extraction_id: str,
) -> dict[str, Any]:
    """Force Qwen output into EDU-Mate's knowledge.extraction payload shape."""
    allowed_ids = [segment.segment_id for segment in segments]
    source_ids = valid_source_ids(data.get("source_segment_ids"), allowed_ids)
    entities = normalize_entities(data.get("entities"))
    relations = normalize_relations(data.get("relations"))
    importance = normalized_importance(data.get("importance"))
    timestamp_range = [
        round(min(segment.start_ts for segment in segments), 3),
        round(max(segment.end_ts for segment in segments), 3),
    ]
    return {
        "extraction_id": str(data.get("extraction_id") or extraction_id),
        "session_id": session_id,
        "source_segment_ids": source_ids,
        "source_visual_ids": [],
        "timestamp_range": timestamp_range,
        "entities": entities,
        "relations": relations,
        "importance": importance,
    }


def valid_source_ids(value: object, allowed_ids: list[str]) -> list[str]:
    """Keep source refs constrained to the prompted segment IDs."""
    if not isinstance(value, list):
        return allowed_ids
    allowed = set(allowed_ids)
    valid = [str(item) for item in value if str(item) in allowed]
    return valid or allowed_ids


def normalize_entities(value: object) -> list[dict[str, Any]]:
    """Normalize model entities and drop unusable entries."""
    if not isinstance(value, list):
        return []

    entities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"))
        if not name:
            continue
        key = name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        entity_id = clean_text(item.get("entity_id")) or node_id_for_name(name)
        entities.append(
            {
                "entity_id": entity_id,
                "name": name,
                "type": clean_text(item.get("type")) or "concept",
                "description": clean_text(item.get("description")) or None,
            }
        )
    return entities


def normalize_relations(value: object) -> list[dict[str, str]]:
    """Normalize model relations and drop incomplete entries."""
    if not isinstance(value, list):
        return []

    relations: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        source = clean_text(item.get("source"))
        target = clean_text(item.get("target"))
        relation = relation_label(clean_text(item.get("relation")) or "related_to")
        if not source or not target:
            continue
        key = (source.lower(), target.lower(), relation)
        if key in seen:
            continue
        seen.add(key)
        relations.append({"source": source, "target": target, "relation": relation})
    return relations


def clean_text(value: object) -> str:
    """Convert a scalar-ish model value to trimmed text."""
    if value is None:
        return ""
    return str(value).strip()


def node_id_for_name(name: str) -> str:
    """Create a stable graph node ID for a model entity name."""
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", name.strip()).strip("_")
    return f"node_{slug or 'unknown'}"


def relation_label(value: str) -> str:
    """Make relation labels graph-friendly while preserving clear Chinese labels."""
    normalized = value.strip().lower()
    if not normalized:
        return "related_to"
    if re.search(r"[A-Za-z0-9]", normalized):
        normalized = re.sub(r"[^0-9A-Za-z]+", "_", normalized).strip("_")
    return normalized or "related_to"


def normalized_importance(value: object) -> float:
    """Clamp importance into the backend's expected 0..1 range."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.75
    return max(0.0, min(1.0, number))


def post_json(
    base_url: str,
    path: str,
    body: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    """POST JSON to the EDU-Mate backend."""
    url = base_url.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with open_backend_request(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed: {exc.code} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot connect to backend at {url}: {exc}") from exc
    return json.loads(raw) if raw else {}


def get_json(
    base_url: str,
    path: str,
    *,
    timeout: float,
) -> Any:
    """GET JSON from the EDU-Mate backend."""
    url = base_url.rstrip("/") + path
    request = Request(url, method="GET")

    try:
        with open_backend_request(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed: {exc.code} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot connect to backend at {url}: {exc}") from exc
    return json.loads(raw) if raw else {}


def send_event(
    *,
    base_url: str,
    session_id: str,
    event_type: str,
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    """Send one RealtimeEvent to the existing backend /events endpoint."""
    return post_json(
        base_url,
        "/events",
        {
            "session_id": session_id,
            "event_type": event_type,
            "payload": payload,
        },
        timeout=timeout,
    )


def end_session(*, base_url: str, session_id: str, timeout: float) -> dict[str, Any]:
    """End the classroom through the existing backend API."""
    return post_json(base_url, f"/sessions/{session_id}/end", {}, timeout=timeout)


def start_backend_session(
    *,
    base_url: str,
    timeout: float,
    title: str = "本地音频联调课堂",
    course: str = "EDU-Mate Local Test",
) -> dict[str, Any]:
    """Create a backend recording session for local integration tests."""
    return post_json(
        base_url,
        "/sessions/start",
        {
            "title": title,
            "course": course,
            "language": "zh-CN",
            "created_by": "local-script",
            "device_id": "local-audio-test",
        },
        timeout=timeout,
    )


def list_backend_recording_sessions(
    *,
    base_url: str,
    timeout: float,
) -> list[dict[str, Any]]:
    """Return currently recording backend sessions, newest first."""
    payload = get_json(base_url, "/sessions/recording", timeout=timeout)
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def resolve_backend_session_id(
    *,
    requested_session_id: str,
    base_url: str,
    timeout: float,
    create_if_missing: bool = True,
) -> str:
    """Resolve ``auto``/empty session ids for local test scripts."""
    requested = requested_session_id.strip()
    if requested and requested.lower() != "auto":
        return requested

    sessions = list_backend_recording_sessions(base_url=base_url, timeout=timeout)
    if sessions:
        if len(sessions) > 1:
            log(
                "Found multiple recording sessions; using newest "
                f"{sessions[0].get('session_id')}"
            )
        session_id = str(sessions[0].get("session_id") or "").strip()
        if session_id:
            log(f"Using existing recording session: {session_id}")
            return session_id

    if not create_if_missing:
        raise RuntimeError("No recording backend session is available.")

    session = start_backend_session(base_url=base_url, timeout=timeout)
    session_id = str(session.get("session_id") or "").strip()
    if not session_id:
        raise RuntimeError("Backend did not return a session_id for the new session.")
    log(f"Created backend test session: {session_id}")
    return session_id


def polished_or_raw_transcript(
    *,
    raw_text: str,
    polisher: TranscriptPolisher | None,
    previous_context: list[str],
    max_new_tokens: int,
    enabled: bool,
) -> list[str]:
    """Return polished subtitle sentences when enabled, otherwise raw text."""
    if not enabled:
        return [raw_text]
    if polisher is None:
        log("Transcript polish requested but no Qwen polisher is available; using raw text")
        return [raw_text]

    start = time.time()
    sentences = polisher.polish(
        raw_text=raw_text,
        previous_context=previous_context,
        max_new_tokens=max_new_tokens,
    )
    log(f"Transcript polish finished in {time.time() - start:.2f}s")
    if not sentences:
        log("Transcript polish returned no usable sentences; using raw Whisper text")
        return [raw_text]

    safe_sentences = filter_grounded_polished_sentences(
        raw_text=raw_text,
        sentences=sentences,
        previous_context=previous_context,
    )
    if not safe_sentences:
        log("Transcript polish failed grounding checks; using raw Whisper text")
        return [raw_text]
    return safe_sentences


def is_recent_duplicate_transcript(text: str, recent_keys: list[str]) -> bool:
    """Return True when a long transcript was already sent recently."""
    key = transcript_compare_key(text)
    return len(key) >= 8 and key in recent_keys


def remember_transcript(text: str, recent_keys: list[str], *, limit: int = 24) -> None:
    """Track recently sent transcript text for duplicate suppression."""
    key = transcript_compare_key(text)
    if len(key) < 8:
        return
    recent_keys.append(key)
    del recent_keys[:-limit]


def longest_suffix_prefix_overlap(left: str, right: str) -> int:
    """Return the longest exact overlap between left suffix and right prefix."""
    max_length = min(len(left), len(right))
    for length in range(max_length, 0, -1):
        if left[-length:] == right[:length]:
            return length
    return 0


def drop_prefix_by_compare_chars(text: str, drop_chars: int) -> str:
    """Drop a prefix measured in normalized transcript characters."""
    if drop_chars <= 0:
        return text
    seen = 0
    for index, char in enumerate(text):
        if transcript_compare_key(char):
            seen += 1
        if seen >= drop_chars:
            return text[index + 1 :].strip()
    return ""


def prefix_by_compare_chars(text: str, keep_chars: int) -> str:
    """Keep a prefix measured in normalized transcript characters."""
    if keep_chars <= 0:
        return ""
    seen = 0
    for index, char in enumerate(text):
        if transcript_compare_key(char):
            seen += 1
        if seen >= keep_chars:
            return text[: index + 1].strip()
    return text.strip()


def strip_committed_overlap(text: str, committed_key: str) -> str:
    """Remove content already committed by previous overlapping ASR windows."""
    candidate_key = transcript_compare_key(text)
    if not candidate_key:
        return ""
    if candidate_key in committed_key:
        return ""
    overlap = longest_suffix_prefix_overlap(committed_key, candidate_key)
    if overlap <= 0:
        return text.strip()
    return drop_prefix_by_compare_chars(text, overlap)


def trim_unstable_tail(text: str, hold_chars: int) -> str:
    """Hold back a short tail so boundary fragments can be confirmed later."""
    if hold_chars <= 0:
        return text.strip()
    key_length = len(transcript_compare_key(text))
    if key_length <= hold_chars + 8:
        return text.strip()
    return prefix_by_compare_chars(text, key_length - hold_chars)


class TranscriptCommitter:
    """Delay and de-duplicate overlapping Whisper window transcripts."""

    def __init__(self, *, stable_rounds: int, stable_tail_chars: int) -> None:
        if stable_rounds <= 0:
            raise ValueError("stable_rounds must be positive")
        if stable_tail_chars < 0:
            raise ValueError("stable_tail_chars must be non-negative")
        self.stable_rounds = stable_rounds
        self.stable_tail_chars = stable_tail_chars
        self.pending: list[StableTranscript] = []
        self.committed_key = ""

    def push(self, *, chunk: AudioChunk, text: str) -> list[StableTranscript]:
        """Add a window result and return newly stable transcripts."""
        clean = text.strip()
        if not clean:
            return []
        self.pending.append(StableTranscript(chunk=chunk, text=clean))
        if len(self.pending) < self.stable_rounds:
            return []
        return self._commit(self.pending.pop(0), final=False)

    def flush(self) -> list[StableTranscript]:
        """Commit all pending transcripts at end-of-stream."""
        stable: list[StableTranscript] = []
        while self.pending:
            stable.extend(self._commit(self.pending.pop(0), final=True))
        return stable

    def _commit(self, item: StableTranscript, *, final: bool) -> list[StableTranscript]:
        text = strip_committed_overlap(item.text, self.committed_key)
        if not text:
            return []
        if not final:
            text = trim_unstable_tail(text, self.stable_tail_chars)
        key = transcript_compare_key(text)
        if not key:
            return []
        self.committed_key = (self.committed_key + key)[-8000:]
        return [StableTranscript(chunk=item.chunk, text=text)]


@dataclass(frozen=True)
class ExtractionTask:
    """One queued knowledge extraction batch."""

    segments: list[TranscriptRecord]
    batch_index: int


class ExtractionWorker:
    """Background Qwen worker so ASR transcript delivery is not blocked."""

    def __init__(
        self,
        *,
        config: StreamConfig,
        extractor_factory: Callable[[], KnowledgeExtractor],
        send_event_func: Callable[..., dict[str, Any]],
    ) -> None:
        self.config = config
        self.extractor_factory = extractor_factory
        self.send_event_func = send_event_func
        self.tasks: queue.Queue[ExtractionTask | None] = queue.Queue(
            maxsize=max(1, config.extraction_queue_size)
        )
        self.extraction_count = 0
        self.warning_count = 0
        self.lock = threading.Lock()
        self.thread = threading.Thread(
            target=self._run,
            name="local-audio-qwen-extractor",
            daemon=True,
        )
        self.thread.start()

    def submit(self, *, segments: list[TranscriptRecord], batch_index: int) -> bool:
        """Queue an extraction batch without blocking the ASR loop."""
        try:
            self.tasks.put_nowait(
                ExtractionTask(segments=list(segments), batch_index=batch_index)
            )
        except queue.Full:
            log(f"Dropped knowledge batch {batch_index}; extraction queue is full")
            with self.lock:
                self.warning_count += 1
            return False
        log(f"Queued knowledge batch {batch_index} from {len(segments)} segment(s)")
        return True

    def close(self) -> tuple[int, int]:
        """Stop the worker after all queued batches complete."""
        self.tasks.put(None)
        self.thread.join()
        with self.lock:
            return self.extraction_count, self.warning_count

    def _run(self) -> None:
        extractor: KnowledgeExtractor | None = None
        while True:
            task = self.tasks.get()
            try:
                if task is None:
                    return
                if extractor is None:
                    extractor = self.extractor_factory()
                ok = send_extraction_batch(
                    config=self.config,
                    extractor=extractor,
                    segments=task.segments,
                    batch_index=task.batch_index,
                    send_event_func=self.send_event_func,
                )
                with self.lock:
                    if ok:
                        self.extraction_count += 1
                    else:
                        self.warning_count += 1
            except Exception as exc:  # noqa: BLE001
                log(f"Knowledge extraction worker failed: {exc}")
                with self.lock:
                    self.warning_count += 1
            finally:
                self.tasks.task_done()


def run_audio_stream(
    config: StreamConfig,
    *,
    chunks: Iterable[AudioChunk] | None = None,
    transcriber: Transcriber | None = None,
    extractor: KnowledgeExtractor | None = None,
    polisher: TranscriptPolisher | None = None,
    send_event_func: Callable[..., dict[str, Any]] = send_event,
    end_session_func: Callable[..., dict[str, Any]] = end_session,
    sleep_func: Callable[[float], None] = time.sleep,
) -> StreamStats:
    """Run the full stream with injectable fakes for tests."""
    if config.extract_every <= 0:
        raise ValueError("extract_every must be positive")

    media_path = find_media(config.input_path)
    log(f"Input media: {media_path}")
    log(f"Session: {config.session_id}")
    log(f"Devices: Whisper={config.whisper_device}, Qwen={config.qwen_device}")
    if config.whisper_device.upper() == "NPU":
        log(
            "Warning: Whisper large-v3-turbo on NPU may trigger "
            "ZE_RESULT_ERROR_DEVICE_LOST on this platform. "
            "Use --whisper-device GPU or CPU if the process aborts."
        )
    log(
        "ASR streaming: "
        f"chunk={config.chunk_seconds:.2f}s, "
        f"overlap={config.overlap_seconds:.2f}s, "
        f"stable_rounds={config.stable_rounds}"
    )

    active_chunks = chunks
    if active_chunks is None:
        active_chunks = iter_audio_chunks(
            media_path,
            chunk_seconds=config.chunk_seconds,
            overlap_seconds=config.overlap_seconds,
            max_audio_seconds=config.max_audio_seconds,
        )

    active_transcriber = transcriber or OpenVINOWhisperTranscriber(
        model_path=config.whisper_model,
        device=config.whisper_device,
        task=config.task,
        language=config.language,
    )

    sync_extractor = extractor

    def extractor_factory() -> KnowledgeExtractor:
        if extractor is not None:
            return extractor
        return OpenVINOQwenExtractor(
            model_path=config.qwen_model,
            device=config.qwen_device,
        )

    def get_sync_extractor() -> KnowledgeExtractor:
        nonlocal sync_extractor
        if sync_extractor is None:
            sync_extractor = OpenVINOQwenExtractor(
                model_path=config.qwen_model,
                device=config.qwen_device,
            )
        return sync_extractor

    active_polisher = polisher
    if active_polisher is None and config.polish_transcript:
        active_polisher = OpenVINOQwenExtractor(
            model_path=config.qwen_model,
            device=config.qwen_device,
        )

    extraction_worker = (
        ExtractionWorker(
            config=config,
            extractor_factory=extractor_factory,
            send_event_func=send_event_func,
        )
        if config.async_extraction
        else None
    )

    stats = StreamStats()
    pending_segments: list[TranscriptRecord] = []
    previous_context: list[str] = []
    recent_transcript_keys: list[str] = []
    committer = TranscriptCommitter(
        stable_rounds=config.stable_rounds,
        stable_tail_chars=config.stable_tail_chars,
    )
    batch_index = 1

    def schedule_extraction(segments: list[TranscriptRecord], current_batch_index: int) -> None:
        if extraction_worker is not None:
            extraction_worker.submit(
                segments=segments,
                batch_index=current_batch_index,
            )
            return

        if send_extraction_batch(
            config=config,
            extractor=get_sync_extractor(),
            segments=segments,
            batch_index=current_batch_index,
            send_event_func=send_event_func,
        ):
            stats.extraction_count += 1
        else:
            stats.warning_count += 1

    def handle_stable_transcript(stable: StableTranscript) -> None:
        nonlocal batch_index, pending_segments, previous_context
        transcript_texts = polished_or_raw_transcript(
            raw_text=stable.text,
            polisher=active_polisher,
            previous_context=previous_context,
            max_new_tokens=config.polish_tokens,
            enabled=config.polish_transcript,
        )
        payloads = transcript_payloads_for_chunk(
            session_id=config.session_id,
            chunk=stable.chunk,
            texts=transcript_texts,
        )

        for payload in payloads:
            payload_text = str(payload["text"])
            if is_recent_duplicate_transcript(payload_text, recent_transcript_keys):
                log(f"Skipped duplicate transcript.segment {payload['segment_id']}")
                continue

            send_event_func(
                base_url=config.base_url,
                session_id=config.session_id,
                event_type="transcript.segment",
                payload=payload,
                timeout=config.http_timeout,
            )
            stats.transcript_count += 1
            remember_transcript(payload_text, recent_transcript_keys)

            segment = TranscriptRecord(
                segment_id=str(payload["segment_id"]),
                start_ts=float(payload["start_ts"]),
                end_ts=float(payload["end_ts"]),
                text=payload_text,
            )
            pending_segments.append(segment)
            previous_context.append(segment.text)
            previous_context = previous_context[-6:]

            if len(pending_segments) >= config.extract_every:
                schedule_extraction(pending_segments, batch_index)
                pending_segments = []
                batch_index += 1

    for chunk in active_chunks:
        log(f"Transcribing chunk {chunk.index} ({chunk.start_ts:.2f}-{chunk.end_ts:.2f}s)")
        start = time.time()
        raw_text = active_transcriber.transcribe(chunk.audio).strip()
        log(f"Whisper chunk {chunk.index} finished in {time.time() - start:.2f}s")
        if not raw_text:
            stats.skipped_empty_chunks += 1
            log(f"Chunk {chunk.index} transcript was empty; skipped")
            continue

        stable_items = committer.push(chunk=chunk, text=raw_text)
        if not stable_items:
            log(f"Chunk {chunk.index} waiting for stability confirmation")
        for stable in stable_items:
            handle_stable_transcript(stable)

        if config.delay > 0:
            sleep_func(config.delay)

    for stable in committer.flush():
        handle_stable_transcript(stable)

    if pending_segments and config.final_extraction:
        schedule_extraction(pending_segments, batch_index)

    if extraction_worker is not None:
        extraction_count, warning_count = extraction_worker.close()
        stats.extraction_count += extraction_count
        stats.warning_count += warning_count

    if config.end_session:
        end_session_func(
            base_url=config.base_url,
            session_id=config.session_id,
            timeout=config.http_timeout,
        )
        log("Ended session via backend API")

    log(
        "Stream finished: "
        f"transcripts={stats.transcript_count}, "
        f"extractions={stats.extraction_count}, "
        f"warnings={stats.warning_count}, "
        f"empty_chunks={stats.skipped_empty_chunks}"
    )
    return stats


def send_extraction_batch(
    *,
    config: StreamConfig,
    extractor: KnowledgeExtractor,
    segments: list[TranscriptRecord],
    batch_index: int,
    send_event_func: Callable[..., dict[str, Any]],
) -> bool:
    """Run Qwen on one transcript batch and send a knowledge event if valid."""
    log(f"Extracting knowledge batch {batch_index} from {len(segments)} segment(s)")
    start = time.time()
    payload = extractor.extract(
        session_id=config.session_id,
        segments=segments,
        batch_index=batch_index,
        max_new_tokens=config.qwen_tokens,
    )
    log(f"Qwen batch {batch_index} finished in {time.time() - start:.2f}s")
    if payload is None:
        return False

    send_event_func(
        base_url=config.base_url,
        session_id=config.session_id,
        event_type="knowledge.extraction",
        payload=payload,
        timeout=config.http_timeout,
    )
    log(
        f"Sent knowledge.extraction {payload['extraction_id']} "
        f"({len(payload['entities'])} nodes, {len(payload['relations'])} edges)"
    )
    return True


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Stream local audio through OpenVINO Whisper and Qwen into EDU-Mate."
    )
    parser.add_argument(
        "--session-id",
        default="auto",
        help=(
            "Frontend-created session_id. Use 'auto' or omit it to attach to the "
            "newest recording backend session, creating one if none exists."
        ),
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Media file or directory.")
    parser.add_argument("--whisper-model", default=str(DEFAULT_WHISPER_MODEL))
    parser.add_argument("--qwen-model", default=str(DEFAULT_QWEN_MODEL))
    parser.add_argument(
        "--whisper-device",
        default=DEFAULT_WHISPER_DEVICE,
        help=f"Whisper OpenVINO device. Default: {DEFAULT_WHISPER_DEVICE}.",
    )
    parser.add_argument(
        "--qwen-device",
        default=DEFAULT_QWEN_DEVICE,
        help=f"Qwen OpenVINO device. Default: {DEFAULT_QWEN_DEVICE}.",
    )
    parser.add_argument(
        "--chunk-seconds",
        type=float,
        default=4.0,
        help="ASR window size in seconds.",
    )
    parser.add_argument(
        "--overlap-seconds",
        type=float,
        default=1.0,
        help="Audio overlap between ASR windows to reduce boundary drops.",
    )
    parser.add_argument(
        "--stable-rounds",
        type=int,
        default=2,
        help="Number of ASR windows before a transcript is committed.",
    )
    parser.add_argument(
        "--stable-tail-chars",
        type=int,
        default=4,
        help="Normalized transcript chars held back before the next ASR window.",
    )
    parser.add_argument(
        "--max-audio-seconds",
        type=float,
        default=120.0,
        help="Limit audio for smoke tests. 0 means full file.",
    )
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between chunks.")
    parser.add_argument("--extract-every", type=int, default=2)
    parser.add_argument("--qwen-tokens", type=int, default=600)
    parser.add_argument(
        "--sync-extraction",
        action="store_true",
        help="Run Qwen extraction inline. Default queues extraction in the background.",
    )
    parser.add_argument(
        "--extraction-queue-size",
        type=int,
        default=8,
        help="Max queued Qwen extraction batches before new batches are dropped.",
    )
    parser.add_argument(
        "--polish-transcript",
        action="store_true",
        help="Use Qwen to add punctuation, fix obvious typos, and split subtitles.",
    )
    parser.add_argument(
        "--polish-tokens",
        type=int,
        default=300,
        help="Max new tokens for each transcript polish request.",
    )
    parser.add_argument("--task", default="transcribe", choices=["transcribe", "translate"])
    parser.add_argument("--language", default="", help="Optional Whisper language token.")
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument(
        "--end-session",
        action="store_true",
        help="End and save the classroom after streaming. Default keeps it recording.",
    )
    parser.add_argument(
        "--no-final-extraction",
        action="store_true",
        help="Skip extracting a final incomplete transcript batch.",
    )
    return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> StreamConfig:
    """Build a typed config from parsed CLI args."""
    session_id = resolve_backend_session_id(
        requested_session_id=args.session_id,
        base_url=args.base_url,
        timeout=args.http_timeout,
    )
    return StreamConfig(
        session_id=session_id,
        base_url=args.base_url,
        input_path=Path(args.input),
        whisper_model=Path(args.whisper_model),
        qwen_model=Path(args.qwen_model),
        whisper_device=args.whisper_device,
        qwen_device=args.qwen_device,
        chunk_seconds=args.chunk_seconds,
        overlap_seconds=args.overlap_seconds,
        max_audio_seconds=args.max_audio_seconds,
        delay=args.delay,
        extract_every=args.extract_every,
        qwen_tokens=args.qwen_tokens,
        task=args.task,
        language=args.language,
        http_timeout=args.http_timeout,
        end_session=args.end_session,
        final_extraction=not args.no_final_extraction,
        polish_transcript=args.polish_transcript,
        polish_tokens=args.polish_tokens,
        stable_rounds=args.stable_rounds,
        stable_tail_chars=args.stable_tail_chars,
        async_extraction=not args.sync_extraction,
        extraction_queue_size=args.extraction_queue_size,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = parse_args(argv or sys.argv[1:])
    config = config_from_args(args)
    run_audio_stream(config)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
