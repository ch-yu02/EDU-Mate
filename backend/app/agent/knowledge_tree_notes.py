"""Cloud-agent extraction from structured Markdown classroom notes.

WhisperLive/Qwen produces a continuously updated Markdown note during class.
This module turns each note snapshot into the existing ``knowledge.extraction``
payload shape so the rest of EDU-Mate can keep using the same graph pipeline.
"""

import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Protocol

from pydantic import ValidationError

from backend.app import prompts
from backend.app.knowledge_quality import (
    is_low_value_entity_name,
    strip_generic_entity_affixes,
)
from backend.app.agent.schemas import NotesKnowledgeTreeUpdateRequest
from backend.app.llm import CloudLLMClient, CloudLLMError, load_llm_settings
from backend.app.models import KnowledgeExtraction, KnowledgeTree


class JsonLLMClient(Protocol):
    """Minimum JSON-completion interface used by the notes graph agent."""

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        """Return one JSON object from the model."""


@dataclass(frozen=True)
class NotesGraphExtractionResult:
    """Result of asking the cloud agent to extract one graph update."""

    extraction: KnowledgeExtraction | None = None
    markdown_hash: str = ""
    session_title: str | None = None
    course: str | None = None
    warnings: tuple[str, ...] = ()
    failed: bool = False


