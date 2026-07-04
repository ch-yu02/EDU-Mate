// 前端共享类型定义。
//
// 这里的字段尽量与 docs/API_SCHEMA.md 和后端 Pydantic model 保持一致。
// 注意：后端 API 使用 snake_case，前端也保留 snake_case，减少接口对接时的
// 序列化/反序列化转换成本。

// POST /sessions/start 和 /sessions/{id}/end 返回的课堂元信息。
export type LectureSession = {
  session_id: string;
  title: string;
  course?: string | null;
  teacher?: string | null;
  start_time: string;
  end_time?: string | null;
  status: "recording" | "ended";
  language: string;
  created_by: string;
  device_id?: string | null;
};

// WebSocket 连接状态只描述前端连接生命周期，不等同于课堂 session.status。
export type WebSocketStatus = "disconnected" | "connecting" | "connected" | "error";

export type PostClassStatus = "idle" | "generating" | "ready" | "failed";
export type KnowledgeGraphStatus = "live" | "finalizing" | "final" | "failed";

// ASR 字幕片段，对应 event_type = "transcript.segment"。
export type TranscriptSegment = {
  segment_id: string;
  session_id?: string;
  start_ts: number;
  end_ts: number;
  text: string;
  speaker?: string | null;
  confidence?: number | null;
  is_final?: boolean;
  source?: string | null;
  created_at?: string;
};

// 图片、OCR 或 VLM 视觉事件，对应 event_type = "image.capture"。
export type ImageCapture = {
  image_id: string;
  session_id?: string;
  capture_ts: number;
  upload_time?: string;
  image_path: string;
  source?: string | null;
  image_type?: string | null;
  status: string;
  ocr_text?: string | null;
  caption?: string | null;
  visual_text?: string[];
  key_points?: string[];
};

// ContextManager 生成的统一时间线条目。
// 三类实时事件都会归一成 TimelineItem，方便前端只维护一条课堂流。
export type TimelineItem = {
  item_id: string;
  session_id: string;
  type: "transcript" | "visual" | "knowledge";
  ts: number;
  title: string;
  data: Record<string, unknown>;
  created_at?: string;
};

export type SourceRef = {
  type: string;
  id: string;
  ts?: number | null;
  text?: string | null;
};

// 知识图谱节点。字段来自 GraphPatch operation 中的 node。
export type KnowledgeNode = {
  node_id: string;
  label: string;
  type: string;
  summary?: string | null;
  level?: number | null;
  importance?: number | null;
  source_refs?: SourceRef[];
};

// 知识图谱关系。字段来自 GraphPatch operation 中的 edge。
export type KnowledgeEdge = {
  edge_id: string;
  source: string;
  target: string;
  relation: string;
  source_refs?: SourceRef[];
};

// 前端渲染用的图谱缓存。
// 后端按版本推送 patch，前端归并后得到这一份 view model。
export type KnowledgeGraphView = {
  nodes: KnowledgeNode[];
  edges: KnowledgeEdge[];
  version: number;
};

// 后端保存到 knowledge_graph.json 的完整图谱快照。
// 历史详情读取的是完整快照，不是实时 graph_patch。
export type KnowledgeTree = KnowledgeGraphView & {
  // 这三个字段来自后端 KnowledgeTree。当前前端图谱面板只需要 nodes/edges/version，
  // 但保留 session_id/root_nodes/updated_at 可以让后续历史详情页、树形目录或
  // post-class skill 不必重新改 API 类型。
  session_id: string;
  root_nodes: string[];
  updated_at?: string;
};

// GET /sessions 的单条历史课程摘要。
export type SessionHistorySummary = {
  // 后端 metadata.json 反序列化出的课堂元信息。
  session: LectureSession;
  // 后端通过 timeline.json 长度计算，用来在列表里快速显示课堂内容量。
  event_count: number;
  // 本地存储目录，主要用于后续本地文件定位；UI 不依赖它拼文件路径。
  storage_path: string;
};

// GET /sessions 的响应体。
export type SessionHistoryListResponse = {
  sessions: SessionHistorySummary[];
};

