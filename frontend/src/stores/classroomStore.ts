import type {
  ClassroomDashboardState,
  ContextUpdate,
  GraphPatch,
  GraphPatchOperation,
  ImageCapture,
  KnowledgeEdge,
  KnowledgeGraphView,
  KnowledgeGraphStatus,
  KnowledgeNode,
  LectureSession,
  PostClassStatus,
  SessionHistoryDetail,
  SessionPostClassArtifacts,
  TimelineItem,
  TranscriptSegment,
  WebSocketMessage,
  WebSocketStatus,
} from "../types/classroom";

// 前端课堂 store。
//
// 这个文件集中维护“后端实时消息 -> 前端页面状态”的转换规则。
// App 负责发起 API、建立 WebSocket 和 dispatch action；store 负责纯数据归并。
// 这样后续新增字幕滚动、图谱渲染、历史课程等功能时，不需要把状态处理逻辑
// 堆在 App.tsx 里。

export const initialDashboardState: ClassroomDashboardState = {
  session: null,
  websocketStatus: "disconnected",
  eventCount: 0,
  transcript: [],
  partialTranscript: null,
  timeline: [],
  visuals: [],
  graph: {
    nodes: [],
    edges: [],
    version: 0,
  },
  knowledgeGraphStatus: "live",
  postClassStatus: "idle",
  postClassArtifacts: {
    summary_markdown: null,
    todos: [],
    quiz: [],
    agent_artifacts: [],
    agent_messages: [],
  },
};

export type ClassroomAction =
  | {
      type: "session.started";
      session: LectureSession;
    }
  | {
      type: "session.ended";
      session: LectureSession;
    }
  | {
      type: "session.updated";
      session: LectureSession;
    }
  | {
      type: "websocket.statusChanged";
      status: WebSocketStatus;
    }
  | {
      type: "websocket.messageReceived";
      message: WebSocketMessage;
    }
  | {
      // 历史详情加载完成后进入同一个 reducer。这样四个展示面板不用关心
      // 数据来自 WebSocket 实时流，还是来自本地历史文件快照。
      type: "history.loaded";
      detail: SessionHistoryDetail;
    }
  | {
      // 删除历史课成功后通知 reducer。如果右侧正在展示被删课堂，就清空
      // dashboard；否则保持当前看板不变。
      type: "history.deleted";
      sessionId: string;
    }
  | {
      // 用户在课后产物面板主动生成自测后，直接把 Agent 返回的 quiz artifact
      // 合并进当前看板，避免必须重新打开历史课堂才能看到 quiz.json 内容。
      type: "post_class.quiz.generated";
      quiz: Array<Record<string, unknown>>;
    };

export function classroomReducer(
  state: ClassroomDashboardState,
  action: ClassroomAction,
): ClassroomDashboardState {
  switch (action.type) {
    case "session.started":
      // 开始新课堂时清空上一节课的实时数据，但保留新的 session。
      // WebSocket 随后会进入 connecting/connected。
      return {
        ...initialDashboardState,
        session: action.session,
      };

    case "session.ended":
      // 结束课堂由 HTTP 响应或 WebSocket session.ended 广播触发。
      // 保留现有字幕/时间线/图谱，方便用户结束后继续查看本次课堂结果。
      return {
        ...state,
        session: action.session,
        websocketStatus: "disconnected",
        knowledgeGraphStatus: "finalizing",
        postClassStatus: "generating",
      };

    case "session.updated":
      return {
        ...state,
        session: action.session,
      };

    case "websocket.statusChanged":
      return {
        ...state,
        websocketStatus: action.status,
      };

    case "websocket.messageReceived":
      return applyWebSocketMessage(state, action.message);

    case "history.loaded":
      // 打开历史课时，旧实时课堂状态会被完整替换为历史快照。
      // 左侧历史列表的加载/选中状态不放在这里，仍由 App 管理。
      return applyHistoryDetail(action.detail);

    case "history.deleted":
      if (state.session?.session_id !== action.sessionId) {
        return state;
      }

      return initialDashboardState;

    case "post_class.quiz.generated":
      return {
        ...state,
        postClassStatus:
          state.postClassStatus === "idle" ? "ready" : state.postClassStatus,
        postClassArtifacts: {
          ...state.postClassArtifacts,
          quiz: action.quiz,
        },
      };
  }
}