class MarkdownKnowledgeTreeAgent:
    """Generate ``KnowledgeExtraction`` updates from structured Markdown notes."""

    provider_name = "markdown_cloud_llm"
    MAX_SOURCE_SEGMENT_IDS = 5
    MAX_ENTITIES_PER_SNAPSHOT = 8
    MAX_RELATIONS_PER_SNAPSHOT = 10

    def __init__(self, llm_client: JsonLLMClient | None = None) -> None:
        self.llm_client = llm_client
        self._processed_hashes: dict[str, set[str]] = {}
        self._processed_extraction_signatures: dict[str, set[str]] = {}
        self._latest_markdown: dict[str, str] = {}

    def has_processed(self, session_id: str, markdown_hash: str) -> bool:
        """Return true when the same Markdown snapshot was already accepted."""
        return markdown_hash in self._processed_hashes.get(session_id, set())

    def remember_processed(self, session_id: str, markdown_hash: str) -> None:
        """Remember a Markdown snapshot hash after it has been handled."""
        self._processed_hashes.setdefault(session_id, set()).add(markdown_hash)

    def reset(self) -> None:
        """Clear in-memory idempotency state. Tests use this between cases."""
        self._processed_hashes.clear()
        self._processed_extraction_signatures.clear()
        self._latest_markdown.clear()

    def latest_markdown(self, session_id: str) -> str | None:
        """Return the latest structured notes Markdown seen for a session."""
        markdown = self._latest_markdown.get(session_id)
        if markdown and markdown.strip():
            return markdown
        return None

    def extract(
        self,
        request: NotesKnowledgeTreeUpdateRequest,
        knowledge_graph: KnowledgeTree,
    ) -> NotesGraphExtractionResult:
        """Ask the cloud model for a grounded knowledge tree update."""
        markdown_hash = normalized_markdown_hash(request)
        self._latest_markdown[request.session_id] = request.markdown
        if (
            request.update_status != "final"
            and self.has_processed(request.session_id, markdown_hash)
        ):
            return NotesGraphExtractionResult(
                markdown_hash=markdown_hash,
                warnings=("Markdown snapshot already processed; skipped duplicate.",),
            )

        client = self._client_or_none()
        if client is None:
            return NotesGraphExtractionResult(
                markdown_hash=markdown_hash,
                failed=True,
                warnings=("Cloud LLM is not configured for Markdown knowledge tree updates.",),
            )

        try:
            payload = client.complete_json(
                self._system_prompt(),
                self._user_prompt(request, knowledge_graph),
                temperature=0.1,
            )
            extraction, warnings, session_title, course = self._validate_payload(
                payload,
                request=request,
                markdown_hash=markdown_hash,
                knowledge_graph=knowledge_graph,
            )
        except (CloudLLMError, ValidationError, KeyError, TypeError, ValueError) as exc:
            return NotesGraphExtractionResult(
                markdown_hash=markdown_hash,
                failed=True,
                warnings=(f"{exc.__class__.__name__}: {exc}",),
            )

        signature = extraction_signature(extraction)
        if signature and signature in self._processed_extraction_signatures.get(
            request.session_id,
            set(),
        ):
            return NotesGraphExtractionResult(
                markdown_hash=markdown_hash,
                session_title=session_title,
                course=course,
                warnings=tuple(
                    [
                        *warnings,
                        "Graph extraction content already processed; skipped duplicate.",
                    ]
                ),
            )

        if not extraction.entities and not extraction.relations:
            return NotesGraphExtractionResult(
                markdown_hash=markdown_hash,
                session_title=session_title,
                course=course,
                warnings=tuple([*warnings, "No grounded graph items were extracted."]),
            )

        if signature:
            self._processed_extraction_signatures.setdefault(
                request.session_id,
                set(),
            ).add(signature)

        return NotesGraphExtractionResult(
            extraction=extraction,
            markdown_hash=markdown_hash,
            session_title=session_title,
            course=course,
            warnings=tuple(warnings),
        )

    def _client_or_none(self) -> JsonLLMClient | None:
        if self.llm_client is not None:
            return self.llm_client

        settings = load_llm_settings()
        if not settings.enabled:
            return None
        return CloudLLMClient(settings)

    def _validate_payload(
        self,
        payload: dict[str, Any],
        *,
        request: NotesKnowledgeTreeUpdateRequest,
        markdown_hash: str,
        knowledge_graph: KnowledgeTree,
    ) -> tuple[KnowledgeExtraction, list[str], str | None, str | None]:
        focus_segments = focused_source_segments(request)
        source_ids = [segment.segment_id for segment in focus_segments]
        source_text = grounding_source_text(request)
        source_key = comparable_text(source_text)
        warnings: list[str] = []
        existing_labels = [node.label for node in knowledge_graph.nodes]

        entities, allowed_names = self._grounded_entities(
            payload.get("entities"),
            source_key=source_key,
            existing_labels=existing_labels,
            warnings=warnings,
        )
        allowed_names.update(existing_labels)
        relations = self._grounded_relations(
            payload.get("relations"),
            source_key=source_key,
            allowed_names=allowed_names,
            existing_labels=existing_labels,
            warnings=warnings,
        )
        source_segment_ids = self._valid_source_ids(
            payload.get("source_segment_ids"),
            allowed=source_ids,
        )
        session_title, course = session_metadata_from_payload(payload)

        safe_payload = {
            "extraction_id": str(
                payload.get("extraction_id")
                or extraction_id_for_snapshot(request, markdown_hash)
            ),
            "session_id": request.session_id,
            "source_segment_ids": source_segment_ids,
            "source_visual_ids": [],
            "timestamp_range": timestamp_range(request, source_segment_ids),
            "entities": entities[: self.MAX_ENTITIES_PER_SNAPSHOT],
            "relations": relations[: self.MAX_RELATIONS_PER_SNAPSHOT],
            "importance": coerce_importance(payload.get("importance")),
        }
        return KnowledgeExtraction.model_validate(safe_payload), warnings, session_title, course

    def _grounded_entities(
        self,
        value: object,
        *,
        source_key: str,
        existing_labels: list[str],
        warnings: list[str],
    ) -> tuple[list[dict[str, Any]], set[str]]:
        entities: list[dict[str, Any]] = []
        allowed_names: set[str] = set()
        if not isinstance(value, list):
            return entities, allowed_names

        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            name = clean_text(item.get("name"))
            if not name:
                continue
            if is_low_value_entity_name(name):
                warnings.append(f"Dropped low-value entity: {name}")
                continue
            name = canonical_entity_name(name, existing_labels)
            key = comparable_text(name)
            if key in seen:
                continue
            if not is_grounded(name, source_key):
                warnings.append(f"Dropped ungrounded entity: {name}")
                continue
            description = clean_text(item.get("description"))
            if description and not is_grounded(description, source_key, min_coverage=0.6):
                warnings.append(f"Dropped ungrounded description for entity: {name}")
                description = ""
            seen.add(key)
            allowed_names.add(name)
            entity = {
                "entity_id": clean_text(item.get("entity_id")) or None,
                "name": name,
                "type": clean_relation_label(item.get("type"), default="concept"),
                "description": description or None,
            }
            entities.append(entity)
        return entities, allowed_names

    def _grounded_relations(
        self,
        value: object,
        *,
        source_key: str,
        allowed_names: set[str],
        existing_labels: list[str],
        warnings: list[str],
    ) -> list[dict[str, str]]:
        relations: list[dict[str, str]] = []
        if not isinstance(value, list):
            return relations

        allowed_keys = {comparable_text(name) for name in allowed_names}
        seen: set[tuple[str, str, str]] = set()
        for item in value:
            if not isinstance(item, dict):
                continue
            source = clean_text(item.get("source"))
            target = clean_text(item.get("target"))
            relation = clean_relation_label(item.get("relation"), default="related_to")
            if not source or not target:
                continue
            if is_low_value_entity_name(source) or is_low_value_entity_name(target):
                warnings.append(f"Dropped low-value relation endpoints: {source}->{target}")
                continue

            source = canonical_entity_name(source, existing_labels)
            target = canonical_entity_name(target, existing_labels)

            source, target, relation = normalize_relation_direction(
                source,
                target,
                relation,
            )
            relation_source_key = comparable_text(source)
            relation_target_key = comparable_text(target)
            if relation_source_key not in allowed_keys or relation_target_key not in allowed_keys:
                if not (
                    is_grounded(source, source_key)
                    and is_grounded(target, source_key)
                ):
                    warnings.append(f"Dropped relation with unsupported endpoints: {source}->{target}")
                    continue
            key = (relation_source_key, relation_target_key, relation)
            if key in seen:
                continue
            seen.add(key)
            relations.append(
                {
                    "source": source,
                    "target": target,
                    "relation": relation,
                }
            )
        return relations

    def _valid_source_ids(self, value: object, *, allowed: list[str]) -> list[str]:
        if not allowed:
            return []
        if not isinstance(value, list):
            return allowed[-self.MAX_SOURCE_SEGMENT_IDS :]
        allowed_set = set(allowed)
        valid: list[str] = []
        seen: set[str] = set()
        for item in value:
            source_id = str(item)
            if source_id not in allowed_set or source_id in seen:
                continue
            seen.add(source_id)
            valid.append(source_id)
        if not valid:
            return allowed[-self.MAX_SOURCE_SEGMENT_IDS :]
        if len(valid) <= self.MAX_SOURCE_SEGMENT_IDS:
            return valid

        allowed_order = {source_id: index for index, source_id in enumerate(allowed)}
        return sorted(valid, key=lambda item: allowed_order[item])[
            -self.MAX_SOURCE_SEGMENT_IDS :
        ]

    def _system_prompt(self) -> str:
        return prompts.markdown_knowledge_tree_system_prompt()

    def _user_prompt(
        self,
        request: NotesKnowledgeTreeUpdateRequest,
        knowledge_graph: KnowledgeTree,
    ) -> str:
        return prompts.markdown_knowledge_tree_user_prompt(
            session_id=request.session_id,
            snapshot_id=request.snapshot_id,
            sequence=request.sequence,
            update_status=request.update_status,
            existing_nodes=[node.label for node in knowledge_graph.nodes[:80]],
            existing_edges=[
                f"{edge.source} {edge.relation} {edge.target}"
                for edge in knowledge_graph.edges[:80]
            ],
            source_segments=[
                {
                    "segment_id": segment.segment_id,
                    "start_ts": segment.start_ts,
                    "end_ts": segment.end_ts,
                    "text": segment.text,
                }
                for segment in request.source_segments
            ],
            markdown=markdown_without_whisperlive_subtitles(request.markdown),
            recent_source_segments=[
                {
                    "segment_id": segment.segment_id,
                    "start_ts": segment.start_ts,
                    "end_ts": segment.end_ts,
                    "text": segment.text,
                }
                for segment in focused_source_segments(request)
            ],
        )


