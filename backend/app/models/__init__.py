"""Shared Pydantic models used by API, core managers, and storage."""

from .common import SourceRef, utc_now_iso
from .context import ClassroomContext, ContextUpdate, TimelineItem
from .events import (
    EventAcceptedResponse,
    EventType,
    ImageCapture,
    KnowledgeExtraction,
    RealtimeEvent,
    TranscriptSegment,
)
from .knowledge import (
    GraphPatch,
    GraphPatchOperation,
    KnowledgeEdge,
    KnowledgeEntity,
    KnowledgeNode,
    KnowledgeRelation,
    KnowledgeTree,
)
from .session import (
    KnowledgeGraphStatus,
    LectureSession,
    PostClassStatus,
    SessionDeleteResponse,
    SessionHistoryDetail,
    SessionHistoryListResponse,
    SessionPostClassArtifacts,
    SessionHistorySummary,
    SessionStatus,
    StartSessionRequest,
    UpdateSessionRequest,
    new_session_id,
)
from .websocket import WebSocketMessage

__all__ = [
    "EventAcceptedResponse",
    "EventType",
    "ClassroomContext",
    "ContextUpdate",
    "GraphPatch",
    "GraphPatchOperation",
    "ImageCapture",
    "KnowledgeEdge",
    "KnowledgeEntity",
    "KnowledgeExtraction",
    "KnowledgeGraphStatus",
    "KnowledgeNode",
    "KnowledgeRelation",
    "KnowledgeTree",
    "LectureSession",
    "PostClassStatus",
    "RealtimeEvent",
    "SessionHistoryDetail",
    "SessionDeleteResponse",
    "SessionHistoryListResponse",
    "SessionPostClassArtifacts",
    "SessionHistorySummary",
    "SessionStatus",
    "SourceRef",
    "StartSessionRequest",
    "TimelineItem",
    "TranscriptSegment",
    "UpdateSessionRequest",
    "WebSocketMessage",
    "new_session_id",
    "utc_now_iso",
]
