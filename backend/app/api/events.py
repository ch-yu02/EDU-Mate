"""实时事件接收 API —— 课堂数据流的唯一入口。

本模块是 EDU-Mate 后端 **最重要的路由模块**。所有课堂实时数据
（ASR 语音识别文本、摄像头/截图画面、端侧 SLM 知识提取结果）都
通过 ``POST /api/events`` 进入后端，经过以下管道处理：

::

  前端/端侧 SDK
      │  POST /api/events  (RealtimeEvent)
      ▼
  events.py  receive_event()
      │
      ├── 1. SessionManager.require_recording()   ← 门控检查
      │     会话不存在 → 404；会话已结束 → 409
      │
      ├── 2. ContextManager.handle_event()          ← 解析并更新课堂上下文
      │     ├─ transcript.segment  → 追加到 transcript / timeline
      │     ├─ image.capture      → 追加到 visuals / timeline
      │     └─ knowledge.extraction → 追加到 knowledge_extractions / timeline
      │     └─ 未知 event_type   → 400
      │
      ├── 3. KnowledgeGraphManager.handle_event()   ← 更新知识图谱
      │     └─ 仅处理 knowledge.extraction 事件
      │     └─ 非知识类事件返回 None（跳过图谱更新）
      │
      ├── 4. WebSocketManager.broadcast()            ← 推送 WebSocket
      │     └─ 广播 event.received 消息（含上下文更新摘要 + 图谱增量补丁）
      │
      └── 5. 返回 EventAcceptedResponse（HTTP 202）
"""

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ValidationError

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
from backend.app.extraction import ExtractionResult, knowledge_extraction_service
from backend.app.models import (
    EventAcceptedResponse,
    RealtimeEvent,
    TranscriptSegment,
    WebSocketMessage,
)


router = APIRouter(prefix="/events", tags=["events"])
"""事件路由实例，所有端点挂载在 ``/events`` 路径前缀下。"""

_latest_transcript_previews: dict[str, TranscriptSegment] = {}
"""Latest non-persistent ASR preview seen by the backend for each session."""