function applyHistoryDetail(detail: SessionHistoryDetail): ClassroomDashboardState {
  // 历史详情和实时消息的形态不同：
  // - 实时模式通过 event.received 一条条推送 transcript/image/graph_patch。
  // - 历史模式直接返回 timeline + knowledge_graph 完整快照。
  //
  // 为了复用现有四个面板，这里把历史快照转换成 ClassroomDashboardState：
  // - transcript 从 timeline 中 type=transcript 的 data 反推。
  // - visuals 从 timeline 中 type=visual 的 data 反推。
  // - graph 直接使用 knowledge_graph 的最终 nodes/edges/version。
  // - websocketStatus 固定为 disconnected，因为历史课是只读档案。
  return {
    session: detail.session,
    websocketStatus: "disconnected",
    eventCount: detail.timeline.length,
    transcript: extractTranscriptFromTimeline(detail.timeline),
    partialTranscript: null,
    timeline: detail.timeline,
    visuals: extractVisualsFromTimeline(detail.timeline),
    graph: {
      nodes: detail.knowledge_graph.nodes,
      edges: detail.knowledge_graph.edges,
      version: detail.knowledge_graph.version,
    },
    knowledgeGraphStatus: parseKnowledgeGraphStatus(
      detail.knowledge_graph_status,
      detail.post_class_status,
    ),
    postClassArtifacts: detail.post_class_artifacts ?? emptyPostClassArtifacts(),
    postClassStatus: parsePostClassStatus(detail.post_class_status),
  };
}

function emptyPostClassArtifacts(): SessionPostClassArtifacts {
  return {
    summary_markdown: null,
    todos: [],
    quiz: [],
    agent_artifacts: [],
    agent_messages: [],
  };
}

function applyWebSocketMessage(
  state: ClassroomDashboardState,
  message: WebSocketMessage,
): ClassroomDashboardState {
  // ws.connected 是后端对订阅成功的确认。浏览器 onopen 也会把状态设为
  // connected，两处都设置是幂等的，能覆盖“onopen 已到但后端确认稍后到”的场景。
  if (message.type === "ws.connected") {
    return {
      ...state,
      websocketStatus: "connected",
    };
  }

  if (message.type === "session.ended") {
    return {
      ...applySessionEndedMessage(state, message),
      partialTranscript: null,
    };
  }

  if (message.type === "session.updated") {
    return applySessionUpdatedMessage(state, message);
  }

  if (message.type === "transcript.preview") {
    return applyTranscriptPreviewMessage(state, message);
  }

  if (message.type === "post_class.updated") {
    return applyPostClassUpdatedMessage(state, message);
  }

  if (message.type === "event.received") {
    return applyEventReceivedMessage(state, message);
  }

  // session.started 通常发生在前端连接 WebSocket 之前，当前页面不依赖它。
  return state;
}

function applyPostClassUpdatedMessage(
  state: ClassroomDashboardState,
  message: WebSocketMessage,
): ClassroomDashboardState {
  const status = parsePostClassStatus(message.data.status);
  const artifacts = parsePostClassArtifacts(message.data.post_class_artifacts);
  const session = readObject(message.data.session);

  return {
    ...state,
    session:
      state.session && session && typeof session.session_id === "string"
        ? {
            ...state.session,
            ...session,
          }
        : state.session,
    postClassStatus: status,
    knowledgeGraphStatus: parseKnowledgeGraphStatus(
      message.data.knowledge_graph_status,
      status,
      state.knowledgeGraphStatus,
    ),
    postClassArtifacts: artifacts ?? state.postClassArtifacts,
  };
}

function parsePostClassStatus(value: unknown): PostClassStatus {
  return value === "ready" || value === "failed" || value === "generating"
    ? value
    : "ready";
}

