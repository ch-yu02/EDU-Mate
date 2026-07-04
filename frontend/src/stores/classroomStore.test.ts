import { describe, expect, it } from "vitest";

import { classroomReducer, initialDashboardState } from "./classroomStore";
import type { WebSocketMessage } from "../types/classroom";

// Store smoke tests.
//
// 这些用例不依赖浏览器、不连接真实后端，只验证 reducer 是否能正确消费
// 后端 WebSocket 的 event.received 信封。真实运行时，WebSocket service
// 只负责把 JSON 解析成 WebSocketMessage，然后 dispatch 给同一个 reducer。

describe("classroomReducer", () => {
  it("stores transcript event.received updates", () => {
    const message = eventReceivedMessage({
      event_type: "transcript.segment",
      event_count: 1,
      payload: {},
      context_update: {
        session_id: "lec_test",
        event_type: "transcript.segment",
        timeline_item: {
          item_id: "seg_001",
          session_id: "lec_test",
          type: "transcript",
          ts: 1,
          title: "傅里叶变换可以把时域信号转换到频域。",
          data: {
            segment_id: "seg_001",
            session_id: "lec_test",
            start_ts: 1,
            end_ts: 3,
            text: "傅里叶变换可以把时域信号转换到频域。",
          },
        },
        transcript_count: 1,
        visual_count: 0,
        knowledge_extraction_count: 0,
      },
      graph_patch: null,
    });

    const state = classroomReducer(initialDashboardState, {
      type: "websocket.messageReceived",
      message,
    });

    expect(state.eventCount).toBe(1);
    expect(state.timeline).toHaveLength(1);
    expect(state.transcript).toHaveLength(1);
    expect(state.transcript[0].start_ts).toBe(1);
    expect(state.transcript[0].end_ts).toBe(3);
    expect(state.transcript[0].text).toContain("傅里叶变换");
  });

  it("stores visual event.received updates", () => {
    const message = eventReceivedMessage({
      event_type: "image.capture",
      event_count: 1,
      payload: {},
      context_update: {
        session_id: "lec_test",
        event_type: "image.capture",
        timeline_item: {
          item_id: "img_001",
          session_id: "lec_test",
          type: "visual",
          ts: 10,
          title: "X(f)=∫x(t)e^{-j2πft}dt",
          data: {
            image_id: "img_001",
            session_id: "lec_test",
            capture_ts: 10,
            image_path: "local://sessions/lec_test/images/img_001.jpg",
            source: "camera",
            image_type: "slide",
            status: "processed",
            ocr_text: "X(f)=∫x(t)e^{-j2πft}dt",
            caption: "课件展示傅里叶变换公式。",
          },
        },
        transcript_count: 0,
        visual_count: 1,
        knowledge_extraction_count: 0,
      },
      graph_patch: null,
    });

    const state = classroomReducer(initialDashboardState, {
      type: "websocket.messageReceived",
      message,
    });

    expect(state.timeline).toHaveLength(1);
    expect(state.visuals).toHaveLength(1);
    expect(state.visuals[0].ocr_text).toContain("X(f)");
    expect(state.visuals[0].source).toBe("camera");
    expect(state.visuals[0].caption).toContain("傅里叶变换");
  });

  it("falls back to raw payload when timeline item data is not available", () => {
    const message = eventReceivedMessage({
      event_type: "transcript.segment",
      event_count: 1,
      payload: {
        segment_id: "seg_payload",
        session_id: "lec_test",
        start_ts: 5,
        end_ts: 7,
        text: "这条字幕来自原始 payload。",
      },
      context_update: {
        session_id: "lec_test",
        event_type: "transcript.segment",
        timeline_item: {
          item_id: "seg_payload",
          session_id: "lec_test",
          type: "transcript",
          ts: 5,
          title: "payload fallback",
          data: {},
        },
        transcript_count: 1,
        visual_count: 0,
        knowledge_extraction_count: 0,
      },
      graph_patch: null,
    });

    const state = classroomReducer(initialDashboardState, {
      type: "websocket.messageReceived",
      message,
    });

    expect(state.transcript).toHaveLength(1);
    expect(state.transcript[0].segment_id).toBe("seg_payload");
    expect(state.transcript[0].text).toContain("原始 payload");
  });

  it("shows transcript preview without adding timeline or saved transcript", () => {
    const previewState = classroomReducer(initialDashboardState, {
      type: "websocket.messageReceived",
      message: {
        type: "transcript.preview",
        session_id: "lec_test",
        created_at: "2026-06-05T00:00:00.000000+00:00",
        data: {
          event_count: 0,
          payload: {
            segment_id: "seg_partial_001",
            session_id: "lec_test",
            start_ts: 1,
            end_ts: 2,
            text: "正在识别的临时字幕",
            is_final: false,
          },
        },
      },
    });

    expect(previewState.transcript).toHaveLength(0);
    expect(previewState.timeline).toHaveLength(0);
    expect(previewState.partialTranscript?.text).toContain("临时字幕");
  });

  it("clears transcript preview when final transcript arrives", () => {
    const previewState = classroomReducer(initialDashboardState, {
      type: "websocket.messageReceived",
      message: {
        type: "transcript.preview",
        session_id: "lec_test",
        created_at: "2026-06-05T00:00:00.000000+00:00",
        data: {
          payload: {
            segment_id: "seg_partial_001",
            session_id: "lec_test",
            start_ts: 1,
            end_ts: 2,
            text: "正在识别的临时字幕",
            is_final: false,
          },
        },
      },
    });
    const finalState = classroomReducer(previewState, {
      type: "websocket.messageReceived",
      message: eventReceivedMessage({
        event_type: "transcript.segment",
        event_count: 1,
        payload: {
          segment_id: "seg_final_001",
          session_id: "lec_test",
          start_ts: 1,
          end_ts: 2,
          text: "最终字幕。",
        },
        context_update: {
          session_id: "lec_test",
          event_type: "transcript.segment",
          timeline_item: {
            item_id: "seg_final_001",
            session_id: "lec_test",
            type: "transcript",
            ts: 1,
            title: "最终字幕。",
            data: {},
          },
          transcript_count: 1,
          visual_count: 0,
          knowledge_extraction_count: 0,
        },
        graph_patch: null,
      }),
    });

    expect(finalState.partialTranscript).toBeNull();
    expect(finalState.transcript).toHaveLength(1);
    expect(finalState.transcript[0].text).toBe("最终字幕。");
  });

  it("marks post-class artifacts as ready from background update", () => {
    const endedState = classroomReducer(
      {
        ...initialDashboardState,
        session: {
          session_id: "lec_test",
          title: "课堂记录",
          start_time: "2026-06-05T00:00:00+08:00",
          status: "recording",
          language: "zh-CN",
          created_by: "student",
        },
      },
      {
        type: "session.ended",
        session: {
          session_id: "lec_test",
          title: "课堂记录",
          start_time: "2026-06-05T00:00:00+08:00",
          end_time: "2026-06-05T01:00:00+08:00",
          status: "ended",
          language: "zh-CN",
          created_by: "student",
        },
      },
    );
    const readyState = classroomReducer(endedState, {
      type: "websocket.messageReceived",
      message: {
        type: "post_class.updated",
        session_id: "lec_test",
        created_at: "2026-06-05T00:00:00.000000+00:00",
          data: {
            status: "ready",
            knowledge_graph_status: "final",
            session: {
              session_id: "lec_test",
            title: "Web and HTTP (Part 1)",
            start_time: "2026-06-05T00:00:00+08:00",
            end_time: "2026-06-05T01:00:00+08:00",
            status: "ended",
            language: "zh-CN",
            created_by: "student",
          },
          post_class_artifacts: {
            summary_markdown: "这是一份后台生成的总结。",
            todos: [{ title: "复习第一章", confidence: 0.7 }],
            quiz: [],
            agent_artifacts: [],
            agent_messages: [],
          },
        },
      },
    });

    expect(endedState.postClassStatus).toBe("generating");
    expect(endedState.knowledgeGraphStatus).toBe("finalizing");
    expect(readyState.postClassStatus).toBe("ready");
    expect(readyState.knowledgeGraphStatus).toBe("final");
    expect(readyState.session?.title).toBe("Web and HTTP (Part 1)");
    expect(readyState.postClassArtifacts.summary_markdown).toContain("后台生成");
    expect(readyState.postClassArtifacts.todos[0].title).toBe("复习第一章");
  });

  it("merges generated quiz into post-class artifacts", () => {
    const state = classroomReducer(
      {
        ...initialDashboardState,
        postClassStatus: "ready",
        postClassArtifacts: {
          summary_markdown: "课堂总结",
          todos: [{ title: "复习第一章" }],
          quiz: [],
          agent_artifacts: [],
          agent_messages: [],
        },
      },
      {
        type: "post_class.quiz.generated",
        quiz: [{ question: "第一题？", answer: "答案。" }],
      },
    );

    expect(state.postClassArtifacts.summary_markdown).toBe("课堂总结");
    expect(state.postClassArtifacts.todos[0].title).toBe("复习第一章");
    expect(state.postClassArtifacts.quiz[0].question).toBe("第一题？");
  });

  it("keeps all timeline items needed by the timeline panel", () => {
    const firstState = classroomReducer(initialDashboardState, {
      type: "websocket.messageReceived",
      message: eventReceivedMessage({
        event_type: "transcript.segment",
        event_count: 1,
        payload: {},
        context_update: {
          session_id: "lec_test",
          event_type: "transcript.segment",
          timeline_item: {
            item_id: "seg_late",
            session_id: "lec_test",
            type: "transcript",
            ts: 12,
            title: "较晚的字幕",
            data: {
              segment_id: "seg_late",
              session_id: "lec_test",
              start_ts: 12,
              end_ts: 13,
              text: "较晚的字幕",
            },
          },
          transcript_count: 1,
          visual_count: 0,
          knowledge_extraction_count: 0,
        },
        graph_patch: null,
      }),
    });

    const secondState = classroomReducer(firstState, {
      type: "websocket.messageReceived",
      message: eventReceivedMessage({
        event_type: "image.capture",
        event_count: 2,
        payload: {},
        context_update: {
          session_id: "lec_test",
          event_type: "image.capture",
          timeline_item: {
            item_id: "img_early",
            session_id: "lec_test",
            type: "visual",
            ts: 3,
            title: "较早的图片",
            data: {
              image_id: "img_early",
              session_id: "lec_test",
              capture_ts: 3,
              image_path: "local://sessions/lec_test/images/img_early.jpg",
              status: "processed",
            },
          },
          transcript_count: 1,
          visual_count: 1,
          knowledge_extraction_count: 0,
        },
        graph_patch: null,
      }),
    });

    expect(secondState.eventCount).toBe(2);
    expect(secondState.timeline.map((item) => item.item_id)).toEqual([
      "seg_late",
      "img_early",
    ]);
    expect(secondState.timeline.map((item) => item.type)).toEqual(["transcript", "visual"]);
  });

  it("applies knowledge graph patches from event.received updates", () => {
    const message = eventReceivedMessage({
      event_type: "knowledge.extraction",
      event_count: 1,
      payload: {},
      context_update: {
        session_id: "lec_test",
        event_type: "knowledge.extraction",
        timeline_item: {
          item_id: "ext_001",
          session_id: "lec_test",
          type: "knowledge",
          ts: 1,
          title: "知识点：傅里叶变换、频域",
          data: {},
        },
        transcript_count: 0,
        visual_count: 0,
        knowledge_extraction_count: 1,
      },
      graph_patch: {
        session_id: "lec_test",
        from_version: 0,
        to_version: 1,
        operations: [
          {
            op: "add_node",
            node: {
              node_id: "node_fourier",
              label: "傅里叶变换",
              type: "concept",
              summary: "将信号从时域表示转换到频域表示的数学工具",
              level: 0,
              importance: 0.92,
              source_refs: [{ type: "segment", id: "seg_001", ts: 1 }],
            },
            edge: null,
            data: {},
          },
          {
            op: "add_node",
            node: {
              node_id: "node_freq",
              label: "频域",
              type: "concept",
              summary: null,
              level: 0,
              importance: null,
            },
            edge: null,
            data: {},
          },
          {
            op: "add_edge",
            node: null,
            edge: {
              edge_id: "edge_fourier_freq",
              source: "node_fourier",
              target: "node_freq",
              relation: "maps_to",
              source_refs: [{ type: "event", id: "ext_001" }],
            },
            data: {},
          },
        ],
      },
    });

    const state = classroomReducer(initialDashboardState, {
      type: "websocket.messageReceived",
      message,
    });

    expect(state.timeline).toHaveLength(1);
    expect(state.graph.version).toBe(1);
    expect(state.graph.nodes).toHaveLength(2);
    expect(state.graph.edges).toHaveLength(1);
    expect(state.graph.nodes[0].label).toBe("傅里叶变换");
    expect(state.graph.nodes[0].source_refs?.[0].id).toBe("seg_001");
    expect(state.graph.edges[0].source_refs?.[0].id).toBe("ext_001");
  });

  it("updates existing graph nodes by node_id", () => {
    const firstState = classroomReducer(initialDashboardState, {
      type: "websocket.messageReceived",
      message: eventReceivedMessage({
        event_type: "knowledge.extraction",
        event_count: 1,
        payload: {},
        context_update: knowledgeContextUpdate("ext_add"),
        graph_patch: {
          session_id: "lec_test",
          from_version: 0,
          to_version: 1,
          operations: [
            {
              op: "add_node",
              node: {
                node_id: "node_fourier",
                label: "傅里叶变换",
                type: "concept",
                summary: "旧摘要",
                level: 0,
                importance: 0.5,
              },
              edge: null,
              data: {},
            },
          ],
        },
      }),
    });

    const secondState = classroomReducer(firstState, {
      type: "websocket.messageReceived",
      message: eventReceivedMessage({
        event_type: "knowledge.extraction",
        event_count: 2,
        payload: {},
        context_update: knowledgeContextUpdate("ext_update"),
        graph_patch: {
          session_id: "lec_test",
          from_version: 1,
          to_version: 2,
          operations: [
            {
              op: "update_node",
              node: {
                node_id: "node_fourier",
                label: "傅里叶变换",
                type: "concept",
                summary: "新摘要",
                level: 0,
                importance: 0.9,
              },
              edge: null,
              data: {},
            },
          ],
        },
      }),
    });

    expect(secondState.graph.version).toBe(2);
    expect(secondState.graph.nodes).toHaveLength(1);
    expect(secondState.graph.nodes[0].summary).toBe("新摘要");
    expect(secondState.graph.nodes[0].importance).toBe(0.9);
  });

  it("removes graph edges and related edges when nodes are removed", () => {
    const populatedState = classroomReducer(initialDashboardState, {
      type: "websocket.messageReceived",
      message: eventReceivedMessage({
        event_type: "knowledge.extraction",
        event_count: 1,
        payload: {},
        context_update: knowledgeContextUpdate("ext_graph"),
        graph_patch: {
          session_id: "lec_test",
          from_version: 0,
          to_version: 1,
          operations: [
            {
              op: "add_node",
              node: {
                node_id: "node_a",
                label: "时域",
                type: "concept",
                summary: null,
                level: 0,
                importance: null,
              },
              edge: null,
              data: {},
            },
            {
              op: "add_node",
              node: {
                node_id: "node_b",
                label: "频域",
                type: "concept",
                summary: null,
                level: 0,
                importance: null,
              },
              edge: null,
              data: {},
            },
            {
              op: "add_edge",
              node: null,
              edge: {
                edge_id: "edge_ab",
                source: "node_a",
                target: "node_b",
                relation: "maps_to",
              },
              data: {},
            },
          ],
        },
      }),
    });

    const withoutEdge = classroomReducer(populatedState, {
      type: "websocket.messageReceived",
      message: eventReceivedMessage({
        event_type: "knowledge.extraction",
        event_count: 2,
        payload: {},
        context_update: knowledgeContextUpdate("ext_remove_edge"),
        graph_patch: {
          session_id: "lec_test",
          from_version: 1,
          to_version: 2,
          operations: [
            {
              op: "remove_edge",
              node: null,
              edge: {
                edge_id: "edge_ab",
                source: "node_a",
                target: "node_b",
                relation: "maps_to",
              },
              data: {},
            },
          ],
        },
      }),
    });

    const withoutNode = classroomReducer(populatedState, {
      type: "websocket.messageReceived",
      message: eventReceivedMessage({
        event_type: "knowledge.extraction",
        event_count: 3,
        payload: {},
        context_update: knowledgeContextUpdate("ext_remove_node"),
        graph_patch: {
          session_id: "lec_test",
          from_version: 1,
          to_version: 3,
          operations: [
            {
              op: "remove_node",
              node: {
                node_id: "node_a",
                label: "时域",
                type: "concept",
                summary: null,
                level: 0,
                importance: null,
              },
              edge: null,
              data: {},
            },
          ],
        },
      }),
    });

    expect(withoutEdge.graph.nodes).toHaveLength(2);
    expect(withoutEdge.graph.edges).toHaveLength(0);
    expect(withoutNode.graph.nodes.map((node) => node.node_id)).toEqual(["node_b"]);
    expect(withoutNode.graph.edges).toHaveLength(0);
  });

  it("loads persisted history details into the dashboard state", () => {
    const state = classroomReducer(initialDashboardState, {
      type: "history.loaded",
      detail: {
        session: {
          session_id: "lec_history",
          title: "历史课堂",
          course: "通信原理",
          teacher: "王老师",
          start_time: "2026-06-05T12:00:00+08:00",
          end_time: "2026-06-05T13:00:00+08:00",
          status: "ended",
          language: "zh-CN",
          created_by: "student",
          device_id: null,
        },
        transcript_markdown: "# Transcript - lec_history",
        timeline: [
          {
            item_id: "seg_history",
            session_id: "lec_history",
            type: "transcript",
            ts: 1,
            title: "历史字幕",
            data: {
              segment_id: "seg_history",
              session_id: "lec_history",
              start_ts: 1,
              end_ts: 2,
              text: "这是一条历史字幕。",
            },
          },
          {
            item_id: "img_history",
            session_id: "lec_history",
            type: "visual",
            ts: 3,
            title: "历史图片",
            data: {
              image_id: "img_history",
              session_id: "lec_history",
              capture_ts: 3,
              image_path: "local://history/img.jpg",
              status: "processed",
              ocr_text: "历史 OCR",
            },
          },
        ],
        knowledge_graph: {
          session_id: "lec_history",
          version: 4,
          root_nodes: ["node_history"],
          nodes: [
            {
              node_id: "node_history",
              label: "历史知识点",
              type: "concept",
              summary: null,
              level: 0,
              importance: null,
            },
          ],
          edges: [],
        },
        storage_path: "data/sessions/lec_history",
        post_class_artifacts: {
          summary_markdown: "这是一份历史课堂总结。",
          todos: [{ title: "完成第三题", confidence: 0.6 }],
          quiz: [{ question: "傅里叶变换有什么作用？", answer: "转换到频域。" }],
          agent_artifacts: [],
          agent_messages: [],
        },
      },
    });

    expect(state.session?.session_id).toBe("lec_history");
    expect(state.websocketStatus).toBe("disconnected");
    expect(state.eventCount).toBe(2);
    expect(state.transcript[0].text).toContain("历史字幕");
    expect(state.visuals[0].ocr_text).toBe("历史 OCR");
    expect(state.graph.version).toBe(4);
    expect(state.graph.nodes[0].label).toBe("历史知识点");
    expect(state.postClassArtifacts.summary_markdown).toContain("历史课堂总结");
    expect(state.postClassArtifacts.todos[0].title).toBe("完成第三题");
    expect(state.postClassArtifacts.quiz[0].question).toContain("傅里叶变换");
  });

  it("clears dashboard when the loaded history session is deleted", () => {
    const loadedState = classroomReducer(initialDashboardState, {
      type: "history.loaded",
      detail: historyDetail("lec_history_current"),
    });

    const deletedState = classroomReducer(loadedState, {
      type: "history.deleted",
      sessionId: "lec_history_current",
    });

    expect(loadedState.session?.session_id).toBe("lec_history_current");
    expect(loadedState.timeline).toHaveLength(1);
    expect(deletedState).toEqual(initialDashboardState);
  });

  it("keeps dashboard when another history session is deleted", () => {
    const loadedState = classroomReducer(initialDashboardState, {
      type: "history.loaded",
      detail: historyDetail("lec_history_current"),
    });

    const deletedState = classroomReducer(loadedState, {
      type: "history.deleted",
      sessionId: "lec_history_other",
    });

    expect(deletedState).toEqual(loadedState);
  });

  it("uses persisted post-class status when loading history", () => {
    const state = classroomReducer(initialDashboardState, {
      type: "history.loaded",
      detail: {
        ...historyDetail("lec_history_failed_post_class"),
        post_class_status: "failed" as const,
        post_class_warnings: ["Post-class generation was interrupted."],
      },
    });

    expect(state.postClassStatus).toBe("failed");
    expect(state.knowledgeGraphStatus).toBe("failed");
  });
});

