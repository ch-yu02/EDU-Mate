"""本地课堂数据存储模块。

LocalStorage 负责把一次课堂结束时的内存状态保存到磁盘，并在课后
历史功能中把这些文件重新读回为 API 响应模型。MVP 阶段的保存目录遵循
任务清单中的结构：

  data/sessions/{session_id}/metadata.json
  data/sessions/{session_id}/transcript.md
  data/sessions/{session_id}/timeline.json
  data/sessions/{session_id}/knowledge_graph.json

职责边界
--------
- 负责：创建目录、序列化/反序列化 Pydantic 模型、写入和读取课堂产物文件。
- 不负责：判断 session 是否可结束、处理实时事件、生成知识图谱、映射 HTTP 状态码。

为什么结束课堂时统一写？
----------------------
MVP 阶段先保证“演示主链路”稳定：课堂中全部数据留在内存，结束时一次性
落盘。这样逻辑简单、易测试。后续若担心断电丢数据，可以扩展为事件级
增量写入或定时快照。

历史读取约定
----------
历史 API 不尝试恢复一节课为可写的 recording session，而是把本地文件
作为只读课堂档案返回：
  - GET /sessions 读取 metadata.json 和 timeline.json，生成轻量列表。
  - GET /sessions/{session_id}/history 读取四个完整产物，生成回放详情。
  - GET /sessions/{session_id} 内存未命中时，只回退读取 metadata.json。

这种设计让课后浏览和实时课堂写入解耦：后端重启后仍能看历史，但不会误把
旧 session 当作仍可接收事件的课堂。
"""

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from backend.app.models import (
    ClassroomContext,
    KnowledgeGraphStatus,
    KnowledgeTree,
    LectureSession,
    PostClassStatus,
    SessionHistoryDetail,
    SessionPostClassArtifacts,
    SessionHistorySummary,
    TimelineItem,
    utc_now_iso,
)


@dataclass(frozen=True)
class StorageWriteResult:
    """一次课堂保存操作的结果。

    session_dir: 本次课堂数据目录
    files:       写出的文件名到绝对/相对路径的映射，便于 API 日志和测试断言
    """

    session_dir: Path
    files: dict[str, Path]