function parseKnowledgeGraphStatus(
  value: unknown,
  postClassStatus?: unknown,
  fallback: KnowledgeGraphStatus = "final",
): KnowledgeGraphStatus {
  if (
    value === "live" ||
    value === "finalizing" ||
    value === "final" ||
    value === "failed"
  ) {
    return value;
  }
  if (postClassStatus === "generating") {
    return "finalizing";
  }
  if (postClassStatus === "failed") {
    return "failed";
  }
  return fallback;
}

function parsePostClassArtifacts(value: unknown): SessionPostClassArtifacts | null {
  const data = readObject(value);
  if (!data) {
    return null;
  }
  return {
    summary_markdown:
      typeof data.summary_markdown === "string" ? data.summary_markdown : null,
    todos: Array.isArray(data.todos) ? data.todos.flatMap(readRecord) : [],
    quiz: Array.isArray(data.quiz) ? data.quiz.flatMap(readRecord) : [],
    agent_artifacts: Array.isArray(data.agent_artifacts)
      ? data.agent_artifacts.flatMap(readRecord)
      : [],
    agent_messages: Array.isArray(data.agent_messages)
      ? data.agent_messages.flatMap(readRecord)
      : [],
  };
}

function applyTranscriptPreviewMessage(
  state: ClassroomDashboardState,
  message: WebSocketMessage,
): ClassroomDashboardState {
  const payload = readObject(message.data.payload);
  if (!isTranscriptSegment(payload)) {
    return state;
  }

  return {
    ...state,
    eventCount:
      typeof message.data.event_count === "number"
        ? message.data.event_count
        : state.eventCount,
    partialTranscript: {
      ...payload,
      is_final: false,
    },
  };
}

function applySessionUpdatedMessage(
  state: ClassroomDashboardState,
  message: WebSocketMessage,
): ClassroomDashboardState {
  const session = readObject(message.data.session);
  if (!session || typeof session.session_id !== "string") {
    return state;
  }

  return {
    ...state,
    session: state.session
      ? {
          ...state.session,
          ...session,
        }
      : state.session,
  };
}

function applySessionEndedMessage(
  state: ClassroomDashboardState,
  message: WebSocketMessage,
): ClassroomDashboardState {
  const session = readObject(message.data.session);

  if (!session || typeof session.session_id !== "string") {
    return {
      ...state,
      websocketStatus: "disconnected",
    };
  }

  // 后端的 session.ended 广播可能只带部分 session 字段。这里与当前 session
  // 合并，避免因为广播字段较少而丢掉 title/course 等前端正在展示的信息。
  return {
    ...state,
    session: state.session
      ? {
          ...state.session,
          ...session,
          status: "ended",
        }
      : state.session,
    websocketStatus: "disconnected",
    knowledgeGraphStatus: parseKnowledgeGraphStatus(
      readObject(message.data.storage)?.knowledge_graph_status,
      readObject(message.data.storage)?.post_class_status,
      "finalizing",
    ),
  };
}

function applyEventReceivedMessage(
  state: ClassroomDashboardState,
  message: WebSocketMessage,
): ClassroomDashboardState {
  const data = message.data;
  const eventType = typeof data.event_type === "string" ? data.event_type : "";
  const payload = readObject(data.payload);
  const contextUpdate = parseContextUpdate(data.context_update);
  const graphPatch = parseGraphPatch(data.graph_patch);
  const eventCount = typeof data.event_count === "number" ? data.event_count : state.eventCount;

  let nextState: ClassroomDashboardState = {
    ...state,
    eventCount,
  };

  if (contextUpdate) {
    nextState = {
      ...nextState,
      timeline: upsertById(nextState.timeline, contextUpdate.timeline_item, "item_id"),
    };
  }

  // 优先使用 ContextManager 标准化后的 timeline_item.data；如果缺失，则回退到
  // 原始 payload。这样前端能兼容不同事件来源和后端默认补齐字段。
  const timelineData = readObject(contextUpdate?.timeline_item.data);
  const normalizedPayload =
    timelineData && Object.keys(timelineData).length > 0 ? timelineData : payload;

  if (eventType === "transcript.segment" && isTranscriptSegment(normalizedPayload)) {
    nextState = {
      ...nextState,
      transcript: upsertById(nextState.transcript, normalizedPayload, "segment_id"),
      partialTranscript: null,
    };
  }

  if (eventType === "image.capture" && isImageCapture(normalizedPayload)) {
    nextState = {
      ...nextState,
      visuals: upsertById(nextState.visuals, normalizedPayload, "image_id"),
    };
  }

  if (graphPatch) {
    nextState = {
      ...nextState,
      graph: applyGraphPatch(nextState.graph, graphPatch),
    };
  }

  return nextState;
}

