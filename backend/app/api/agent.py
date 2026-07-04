"""课堂 Agent 的 HTTP 路由。

本模块保持 API 层“薄路由”原则：
- 请求/响应模型来自 ``backend.app.agent.schemas``。
- 业务逻辑全部委托给 ``ClassroomAgent``。
- 这里只做领域异常到 HTTP 状态码的映射。

这样后续给 Agent 接 LlamaIndex、Cloud LLM 或更多 Skill 时，不需要把复杂逻辑
搬进路由文件。
"""

import asyncio
import mimetypes
import time

from fastapi import APIRouter, HTTPException

from backend.app.agent import (
    AgentChatRequest,
    AgentChatResponse,
    AgentSessionNotFoundError,
    CourseKnowledgeTreeResponse,
    CourseListResponse,
    GlobalSearchRequest,
    GlobalSearchResponse,
    NotesKnowledgeTreeUpdateRequest,
    NotesKnowledgeTreeUpdateResponse,
    VisualAnalysisRequest,
    VisualAnalysisResponse,
    classroom_agent,
    global_search_service,
    markdown_knowledge_tree_agent,
    visual_analysis_agent,
)
from backend.app.core import (
    ContextEventError,
    ContextNotFoundError,
    KnowledgeGraphEventError,
    KnowledgeGraphNotFoundError,
    SessionConflictError,
    SessionNotFoundError,
    context_manager,
    knowledge_graph_manager,
    session_manager,
    websocket_manager,
)
from backend.app.models import (
    ContextUpdate,
    GraphPatch,
    LectureSession,
    RealtimeEvent,
    WebSocketMessage,
)
from backend.app.storage import local_storage


router = APIRouter(prefix="/agent", tags=["agent"])
"""Agent API 路由，当前只有自然语言入口 ``POST /agent/chat``。"""


def _notes_agent_log(message: str) -> None:
    """Emit a compact backend log line for notes-driven graph updates."""
    print(f"[notes-agent] {message}", flush=True)


@router.post("/chat", response_model=AgentChatResponse)
async def chat(request: AgentChatRequest) -> AgentChatResponse:
    """针对一次 prompt 运行课堂 Agent。

    404 的语义是“内存和本地历史文件都找不到这个 session”。如果 session 存在
    但课堂资料不足，Agent 会正常返回 200，并在 ``warnings`` 中说明数据不足。
    """
    try:
        return classroom_agent.chat(request)
    except AgentSessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")


@router.post("/search", response_model=GlobalSearchResponse)
async def search(request: GlobalSearchRequest) -> GlobalSearchResponse:
    """跨已保存历史课堂搜索课堂资料。

    这个接口是 Phase 7 的第一版“长期记忆”入口。它只读取本地已保存历史课堂，
    不搜索正在录制但尚未结束保存的内存课堂。这样用户得到的结果都能对应到
    ``data/sessions/{session_id}`` 中稳定存在的课后档案。
    """
    return global_search_service.search(request)


@router.post("/review", response_model=GlobalSearchResponse)
async def review(request: GlobalSearchRequest) -> GlobalSearchResponse:
    """跨历史课堂做来源约束的课后复习问答。"""
    return global_search_service.review(request)


@router.get("/courses", response_model=CourseListResponse)
async def list_courses() -> CourseListResponse:
    """按课程聚合已保存历史课堂，供课后复习入口使用。"""
    return global_search_service.list_courses()


@router.get("/courses/{course}/knowledge-tree", response_model=CourseKnowledgeTreeResponse)
async def merged_course_tree(course: str) -> CourseKnowledgeTreeResponse:
    """合并同一课程多节历史课堂的知识树快照。"""
    return global_search_service.merged_course_tree(course)