function eventReceivedMessage(data: Record<string, unknown>): WebSocketMessage {
  return {
    type: "event.received",
    session_id: "lec_test",
    data,
    created_at: "2026-06-05T00:00:00.000000+00:00",
  };
}

function knowledgeContextUpdate(itemId: string): Record<string, unknown> {
  return {
    session_id: "lec_test",
    event_type: "knowledge.extraction",
    timeline_item: {
      item_id: itemId,
      session_id: "lec_test",
      type: "knowledge",
      ts: 1,
      title: "知识点更新",
      data: {},
    },
    transcript_count: 0,
    visual_count: 0,
    knowledge_extraction_count: 1,
  };
}

function historyDetail(sessionId: string) {
  return {
    session: {
      session_id: sessionId,
      title: "历史课堂",
      course: "通信原理",
      teacher: "张老师",
      start_time: "2026-06-05T00:00:00+08:00",
      end_time: "2026-06-05T01:00:00+08:00",
      status: "ended" as const,
      language: "zh-CN",
      created_by: "student",
      device_id: null,
    },
    transcript_markdown: "# Transcript",
    timeline: [
      {
        item_id: "seg_history",
        session_id: sessionId,
        type: "transcript" as const,
        ts: 1,
        title: "历史字幕",
        data: {
          segment_id: "seg_history",
          session_id: sessionId,
          start_ts: 1,
          end_ts: 2,
          text: "历史字幕",
        },
      },
    ],
    knowledge_graph: {
      session_id: sessionId,
      version: 0,
      root_nodes: [],
      nodes: [],
      edges: [],
    },
    storage_path: `data/sessions/${sessionId}`,
    post_class_artifacts: {
      summary_markdown: null,
      todos: [],
      quiz: [],
      agent_artifacts: [],
      agent_messages: [],
    },
  };
}