@router.post("", response_model=EventAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
async def receive_event(event: RealtimeEvent) -> EventAcceptedResponse:
    """接收并处理一条课堂实时事件，返回 HTTP 202 确认。

    每秒可能收到数十条事件（ASR 片段、截图、知识提取），因此这个端点
    必须轻量快速。所有处理都在内存中进行，业务逻辑委托给下游 Manager。

    请求体示例
    ----------
    ::

        {
            "session_id": "sess_abc123",
            "event_type": "transcript.segment",
            "payload": {
                "text": "牛顿第二定律指出，物体的加速度与合外力成正比",
                "start_ts": 120.5,
                "end_ts": 124.3,
                "speaker": "teacher",
                "segment_id": "seg_001"
            }
        }

    错误处理
    --------
    =====================  ======  ==========================================
    场景                    状态码  说明
    =====================  ======  ==========================================
    会话 ID 不存在          404    调用 ``session_manager.require_recording``
    会话已结束              409    已结束的课堂无法接收新事件
    上下文不存在            404    调用 ``context_manager.handle_event``
    事件格式错误            400    ContextManager 或 KnowledgeGraphManager 抛出异常
    知识图谱不存在          404    调用 ``knowledge_graph_manager.handle_event``
    =====================  ======  ==========================================
    """
    # ── 步骤 1：门控检查 ──────────────────────────────────────────
    # 只有 "recording" 状态的会话才能接收实时事件。已结束的课堂不可再写。
    try:
        session_manager.require_recording(event.session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except SessionConflictError:
        raise HTTPException(status_code=409, detail="Session is not recording")

    # ── 步骤 2：更新课堂上下文 ────────────────────────────────────
    # ContextManager 根据 event_type 将 payload 解析为对应的结构化模型：
    #   - "transcript.segment"   → TranscriptSegment → 追加到 transcript + timeline
    #   - "image.capture"        → ImageCapture      → 追加到 visuals + timeline
    #   - "knowledge.extraction" → KnowledgeExtraction → 追加到 knowledge_extractions + timeline
    # 返回 ContextUpdate 摘要（含新增的 timeline_item、各数据源计数）。
    try:
        context_update = context_manager.handle_event(event)
    except ContextNotFoundError:
        raise HTTPException(status_code=404, detail="Context not found")
    except ContextEventError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    _forget_preview_covered_by_event(event)

    # ContextManager 处理成功后，三类计数相加就是该课堂已接受的事件数。
    # 这样无需额外的 api/state.py 临时缓冲，也避免原始事件无限增长。
    event_count = (
        context_update.transcript_count
        + context_update.visual_count
        + context_update.knowledge_extraction_count
    )

    # ── 步骤 3：更新知识图谱 ──────────────────────────────────────
    # KnowledgeGraphManager 只处理 "knowledge.extraction" 事件。
    # 字幕和图片事件会返回 None，表示没有图谱变更需要推送。
    # 返回值 GraphPatch 包含本次增量操作（add_node / update_node / add_edge）
    # 和版本号，前端可以直接应用到本地图谱缓存。
    try:
        graph_patch = knowledge_graph_manager.handle_event(event)
    except KnowledgeGraphNotFoundError:
        raise HTTPException(status_code=404, detail="Knowledge graph not found")
    except KnowledgeGraphEventError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Realtime extraction runs after the triggering ASR/OCR event has already
    # entered the classroom context. That means the extractor sees the newest
    # transcript/visual data, but the original event can still be acknowledged
    # and broadcast as its own update.
    realtime_extraction = _run_realtime_knowledge_extraction(event)

    # ── 步骤 4：WebSocket 广播 ─────────────────────────────────────
    # 将处理结果推送给该 session 的所有已连接前端。消息包含：
    #   - event_type / payload：原始事件信息（供前端判断事件种类）
    #   - event_count：该 session 累计事件数（可用于检测丢包）
    #   - context_update：ContextManager 处理后的上下文变更摘要
    #   - graph_patch：KnowledgeGraphManager 生成的图谱增量补丁
    await _broadcast_event_received(
        event=event,
        event_count=event_count,
        context_update=context_update.model_dump(),
        graph_patch=graph_patch.model_dump() if graph_patch else None,
        knowledge_extraction=(
            # Keep this as a lightweight status object on the triggering event.
            # The full graph mutation is broadcast below as a normal internal
            # knowledge.extraction event so the frontend can reuse its existing
            # graph_patch handling.
            _extraction_result_payload(event.session_id, realtime_extraction)
            if realtime_extraction is not None
            else None
        ),
    )
    if realtime_extraction is not None:
        await _broadcast_internal_extractions(realtime_extraction)

    # ── 步骤 5：返回确认 ──────────────────────────────────────────
    # 使用 HTTP 202 Accepted 表示事件已接收并开始处理，而非同步完成。
    # 前端收到此响应即可释放事件发送缓冲区，不需要等待后续广播到达。
    return EventAcceptedResponse(
        status="accepted",
        session_id=event.session_id,
        event_type=event.event_type,
        event_count=event_count,
    )


class TranscriptPreviewRequest(BaseModel):
    """Non-persistent ASR preview used for low-latency partial subtitles."""

    session_id: str
    payload: dict[str, Any]


class TranscriptPreviewFinalizeRequest(BaseModel):
    """Promote the latest frontend-visible ASR preview into a final segment."""

    session_id: str
    payload: dict[str, Any]


@router.post(
    "/transcript-preview",
    response_model=EventAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def receive_transcript_preview(
    request: TranscriptPreviewRequest,
) -> EventAcceptedResponse:
    """Broadcast a partial ASR subtitle without saving it to classroom history.

    WhisperLive can produce tentative text before a segment is completed. This
    endpoint lets the frontend display that text as one replaceable preview row,
    while ``POST /events`` remains the only path that mutates transcript,
    timeline, notes, graph, and saved history.
    """
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

    payload = {
        **request.payload,
        "session_id": request.session_id,
        "is_final": False,
    }
    try:
        segment = TranscriptSegment.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _latest_transcript_previews[request.session_id] = segment

    event_count = (
        len(context.transcript)
        + len(context.visuals)
        + len(context.knowledge_extractions)
    )
    await websocket_manager.broadcast(
        request.session_id,
        WebSocketMessage(
            type="transcript.preview",
            session_id=request.session_id,
            data={
                "payload": segment.model_dump(),
                "event_count": event_count,
            },
        ),
    )
    return EventAcceptedResponse(
        status="accepted",
        session_id=request.session_id,
        event_type="transcript.preview",
        event_count=event_count,
    )


@router.post(
    "/transcript-preview/finalize",
    response_model=EventAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def finalize_transcript_preview(
    request: TranscriptPreviewFinalizeRequest,
) -> EventAcceptedResponse:
    """Persist the last ASR preview before ending a classroom.

    Preview subtitles are intentionally non-persistent while a lesson is live.
    When the user ends a class, the frontend may still be displaying one useful
    partial subtitle that has not arrived as WhisperLive final output yet. This
    endpoint promotes that preview to a normal ``transcript.segment`` so it is
    included in ``transcript.md`` and downstream notes/graph context.
    """
    response = await _promote_transcript_preview_payload(
        session_id=request.session_id,
        payload=request.payload,
    )
    _latest_transcript_previews.pop(request.session_id, None)
    return response


async def promote_latest_transcript_preview(
    session_id: str,
) -> EventAcceptedResponse | None:
    """Promote the newest backend-seen preview before ending the classroom."""
    preview = _latest_transcript_previews.get(session_id)
    if preview is None or not preview.text.strip():
        return None
    response = await _promote_transcript_preview_payload(
        session_id=session_id,
        payload=preview.model_dump(),
    )
    _latest_transcript_previews.pop(session_id, None)
    return response


async def _promote_transcript_preview_payload(
    *,
    session_id: str,
    payload: dict[str, Any],
) -> EventAcceptedResponse:
    """Convert one preview payload into a normal final transcript event."""
    try:
        session_manager.require_recording(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except SessionConflictError:
        raise HTTPException(status_code=409, detail="Session is not recording")

    payload = {
        **payload,
        "session_id": session_id,
        "is_final": True,
        "source": payload.get("source") or "whisperlive_preview_finalized",
        "skip_realtime_extraction": True,
    }
    if not str(payload.get("segment_id") or "").strip():
        start_ms = int(round(float(payload.get("start_ts", 0.0)) * 1000))
        end_ms = int(round(float(payload.get("end_ts", 0.0)) * 1000))
        payload["segment_id"] = f"seg_preview_final_{start_ms}_{end_ms}"

    event = RealtimeEvent(
        session_id=session_id,
        event_type="transcript.segment",
        payload=payload,
    )
    return await receive_event(event)


def _forget_preview_covered_by_event(event: RealtimeEvent) -> None:
    """Drop stale previews once an overlapping final transcript arrives."""
    if event.event_type != "transcript.segment":
        return
    preview = _latest_transcript_previews.get(event.session_id)
    if preview is None:
        return
    try:
        final_segment = TranscriptSegment.model_validate(
            {**event.payload, "session_id": event.session_id}
        )
    except ValidationError:
        return
    if _preview_is_covered_by_final(preview, final_segment):
        _latest_transcript_previews.pop(event.session_id, None)


def _preview_is_covered_by_final(
    preview: TranscriptSegment,
    final_segment: TranscriptSegment,
) -> bool:
    """Return true when a final segment makes the latest preview obsolete."""
    if preview.segment_id == final_segment.segment_id:
        return True
    if preview.text.strip() and preview.text.strip() == final_segment.text.strip():
        return True
    preview_midpoint = (preview.start_ts + preview.end_ts) / 2.0
    if (
        final_segment.start_ts - 0.25 <= preview_midpoint
        and preview_midpoint <= final_segment.end_ts + 0.25
    ):
        return True
    return False


def _run_realtime_knowledge_extraction(event: RealtimeEvent) -> ExtractionResult | None:
    """Run lightweight batched extraction after ASR/OCR events when due.

    This helper intentionally runs only after the original event has been
    accepted by ``ContextManager`` and ``KnowledgeGraphManager``. The triggering
    event remains visible as its own WebSocket update, while any generated
    internal extraction is broadcast as a second ``event.received`` message.
    """
    if event.payload.get("skip_realtime_extraction") is True:
        return None

    try:
        context = context_manager.get_context(event.session_id)
    except ContextNotFoundError:
        return None

    if not knowledge_extraction_service.should_extract_realtime(context, event):
        return None

    # The service applies successful extractions immediately and returns the
    # ContextUpdate/GraphPatch objects needed for WebSocket broadcasting.
    result = knowledge_extraction_service.extract_and_apply(
        context=context,
        context_manager=context_manager,
        knowledge_graph_manager=knowledge_graph_manager,
    )
    return result


def _extraction_result_payload(
    session_id: str,
    result: ExtractionResult,
) -> dict[str, object]:
    """Create a compact status payload for realtime extraction attempts.

    This payload is attached to the ASR/OCR event that triggered extraction.
    It is intentionally summary-shaped: frontend state should still be updated
    from the separate internal knowledge event, not by parsing this field.
    """
    return {
        "session_id": session_id,
        "provider": knowledge_extraction_service.extractor.provider_name,
        "extraction_count": len(result.extractions),
        "processed_source_ids": result.processed_source_ids,
        "errors": [error.model_dump() for error in result.errors],
        "applied": [
            {
                "extraction_id": item.extraction.extraction_id,
                "graph_patch_operations": (
                    len(item.graph_patch.operations) if item.graph_patch else 0
                ),
            }
            for item in result.applied
        ],
    }


async def _broadcast_event_received(
    *,
    event: RealtimeEvent,
    event_count: int,
    context_update: dict[str, object],
    graph_patch: dict[str, object] | None,
    knowledge_extraction: dict[str, object] | None = None,
) -> None:
    """Broadcast the standard event.received envelope.

    Both external input events and internally generated knowledge events use
    this helper. Keeping one envelope builder protects the frontend contract
    from small drift between "real" and internal events.
    """
    data = {
        "event_type": event.event_type,
        "payload": event.payload,
        "event_count": event_count,
        "context_update": context_update,
        "graph_patch": graph_patch,
    }
    if knowledge_extraction is not None:
        data["knowledge_extraction"] = knowledge_extraction

    await websocket_manager.broadcast(
        event.session_id,
        WebSocketMessage(
            type="event.received",
            session_id=event.session_id,
            data=data,
        ),
    )


async def _broadcast_internal_extractions(result: ExtractionResult) -> None:
    """Broadcast each applied internal extraction as a normal knowledge event.

    The extractor has already mutated context and graph through the service.
    Here we only replay the resulting update objects over WebSocket so connected
    dashboards can show the new timeline item and graph patch immediately.
    """
    for item in result.applied:
        # Reconstruct a RealtimeEvent for the payload shape only. We do not send
        # it through managers again, because that would duplicate the extraction
        # in context and could create redundant graph updates.
        event = RealtimeEvent(
            session_id=item.extraction.session_id,
            event_type="knowledge.extraction",
            payload=item.extraction.model_dump(),
        )
        update = item.context_update
        # Use post-extraction counts for the internal event. This mirrors a
        # direct POST /events knowledge.extraction response.
        event_count = (
            update.transcript_count
            + update.visual_count
            + update.knowledge_extraction_count
        )
        await _broadcast_event_received(
            event=event,
            event_count=event_count,
            context_update=update.model_dump(),
            graph_patch=item.graph_patch.model_dump() if item.graph_patch else None,
        )
