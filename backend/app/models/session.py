"""课堂会话模型 —— 定义课堂会话的创建请求和状态表示。

该模块提供：
1. 会话生命周期管理（'recording' → 'ended'）的状态类型
2. 前端发起课堂时的请求模型 ``StartSessionRequest``
3. 核心的 ``LectureSession`` 模型，在 HTTP API、WebSocket
   和持久化存储之间共享
"""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .context import TimelineItem
from .knowledge import KnowledgeTree


# ── 类型别名 ────────────────────────────────────────────────────

SessionStatus = Literal["recording", "ended"]
"""课堂会话的生命周期状态。

- ``"recording"`` —— 正在录制/进行中
- ``"ended"``     —— 已结束
"""

PostClassStatus = Literal["idle", "generating", "ready", "failed"]
"""课后产物生成状态。"""

KnowledgeGraphStatus = Literal["live", "finalizing", "final", "failed"]
"""知识图谱最终化状态。"""


# ── 工具函数 ────────────────────────────────────────────────────

def new_session_id() -> str:
    """生成可读的本地会话 ID，用于演示、日志和文件路径。

    ID 格式为 ``lec_{UTC时间戳}_{UUID前缀}``，兼顾可读性和唯一性：
      - 时间戳部分（``YYYYmmdd_HHMMSS``）便于在文件系统中按时间排序
      - UUID 前缀（8位十六进制）确保高并发下不冲突

    Returns:
        str: 如 ``lec_20260602_143021_a1b2c3d4``

    Note:
        Demo 阶段由后端本地生成。后续若对接硬件设备 ID 或外部存储，
        ``SessionManager`` 可能改为接受外部传入的 ID。
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = uuid4().hex[:8]
    return f"lec_{timestamp}_{suffix}"


# ── 会话模型 ────────────────────────────────────────────────────

class StartSessionRequest(BaseModel):
    """前端发起课堂会话的请求体模型。

    Attributes:
        title:      课堂标题，UI 侧默认为"未命名课堂"
        course:     课程名称（可选，如"高等数学"）
        teacher:    教师姓名（可选）
        language:   课堂使用的语言代码，默认 "zh-CN"
        created_by: 创建者角色，默认 "student"（课堂由学生端发起）
        device_id:  发起设备的硬件标识（可选，后续用于多端配对）
    """

    title: str = Field(default="未命名课堂")
    """课堂标题。"""
    course: str | None = None
    """课程名称（可选）。"""
    teacher: str | None = None
    """教师姓名（可选）。"""
    language: str = Field(default="zh-CN")
    """课堂语言代码，默认中文。"""
    created_by: str = Field(default="student")
    """创建者身份标识（student / teacher），默认学生端发起。"""
    device_id: str | None = None
    """发起设备的硬件标识（可选），后续用于多设备配对。"""


class UpdateSessionRequest(BaseModel):
    """更新课堂元信息的请求体模型。"""

    title: str | None = Field(default=None)
    """课堂标题。省略表示不修改；空字符串会被 API 拒绝。"""
    course: str | None = Field(default=None)
    """课程名称。省略表示不修改；传 null 或空字符串表示清空。"""


class LectureSession(BaseModel):
    """课堂会话的完整数据模型，在 HTTP API、WebSocket 和存储之间共享。

    一条会话记录代表一次完整的课堂录制周期，从创建（recording）
    到结束（ended）。所有时间戳统一使用 ``utc_now_iso()`` 的
    ISO-8601 格式。

    Attributes:
        session_id: 全局唯一会话 ID（由 new_session_id() 生成）
        title:      课堂标题
        course:     课程名称
        teacher:    教师姓名
        start_time: 课堂开始时间（ISO-8601 格式）
        end_time:   课堂结束时间（结束前为 None）
        status:     当前状态（recording / ended）
        language:   课堂语言
        created_by: 创建者
        device_id:  发起设备标识
    """

    session_id: str
    """全局唯一的会话标识符。"""
    title: str
    """课堂标题。"""
    course: str | None = None
    """课程名称。"""
    teacher: str | None = None
    """教师姓名。"""
    start_time: str
    """课堂开始时间的 ISO-8601 字符串。"""
    end_time: str | None = None
    """课堂结束时间（会话进行中为 None）。"""
    status: SessionStatus
    """会话当前状态。"""
    language: str
    """课堂使用的语言代码。"""
    created_by: str
    """创建者身份标识。"""
    device_id: str | None = None
    """发起设备的硬件标识。"""


class SessionHistorySummary(BaseModel):
    """历史课堂列表中的单条摘要。"""

    session: LectureSession
    """课堂元信息。"""
    event_count: int = 0
    """已保存时间线条目数，用于列表快速展示课堂内容量。"""
    storage_path: str
    """本地历史课堂目录路径。"""


class SessionHistoryListResponse(BaseModel):
    """历史课堂列表响应。"""

    sessions: list[SessionHistorySummary] = Field(default_factory=list)
    """按开始时间倒序排列的历史课堂摘要。"""


class SessionDeleteResponse(BaseModel):
    """删除历史课堂后的响应。"""

    status: Literal["deleted"]
    """删除状态。当前只有 deleted，便于前端做明确分支。"""
    session_id: str
    """被删除的历史课堂 ID。"""


class SessionHistoryDetail(BaseModel):
    """单节历史课堂的完整读取结果。"""

    session: LectureSession
    """课堂元信息。"""
    transcript_markdown: str
    """保存到 transcript.md 的人可读字幕记录。"""
    structured_notes_markdown: str | None = None
    """保存到 structured_notes.md 的 Qwen 结构化课堂笔记。旧课堂可能为空。"""
    timeline: list[TimelineItem] = Field(default_factory=list)
    """保存到 timeline.json 的统一时间线。"""
    knowledge_graph: KnowledgeTree
    """保存到 knowledge_graph.json 的完整知识图谱快照。"""
    storage_path: str
    """本地历史课堂目录路径。"""
    post_class_artifacts: "SessionPostClassArtifacts" = Field(
        default_factory=lambda: SessionPostClassArtifacts()
    )
    """课后产物读取结果，包括 summary/todos/quiz/agent_artifacts。"""
    post_class_status: PostClassStatus = "ready"
    """课后产物生成状态。"""
    post_class_warnings: list[str] = Field(default_factory=list)
    """课后产物生成过程中的非致命提示或失败原因。"""
    knowledge_graph_status: KnowledgeGraphStatus = "final"
    """知识图谱状态；final 表示最终全量图谱抽取已经完成。"""


class SessionPostClassArtifacts(BaseModel):
    """历史课堂目录中的可选课后产物。

    这些文件不是结束课堂主链路的必要条件。旧历史课堂可能没有这些文件，因此
    字段都提供默认值，让前端可以安全区分“没有生成”和“读取失败”。
    """

    summary_markdown: str | None = None
    """summary.md 的文本内容。"""
    todos: list[dict[str, Any]] = Field(default_factory=list)
    """todos.json 的结构化待办候选。"""
    quiz: list[dict[str, Any]] = Field(default_factory=list)
    """quiz.json 的结构化自测题。"""
    agent_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    """agent_artifacts.json 中保存的完整 Agent artifact 快照。"""
    agent_messages: list[dict[str, Any]] = Field(default_factory=list)
    """agent_messages.json 中保存的历史 Agent 对话。"""
