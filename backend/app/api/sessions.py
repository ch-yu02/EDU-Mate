"""课堂会话 REST API —— 会话生命周期的 HTTP 入口。

本模块提供课堂会话的完整生命周期管理：创建、查询、结束。
每个写操作（创建/结束）都会通过 ``WebSocketManager`` 向 WebSocket
订阅者广播状态变更，保证前端实时感知会话状态。

端点一览
--------
=======================  ======  ==========================================
端点                      方法    用途
=======================  ======  ==========================================
``/sessions/start``       POST    创建新课堂会话，返回 LectureSession
``/sessions/{id}``        GET     按 ID 查询会话元数据
``/sessions/{id}/end``    POST    结束会话，状态 recording → ended
=======================  ======  ==========================================

分层约定
--------
本模块遵循 api 层的统一分层约定：

- **core 层**（SessionManager）抛出领域异常（SessionNotFoundError）
- **api 层**（本模块）将领域异常映射为 HTTP 状态码（404）

未来扩展
--------
- GET /sessions: 列表查询（分页），用于历史课堂浏览
- 持久化回退：GET 找不到内存中的会话时，尝试从 LocalStorage 读取
- end_session 触发持久化：metadata.json, transcript.md, timeline.json,
  knowledge_graph.json 写入磁盘
"""

import asyncio
import json
import os
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse

from backend.app.agent.knowledge_tree_notes import (
    MarkdownKnowledgeTreeAgent,
    markdown_knowledge_tree_agent,
    markdown_without_whisperlive_subtitles,
)
from backend.app.agent.schemas import NotesKnowledgeTreeUpdateRequest, NotesSourceSegment
from backend.app.api.events import promote_latest_transcript_preview
from backend.app.core import (
    ContextNotFoundError,
    KnowledgeGraphNotFoundError,
    SessionNotFoundError,
    context_manager,
    knowledge_graph_manager,
    session_manager,
    websocket_manager,
)
from backend.app.extraction import knowledge_extraction_service
from backend.app.llm import CloudLLMClient, load_llm_settings
from backend.app.rag import LlamaIndexQueryService, build_session_documents
from backend.app.models import (
    LectureSession,
    RealtimeEvent,
    SessionDeleteResponse,
    SessionHistoryDetail,
    SessionHistoryListResponse,
    StartSessionRequest,
    UpdateSessionRequest,
    WebSocketMessage,
    ImageCapture,
    utc_now_iso,
)
from backend.app.skills import SummarizerSkill, TodoDetectiveSkill
from backend.app.storage import local_storage


router = APIRouter(prefix="/sessions", tags=["sessions"])
"""会话路由实例，所有端点挂载在 ``/sessions`` 路径前缀下。"""

_post_class_tasks: dict[str, asyncio.Task[None]] = {}
"""In-process post-class generation tasks keyed by session_id."""

_cancelled_post_class_sessions: set[str] = set()
"""Sessions whose post-class generation should not write any more files."""


# ── 创建会话 ──────────────────────────────────────────────────────


@router.post("/start", response_model=LectureSession, status_code=status.HTTP_201_CREATED)
async def start_session(request: StartSessionRequest) -> LectureSession:
    """创建新课堂会话并通知所有 WebSocket 订阅者。

    请求体示例
    ----------
    ::

        {
            "title": "牛顿力学导论",
            "course": "PHYS101",
            "teacher": "张老师",
            "language": "zh-CN",
            "created_by": "teacher-001",
            "device_id": "dev-abc123"
        }

    处理流程
    --------
    1. 调用 ``session_manager.create_session()`` 在内存中创建会话，
       自动生成唯一 session_id，状态固定为 ``"recording"``
    2. 通过 ``websocket_manager.broadcast()`` 向该 session 的所有
       WebSocket 订阅者推送 ``session.started`` 消息
    3. 返回完整的 ``LectureSession`` 对象（HTTP 201）

    未来扩展
    --------
    - 在 LocalStorage 中创建会话数据目录
    """
    session = session_manager.create_session(request)
    context_manager.start_session(session.session_id)
    knowledge_graph_manager.start_session(session.session_id)

    # 创建成功后立即广播，此时通常还没有 WebSocket 订阅者，
    # 但 broadcast 对空列表是安全的（直接跳过遍历）
    await websocket_manager.broadcast(
        session.session_id,
        WebSocketMessage(
            type="session.started",
            session_id=session.session_id,
            data={"session": session.model_dump()},
        ),
    )
    return session


@router.get("/recording", response_model=list[LectureSession])
async def list_recording_sessions() -> list[LectureSession]:
    """Return in-memory sessions that are still accepting realtime events.

    This is mainly a local integration helper: file-stream scripts can attach to
    the classroom the frontend has already started, and the frontend can attach
    to a script-created test classroom without copying a session_id by hand.
    """
    sessions = [
        session
        for session in session_manager.list_sessions()
        if session.status == "recording"
    ]
    return sorted(sessions, key=lambda session: session.start_time, reverse=True)


@router.patch("/{session_id}", response_model=LectureSession)
async def update_session(
    session_id: str,
    request: UpdateSessionRequest,
) -> LectureSession:
    """Update mutable classroom metadata such as title and course."""
    updates = _session_metadata_updates(request)

    updated_session: LectureSession | None = None
    session_in_memory = True
    try:
        updated_session = session_manager.update_session_metadata(session_id, updates)
    except SessionNotFoundError:
        session_in_memory = False

    if local_storage.session_exists(session_id):
        try:
            persisted_session = local_storage.update_session_metadata(session_id, updates)
            if updated_session is None:
                updated_session = persisted_session
        except (FileNotFoundError, ValueError):
            if not session_in_memory:
                raise HTTPException(status_code=404, detail="Session not found")

    if updated_session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    await websocket_manager.broadcast(
        session_id,
        WebSocketMessage(
            type="session.updated",
            session_id=session_id,
            data={"session": updated_session.model_dump()},
        ),
    )
    return updated_session


# ── 查询会话 ──────────────────────────────────────────────────────


@router.get("/{session_id}", response_model=LectureSession)
async def get_session(session_id: str) -> LectureSession:
    """按 session_id 查询单个课堂会话的元数据。

    这是前端加载课堂详情页的主要入口。返回的数据包含标题、课程名、
    教师、开始时间、状态等元信息。

    错误处理
    --------
    将 core 层的 ``SessionNotFoundError`` 映射为 HTTP 404，
    前端可据此展示"课堂不存在或已过期"。

    未来扩展
    --------
    若 session 不在内存中（如后端重启后已结束的课堂），
    尝试从 LocalStorage 回退读取，使历史课堂仍可被查询。
    """
    try:
        return session_manager.get_session(session_id)
    except SessionNotFoundError:
        try:
            return LectureSession.model_validate(local_storage.read_metadata(session_id))
        except (FileNotFoundError, ValueError):
            raise HTTPException(status_code=404, detail="Session not found")