function applyGraphPatch(graph: KnowledgeGraphView, patch: GraphPatch): KnowledgeGraphView {
  // GraphPatch 是有序操作流。必须按后端给出的顺序应用，因为 relation 可能引用
  // 同一个 patch 中刚刚创建的占位节点。
  return patch.operations.reduce(
    (currentGraph, operation) => applyGraphOperation(currentGraph, operation),
    {
      ...graph,
      version: patch.to_version,
    },
  );
}

function applyGraphOperation(
  graph: KnowledgeGraphView,
  operation: GraphPatchOperation,
): KnowledgeGraphView {
  if ((operation.op === "add_node" || operation.op === "update_node") && operation.node) {
    return {
      ...graph,
      nodes: upsertById(graph.nodes, operation.node, "node_id"),
    };
  }

  if (operation.op === "add_edge" && operation.edge) {
    return {
      ...graph,
      edges: upsertById(graph.edges, operation.edge, "edge_id"),
    };
  }

  if (operation.op === "remove_node" && operation.node) {
    return {
      ...graph,
      nodes: graph.nodes.filter((node) => node.node_id !== operation.node?.node_id),
      edges: graph.edges.filter(
        (edge) =>
          edge.source !== operation.node?.node_id && edge.target !== operation.node?.node_id,
      ),
    };
  }

  if (operation.op === "remove_edge" && operation.edge) {
    return {
      ...graph,
      edges: graph.edges.filter((edge) => edge.edge_id !== operation.edge?.edge_id),
    };
  }

  return graph;
}

function upsertById<T extends Record<K, string>, K extends keyof T>(
  items: T[],
  nextItem: T,
  key: K,
): T[] {
  const index = items.findIndex((item) => item[key] === nextItem[key]);

  if (index === -1) {
    return [...items, nextItem];
  }

  return items.map((item, itemIndex) =>
    itemIndex === index
      ? {
          ...item,
          ...nextItem,
        }
      : item,
  );
}

function parseContextUpdate(value: unknown): ContextUpdate | null {
  const data = readObject(value);
  const timelineItem = parseTimelineItem(data?.timeline_item);

  if (!data || !timelineItem || typeof data.session_id !== "string") {
    return null;
  }

  return {
    session_id: data.session_id,
    event_type: typeof data.event_type === "string" ? data.event_type : "",
    timeline_item: timelineItem,
    transcript_count: readNumber(data.transcript_count),
    visual_count: readNumber(data.visual_count),
    knowledge_extraction_count: readNumber(data.knowledge_extraction_count),
  };
}

function parseTimelineItem(value: unknown): TimelineItem | null {
  const data = readObject(value);

  if (
    !data ||
    typeof data.item_id !== "string" ||
    typeof data.session_id !== "string" ||
    typeof data.type !== "string" ||
    typeof data.title !== "string"
  ) {
    return null;
  }

  const timelineType = data.type;

  if (!["transcript", "visual", "knowledge"].includes(timelineType)) {
    return null;
  }

  return {
    item_id: data.item_id,
    session_id: data.session_id,
    type: timelineType as TimelineItem["type"],
    ts: readNumber(data.ts),
    title: data.title,
    data: readObject(data.data) ?? {},
  };
}

function parseGraphPatch(value: unknown): GraphPatch | null {
  const data = readObject(value);

  if (!data || typeof data.session_id !== "string" || !Array.isArray(data.operations)) {
    return null;
  }

  return {
    session_id: data.session_id,
    from_version: readNumber(data.from_version),
    to_version: readNumber(data.to_version),
    operations: data.operations.flatMap((operation) => {
      const parsedOperation = parseGraphPatchOperation(operation);
      return parsedOperation ? [parsedOperation] : [];
    }),
  };
}