class LocalStorage:
    """Persist and read classroom session artifacts on the local filesystem.

    LocalStorage 是 storage 层的唯一入口。API 路由只调用这里的公开方法，
    不直接拼接文件路径或读取 JSON 文件，避免存储结构散落到路由层。
    """

    def __init__(self, base_dir: Path | str = Path("data/sessions")) -> None:
        # base_dir 默认是项目根目录下的 data/sessions。测试时可传临时目录，
        # 避免污染真实 demo 数据。
        self.base_dir = Path(base_dir)

    # ── 对外入口 ─────────────────────────────────────────────

    def save_session(
        self,
        session: LectureSession,
        context: ClassroomContext,
        knowledge_graph: KnowledgeTree,
        structured_notes_markdown: str | None = None,
        *,
        create: bool = True,
    ) -> StorageWriteResult:
        """Persist all MVP classroom artifacts for one ended session.

        调用时机：POST /sessions/{session_id}/end 成功后。

        写入内容：
          - metadata.json：课堂元信息
          - transcript.md：人可读的 Markdown 字幕记录
          - timeline.json：前端可回放的统一时间线
          - knowledge_graph.json：完整知识图谱快照
        """
        session_dir = self.session_dir(session.session_id)
        if create:
            session_dir.mkdir(parents=True, exist_ok=True)
        elif not session_dir.exists() or not session_dir.is_dir():
            raise FileNotFoundError(f"Saved session not found: {session.session_id}")

        files = {
            "metadata": session_dir / "metadata.json",
            "transcript": session_dir / "transcript.md",
            "timeline": session_dir / "timeline.json",
            "knowledge_graph": session_dir / "knowledge_graph.json",
        }
        if structured_notes_markdown and structured_notes_markdown.strip():
            files["structured_notes"] = session_dir / "structured_notes.md"

        self._write_json(files["metadata"], session.model_dump())
        self._write_text(files["transcript"], self._render_transcript_markdown(context))
        if "structured_notes" in files:
            self._write_text(files["structured_notes"], structured_notes_markdown)
        self._write_json(
            files["timeline"],
            [item.model_dump() for item in context.timeline],
        )
        self._write_json(files["knowledge_graph"], knowledge_graph.model_dump())

        return StorageWriteResult(session_dir=session_dir, files=files)

    def session_dir(self, session_id: str) -> Path:
        """Return the filesystem directory for a session.

        这里不检查目录是否存在，只负责统一路径规则。读写方法在自己的
        语义里决定是创建目录、返回空列表，还是把 FileNotFoundError
        交给 API 层映射为 404。
        """
        return self.base_dir / session_id

    def session_exists(self, session_id: str) -> bool:
        """Return whether a persisted session directory exists.

        这个方法只判断目录存在性，不保证目录内四个课堂产物文件完整。
        若需要完整读取，请使用 read_session()，让缺失文件自然暴露为错误。
        """
        return self.session_dir(session_id).exists()

    def session_index_dir(self, session_id: str) -> Path:
        """返回某节历史课堂的 LlamaIndex 持久化目录。

        路径固定为：

        ``data/sessions/{session_id}/llama_index``

        这个方法只负责安全地计算路径，不主动创建目录。真正创建发生在 RAG
        服务确认要持久化索引时。这样 storage 层仍然是路径边界的唯一来源，
        API/RAG 层不需要手写 ``data/sessions`` 拼接逻辑。
        """
        return self._safe_session_dir(session_id) / "llama_index"

    def session_image_path(self, session_id: str, image_id: str, image_path: str | None = None) -> Path:
        """Resolve a classroom image path inside the saved session directory.

        For security, this method does not serve arbitrary absolute paths from
        incoming ``image.capture`` events. It only resolves files under:

        ``data/sessions/{session_id}/images/``

        ``image_path`` may be a future ``local://sessions/{session_id}/images/x.jpg``
        URI. If it is missing or points elsewhere, the stable ``image_id`` is
        used as the filename stem and common image extensions are checked.
        """
        images_dir = self._safe_session_dir(session_id) / "images"
        candidates: list[Path] = []

        if image_path and image_path.startswith("local://"):
            local_prefix = f"local://sessions/{session_id}/images/"
            if image_path.startswith(local_prefix):
                candidates.append(images_dir / image_path[len(local_prefix):])

        raw = images_dir / image_id
        candidates.extend(
            [
                raw,
                raw.with_suffix(".jpg"),
                raw.with_suffix(".jpeg"),
                raw.with_suffix(".png"),
                raw.with_suffix(".webp"),
            ]
        )

        safe_root = images_dir.resolve()
        for candidate in candidates:
            resolved = candidate.resolve()
            if not resolved.is_relative_to(safe_root):
                continue
            if resolved.exists() and resolved.is_file():
                return resolved
        raise FileNotFoundError(f"Image not found: {session_id}/{image_id}")

    def save_session_image(
        self,
        session_id: str,
        image_id: str,
        content: bytes,
        content_type: str | None,
    ) -> Path:
        """Save raw image bytes under the session images directory.

        The API uses raw request bodies instead of multipart uploads so the
        project does not need an extra ``python-multipart`` dependency. File
        extensions are derived from the content type and the final path is
        constrained to ``data/sessions/{session_id}/images``.
        """
        if not content:
            raise ValueError("image content is empty")

        images_dir = self._safe_session_dir(session_id) / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        extension = self._image_extension(content_type)
        path = (images_dir / f"{image_id}{extension}").resolve()
        safe_root = images_dir.resolve()
        if not path.is_relative_to(safe_root):
            raise ValueError("image path escapes session images directory")

        path.write_bytes(content)
        return path

    def global_index_dir(self) -> Path:
        """返回跨课堂全局索引目录。

        默认 ``base_dir`` 是 ``data/sessions``，因此全局索引放在同级的
        ``data/indexes/global``。这样单节课档案和跨课索引分开存放，删除某节
        历史课堂时不会误删全局目录。
        """
        return self.base_dir.parent / "indexes" / "global"

    def read_metadata(self, session_id: str) -> dict:
        """Read persisted metadata.json for history features.

        GET /sessions/{session_id} 在内存 miss 时调用此方法回退读取历史
        课堂元信息。返回 dict 而不是 LectureSession，是为了让调用方明确
        决定要不要做 Pydantic 校验，以及如何把校验失败映射给用户。
        """
        return self._read_json(self.session_dir(session_id) / "metadata.json")

    def update_session_metadata(
        self,
        session_id: str,
        updates: dict[str, object],
    ) -> LectureSession:
        """Update persisted ``metadata.json`` for a saved classroom."""
        metadata_path = self._safe_session_dir(session_id) / "metadata.json"
        session = LectureSession.model_validate(self._read_json(metadata_path))
        if not updates:
            return session

        updated_session = session.model_copy(update=updates)
        self._write_json(metadata_path, updated_session.model_dump())
        return updated_session

    def list_sessions(self) -> list[SessionHistorySummary]:
        """Return summaries for all persisted sessions, newest first.

        列表页需要“快”和“宽容”：它只依赖 metadata.json，并在 timeline.json
        存在时额外统计 event_count。这样即使某个历史目录还没有完整产物
        或者是开发时手动残留的目录，也不会阻塞整个历史列表。

        目录过滤策略：
          - base_dir 不存在：说明还没有任何已保存课堂，返回空列表。
          - 非目录条目：跳过，例如 .gitkeep。
          - 缺少 metadata.json 或 metadata 格式损坏：跳过，因为没有可展示的课堂元信息。
          - 缺少 timeline.json：event_count 记为 0，仍允许展示摘要。
        """
        summaries: list[SessionHistorySummary] = []
        if not self.base_dir.exists():
            return summaries

        for session_dir in self.base_dir.iterdir():
            # data/sessions 里可能有 .gitkeep 或临时文件；历史列表只关心
            # 每个 session_id 对应的目录。
            if not session_dir.is_dir():
                continue

            metadata_path = session_dir / "metadata.json"
            timeline_path = session_dir / "timeline.json"
            # 没有 metadata 就无法构造 LectureSession。与其让一个坏目录
            # 破坏整个列表，不如跳过它，完整详情读取再严格报错。
            if not metadata_path.exists():
                continue

            # 读取时重新通过 Pydantic 校验，保证历史 API 输出仍遵守当前
            # 后端模型契约。旧文件若格式不兼容，会尽早暴露。
            try:
                session = LectureSession.model_validate(self._read_json(metadata_path))
            except (ValueError, ValidationError):
                # 列表页应尽量宽容。开发过程中可能留下半写入或手动修改坏的
                # metadata.json；跳过坏目录可以保证其他正常历史课堂仍可展示。
                continue
            event_count = 0
            if timeline_path.exists():
                event_count = len(self._read_json_list(timeline_path))

            summaries.append(
                SessionHistorySummary(
                    session=session,
                    event_count=event_count,
                    storage_path=str(session_dir),
                )
            )

        return sorted(
            summaries,
            # new_session_id 中含时间戳，但排序应以模型字段为准；这样即使
            # 后续 session_id 规则改变，历史列表仍按课堂开始时间倒序。
            key=lambda item: item.session.start_time,
            reverse=True,
        )

    def read_session(self, session_id: str) -> SessionHistoryDetail:
        """Read a complete persisted session for history playback.

        与 list_sessions() 的宽容策略不同，详情页需要完整课后档案。
        因此这里要求四个文件都存在且格式正确：
          - metadata.json -> LectureSession
          - timeline.json -> list[TimelineItem]
          - knowledge_graph.json -> KnowledgeTree
          - transcript.md -> 原始 Markdown 字符串

        缺文件或 JSON 结构错误会抛出 FileNotFoundError / ValueError /
        Pydantic ValidationError。API 层负责把这些异常转换成 404 或后续
        更细的错误响应。
        """
        session_dir = self.session_dir(session_id)
        # metadata 是详情页标题、课程、起止时间和状态的唯一来源。
        session = LectureSession.model_validate(
            self._read_json(session_dir / "metadata.json")
        )
        # timeline 用于前端历史回放。逐条校验可以避免坏数据进入 UI reducer。
        timeline = [
            TimelineItem.model_validate(item)
            for item in self._read_json_list(session_dir / "timeline.json")
        ]
        # knowledge_graph 是结束课堂时保存的完整快照；历史页不需要重放
        # graph_patch，而是直接渲染这个最终状态。
        knowledge_graph = KnowledgeTree.model_validate(
            self._read_json(session_dir / "knowledge_graph.json")
        )
        # transcript.md 保留 Markdown 形态，方便前端直接展示，也方便未来
        # post-class skill 把它作为 LLM 输入素材。
        transcript_markdown = (session_dir / "transcript.md").read_text(
            encoding="utf-8"
        )
        structured_notes_path = session_dir / "structured_notes.md"

        return SessionHistoryDetail(
            session=session,
            transcript_markdown=transcript_markdown,
            structured_notes_markdown=(
                structured_notes_path.read_text(encoding="utf-8")
                if structured_notes_path.exists()
                else None
            ),
            timeline=timeline,
            knowledge_graph=knowledge_graph,
            storage_path=str(session_dir),
            post_class_artifacts=self._read_post_class_artifacts(session_dir),
            **self._read_post_class_status(session_dir),
        )

    def save_agent_artifacts(
        self,
        session_id: str,
        artifacts: list[dict],
    ) -> dict[str, Path]:
        """保存 Agent 生成的课后结构化产物。

        这是 Phase 4 的可选保存能力。调用方把“本次已经生成出来”的 artifact
        列表传进来，本方法只负责按类型落盘，不负责决定哪些技能应该被触发。
        因此文件是否出现，完全取决于传入的 artifact 类型：

        ``data/sessions/{session_id}/summary.md``
            课堂总结。结束课堂时会自动传入 summary artifact，因此通常会自动
            出现在已完成课堂目录里；用户手动重新总结时也会覆盖更新。

        ``data/sessions/{session_id}/todos.json``
            待办候选列表。结束课堂时会自动传入 todos artifact，保持 JSON
            方便前端或手机端后续结构化展示。

        ``data/sessions/{session_id}/quiz.json``
            自测题列表。不会在结束课堂时自动创建；只有用户在 AgentPanel
            主动点击“生成自测”或输入出题类 prompt，Agent 传入 quiz artifact
            后才会写出，避免无需求时提前生成练习题。

        ``data/sessions/{session_id}/agent_artifacts.json``
            各类型最新 artifact 的合并快照。结束课堂自动生成时先写入
            summary/todos；用户主动生成自测时再合并 quiz，而不是丢掉之前的
            summary/todos，便于调试和未来恢复 Agent 消息。

        这个方法只写已存在的历史课堂目录，不会为录制中的课堂提前创建
        session 目录。这样可以避免 ``data/sessions`` 里出现没有 metadata 的
        半成品目录，保持历史列表语义清晰。
        """
        session_dir = self._safe_session_dir(session_id)
        if not session_dir.exists() or not session_dir.is_dir():
            raise FileNotFoundError(f"Saved session not found: {session_id}")

        written: dict[str, Path] = {}
        for artifact in artifacts:
            artifact_type = str(artifact.get("type", "artifact"))
            content = artifact.get("content")

            if artifact_type == "summary":
                path = session_dir / "summary.md"
                self._write_text(path, self._artifact_text(content))
            elif artifact_type == "todos":
                path = session_dir / "todos.json"
                self._write_json(path, content if isinstance(content, (list, dict)) else [])
            elif artifact_type == "quiz":
                # quiz 的写入入口保留在 storage 层，但触发时机由调用方控制。
                # 当前业务规则是：结束课堂不传 quiz artifact；用户主动请求
                # Agent 出题后才传入 quiz artifact，于是这里才会创建 quiz.json。
                path = session_dir / "quiz.json"
                self._write_json(path, content if isinstance(content, (list, dict)) else [])
            else:
                # 未知产物不丢弃，保存为独立 JSON，便于未来扩展新技能时调试。
                path = session_dir / f"artifact_{artifact_type}.json"
                self._write_json(path, artifact)

            written[artifact_type] = path

        # 除了类型专属文件，也维护一个按 type 合并的 artifact 索引。这样用户
        # 结束课堂后先得到 summary/todos，稍后再主动生成 quiz 时，索引里会同时
        # 保留三类最新产物；若用户重新生成某一类，则用新的 artifact 覆盖旧值。
        snapshot_path = session_dir / "agent_artifacts.json"
        merged_artifacts = self._merge_agent_artifacts(snapshot_path, artifacts)
        self._write_json(snapshot_path, merged_artifacts)
        written["agent_artifacts"] = snapshot_path
        return written

    def save_post_class_status(
        self,
        session_id: str,
        status: PostClassStatus,
        *,
        warnings: list[str] | None = None,
        knowledge_graph_status: KnowledgeGraphStatus | None = None,
        stage: str | None = None,
        steps: dict[str, object] | None = None,
    ) -> Path:
        """Persist post-class background generation status for history recovery.

        This method intentionally requires an existing saved session directory.
        It must not recreate a classroom that the user has deleted while a
        background task was still running.
        """
        session_dir = self._safe_session_dir(session_id)
        if not session_dir.exists() or not session_dir.is_dir():
            raise FileNotFoundError(f"Saved session not found: {session_id}")

        path = session_dir / "post_class_status.json"
        self._write_json(
            path,
            {
                "status": status,
                "warnings": warnings or [],
                **({"stage": stage} if stage else {}),
                **({"steps": steps} if steps else {}),
                **(
                    {"knowledge_graph_status": knowledge_graph_status}
                    if knowledge_graph_status
                    else {}
                ),
                "updated_at": utc_now_iso(),
            },
        )
        return path

    def append_agent_messages(
        self,
        session_id: str,
        messages: list[dict],
    ) -> Path:
        """追加保存一轮 Agent 对话到 ``agent_messages.json``。

        只对已落盘历史课堂写入。正在录制的课堂还没有完整 metadata/timeline，
        不在这里提前创建目录，避免历史列表出现半成品课堂。
        """
        session_dir = self._safe_session_dir(session_id)
        if not session_dir.exists() or not session_dir.is_dir():
            raise FileNotFoundError(f"Saved session not found: {session_id}")

        path = session_dir / "agent_messages.json"
        existing = self._read_json_list(path) if path.exists() else []
        existing.extend(messages)
        self._write_json(path, existing)
        return path

    def save_global_search_index(self, documents: list[dict]) -> Path:
        """保存跨课堂搜索的本地全局索引快照。

        这不是向量库索引，而是 Phase 7 第一版的可审计文档快照：
        ``data/indexes/global/documents.json``。后续切到真正全局 LlamaIndex 时，
        这个目录仍可作为全局索引根目录。
        """
        index_dir = self.global_index_dir()
        index_dir.mkdir(parents=True, exist_ok=True)
        path = index_dir / "documents.json"
        self._write_json(path, documents)
        return path

    def delete_session(self, session_id: str) -> bool:
        """Delete one persisted session directory from local storage.

        历史课堂删除是一个真正的本地文件删除操作。为了避免调用方传入
        ``../`` 之类的路径逃出 ``data/sessions``，这里先把目标目录和
        base_dir 都 resolve，再确认目标目录仍位于 base_dir 内部。

        返回值：
          - True：目录存在且已删除。
          - False：目录不存在，或目标不是目录。

        API 层会把 False 映射为 404。这个方法不删除内存中的 SessionManager
        数据，因为历史删除只针对已落盘的课后档案。
        """
        session_dir = self._safe_session_dir(session_id)
        if not session_dir.exists() or not session_dir.is_dir():
            return False

        shutil.rmtree(session_dir)
        return True

    # ── 文件格式处理 ─────────────────────────────────────────

    def _render_transcript_markdown(self, context: ClassroomContext) -> str:
        """Render transcript segments into a readable Markdown file.

        Markdown 比纯 JSON 更适合学生课后直接打开阅读。每条字幕带课堂内
        起止时间，方便和 timeline / 图片回溯对齐。
        """
        lines = [
            f"# Transcript - {context.session_id}",
            "",
        ]

        if not context.transcript:
            lines.append("_No transcript segments recorded._")
            lines.append("")
            return "\n".join(lines)

        for segment in context.transcript:
            speaker = segment.speaker or "unknown"
            lines.append(
                f"- [{segment.start_ts:.2f}s - {segment.end_ts:.2f}s] "
                f"**{speaker}**: {segment.text}"
            )

        lines.append("")
        return "\n".join(lines)

    def _write_json(self, path: Path, data: object) -> None:
        """Write JSON with UTF-8 and stable pretty formatting."""
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_json(self, path: Path) -> dict:
        """Read one JSON file as a dictionary.

        metadata.json 和 knowledge_graph.json 的顶层结构都应是 object。
        如果未来某个调用点需要读取数组，请使用 _read_json_list()，
        这样文件格式错误会在 storage 层更清楚地暴露。
        """
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Expected JSON object in {path}")
        return data

    def _read_json_list(self, path: Path) -> list:
        """Read one JSON file as a list.

        timeline.json 的顶层结构是数组。这里单独做类型检查，是为了避免
        后续代码把 dict 当成可迭代条目列表时产生更难定位的错误。
        """
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"Expected JSON list in {path}")
        return data

    def _write_text(self, path: Path, content: str) -> None:
        """Write UTF-8 text content."""
        path.write_text(content, encoding="utf-8")

    def _artifact_text(self, content: object) -> str:
        """把 artifact content 转成适合写入 Markdown 的文本。

        总结技能通常返回字符串；如果未来某个技能返回结构化对象，这里退回到
        JSON pretty 格式，保证文件仍然可读。
        """
        if isinstance(content, str):
            return content if content.endswith("\n") else content + "\n"

        return json.dumps(content, ensure_ascii=False, indent=2) + "\n"

    def _image_extension(self, content_type: str | None) -> str:
        """Map accepted image content types to stable file extensions."""
        normalized = (content_type or "").split(";", 1)[0].strip().lower()
        if normalized == "image/png":
            return ".png"
        if normalized == "image/webp":
            return ".webp"
        if normalized in {"image/jpeg", "image/jpg", ""}:
            return ".jpg"
        raise ValueError(f"Unsupported image content type: {content_type}")

    def _read_post_class_artifacts(self, session_dir: Path) -> SessionPostClassArtifacts:
        """读取一节历史课堂的可选课后产物。

        旧版本保存的课堂目录通常没有 summary.md / todos.json / quiz.json。
        新规则下，quiz.json 也可能长期不存在，因为自测题需要用户主动生成。
        这里采用宽容读取策略：文件不存在就返回默认空值；文件存在但格式错误时
        仍然抛出 ValueError，让 API 层暴露数据损坏问题，避免前端展示半可信内容。
        """
        summary_path = session_dir / "summary.md"
        todos_path = session_dir / "todos.json"
        quiz_path = session_dir / "quiz.json"
        artifacts_path = session_dir / "agent_artifacts.json"
        messages_path = session_dir / "agent_messages.json"

        return SessionPostClassArtifacts(
            summary_markdown=(
                summary_path.read_text(encoding="utf-8")
                if summary_path.exists()
                else None
            ),
            todos=(
                self._read_json_list(todos_path)
                if todos_path.exists()
                else []
            ),
            quiz=(
                self._read_json_list(quiz_path)
                if quiz_path.exists()
                else []
            ),
            agent_artifacts=(
                self._read_json_list(artifacts_path)
                if artifacts_path.exists()
                else []
            ),
            agent_messages=(
                self._read_json_list(messages_path)
                if messages_path.exists()
                else []
            ),
        )

    def _read_post_class_status(self, session_dir: Path) -> dict[str, object]:
        """Read persisted post-class generation status.

        Old sessions do not have this file; treat them as ready so historical
        playback stays compatible.
        """
        status_path = session_dir / "post_class_status.json"
        if not status_path.exists():
            return {
                "post_class_status": "ready",
                "post_class_warnings": [],
                "knowledge_graph_status": "final",
            }

        data = self._read_json(status_path)
        status = data.get("status")
        if status not in {"idle", "generating", "ready", "failed"}:
            status = "failed"
        warnings = data.get("warnings")
        graph_status = data.get("knowledge_graph_status")
        if graph_status not in {"live", "finalizing", "final", "failed"}:
            graph_status = self._infer_legacy_knowledge_graph_status(
                status,
                warnings if isinstance(warnings, list) else [],
            )
        return {
            "post_class_status": status,
            "post_class_warnings": warnings if isinstance(warnings, list) else [],
            "knowledge_graph_status": graph_status,
        }

    @staticmethod
    def _infer_legacy_knowledge_graph_status(
        post_class_status: object,
        warnings: list[object],
    ) -> KnowledgeGraphStatus:
        """Infer graph finalization for status files written before this field existed."""
        if post_class_status == "generating":
            return "finalizing"
        warning_text = "\n".join(str(item) for item in warnings)
        if (
            post_class_status == "failed"
            or "Final Qwen structured notes failed" in warning_text
            or "Final knowledge extraction failed" in warning_text
        ):
            return "failed"
        return "final"

    def _merge_agent_artifacts(
        self,
        snapshot_path: Path,
        new_artifacts: list[dict],
    ) -> list[dict]:
        """把新 artifact 合并进已有 agent_artifacts.json 快照。

        合并规则按 artifact.type 去重：
        - 已存在同类型 artifact：用新内容覆盖，表示用户重新生成了该产物；
        - 新类型 artifact：追加到末尾，例如 summary/todos 之后追加 quiz；
        - 没有 type 的异常 artifact：保留为 ``artifact`` 类型，仍然可调试。

        如果旧快照文件不存在，说明这是该课堂第一次保存 Agent 产物，直接从空
        列表开始即可。若旧快照存在但不是 JSON list，_read_json_list() 会抛出
        ValueError，让调用方尽早发现本地文件损坏。
        """
        merged_by_type: dict[str, dict] = {}
        ordered_types: list[str] = []

        if snapshot_path.exists():
            for artifact in self._read_json_list(snapshot_path):
                if not isinstance(artifact, dict):
                    continue
                artifact_type = str(artifact.get("type", "artifact"))
                if artifact_type not in merged_by_type:
                    ordered_types.append(artifact_type)
                merged_by_type[artifact_type] = artifact

        for artifact in new_artifacts:
            artifact_type = str(artifact.get("type", "artifact"))
            if artifact_type not in merged_by_type:
                ordered_types.append(artifact_type)
            merged_by_type[artifact_type] = artifact

        return [merged_by_type[artifact_type] for artifact_type in ordered_types]

    def _safe_session_dir(self, session_id: str) -> Path:
        """Return a resolved session dir and ensure it stays under base_dir."""
        base_dir = self.base_dir.resolve()
        session_dir = (self.base_dir / session_id).resolve()

        try:
            session_dir.relative_to(base_dir)
        except ValueError as exc:
            raise ValueError(f"Unsafe session_id path: {session_id}") from exc

        return session_dir


local_storage = LocalStorage()
"""Default storage instance used by API routes."""