@router.post("/visual/analyze", response_model=VisualAnalysisResponse)
async def analyze_visual(request: VisualAnalysisRequest) -> VisualAnalysisResponse:
    """Analyze one saved classroom image with the configured multimodal LLM."""
    try:
        session_manager.require_recording(request.session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except SessionConflictError:
        raise HTTPException(status_code=409, detail="Session is not recording")

    try:
        context = context_manager.get_context(request.session_id)
    except ContextNotFoundError:
        raise HTTPException(status_code=404, detail="Context not found")

    try:
        knowledge_graph = knowledge_graph_manager.get_graph(request.session_id)
    except KnowledgeGraphNotFoundError:
        raise HTTPException(status_code=404, detail="Knowledge graph not found")

    visual = next((item for item in context.visuals if item.image_id == request.image_id), None)
    if visual is None:
        raise HTTPException(status_code=404, detail="Image event not found")

    try:
        image_path = local_storage.session_image_path(
            request.session_id,
            request.image_id,
            visual.image_path,
        )
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="Image file not found")

    image_bytes = await asyncio.to_thread(image_path.read_bytes)
    media_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    graph_snapshot = knowledge_graph.model_copy(deep=True)
    result = await asyncio.to_thread(
        visual_analysis_agent.analyze,
        request,
        context=context.model_copy(deep=True),
        knowledge_graph=graph_snapshot,
        image_bytes=image_bytes,
        media_type=media_type,
    )

    visual_update: ContextUpdate | None = None
    if result.visual is not None and not result.skipped:
        visual_event = RealtimeEvent(
            session_id=request.session_id,
            event_type="image.capture",
            payload=result.visual.model_dump(),
        )
        try:
            visual_update = context_manager.handle_event(visual_event)
        except ContextEventError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await _broadcast_agent_event(
            event=visual_event,
            context_update=visual_update,
            graph_patch=None,
        )

    graph_patch: GraphPatch | None = None
    if result.extraction is not None:
        knowledge_event = RealtimeEvent(
            session_id=request.session_id,
            event_type="knowledge.extraction",
            payload=result.extraction.model_dump(),
        )
        try:
            knowledge_update = context_manager.handle_event(knowledge_event)
            graph_patch = knowledge_graph_manager.handle_event(knowledge_event)
        except ContextEventError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except KnowledgeGraphEventError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await _broadcast_agent_event(
            event=knowledge_event,
            context_update=knowledge_update,
            graph_patch=graph_patch,
        )

    operation_count = len(graph_patch.operations) if graph_patch else 0
    status = "failed" if result.failed else "skipped" if result.skipped else "applied"
    return VisualAnalysisResponse(
        status=status,
        session_id=request.session_id,
        image_id=request.image_id,
        caption=result.visual.caption if result.visual else None,
        visual_text=result.visual.visual_text if result.visual else [],
        key_points=result.visual.key_points if result.visual else [],
        extraction_id=result.extraction.extraction_id if result.extraction else None,
        graph_patch_operations=operation_count,
        warnings=list(result.warnings),
    )