function parseGraphPatchOperation(value: unknown): GraphPatchOperation | null {
  const data = readObject(value);

  if (!data || typeof data.op !== "string") {
    return null;
  }

  const operationType = data.op;

  if (
    ![
      "add_node",
      "update_node",
      "add_edge",
      "remove_node",
      "remove_edge",
    ].includes(operationType)
  ) {
    return null;
  }

  return {
    op: operationType as GraphPatchOperation["op"],
    node: parseKnowledgeNode(data.node),
    edge: parseKnowledgeEdge(data.edge),
    data: readObject(data.data) ?? {},
  };
}

function parseKnowledgeNode(value: unknown): KnowledgeNode | null {
  const data = readObject(value);

  if (!data || typeof data.node_id !== "string" || typeof data.label !== "string") {
    return null;
  }

  return {
    node_id: data.node_id,
    label: data.label,
    type: typeof data.type === "string" ? data.type : "concept",
    summary: typeof data.summary === "string" ? data.summary : null,
    level: typeof data.level === "number" ? data.level : undefined,
    importance: typeof data.importance === "number" ? data.importance : null,
    source_refs: parseSourceRefs(data.source_refs),
  };
}

function parseKnowledgeEdge(value: unknown): KnowledgeEdge | null {
  const data = readObject(value);

  if (
    !data ||
    typeof data.edge_id !== "string" ||
    typeof data.source !== "string" ||
    typeof data.target !== "string" ||
    typeof data.relation !== "string"
  ) {
    return null;
  }

  return {
    edge_id: data.edge_id,
    source: data.source,
    target: data.target,
    relation: data.relation,
    source_refs: parseSourceRefs(data.source_refs),
  };
}

function parseSourceRefs(value: unknown) {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((item) => {
    const data = readObject(item);
    if (!data || typeof data.type !== "string" || typeof data.id !== "string") {
      return [];
    }
    return [
      {
        type: data.type,
        id: data.id,
        ts: typeof data.ts === "number" ? data.ts : null,
        text: typeof data.text === "string" ? data.text : null,
      },
    ];
  });
}

function isTranscriptSegment(value: unknown): value is TranscriptSegment {
  const data = readObject(value);

  return (
    !!data &&
    typeof data.segment_id === "string" &&
    typeof data.text === "string" &&
    typeof data.start_ts === "number" &&
    typeof data.end_ts === "number"
  );
}

function isImageCapture(value: unknown): value is ImageCapture {
  const data = readObject(value);

  return (
    !!data &&
    typeof data.image_id === "string" &&
    typeof data.capture_ts === "number" &&
    typeof data.image_path === "string" &&
    typeof data.status === "string"
  );
}

function extractTranscriptFromTimeline(timeline: TimelineItem[]): TranscriptSegment[] {
  // LocalStorage 保存 transcript.md 主要给人读；前端面板更适合消费结构化
  // TranscriptSegment。ContextManager 在保存 timeline 时会把标准化片段放进
  // timeline_item.data，所以历史回放可以从这里还原字幕列表。
  return timeline.flatMap((item) => {
    if (item.type !== "transcript" || !isTranscriptSegment(item.data)) {
      return [];
    }

    return [item.data];
  });
}

function extractVisualsFromTimeline(timeline: TimelineItem[]): ImageCapture[] {
  // 图片/OCR 面板依赖 ImageCapture。历史详情没有单独返回 visuals 数组，
  // 因此从 type=visual 的 timeline item 中还原。遇到不完整的历史数据时
  // 直接跳过该项，时间线本身仍会展示出来，页面不会整体崩掉。
  return timeline.flatMap((item) => {
    if (item.type !== "visual" || !isImageCapture(item.data)) {
      return [];
    }

    return [item.data];
  });
}

function readObject(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }

  return null;
}

function readRecord(value: unknown): Array<Record<string, unknown>> {
  const data = readObject(value);
  return data ? [data] : [];
}

function readNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}