_WHISPERLIVE_SUBTITLE_HEADING_RE = re.compile(
    r"^##\s*(?:WhisperLive\s+Subtitles|WhisperLive\s+字幕|Subtitles|字幕|语音字幕)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def markdown_without_whisperlive_subtitles(markdown: str) -> str:
    """Remove the raw WhisperLive subtitle appendix before cloud graph prompts.

    The saved ``structured_notes.md`` intentionally keeps the subtitle appendix
    for auditing, but sending that whole appendix to the cloud graph agent makes
    final requests unnecessarily large and encourages subtitle-shaped graph
    outputs. Keep the actual notes and drop only the raw subtitle section.
    """
    match = _WHISPERLIVE_SUBTITLE_HEADING_RE.search(markdown)
    if not match:
        return markdown
    return markdown[: match.start()].rstrip() + "\n"


def normalized_markdown_hash(request: NotesKnowledgeTreeUpdateRequest) -> str:
    """Return caller-provided hash or compute one from Markdown content."""
    if request.markdown_hash and request.markdown_hash.strip():
        return request.markdown_hash.strip()
    return hashlib.sha256(request.markdown.encode("utf-8")).hexdigest()


def extraction_id_for_snapshot(
    request: NotesKnowledgeTreeUpdateRequest,
    markdown_hash: str,
) -> str:
    """Create a stable extraction id for one Markdown snapshot."""
    slug = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", request.snapshot_id).strip("_")
    return f"ext_notes_{request.session_id}_{slug or request.sequence}_{markdown_hash[:10]}"


def extraction_signature(extraction: KnowledgeExtraction) -> str:
    """Create a content signature that ignores snapshot/source bookkeeping."""
    entity_keys = sorted(
        comparable_text(entity.name)
        for entity in extraction.entities
        if comparable_text(entity.name)
    )
    relation_keys = sorted(
        (
            comparable_text(relation.source),
            comparable_text(relation.target),
            clean_relation_label(relation.relation, default="related_to"),
        )
        for relation in extraction.relations
        if comparable_text(relation.source) and comparable_text(relation.target)
    )
    if not entity_keys and not relation_keys:
        return ""

    raw = repr((entity_keys, relation_keys))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def timestamp_range(
    request: NotesKnowledgeTreeUpdateRequest,
    source_segment_ids: list[str] | None = None,
) -> tuple[float, float] | None:
    """Compute timestamp range from the subtitle sources."""
    segments = all_source_segments(request)
    if source_segment_ids is not None:
        allowed = set(source_segment_ids)
        segments = [segment for segment in segments if segment.segment_id in allowed]
    if not segments:
        return None
    starts = [segment.start_ts for segment in segments]
    ends = [segment.end_ts for segment in segments]
    return (min(starts), max(ends))


def grounding_source_text(request: NotesKnowledgeTreeUpdateRequest) -> str:
    """Combine full Markdown context and focused subtitle text for grounding checks."""
    subtitles = "\n".join(segment.text for segment in focused_source_segments(request))
    return f"{request.markdown}\n{subtitles}"


def focused_source_segments(request: NotesKnowledgeTreeUpdateRequest) -> list[Any]:
    """Return the subtitle window this graph update may cite."""
    if request.recent_source_segments:
        return list(request.recent_source_segments)
    return list(request.source_segments)


def all_source_segments(request: NotesKnowledgeTreeUpdateRequest) -> list[Any]:
    """Return full and recent source segments without duplicate IDs."""
    segments: list[Any] = []
    seen: set[str] = set()
    for segment in [*request.source_segments, *request.recent_source_segments]:
        if segment.segment_id in seen:
            continue
        seen.add(segment.segment_id)
        segments.append(segment)
    return segments


def clean_text(value: object) -> str:
    """Normalize one model-provided text value."""
    if value is None:
        return ""
    return str(value).strip()


def clean_optional_metadata(value: object) -> str | None:
    """Normalize optional cloud-generated session metadata."""
    text = clean_text(value)
    if not text:
        return None
    if text.lower() in {"null", "none", "unknown", "n/a"}:
        return None
    if text in {"未知", "未识别", "未命名课程", "无法判断", "课堂笔记"}:
        return None
    return text[:80]


def session_metadata_from_payload(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract optional title/course metadata from the cloud notes response."""
    title = clean_optional_metadata(payload.get("session_title") or payload.get("title"))
    course = clean_optional_metadata(payload.get("course"))
    return title, course


def comparable_text(value: str) -> str:
    """Create a punctuation-insensitive key for grounding checks."""
    return re.sub(r"[\W_]+", "", value.lower(), flags=re.UNICODE)


def canonical_entity_name(name: str, existing_labels: list[str]) -> str:
    """Reuse an existing graph label when the new label is an obvious duplicate."""
    key = comparable_text(name)
    if not key:
        return name

    best_label = name
    best_score = 0.0
    for label in existing_labels:
        label_key = comparable_text(label)
        if not label_key:
            continue
        if key == label_key:
            return label
        if strip_generic_entity_affixes(key) == strip_generic_entity_affixes(label_key):
            return label
        score = SequenceMatcher(None, key, label_key, autojunk=False).ratio()
        if score > best_score:
            best_score = score
            best_label = label
    if best_score >= 0.88:
        return best_label
    return name


def is_grounded(candidate: str, source_key: str, *, min_coverage: float = 0.72) -> bool:
    """Return true when a candidate is supported by the note/source text."""
    key = comparable_text(candidate)
    if not key or not source_key:
        return False
    if key in source_key:
        return True
    if len(key) <= 3:
        return False
    matcher = SequenceMatcher(None, key, source_key, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / max(1, len(key)) >= min_coverage


def clean_relation_label(value: object, *, default: str) -> str:
    """Normalize model-provided labels to compact snake_case-style strings."""
    text = clean_text(value).lower()
    if not text:
        return default
    text = re.sub(r"[\s\-]+", "_", text)
    text = re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", "", text)
    return text or default


def normalize_relation_direction(
    source: str,
    target: str,
    relation: str,
) -> tuple[str, str, str]:
    """Normalize hierarchy relations so tree edges point parent -> child."""
    reverse_hierarchy = {
        "part_of",
        "belongs_to",
        "included_in",
        "subtopic_of",
        "example_of",
        "属于",
        "隶属于",
    }
    if relation in reverse_hierarchy:
        return target, source, "contains" if relation != "example_of" else "has_example"
    return source, target, relation


def coerce_importance(value: object) -> float | None:
    """Validate optional importance into the graph model range."""
    if value is None:
        return 0.75
    try:
        importance = float(value)
    except (TypeError, ValueError):
        return 0.75
    return min(1.0, max(0.0, importance))


markdown_knowledge_tree_agent = MarkdownKnowledgeTreeAgent()