@router.post(
    "/knowledge-tree/update-from-notes",
    response_model=NotesKnowledgeTreeUpdateResponse,
)
async def update_knowledge_tree_from_notes(
    request: NotesKnowledgeTreeUpdateRequest,
) -> NotesKnowledgeTreeUpdateResponse:
    """Use a structured Markdown note snapshot to update the live knowledge tree.

    This endpoint is meant for the local WhisperLive/Qwen note pipeline. The
    script uploads the current Markdown notes every N seconds; the backend keeps
    cloud credentials private, asks the configured cloud LLM for a grounded
    knowledge-tree extraction, and then reuses the existing ``knowledge.extraction``
    event path so the frontend receives the usual graph patch.
    """
    try:
        session = session_manager.get_session(request.session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status != "recording" and request.update_status != "final":
        raise HTTPException(status_code=409, detail="Session is not recording")

    try:
        context_manager.get_context(request.session_id)
    except ContextNotFoundError:
        raise HTTPException(status_code=404, detail="Context not found")

    try:
        knowledge_graph = knowledge_graph_manager.get_graph(request.session_id)
    except KnowledgeGraphNotFoundError:
        raise HTTPException(status_code=404, detail="Knowledge graph not found")

    started_at = time.monotonic()
    recent_count = len(request.recent_source_segments or request.source_segments)
    _notes_agent_log(
        "start "
        f"session={request.session_id} snapshot={request.snapshot_id} "
        f"seq={request.sequence} status={request.update_status} "
        f"markdown_chars={len(request.markdown)} "
        f"source_segments={len(request.source_segments)} "
        f"recent_segments={recent_count}"
    )

    # Cloud LLM calls use the blocking urllib client. Run extraction in a worker
    # thread so a slow DeepSeek request does not block /events transcript posts
    # and WebSocket subtitle updates on the FastAPI event loop.
    graph_snapshot = knowledge_graph.model_copy(deep=True)
    result = await asyncio.to_thread(
        markdown_knowledge_tree_agent.extract,
        request,
        graph_snapshot,
    )
    if result.failed:
        elapsed = time.monotonic() - started_at
        _notes_agent_log(
            "failed "
            f"session={request.session_id} snapshot={request.snapshot_id} "
            f"elapsed={elapsed:.2f}s warnings={list(result.warnings)}"
        )
        return NotesKnowledgeTreeUpdateResponse(
            status="failed",
            session_id=request.session_id,
            snapshot_id=request.snapshot_id,
            markdown_hash=result.markdown_hash,
            warnings=list(result.warnings),
        )

    if result.extraction is None:
        if result.markdown_hash:
            markdown_knowledge_tree_agent.remember_processed(
                request.session_id,
                result.markdown_hash,
            )
        metadata_updated = await _update_session_metadata_from_notes(
            request=request,
            session_title=result.session_title,
            course=result.course,
        )
        persisted_notes = _persist_notes_snapshot_if_saved(request)
        elapsed = time.monotonic() - started_at
        _notes_agent_log(
            "skipped "
            f"session={request.session_id} snapshot={request.snapshot_id} "
            f"elapsed={elapsed:.2f}s metadata_updated={metadata_updated} "
            f"persisted={persisted_notes} "
            f"warnings={list(result.warnings)}"
        )
        return NotesKnowledgeTreeUpdateResponse(
            status="skipped",
            session_id=request.session_id,
            snapshot_id=request.snapshot_id,
            markdown_hash=result.markdown_hash,
            session_title=result.session_title,
            course=result.course,
            session_metadata_updated=metadata_updated,
            warnings=list(result.warnings),
        )

    event = RealtimeEvent(
        session_id=request.session_id,
        event_type="knowledge.extraction",
        payload=result.extraction.model_dump(),
    )
    try:
        context_update = context_manager.handle_event(event)
        graph_patch = knowledge_graph_manager.handle_event(event)
    except ContextEventError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KnowledgeGraphEventError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (ContextNotFoundError, KnowledgeGraphNotFoundError):
        raise HTTPException(status_code=404, detail="Session graph context not found")

    markdown_knowledge_tree_agent.remember_processed(
        request.session_id,
        result.markdown_hash,
    )
    await _broadcast_notes_knowledge_update(
        event=event,
        context_update=context_update,
        graph_patch=graph_patch,
    )
    operation_count = len(graph_patch.operations) if graph_patch else 0
    metadata_updated = await _update_session_metadata_from_notes(
        request=request,
        session_title=result.session_title,
        course=result.course,
    )
    persisted_notes = _persist_notes_snapshot_if_saved(request)
    elapsed = time.monotonic() - started_at
    _notes_agent_log(
        f"{'applied' if operation_count else 'skipped'} "
        f"session={request.session_id} snapshot={request.snapshot_id} "
        f"elapsed={elapsed:.2f}s ops={operation_count} "
        f"metadata_updated={metadata_updated} "
        f"persisted={persisted_notes} "
        f"extraction={result.extraction.extraction_id} warnings={list(result.warnings)}"
    )
    return NotesKnowledgeTreeUpdateResponse(
        status="applied" if operation_count else "skipped",
        session_id=request.session_id,
        snapshot_id=request.snapshot_id,
        markdown_hash=result.markdown_hash,
        extraction_id=result.extraction.extraction_id,
        graph_patch_operations=operation_count,
        session_title=result.session_title,
        course=result.course,
        session_metadata_updated=metadata_updated,
        warnings=list(result.warnings),
    )


def _persist_notes_snapshot_if_saved(request: NotesKnowledgeTreeUpdateRequest) -> bool:
    """Persist late notes-driven updates for a session already saved to disk."""
    if not local_storage.session_exists(request.session_id):
        return False

    try:
        session = session_manager.get_session(request.session_id)
    except SessionNotFoundError:
        try:
            session = LectureSession.model_validate(
                local_storage.read_metadata(request.session_id)
            )
        except (FileNotFoundError, ValueError):
            return False

    try:
        context = context_manager.get_context(request.session_id)
        knowledge_graph = knowledge_graph_manager.get_graph(request.session_id)
    except (
        ContextNotFoundError,
        KnowledgeGraphNotFoundError,
        FileNotFoundError,
        ValueError,
    ):
        return False

    local_storage.save_session(
        session=session,
        context=context,
        knowledge_graph=knowledge_graph,
        structured_notes_markdown=request.markdown,
        create=False,
    )
    return True


async def _update_session_metadata_from_notes(
    *,
    request: NotesKnowledgeTreeUpdateRequest,
    session_title: str | None,
    course: str | None,
) -> bool:
    """Update classroom metadata from the final cloud notes response.

    Final note snapshots may arrive before or after ``/sessions/{id}/end``.
    Keep both the live in-memory session and the saved ``metadata.json`` in
    sync when either representation exists.
    """
    if request.update_status != "final":
        return False

    updates: dict[str, object] = {}
    if session_title:
        updates["title"] = session_title[:80]
    if course:
        updates["course"] = course[:80]
    if not updates:
        return False

    updated_session = None
    try:
        updated_session = session_manager.update_session_metadata(
            request.session_id,
            updates,
        )
    except SessionNotFoundError:
        updated_session = None

    if local_storage.session_exists(request.session_id):
        try:
            persisted_session = local_storage.update_session_metadata(
                request.session_id,
                updates,
            )
            if updated_session is None:
                updated_session = persisted_session
        except (FileNotFoundError, ValueError):
            pass

    if updated_session is None:
        return False

    await websocket_manager.broadcast(
        request.session_id,
        WebSocketMessage(
            type="session.updated",
            session_id=request.session_id,
            data={"session": updated_session.model_dump()},
        ),
    )
    return True


async def _broadcast_notes_knowledge_update(
    *,
    event: RealtimeEvent,
    context_update: ContextUpdate,
    graph_patch: GraphPatch | None,
) -> None:
    """Broadcast the standard realtime envelope for notes-driven graph updates."""
    event_count = (
        context_update.transcript_count
        + context_update.visual_count
        + context_update.knowledge_extraction_count
    )
    await websocket_manager.broadcast(
        event.session_id,
        WebSocketMessage(
            type="event.received",
            session_id=event.session_id,
            data={
                "event_type": event.event_type,
                "payload": event.payload,
                "event_count": event_count,
                "context_update": context_update.model_dump(),
                "graph_patch": graph_patch.model_dump() if graph_patch else None,
            },
        ),
    )


async def _broadcast_agent_event(
    *,
    event: RealtimeEvent,
    context_update: ContextUpdate,
    graph_patch: GraphPatch | None,
) -> None:
    """Broadcast a standard event.received envelope from Agent-side updates."""
    event_count = (
        context_update.transcript_count
        + context_update.visual_count
        + context_update.knowledge_extraction_count
    )
    await websocket_manager.broadcast(
        event.session_id,
        WebSocketMessage(
            type="event.received",
            session_id=event.session_id,
            data={
                "event_type": event.event_type,
                "payload": event.payload,
                "event_count": event_count,
                "context_update": context_update.model_dump(),
                "graph_patch": graph_patch.model_dump() if graph_patch else None,
            },
        ),
    )