@router.get("", response_model=SessionHistoryListResponse)
async def list_history_sessions() -> SessionHistoryListResponse:
    """读取已保存的历史课堂列表。

    返回本地 ``data/sessions`` 下已落盘课堂的元信息摘要。课堂仍在录制但
    尚未结束保存时不会出现在这个历史列表中。
    """
    return SessionHistoryListResponse(sessions=local_storage.list_sessions())


@router.get("/{session_id}/history", response_model=SessionHistoryDetail)
async def get_history_session(session_id: str) -> SessionHistoryDetail:
    """读取一节已保存课堂的课后历史内容。

    包含 metadata、transcript.md、timeline.json 和 knowledge_graph.json，
    供前端历史回放、课后总结和技能模块消费。
    """
    try:
        detail = local_storage.read_session(session_id)
        return _normalize_history_post_class_status(detail)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="Saved session not found")


@router.get("/{session_id}/images/{image_id}")
async def get_session_image(session_id: str, image_id: str) -> FileResponse:
    """Serve a persisted classroom image by stable image_id.

    The endpoint intentionally resolves through LocalStorage rather than
    trusting raw image paths. That keeps future uploads and hardware-captured
    files constrained to ``data/sessions/{session_id}/images``.
    """
    image_path = _image_path_for_session(session_id, image_id)
    try:
        path = local_storage.session_image_path(session_id, image_id, image_path)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(path)