// DELETE /sessions/{session_id}/history 的响应体。
export type SessionDeleteResponse = {
  status: "deleted";
  session_id: string;
};

// GET /sessions/{session_id}/history 的响应体。
export type SessionHistoryDetail = {
  // metadata.json
  session: LectureSession;
  // transcript.md。当前看板的字幕列表从 timeline.data 还原，
  // 这个 Markdown 字段保留给后续“全文阅读/导出/总结”视图。
  transcript_markdown: string;
  // structured_notes.md。由 WhisperLive/Qwen 实时维护，供课后阅读和 Agent/RAG 查询。
  structured_notes_markdown?: string | null;
  // timeline.json。历史看板的时间线、字幕和视觉内容都从这里派生。
  timeline: TimelineItem[];
  // knowledge_graph.json。历史图谱直接展示结束课堂时的最终快照。
  knowledge_graph: KnowledgeTree;
  // 本地 session 目录路径，用于调试和未来打开本地资源。
  storage_path: string;
  // 课后产物。结束课堂时后端会自动生成；旧历史课堂可能为空。
  post_class_artifacts?: SessionPostClassArtifacts;
  // 课后产物后台生成状态。旧历史课堂可能没有该字段，前端按 ready 兼容。
  post_class_status?: PostClassStatus;
  // 课后产物生成过程中的失败原因或提示。
  post_class_warnings?: string[];
  // 知识图谱最终化状态。final 表示最终全量图谱抽取已经完成。
  knowledge_graph_status?: KnowledgeGraphStatus;
};

// 历史课堂目录中的课后产物。
export type SessionPostClassArtifacts = {
  // summary.md 的内容。
  summary_markdown?: string | null;
  // todos.json 的内容。
  todos: Array<Record<string, unknown>>;
  // quiz.json 的内容。
  quiz: Array<Record<string, unknown>>;
  // agent_artifacts.json 的完整 artifact 快照。
  agent_artifacts: Array<Record<string, unknown>>;
  // agent_messages.json 的历史 Agent 对话。
  agent_messages: Array<Record<string, unknown>>;
};

// 单条图谱增量操作。
// 前端 reducer 应按 operations 顺序执行，保证节点先于边创建。
export type GraphPatchOperation = {
  op: "add_node" | "update_node" | "add_edge" | "remove_node" | "remove_edge";
  node?: KnowledgeNode | null;
  edge?: KnowledgeEdge | null;
  data?: Record<string, unknown>;
};

// KnowledgeGraphManager 对一次 knowledge.extraction 产生的增量补丁。
export type GraphPatch = {
  session_id: string;
  from_version: number;
  to_version: number;
  operations: GraphPatchOperation[];
};

// ContextManager 对一次实时事件产生的上下文更新摘要。
// event.received 消息中的 context_update 字段就是这个结构。
export type ContextUpdate = {
  session_id: string;
  event_type: string;
  timeline_item: TimelineItem;
  transcript_count: number;
  visual_count: number;
  knowledge_extraction_count: number;
};

// 后端 WebSocket 统一消息信封。
// data 会随 type 不同而变化，后续接 reducer 时再按 type 做窄化解析。
export type WebSocketMessage = {
  type:
    | "ws.connected"
    | "session.started"
    | "session.updated"
    | "transcript.preview"
    | "event.received"
    | "post_class.updated"
    | "session.ended";
  session_id: string;
  data: Record<string, unknown>;
  created_at: string;
};

// 当前课堂页面的整体状态。
// 这是 App 的本地 state 形状，也是未来 src/stores/ 可以直接迁移的基础。
export type ClassroomDashboardState = {
  session: LectureSession | null;
  websocketStatus: WebSocketStatus;
  eventCount: number;
  transcript: TranscriptSegment[];
  partialTranscript: TranscriptSegment | null;
  timeline: TimelineItem[];
  visuals: ImageCapture[];
  graph: KnowledgeGraphView;
  knowledgeGraphStatus: KnowledgeGraphStatus;
  postClassStatus: PostClassStatus;
  postClassArtifacts: SessionPostClassArtifacts;
};