@router.put("/{session_id}/images/{image_id}", status_code=status.HTTP_201_CREATED)
async def upload_session_image(
    session_id: str,
    image_id: str,
    request: Request,
) -> dict[str, str]:
    """Upload raw classroom image bytes.

    Hardware/camera modules can call this before or after sending the matching
    ``image.capture`` event. The returned ``image_path`` is the stable local URI
    they should place in that event payload.
    """
    try:
        session_manager.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")

    content = await request.body()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large")

    try:
        path = local_storage.save_session_image(
            session_id=session_id,
            image_id=image_id,
            content=content,
            content_type=request.headers.get("content-type"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "session_id": session_id,
        "image_id": image_id,
        "image_path": f"local://sessions/{session_id}/images/{path.name}",
    }


@router.delete("/{session_id}/history", response_model=SessionDeleteResponse)
async def delete_history_session(session_id: str) -> SessionDeleteResponse:
    """删除一节已保存课堂的本地历史数据。

    只删除 ``data/sessions/{session_id}`` 下的课后档案文件，不修改
    SessionManager 中仍保留的内存 session。这样删除历史数据不会干扰当前
    运行中的课堂生命周期，但历史列表和详情将不再能读取该 session。
    """
    _cancelled_post_class_sessions.add(session_id)
    task = _post_class_tasks.pop(session_id, None)
    if task is not None:
        task.cancel()

    try:
        deleted = local_storage.delete_session(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session_id")

    if not deleted:
        _cancelled_post_class_sessions.discard(session_id)
        raise HTTPException(status_code=404, detail="Saved session not found")

    return SessionDeleteResponse(status="deleted", session_id=session_id)


# ── 结束会话 ──────────────────────────────────────────────────────


@router.post("/{session_id}/end", response_model=LectureSession)
async def end_session(session_id: str) -> LectureSession:
    """结束课堂会话并通知所有 WebSocket 订阅者。

    幂等性
    ------
    对已结束的会话再次调用此端点不会报错——``SessionManager.end_session()``
    幂等地返回已有的结束结果。这让前端可以安全重试（网络不稳定时重复发送
    结束请求不会产生副作用）。

    处理流程
    --------
    1. 从 ContextManager / KnowledgeGraphManager 读取课堂内存状态。
    2. 调用 ``session_manager.end_session()`` 将状态从 ``"recording"``
       转为 ``"ended"``，同时记录 ``end_time``。
    3. 调用 LocalStorage 保存 metadata / transcript / timeline / graph。
    4. 通过 ``websocket_manager.broadcast()`` 推送 ``session.ended``，
       前端收到后可以停止事件上报，并显示课后产物生成中。
    5. 创建后台任务生成 summary/todos、最终知识抽取和可选 RAG 索引。
    6. 立刻返回更新后的 ``LectureSession`` 对象，不等待慢任务。

    LocalStorage 会先将以下核心文件写入会话数据目录：
    - ``metadata.json`` —— 会话元信息
    - ``transcript.md`` —— 完整课堂文字记录
    - ``timeline.json`` —— 时间轴事件列表
    - ``knowledge_graph.json`` —— 知识图谱节点与边

    后台任务完成后会追加写入 ``summary.md`` / ``todos.json`` 并广播
    ``post_class.updated``。
    """
    try:
        context = context_manager.get_context(session_id)
    except ContextNotFoundError:
        raise HTTPException(status_code=404, detail="Context not found")

    try:
        knowledge_graph = knowledge_graph_manager.get_graph(session_id)
    except KnowledgeGraphNotFoundError:
        raise HTTPException(status_code=404, detail="Knowledge graph not found")

    structured_notes_markdown = markdown_knowledge_tree_agent.latest_markdown(session_id)

    try:
        current_session = session_manager.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    should_start_post_class_task = current_session.status == "recording"
    if should_start_post_class_task:
        try:
            await promote_latest_transcript_preview(session_id)
            context = context_manager.get_context(session_id)
            knowledge_graph = knowledge_graph_manager.get_graph(session_id)
        except HTTPException as exc:
            print(
                "Finalize latest transcript preview before session end failed: "
                f"{exc.detail}",
                flush=True,
            )

    context_snapshot = context.model_copy(deep=True)
    knowledge_graph_snapshot = knowledge_graph.model_copy(deep=True)

    try:
        ended_session = session_manager.end_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")

    storage_result = local_storage.save_session(
        session=ended_session,
        context=context,
        knowledge_graph=knowledge_graph,
        structured_notes_markdown=structured_notes_markdown,
    )
    post_class_status_path = None
    if should_start_post_class_task:
        _cancelled_post_class_sessions.discard(session_id)
        post_class_status_path = local_storage.save_post_class_status(
            session_id,
            "generating",
            knowledge_graph_status="finalizing",
        )

    await websocket_manager.broadcast(
        session_id,
        WebSocketMessage(
            type="session.ended",
            session_id=session_id,
            data={
                "session": ended_session.model_dump(),
                "storage": {
                    "session_dir": str(storage_result.session_dir),
                    "files": {
                        name: str(path)
                        for name, path in storage_result.files.items()
                    },
                    "post_class_files": {},
                    "post_class_status_file": (
                        str(post_class_status_path)
                        if post_class_status_path is not None
                        else None
                    ),
                    "rag_index": {
                        "enabled": os.getenv("RAG_QUERY_BACKEND", "lexical").strip().lower()
                        == "llamaindex",
                        "status": "pending",
                    },
                    "knowledge_extraction": {
                        "session_id": session_id,
                        "status": "pending",
                    },
                    "post_class_status": "generating",
                    "knowledge_graph_status": "finalizing",
                },
            },
        ),
    )
    if should_start_post_class_task:
        task = asyncio.create_task(
            _finalize_session_after_end(
                session_id=session_id,
                ended_session=ended_session,
                context_snapshot=context_snapshot,
                knowledge_graph_snapshot=knowledge_graph_snapshot,
                structured_notes_markdown=structured_notes_markdown,
            )
        )
        _track_post_class_task(session_id, task)
    return ended_session


async def _finalize_session_after_end(
    *,
    session_id: str,
    ended_session: LectureSession,
    context_snapshot,
    knowledge_graph_snapshot,
    structured_notes_markdown: str | None,
) -> None:
    """Generate post-class artifacts after the end API has returned.

    The slow post-class pipeline is intentionally split into visible stages:
    final Qwen notes are published as soon as they are ready, then final graph
    extraction and summary/todos generation run in parallel. A slow cloud graph
    request must not hide already-finished notes or post-class artifacts.
    """
    warnings: list[str] = []
    steps: dict[str, object] = {}
    _post_class_debug(session_id, "task_start")
    _save_post_class_status_if_present(
        session_id,
        "generating",
        warnings=warnings,
        knowledge_graph_status="finalizing",
        stage="final_notes",
        steps=steps,
    )

    final_notes_markdown: str | None = structured_notes_markdown
    final_notes_info: dict[str, object] = {"status": "skipped"}
    knowledge_graph_status = "finalizing"
    notes_started_at = time.monotonic()
    _post_class_set_step(steps, "final_notes", "running")
    _post_class_debug(session_id, "final_notes_start")
    try:
        final_notes_markdown = await asyncio.to_thread(
            _load_or_generate_final_structured_notes,
            session_id,
        )
        structured_notes_markdown = final_notes_markdown
    except Exception as exc:  # noqa: BLE001 - post-class artifacts should still continue.
        elapsed = time.monotonic() - notes_started_at
        warning = f"Final Qwen structured notes failed: {exc}"
        warnings.append(warning)
        _post_class_set_step(
            steps,
            "final_notes",
            "failed",
            elapsed_seconds=elapsed,
            error=str(exc),
        )
        _post_class_debug(
            session_id,
            "final_notes_failed",
            elapsed_seconds=elapsed,
            error=str(exc),
        )
        final_notes_info = {
            "status": "failed",
            "warning": warning,
            "elapsed_seconds": elapsed,
        }
    else:
        elapsed = time.monotonic() - notes_started_at
        _post_class_set_step(
            steps,
            "final_notes",
            "ready",
            elapsed_seconds=elapsed,
            markdown_chars=len(final_notes_markdown),
        )
        _post_class_debug(
            session_id,
            "final_notes_ready",
            elapsed_seconds=elapsed,
            markdown_chars=len(final_notes_markdown),
        )
        final_notes_info = {
            "status": "ready",
            "elapsed_seconds": elapsed,
        }
        _save_post_class_status_if_present(
            session_id,
            "generating",
            warnings=warnings,
            knowledge_graph_status="finalizing",
            stage="notes_ready",
            steps=steps,
        )
        await _broadcast_post_class_update(
            session_id,
            {
                "status": "generating",
                "post_class_stage": "notes_ready",
                "knowledge_graph_status": "finalizing",
                "storage": {
                    "final_structured_notes": final_notes_info,
                },
                "warnings": warnings,
                "post_class_steps": steps,
            },
        )

    async def graph_job() -> tuple[str, dict[str, object]]:
        return (
            "graph",
            await _finalize_post_class_graph_stage(
                session_id=session_id,
                ended_session=ended_session,
                context_snapshot=context_snapshot,
                knowledge_graph_snapshot=knowledge_graph_snapshot,
                structured_notes_markdown=final_notes_markdown,
                final_notes_info=final_notes_info,
            ),
        )

    async def artifacts_job() -> tuple[str, dict[str, object]]:
        return (
            "artifacts",
            await asyncio.to_thread(
                _finalize_session_after_end_sync,
                session_id=session_id,
                ended_session=ended_session,
                context_snapshot=context_snapshot,
                knowledge_graph_snapshot=knowledge_graph_snapshot,
                structured_notes_markdown=final_notes_markdown,
                initial_warnings=[],
                knowledge_graph_status="finalizing",
            ),
        )

    stage_results: dict[str, dict[str, object]] = {}
    tasks = [asyncio.create_task(graph_job()), asyncio.create_task(artifacts_job())]
    try:
        for task in asyncio.as_completed(tasks):
            stage_name, stage_result = await task
            stage_results[stage_name] = stage_result
            if stage_name == "graph":
                knowledge_graph_status = str(
                    stage_result.get("knowledge_graph_status") or "failed"
                )
                warnings.extend(str(item) for item in stage_result.get("warnings", []))
                _post_class_set_step(
                    steps,
                    "final_graph",
                    "ready" if knowledge_graph_status == "final" else "failed",
                    **_dict_without_none(
                        elapsed_seconds=stage_result.get("elapsed_seconds"),
                        error=stage_result.get("error"),
                    ),
                )
            else:
                warnings.extend(str(item) for item in stage_result.get("warnings", []))
                _post_class_set_step(
                    steps,
                    "artifacts",
                    str(stage_result.get("status") or "failed"),
                    **_dict_without_none(
                        elapsed_seconds=stage_result.get("elapsed_seconds"),
                        error=stage_result.get("error"),
                    ),
                )

            partial_status = _combined_post_class_status(stage_results)
            _save_post_class_status_if_present(
                session_id,
                partial_status,
                warnings=warnings,
                knowledge_graph_status=knowledge_graph_status,
                stage=f"{stage_name}_ready",
                steps=steps,
            )
            await _broadcast_post_class_update(
                session_id,
                _post_class_update_payload(
                    status=partial_status,
                    stage=f"{stage_name}_ready",
                    knowledge_graph_status=knowledge_graph_status,
                    warnings=warnings,
                    stage_results=stage_results,
                    steps=steps,
                    final_notes_info=final_notes_info,
                ),
            )
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        return
    except Exception as exc:  # noqa: BLE001 - background failure should be visible only.
        warning = f"Post-class generation failed: {exc}"
        warnings.append(warning)
        _post_class_debug(session_id, "task_failed", error=str(exc))
        _save_post_class_status_if_present(
            session_id,
            "failed",
            warnings=warnings,
            knowledge_graph_status=knowledge_graph_status,
            stage="failed",
            steps=steps,
        )
        await _broadcast_post_class_update(
            session_id,
            {
                "status": "failed",
                "post_class_stage": "failed",
                "knowledge_graph_status": knowledge_graph_status,
                "warnings": warnings,
                "post_class_steps": steps,
            },
        )
        return

    final_status = _combined_post_class_status(stage_results)
    _post_class_debug(
        session_id,
        "task_done",
        status=final_status,
        knowledge_graph_status=knowledge_graph_status,
    )
    _save_post_class_status_if_present(
        session_id,
        final_status,
        warnings=warnings,
        knowledge_graph_status=knowledge_graph_status,
        stage="done",
        steps=steps,
    )
    await _broadcast_post_class_update(
        session_id,
        _post_class_update_payload(
            status=final_status,
            stage="done",
            knowledge_graph_status=knowledge_graph_status,
            warnings=warnings,
            stage_results=stage_results,
            steps=steps,
            final_notes_info=final_notes_info,
        ),
    )


def _finalize_session_after_end_sync(
    *,
    session_id: str,
    ended_session: LectureSession,
    context_snapshot,
    knowledge_graph_snapshot,
    structured_notes_markdown: str | None,
    initial_warnings: list[str] | None = None,
    knowledge_graph_status: str = "failed",
) -> dict[str, object]:
    """Generate post-class summary/todos/title/index artifacts."""
    started_at = time.monotonic()
    _post_class_debug(session_id, "artifacts_start")
    warnings: list[str] = list(initial_warnings or [])
    if _post_class_should_stop(session_id):
        return {
            "status": "failed",
            "warnings": ["Post-class generation skipped because the saved session was deleted."],
        }

    try:
        context_for_save = context_manager.get_context(session_id)
    except ContextNotFoundError:
        context_for_save = context_snapshot
        warnings.append("Context was not available during post-class finalization.")
    try:
        graph_for_save = knowledge_graph_manager.get_graph(session_id)
    except KnowledgeGraphNotFoundError:
        graph_for_save = knowledge_graph_snapshot
        warnings.append("Knowledge graph was not available during post-class finalization.")

    try:
        post_class_files = _generate_and_save_post_class_artifacts(
            session_id=session_id,
            context=context_for_save,
            knowledge_graph=graph_for_save,
        )
        updated_session = _update_post_class_title_if_needed(
            session_id=session_id,
            ended_session=ended_session,
            context=context_for_save,
            knowledge_graph=graph_for_save,
            structured_notes_markdown=structured_notes_markdown,
            post_class_files=post_class_files,
        )
    except Exception as exc:  # noqa: BLE001 - surface artifacts failures explicitly.
        elapsed = time.monotonic() - started_at
        warning = f"Post-class summary/todos generation failed: {exc}"
        _post_class_debug(
            session_id,
            "artifacts_failed",
            elapsed_seconds=elapsed,
            error=str(exc),
        )
        return {
            "status": "failed",
            "warnings": [*warnings, warning],
            "elapsed_seconds": elapsed,
            "error": str(exc),
        }

    rag_index = _build_rag_index_when_enabled(
        session_id=session_id,
        context=context_for_save,
        knowledge_graph=graph_for_save,
        structured_notes_markdown=structured_notes_markdown,
    )
    if isinstance(rag_index.get("warning"), str):
        warnings.append(str(rag_index["warning"]))

    artifacts = local_storage.read_session(session_id).post_class_artifacts.model_dump()
    elapsed = time.monotonic() - started_at
    _post_class_debug(
        session_id,
        "artifacts_done",
        elapsed_seconds=elapsed,
        files={name: str(path) for name, path in post_class_files.items()},
    )
    return {
        "status": "ready",
        "knowledge_graph_status": knowledge_graph_status,
        "session": updated_session.model_dump(),
        "post_class_artifacts": artifacts,
        "elapsed_seconds": elapsed,
        "storage": {
            "session_dir": str(local_storage.session_dir(session_id)),
            "post_class_files": {
                name: str(path)
                for name, path in post_class_files.items()
            },
            "rag_index": rag_index,
        },
        "warnings": warnings,
    }


def _load_or_generate_final_structured_notes(session_id: str) -> str:
    """Reuse an existing final Qwen notes file, otherwise generate it.

    In app mode the microphone worker already owns the live Qwen notes pipeline
    and writes a final ``structured_notes.md`` when the classroom stops. The
    post-class backend task must not immediately start a second Qwen process on
    the same CPU. It first waits for that final file and only falls back to its
    own subprocess when no usable final notes appear.
    """
    existing = _wait_for_existing_final_structured_notes(session_id)
    if existing is not None:
        return existing
    return _generate_final_structured_notes_with_qwen(session_id)


def _wait_for_existing_final_structured_notes(session_id: str) -> str | None:
    """Wait briefly for the microphone/Qwen worker to flush final notes."""
    session_dir = local_storage.session_dir(session_id)
    notes_path = session_dir / "structured_notes.md"

    if os.getenv("APP_ENABLE_QWEN_NOTES", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return _read_usable_final_structured_notes(notes_path)

    timeout = _float_env("POST_CLASS_FINAL_NOTES_WAIT_SECONDS", 240.0)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if _post_class_should_stop(session_id):
            return None
        markdown = (
            _read_usable_final_structured_notes(notes_path)
            if notes_path.exists()
            else None
        )
        if markdown is not None:
            return markdown
        if time.monotonic() >= deadline:
            return None
        time.sleep(1.0)


def _read_usable_final_structured_notes(notes_path: Path) -> str | None:
    """Return notes markdown only when it is final and contains real notes."""
    try:
        markdown = notes_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    if _structured_notes_markdown_is_final_and_usable(markdown):
        return markdown
    return None


def _structured_notes_markdown_is_final_and_usable(markdown: str) -> bool:
    """Validate final notes without accepting subtitle-only/keyword-only files."""
    text = markdown.strip()
    if not text:
        return False
    if not re.search(r"(Update status|更新状态)\s*[：:]\s*final\b", text, re.IGNORECASE):
        return False

    headings: list[str] = []
    summary_lines: list[str] = []
    current_heading = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading_match = re.match(r"^##\s+(.+?)\s*$", line)
        if heading_match:
            current_heading = heading_match.group(1).strip()
            headings.append(current_heading)
            continue
        if current_heading.lower() in {"summary", "摘要", "课堂摘要", "课程摘要"}:
            if line.startswith(("-", "*")) and len(line) >= 12:
                summary_lines.append(line)

    excluded_headings = {
        "summary",
        "摘要",
        "课堂摘要",
        "课程摘要",
        "keywords",
        "关键词",
        "whisperlivesubtitles",
        "subtitles",
        "字幕",
        "语音字幕",
    }
    note_sections = [
        heading
        for heading in headings
        if _normalize_title_key(heading) not in excluded_headings
    ]
    return bool(summary_lines or note_sections)


def _generate_final_structured_notes_with_qwen(session_id: str) -> str:
    """Run local OpenVINO Qwen in a subprocess to produce final notes."""
    enabled = os.getenv("POST_CLASS_FINAL_QWEN_NOTES", "1").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        raise RuntimeError("POST_CLASS_FINAL_QWEN_NOTES is disabled")

    openvino_root = Path(os.getenv("OPENVINO_ROOT", "/home/edu-mate_user/openvino"))
    openvino_python = Path(
        os.getenv("OPENVINO_PYTHON", str(openvino_root / "venv" / "bin" / "python"))
    )
    if not openvino_python.exists():
        raise RuntimeError(f"OpenVINO Python not found: {openvino_python}")

    session_dir = local_storage.session_dir(session_id)
    output_path = session_dir / "structured_notes.md"
    qwen_model = os.getenv("QWEN_MODEL", str(openvino_root / "qwen2.5-3b-int4"))
    qwen_device = os.getenv("QWEN_DEVICE", "CPU")
    timeout = _float_env("POST_CLASS_QWEN_NOTES_TIMEOUT_SECONDS", 360.0)
    command = [
        str(openvino_python),
        "backend/scripts/whisperlive_qwen_markdown.py",
        "--finalize-session-id",
        session_id,
        "--finalize-session-dir",
        str(session_dir),
        "--sessions-dir",
        str(local_storage.base_dir),
        "--qwen-model",
        qwen_model,
        "--qwen-device",
        qwen_device,
        "--qwen-tokens",
        os.getenv("POST_CLASS_QWEN_NOTES_TOKENS", "1200"),
        "--max-qwen-segments-per-update",
        os.getenv(
            "POST_CLASS_QWEN_MAX_SEGMENTS_PER_UPDATE",
            os.getenv("QWEN_MAX_SEGMENTS_PER_UPDATE", "8"),
        ),
        "--whisperlive-model",
        os.getenv("APP_WHISPERLIVE_MODEL", "OpenVINO/whisper-large-v3-turbo-fp16-ov"),
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[3],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = "\n".join(
            line
            for line in [result.stdout.strip(), result.stderr.strip()]
            if line
        )
        raise RuntimeError(
            f"Qwen final notes command failed with code {result.returncode}: "
            f"{detail[-2000:] or 'no output'}"
        )
    if not output_path.exists():
        raise RuntimeError(f"Qwen final notes command did not create {output_path}")
    markdown = output_path.read_text(encoding="utf-8")
    if "Update status：final" not in markdown and "更新状态：final" not in markdown:
        raise RuntimeError("Qwen final notes output was not marked as final")
    return markdown


async def _finalize_post_class_graph_stage(
    *,
    session_id: str,
    ended_session: LectureSession,
    context_snapshot,
    knowledge_graph_snapshot,
    structured_notes_markdown: str | None,
    final_notes_info: dict[str, object],
) -> dict[str, object]:
    """Finalize the knowledge graph without blocking summary/todos generation."""
    started_at = time.monotonic()
    _post_class_debug(
        session_id,
        "final_graph_start",
        notes_status=final_notes_info.get("status"),
    )
    warnings: list[str] = []
    graph_response: dict[str, object] | None = None
    knowledge_graph_status = "failed"

    if structured_notes_markdown and str(final_notes_info.get("status")) == "ready":
        try:
            graph_response = await _apply_final_notes_knowledge_update(
                session_id=session_id,
                markdown=structured_notes_markdown,
                context_snapshot=context_snapshot,
                knowledge_graph_snapshot=knowledge_graph_snapshot,
            )
            warnings.extend(str(item) for item in graph_response.get("warnings", []))
            if graph_response.get("status") == "failed":
                raise RuntimeError(
                    "Final notes graph update failed: "
                    f"{graph_response.get('warnings') or graph_response}"
                )
            knowledge_graph_status = "final"
        except Exception as exc:  # noqa: BLE001 - keep artifacts independent.
            warnings.append(f"Final notes knowledge graph update failed: {exc}")
            graph_response = {
                "status": "failed",
                "error": str(exc),
            }
            knowledge_graph_status = "failed"
    else:
        warnings.append("Final notes were not ready; skipped final notes graph update.")
        graph_response = {
            "status": "failed",
            "error": "final notes were not ready",
        }

    try:
        extraction_result = _run_internal_knowledge_extraction(
            session_id=session_id,
            context=context_snapshot,
        )
    except Exception as exc:  # noqa: BLE001 - graph save should still proceed.
        extraction_result = {
            "session_id": session_id,
            "status": "failed",
            "errors": [str(exc)],
        }
        warnings.append(f"Final knowledge extraction failed: {exc}")

    try:
        session_for_save = _current_session_or_saved(session_id, ended_session)
        context_for_save = context_manager.get_context(session_id)
    except ContextNotFoundError:
        context_for_save = context_snapshot
    try:
        graph_for_save = knowledge_graph_manager.get_graph(session_id)
    except KnowledgeGraphNotFoundError:
        graph_for_save = knowledge_graph_snapshot

    storage_result = local_storage.save_session(
        session=session_for_save,
        context=context_for_save,
        knowledge_graph=graph_for_save,
        structured_notes_markdown=structured_notes_markdown,
        create=False,
    )
    elapsed = time.monotonic() - started_at
    _post_class_debug(
        session_id,
        "final_graph_done" if knowledge_graph_status == "final" else "final_graph_failed",
        elapsed_seconds=elapsed,
        knowledge_graph_status=knowledge_graph_status,
        graph_response=graph_response,
        extraction_result=extraction_result,
    )
    return {
        "status": "ready" if knowledge_graph_status == "final" else "failed",
        "knowledge_graph_status": knowledge_graph_status,
        "session": session_for_save.model_dump(),
        "elapsed_seconds": elapsed,
        "graph_update": graph_response,
        "storage": {
            "session_dir": str(storage_result.session_dir),
            "knowledge_extraction": extraction_result,
        },
        "warnings": warnings,
        **(
            {"error": graph_response.get("error")}
            if isinstance(graph_response, dict) and graph_response.get("error")
            else {}
        ),
    }


async def _apply_final_notes_knowledge_update(
    *,
    session_id: str,
    markdown: str,
    context_snapshot,
    knowledge_graph_snapshot,
) -> dict[str, object]:
    """Send final Qwen notes through the normal cloud notes-agent path."""
    notes_markdown = markdown_without_whisperlive_subtitles(markdown)
    source_segments = [
        NotesSourceSegment(
            segment_id=segment.segment_id,
            start_ts=segment.start_ts,
            end_ts=segment.end_ts,
            text=segment.text,
        )
        for segment in context_snapshot.transcript
        if segment.text.strip()
    ]
    recent_source_segments = _select_final_notes_source_segments(
        source_segments,
        markdown=notes_markdown,
    )
    request = NotesKnowledgeTreeUpdateRequest(
        session_id=session_id,
        snapshot_id="notes_post_class_final",
        sequence=999_999,
        markdown=notes_markdown,
        source_segments=source_segments,
        recent_source_segments=recent_source_segments,
        update_status="final",
    )
    try:
        knowledge_graph = knowledge_graph_manager.get_graph(session_id)
    except KnowledgeGraphNotFoundError:
        knowledge_graph = knowledge_graph_snapshot

    agent = MarkdownKnowledgeTreeAgent(
        llm_client=_post_class_llm_client(
            timeout_env="POST_CLASS_GRAPH_LLM_TIMEOUT_SECONDS",
            retries_env="POST_CLASS_GRAPH_LLM_MAX_RETRIES",
            default_timeout=45.0,
            default_retries=0,
        )
    )
    started_at = time.monotonic()
    result = await asyncio.to_thread(
        agent.extract,
        request,
        knowledge_graph.model_copy(deep=True),
    )
    elapsed = time.monotonic() - started_at
    if result.failed:
        return {
            "status": "failed",
            "session_id": session_id,
            "snapshot_id": request.snapshot_id,
            "markdown_hash": result.markdown_hash,
            "warnings": list(result.warnings),
            "elapsed_seconds": elapsed,
        }

    metadata_updated = _update_session_metadata_from_notes_result(
        session_id=session_id,
        session_title=result.session_title,
        course=result.course,
    )
    if result.extraction is None:
        return {
            "status": "skipped",
            "session_id": session_id,
            "snapshot_id": request.snapshot_id,
            "markdown_hash": result.markdown_hash,
            "session_title": result.session_title,
            "course": result.course,
            "session_metadata_updated": metadata_updated,
            "warnings": list(result.warnings),
            "elapsed_seconds": elapsed,
        }

    event = RealtimeEvent(
        session_id=session_id,
        event_type="knowledge.extraction",
        payload=result.extraction.model_dump(),
    )
    context_update = context_manager.handle_event(event)
    graph_patch = knowledge_graph_manager.handle_event(event)
    await websocket_manager.broadcast(
        session_id,
        WebSocketMessage(
            type="event.received",
            session_id=session_id,
            data={
                "event": event.model_dump(),
                "context_update": context_update.model_dump(),
                "graph_patch": graph_patch.model_dump(),
            },
        ),
    )
    operation_count = len(graph_patch.operations)
    return {
        "status": "applied" if operation_count else "skipped",
        "session_id": session_id,
        "snapshot_id": request.snapshot_id,
        "markdown_hash": result.markdown_hash,
        "extraction_id": result.extraction.extraction_id,
        "graph_patch_operations": operation_count,
        "session_title": result.session_title,
        "course": result.course,
        "session_metadata_updated": metadata_updated,
        "warnings": list(result.warnings),
        "elapsed_seconds": elapsed,
    }


_FINAL_GRAPH_STOP_WORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "because",
    "been",
    "between",
    "from",
    "have",
    "into",
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
    "would",
}


def _select_final_notes_source_segments(
    source_segments: list[NotesSourceSegment],
    *,
    markdown: str,
) -> list[NotesSourceSegment]:
    """Select a bounded evidence window for the final notes graph request."""
    limit = _int_env("POST_CLASS_FINAL_GRAPH_SOURCE_SEGMENTS", 24)
    if limit <= 0 or len(source_segments) <= limit:
        return source_segments

    notes_markdown = markdown_without_whisperlive_subtitles(markdown)
    note_tokens = _final_graph_content_tokens(notes_markdown)
    selected_indexes: set[int] = set()

    # Keep broad timeline coverage so final graph citations are not only from
    # the beginning of class.
    coverage_count = max(1, min(len(source_segments), limit // 3))
    if coverage_count == 1:
        selected_indexes.add(0)
    else:
        span = len(source_segments) - 1
        for index in range(coverage_count):
            selected_indexes.add(round(index * span / (coverage_count - 1)))

    scored: list[tuple[int, int]] = []
    if note_tokens:
        for index, segment in enumerate(source_segments):
            segment_tokens = _final_graph_content_tokens(segment.text)
            score = len(note_tokens & segment_tokens)
            if score:
                scored.append((score, index))
    scored.sort(key=lambda item: (-item[0], item[1]))

    for _score, index in scored:
        if len(selected_indexes) >= limit:
            break
        selected_indexes.add(index)

    if len(selected_indexes) < limit:
        for index in range(len(source_segments)):
            if len(selected_indexes) >= limit:
                break
            selected_indexes.add(index)

    return [source_segments[index] for index in sorted(selected_indexes)]


def _final_graph_content_tokens(text: str) -> set[str]:
    """Return coarse content tokens for selecting final graph evidence."""
    normalized = text.lower()
    tokens: set[str] = set()
    for word in re.findall(r"[a-z0-9]+", normalized):
        if len(word) < 3 or word in _FINAL_GRAPH_STOP_WORDS:
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
        else:
            tokens.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return tokens


def _track_post_class_task(session_id: str, task: asyncio.Task[None]) -> None:
    """Track a post-class task so history reads can distinguish live work from interruption."""
    _post_class_tasks[session_id] = task
    task.add_done_callback(lambda _task: _post_class_tasks.pop(session_id, None))


async def _broadcast_post_class_update(
    session_id: str,
    payload: dict[str, object],
) -> None:
    """Broadcast one stage-specific post-class update."""
    await websocket_manager.broadcast(
        session_id,
        WebSocketMessage(
            type="post_class.updated",
            session_id=session_id,
            data=payload,
        ),
    )


def _post_class_update_payload(
    *,
    status: str,
    stage: str,
    knowledge_graph_status: str,
    warnings: list[str],
    stage_results: dict[str, dict[str, object]],
    steps: dict[str, object],
    final_notes_info: dict[str, object],
) -> dict[str, object]:
    """Build a WebSocket payload without mixing graph and artifact meanings."""
    payload: dict[str, object] = {
        "status": status,
        "post_class_stage": stage,
        "knowledge_graph_status": knowledge_graph_status,
        "warnings": warnings,
        "post_class_steps": steps,
        "storage": {
            "final_structured_notes": final_notes_info,
        },
    }

    storage = payload["storage"]
    assert isinstance(storage, dict)
    graph_result = stage_results.get("graph")
    if graph_result:
        storage["final_graph"] = graph_result.get("graph_update")
        if graph_result.get("session"):
            payload["session"] = graph_result["session"]
    artifacts_result = stage_results.get("artifacts")
    if artifacts_result:
        if artifacts_result.get("session"):
            payload["session"] = artifacts_result["session"]
        if artifacts_result.get("post_class_artifacts"):
            payload["post_class_artifacts"] = artifacts_result["post_class_artifacts"]
        artifact_storage = artifacts_result.get("storage")
        if isinstance(artifact_storage, dict):
            storage["artifacts"] = artifact_storage
    return payload


def _combined_post_class_status(
    stage_results: dict[str, dict[str, object]],
) -> str:
    """Return summary/todos status only; graph finality is a separate field."""
    artifacts = stage_results.get("artifacts")
    if artifacts is None:
        return "generating"
    return "ready" if artifacts.get("status") == "ready" else "failed"


def _post_class_set_step(
    steps: dict[str, object],
    name: str,
    status: str,
    **extra: object,
) -> None:
    """Update one post-class step in the persisted status object."""
    steps[name] = {
        "status": status,
        "updated_at": utc_now_iso(),
        **{key: value for key, value in extra.items() if value is not None},
    }


def _post_class_debug(session_id: str, event: str, **data: object) -> None:
    """Append detailed post-class stage diagnostics to the session directory."""
    try:
        session_dir = local_storage.session_dir(session_id)
        if not session_dir.exists():
            return
        payload = {
            "ts": utc_now_iso(),
            "event": event,
            **data,
        }
        with (session_dir / "post_class_debug.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return


def _dict_without_none(**items: object) -> dict[str, object]:
    return {key: value for key, value in items.items() if value is not None}


def _current_session_or_saved(
    session_id: str,
    fallback: LectureSession,
) -> LectureSession:
    """Return the freshest session metadata available."""
    try:
        return session_manager.get_session(session_id)
    except SessionNotFoundError:
        pass
    try:
        return LectureSession.model_validate(local_storage.read_metadata(session_id))
    except (FileNotFoundError, ValueError):
        return fallback


def _post_class_llm_client(
    *,
    timeout_env: str,
    retries_env: str,
    default_timeout: float,
    default_retries: int,
):
    """Create a cloud LLM client with stage-specific timeout/retry settings."""
    settings = load_llm_settings()
    if not settings.enabled:
        return None
    settings = replace(
        settings,
        timeout_seconds=_float_env(timeout_env, default_timeout),
        max_retries=max(0, _int_env(retries_env, default_retries)),
    )
    return CloudLLMClient(settings)


def _update_session_metadata_from_notes_result(
    *,
    session_id: str,
    session_title: str | None,
    course: str | None,
) -> bool:
    """Synchronously update metadata from final graph-agent title/course output."""
    updates: dict[str, object] = {}
    if session_title:
        updates["title"] = session_title[:80]
    if course:
        updates["course"] = course[:80]
    if not updates:
        return False

    updated = False
    try:
        session_manager.update_session_metadata(session_id, updates)
        updated = True
    except SessionNotFoundError:
        pass
    if local_storage.session_exists(session_id):
        try:
            local_storage.update_session_metadata(session_id, updates)
            updated = True
        except (FileNotFoundError, ValueError):
            pass
    return updated


def _post_class_should_stop(session_id: str) -> bool:
    """Return whether a background task must stop without writing files."""
    return (
        session_id in _cancelled_post_class_sessions
        or not local_storage.session_exists(session_id)
    )


def _save_post_class_status_if_present(
    session_id: str,
    status: str,
    *,
    warnings: list[str] | None = None,
    knowledge_graph_status: str | None = None,
    stage: str | None = None,
    steps: dict[str, object] | None = None,
) -> None:
    """Persist status if the session still exists; ignore deleted sessions."""
    if _post_class_should_stop(session_id):
        return
    try:
        local_storage.save_post_class_status(
            session_id,
            status,  # type: ignore[arg-type]
            warnings=warnings,
            knowledge_graph_status=knowledge_graph_status,  # type: ignore[arg-type]
            stage=stage,
            steps=steps,
        )
    except FileNotFoundError:
        return


def _normalize_history_post_class_status(
    detail: SessionHistoryDetail,
) -> SessionHistoryDetail:
    """Mark orphaned generating jobs as failed after a process restart.

    The post-class worker is currently an in-process background task. If the app
    exits after ``session.ended`` but before ``post_class.updated``, the status
    file remains ``generating`` and there is no task in memory after restart.
    Returning ``failed`` makes the interruption explicit in history instead of
    showing an endless spinner.
    """
    session_id = detail.session.session_id
    if detail.post_class_status != "generating" or session_id in _post_class_tasks:
        return detail

    warning = (
        "Post-class generation was interrupted before completion. "
        "Restart the classroom flow or regenerate artifacts when retry support is available."
    )
    warnings = [*detail.post_class_warnings, warning]
    _save_post_class_status_if_present(
        session_id,
        "failed",
        warnings=warnings,
        knowledge_graph_status="failed",
    )
    return detail.model_copy(
        update={
            "post_class_status": "failed",
            "post_class_warnings": warnings,
            "knowledge_graph_status": "failed",
        }
    )


def _run_internal_knowledge_extraction(session_id: str, context) -> dict[str, object]:
    """Run internal extraction before saving the final classroom snapshot.

    Successful extractions are routed through the same internal event path used
    by mock/debug knowledge events. Failures are surfaced as explicit errors and
    do not block saving the classroom.
    """
    result = knowledge_extraction_service.extract_and_apply(
        context=context,
        context_manager=context_manager,
        knowledge_graph_manager=knowledge_graph_manager,
    )
    return {
        "session_id": session_id,
        "provider": knowledge_extraction_service.extractor.provider_name,
        "extraction_count": len(result.extractions),
        "processed_source_ids": result.processed_source_ids,
        "errors": [error.model_dump() for error in result.errors],
    }


def _session_metadata_updates(request: UpdateSessionRequest) -> dict[str, object]:
    """Return sanitized metadata updates from a PATCH request."""
    updates: dict[str, object] = {}
    fields_set = request.model_fields_set

    if "title" in fields_set:
        title = (request.title or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title cannot be empty")
        updates["title"] = title[:80]

    if "course" in fields_set:
        course = (request.course or "").strip() if request.course is not None else ""
        updates["course"] = course[:80] or None

    return updates


def _image_path_for_session(session_id: str, image_id: str) -> str | None:
    """Find the image_path recorded in live context or saved history."""
    try:
        context = context_manager.get_context(session_id)
    except ContextNotFoundError:
        context = None
    if context is not None:
        for visual in context.visuals:
            if visual.image_id == image_id:
                return visual.image_path

    try:
        detail = local_storage.read_session(session_id)
    except (FileNotFoundError, ValueError):
        return None
    for item in detail.timeline:
        if item.type != "visual":
            continue
        try:
            visual = ImageCapture.model_validate(item.data)
        except ValueError:
            continue
        if visual.image_id == image_id:
            return visual.image_path
    return None


def _generate_and_save_post_class_artifacts(
    session_id: str,
    context,
    knowledge_graph,
) -> dict[str, object]:
    """结束课堂时自动生成并保存规则版“基础课后产物”。

    这里刻意只自动生成 summary.md 和 todos.json：
    - 总结、待办属于“结束课堂后通常就应该有”的基础学习材料；
    - quiz 属于主动练习场景，必须由用户在 AgentPanel 中点击“生成自测”或
      输入出题类 prompt 后再生成，避免系统在用户没有需求时提前写入题目。

    ``save_agent_artifacts()`` 仍会额外写出 agent_artifacts.json。这个快照只
    记录本次自动生成的 summary/todos，因此结束课堂时不会出现 quiz.json。

    这里仍然通过 LocalStorage 写文件，保持“API 不直接拼路径、不直接写磁盘”
    的存储边界。
    """
    llm_client = _post_class_llm_client(
        timeout_env="POST_CLASS_ARTIFACT_LLM_TIMEOUT_SECONDS",
        retries_env="POST_CLASS_ARTIFACT_LLM_MAX_RETRIES",
        default_timeout=45.0,
        default_retries=0,
    )
    skill_results = [
        SummarizerSkill(llm_client=llm_client).run(session_id, context, knowledge_graph),
        TodoDetectiveSkill(llm_client=llm_client).run(session_id, context, knowledge_graph),
    ]
    artifacts = [
        {
            "type": result.artifact.type,
            "title": result.artifact.title,
            "content": result.artifact.content,
        }
        for result in skill_results
        if result.artifact is not None
    ]
    return local_storage.save_agent_artifacts(session_id, artifacts)


def _update_post_class_title_if_needed(
    *,
    session_id: str,
    ended_session: LectureSession,
    context,
    knowledge_graph,
    structured_notes_markdown: str | None,
    post_class_files: dict[str, object],
) -> LectureSession:
    """Fill a default classroom title from saved post-class content."""
    current_session = _current_session_or_saved(session_id, ended_session)
    current_title = current_session.title
    if not _is_auto_title_candidate(current_title):
        return current_session

    inferred_title = _infer_post_class_title(
        context=context,
        knowledge_graph=knowledge_graph,
        structured_notes_markdown=structured_notes_markdown,
        post_class_files=post_class_files,
    )
    if not inferred_title:
        return ended_session

    updates = {"title": inferred_title[:80]}
    updated_session: LectureSession | None = None
    try:
        updated_session = session_manager.update_session_metadata(session_id, updates)
    except SessionNotFoundError:
        updated_session = None

    if local_storage.session_exists(session_id):
        try:
            persisted_session = local_storage.update_session_metadata(session_id, updates)
            if updated_session is None:
                updated_session = persisted_session
        except (FileNotFoundError, ValueError):
            pass

    return updated_session or current_session


def _infer_post_class_title(
    *,
    context,
    knowledge_graph,
    structured_notes_markdown: str | None,
    post_class_files: dict[str, object],
) -> str | None:
    """Infer a concise classroom title without adding another LLM call."""
    for candidate in _summary_title_candidates(post_class_files):
        title = _clean_session_title_candidate(candidate)
        if title:
            return title

    if structured_notes_markdown:
        for candidate in _markdown_heading_candidates(structured_notes_markdown):
            title = _clean_session_title_candidate(candidate)
            if title:
                return title

    for candidate in _knowledge_graph_title_candidates(knowledge_graph):
        title = _clean_session_title_candidate(candidate)
        if title:
            return title

    for segment in getattr(context, "transcript", [])[:10]:
        title = _clean_session_title_candidate(getattr(segment, "text", ""))
        if title:
            return title
    return None


def _summary_title_candidates(post_class_files: dict[str, object]) -> list[str]:
    """Read likely title lines from the generated summary file."""
    summary_path = post_class_files.get("summary")
    if not hasattr(summary_path, "read_text"):
        return []
    try:
        text = summary_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return _markdown_heading_candidates(text)


def _markdown_heading_candidates(markdown: str) -> list[str]:
    """Return top-level Markdown headings in order."""
    candidates: list[str] = []
    for line in markdown.splitlines():
        match = re.match(r"^\s{0,3}#{1,2}\s+(.+?)\s*$", line)
        if match:
            candidates.append(match.group(1))
    return candidates


def _knowledge_graph_title_candidates(knowledge_graph) -> list[str]:
    """Return graph labels that look like lecture-level topics."""
    preferred_types = {
        "lecture_topic",
        "course_section",
        "chapter",
        "topic",
        "course",
        "subject",
    }
    nodes = list(getattr(knowledge_graph, "nodes", []))
    preferred = [
        node
        for node in nodes
        if str(getattr(node, "type", "")).strip().lower() in preferred_types
    ]
    if not preferred:
        preferred = [
            node
            for node in nodes
            if getattr(node, "level", None) in (0, 1)
            and float(getattr(node, "importance", 0) or 0) >= 0.8
        ]
    preferred.sort(
        key=lambda node: (
            0 if getattr(node, "level", None) == 0 else 1,
            -float(getattr(node, "importance", 0) or 0),
        )
    )
    return [str(getattr(node, "label", "")) for node in preferred[:8]]


def _is_auto_title_candidate(title: str | None) -> bool:
    """Return true when it is safe to replace the current classroom title."""
    normalized = _normalize_title_key(title or "")
    if not normalized:
        return True
    return normalized in {
        "未命名课堂",
        "课堂记录",
        "新课堂",
        "课堂",
        "untitled",
        "untitledclassroom",
        "classroom",
    }


def _clean_session_title_candidate(value: object) -> str | None:
    """Normalize and reject generic or artifact-oriented title candidates."""
    title = str(value or "").strip()
    if not title:
        return None
    title = re.sub(r"^[#>\-\s\d.、]+", "", title)
    title = re.sub(r"[*_`]+", "", title).strip()
    title = re.sub(
        r"\s*[-—–:：]\s*(summary|classroom summary|课堂总结|总结|笔记|notes?)\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()
    title = re.sub(r"\s+", " ", title)
    if len(title) > 80:
        title = title[:80].rstrip(" ，。,.:-")

    key = _normalize_title_key(title)
    if len(key) < 3:
        return None
    if key in {
        "summary",
        "keypoints",
        "knowledgeflow",
        "reviewsuggestions",
        "重点",
        "摘要",
        "总结",
        "关键词",
        "课堂总结",
        "课后产物",
        "whisperlivelocalclassroomnotes",
        "whisperlivesubtitles",
        "transcript",
    }:
        return None
    if key.startswith(("generatedat", "updatestatus", "audiofile", "whisperlivemodel")):
        return None
    return title


def _normalize_title_key(value: str) -> str:
    """Normalize a title for generic/default comparisons."""
    return re.sub(r"[\W_]+", "", value.strip().lower(), flags=re.UNICODE)


def _float_env(name: str, default: float) -> float:
    """Read a float environment variable with a safe fallback."""
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    """Read an integer environment variable with a safe fallback."""
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _build_rag_index_when_enabled(
    session_id: str,
    context,
    knowledge_graph,
    structured_notes_markdown: str | None = None,
) -> dict[str, object]:
    """在启用 LlamaIndex 后为已结束课堂构建持久化索引。

    这是 Phase 6 的结束课堂钩子。它只在 ``RAG_QUERY_BACKEND=llamaindex`` 且
    LlamaIndex 可用时真正写入 ``data/sessions/{session_id}/llama_index``。

    重要设计：
    - 索引构建是课后增强能力，不是结束课堂的主链路。失败时返回 warning，
      不抛出到 API 层，避免课堂 metadata/transcript/timeline/graph 已保存却
      因索引失败让前端看到结束课堂失败。
    - 目录仍通过 ``LocalStorage.session_index_dir()`` 计算，保持 storage
      边界统一。
    """
    backend = os.getenv("RAG_QUERY_BACKEND", "lexical").strip().lower()
    if backend != "llamaindex":
        return {
            "enabled": False,
            "status": "skipped",
        }

    build_enabled = os.getenv("POST_CLASS_BUILD_RAG_INDEX", "0").strip().lower()
    if build_enabled not in {"1", "true", "yes", "on"}:
        return {
            "enabled": True,
            "status": "skipped",
            "reason": "POST_CLASS_BUILD_RAG_INDEX is disabled",
        }

    service = LlamaIndexQueryService(
        index_dir_resolver=local_storage.session_index_dir,
    )
    documents = build_session_documents(
        context,
        knowledge_graph,
        structured_notes_markdown=structured_notes_markdown,
    )
    try:
        index_dir = service.build_and_persist(documents, session_id=session_id)
    except Exception as exc:  # noqa: BLE001 - 可选索引失败不应影响课堂结束。
        return {
            "enabled": True,
            "status": "fallback",
            "warning": f"LlamaIndex index was not persisted: {exc}",
        }

    return {
        "enabled": True,
        "status": "persisted",
        "path": str(index_dir),
    }
