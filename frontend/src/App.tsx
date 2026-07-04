import { useEffect, useReducer, useRef, useState } from "react";

import { AgentPanel } from "./components/AgentPanel";
import { ClassroomControls } from "./components/ClassroomControls";
import { GlobalSearchPanel } from "./components/GlobalSearchPanel";
import { HistoryPanel } from "./components/HistoryPanel";
import { KnowledgeGraphPanel } from "./components/KnowledgeGraphPanel";
import { PostClassArtifactsPanel } from "./components/PostClassArtifactsPanel";
import { RealtimeTranscriptPanel } from "./components/RealtimeTranscriptPanel";
import { StatusStrip } from "./components/StatusStrip";
import { VisualOcrPanel } from "./components/VisualOcrPanel";
import { chatWithAgent } from "./services/agentApi";
import {
  ApiError,
  deleteHistorySession,
  endSession,
  finalizeTranscriptPreview,
  getHistorySession,
  listRecordingSessions,
  listHistorySessions,
  startSession,
  updateSessionMetadata,
} from "./services/api";
import { connectClassroomSocket } from "./services/websocket";
import { classroomReducer, initialDashboardState } from "./stores/classroomStore";
import type { AgentArtifact, GlobalSearchSourceRef } from "./types/agent";
import type { SessionHistorySummary, WebSocketMessage } from "./types/classroom";

const STATUS_NOTICE_LIMIT = 80;
const STATUS_NOTICE_AUTO_PIN_DELAY_MS = 5000;

type StatusNotice = {
  id: string;
  message: string;
  timeLabel: string;
};

function App() {
  // 页面主状态由 classroomReducer 管理。
  //
  // useReducer 比多个 setState 更适合实时消息流：每条 WebSocket 消息都是一个
  // action，reducer 负责把 action 归并到课堂状态。这样 App.tsx 保持在
  // “连接服务 + 触发 action”的层次，具体的数据合并规则放在 stores/。
  const [state, dispatch] = useReducer(classroomReducer, initialDashboardState);

  // 开始/结束课堂都是异步 HTTP 请求。这个状态用于禁用按钮，避免用户
  // 连续点击导致重复创建 session 或重复结束课堂。
  const [isSessionRequestPending, setIsSessionRequestPending] = useState(false);
  const [isAttachRequestPending, setIsAttachRequestPending] = useState(false);
  const [isRenameRequestPending, setIsRenameRequestPending] = useState(false);
  const [isQuizGenerating, setIsQuizGenerating] = useState(false);

  // 页面公告流。WebSocket、历史课堂、拍照、结束课堂等运行状态都追加到这里，
  // 保留最近若干条并提供滚动查看，避免一条新提示覆盖掉刚发生的重要事件。
  const [statusMessages, setStatusMessages] = useState<StatusNotice[]>([]);
  const statusMessageListRef = useRef<HTMLOListElement | null>(null);
  const statusNoticeCounterRef = useRef(0);
  const statusNoticeAutoPinTimerRef = useRef<number | null>(null);

  // 历史课程列表来自 GET /sessions。
  //
  // 这里有意把“列表 UI 状态”留在 App，而不是放进 classroomReducer：
  // - historySessions / selectedHistoryId 只服务左侧历史栏。
  // - isHistoryLoading / isHistoryOpening 是按钮和提示状态。
  // - 真正会影响四个课堂内容面板的数据，才通过 history.loaded 进入 reducer。
  //
  // 这样实时课堂状态、历史详情状态和页面交互状态各自待在合适的位置。
  const [historySessions, setHistorySessions] = useState<SessionHistorySummary[]>([]);
  const [selectedHistoryId, setSelectedHistoryId] = useState<string | null>(null);
  const [isHistoryLoading, setIsHistoryLoading] = useState(false);
  const [isHistoryOpening, setIsHistoryOpening] = useState(false);
  const [deletingHistoryId, setDeletingHistoryId] = useState<string | null>(null);
  // 跨课堂搜索命中的来源。打开历史课堂后，字幕/图片/时间线面板会用它高亮并
  // 滚动到对应条目。它是纯 UI 状态，不进入 classroomReducer，避免和实时数据
  // 合并逻辑混在一起。
  const [focusedSource, setFocusedSource] = useState<GlobalSearchSourceRef | null>(null);

  // 保存当前课堂的 WebSocket 实例。
  //
  // WebSocket 是浏览器对象，不属于可渲染 UI 数据，所以用 ref 而不是 state。
  // 这样关闭旧连接时不会触发额外渲染，也能避免开始新课堂后旧 socket 继续推消息。
  const socketRef = useRef<WebSocket | null>(null);

  function setStatusMessage(message: string | null) {
    if (!message) {
      return;
    }

    statusNoticeCounterRef.current += 1;
    const notice: StatusNotice = {
      id: `${Date.now()}_${statusNoticeCounterRef.current}`,
      message,
      timeLabel: formatStatusNoticeTime(new Date()),
    };
    setStatusMessages((current) => [...current, notice].slice(-STATUS_NOTICE_LIMIT));
  }

  function clearStatusNoticeAutoPinTimer() {
    if (statusNoticeAutoPinTimerRef.current !== null) {
      window.clearTimeout(statusNoticeAutoPinTimerRef.current);
      statusNoticeAutoPinTimerRef.current = null;
    }
  }

  function scrollStatusNoticesToLatest(behavior: ScrollBehavior = "smooth") {
    statusMessageListRef.current?.scrollTo({ top: 0, behavior });
  }

  function scheduleStatusNoticeAutoPin() {
    clearStatusNoticeAutoPinTimer();
    statusNoticeAutoPinTimerRef.current = window.setTimeout(() => {
      scrollStatusNoticesToLatest();
      statusNoticeAutoPinTimerRef.current = null;
    }, STATUS_NOTICE_AUTO_PIN_DELAY_MS);
  }

  function handleStatusNoticeScroll() {
    const list = statusMessageListRef.current;
    if (!list) {
      return;
    }
    if (list.scrollTop <= 4) {
      clearStatusNoticeAutoPinTimer();
      return;
    }
    scheduleStatusNoticeAutoPin();
  }

  // 组件卸载时关闭 WebSocket。
  // Vite 热更新、页面跳转或未来加路由时，如果不清理连接，后端还会保留旧订阅者。
  useEffect(() => {
    // 页面首次打开就静默加载历史列表。silent=true 表示失败时仍会显示错误，
    // 成功时不打扰用户；顶部状态提示继续留给“开始/结束课堂”等主动操作。
    void loadHistorySessions({ silent: true });
    const deepLink = parseHistoryDeepLink();
    if (deepLink) {
      void handleOpenHistory(deepLink.sessionId, deepLink.sourceRef);
    } else {
      void handleAttachRecordingSession({ silent: true });
    }

    return () => {
      clearStatusNoticeAutoPinTimer();
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, []);

  useEffect(() => {
    const list = statusMessageListRef.current;
    if (!list || statusMessages.length === 0) {
      return;
    }

    if (list.scrollTop <= 4) {
      scrollStatusNoticesToLatest("auto");
    } else {
      scheduleStatusNoticeAutoPin();
    }
  }, [statusMessages.length]);

  // 刷新历史课程列表。
  //
  // 后端 GET /sessions 只返回已保存课堂的摘要，不会返回正在录制但尚未结束的
  // 内存 session。结束课堂后这里会被静默调用一次，让新保存的课堂自然出现在
  // 左侧列表里；用户也可以点“刷新”主动重新读取磁盘历史。
  async function loadHistorySessions(options: { silent?: boolean } = {}) {
    setIsHistoryLoading(true);

    if (!options.silent) {
      setStatusMessage(null);
    }

    try {
      const response = await listHistorySessions();
      setHistorySessions(response.sessions);

      if (!options.silent) {
        setStatusMessage(`已刷新历史课程：${response.sessions.length} 节。`);
      }
    } catch (error) {
      setStatusMessage(formatApiError(error, "读取历史课程失败"));
    } finally {
      setIsHistoryLoading(false);
    }
  }

  // 为指定 session 建立 WebSocket 订阅。
  //
  // 这里与 startSession 分开写，是为了以后支持“重新连接”按钮：
  // 只要还持有 session_id，就可以复用这个函数重新订阅同一课堂。
  function connectWebSocket(sessionId: string) {
    // 开始新课堂或重连前，先关闭旧连接，保证页面只消费当前 session 的消息。
    socketRef.current?.close();

    dispatch({ type: "websocket.statusChanged", status: "connecting" });

    // 回调里会引用 socket 自身，用于判断事件是否来自当前连接。
    // WebSocket 的 close/error 事件可能在新连接建立后才到达；如果不做判断，
    // 旧连接的回调可能把新连接状态错误地改成 disconnected/error。
    let socket: WebSocket;
    const isCurrentSocket = () => socketRef.current === socket;

    socket = connectClassroomSocket(sessionId, {
      onOpen: () => {
        if (!isCurrentSocket()) {
          return;
        }

        // onOpen 表示浏览器和后端的 WebSocket 握手已经成功。
        // 后端随后还会推送 ws.connected；两者任意一个到达都可以认为已连接。
        dispatch({ type: "websocket.statusChanged", status: "connected" });
        setStatusMessage("WebSocket 已连接，等待实时事件。");
      },
      onMessage: (message) => {
        if (!isCurrentSocket()) {
          return;
        }

        handleWebSocketMessage(message);
      },
      onError: () => {
        if (!isCurrentSocket()) {
          return;
        }

        dispatch({ type: "websocket.statusChanged", status: "error" });
        setStatusMessage("WebSocket 连接异常，请确认后端服务仍在运行。");
      },
      onClose: (event) => {
        if (!isCurrentSocket()) {
          return;
        }

        // 如果是用户结束课堂或页面卸载触发的正常关闭，状态回到 disconnected。
        // 如果异常关闭但 onerror 没有先触发，这里也给出连接异常提示。
        dispatch({
          type: "websocket.statusChanged",
          status: event.wasClean ? "disconnected" : "error",
        });

        if (!event.wasClean) {
          setStatusMessage(`WebSocket 已断开：${event.code || "unknown"}`);
        }
      },
    });

    socketRef.current = socket;
  }

  // WebSocket 消息分发入口。
  //
  // App 不直接拆解 event.received。它只把完整 WebSocketMessage 交给 reducer，
  // 由 store 统一处理 context_update、payload、graph_patch 等字段。
  function handleWebSocketMessage(message: WebSocketMessage) {
    dispatch({ type: "websocket.messageReceived", message });

    if (message.type === "ws.connected") {
      setStatusMessage("WebSocket 订阅确认成功。");
      return;
    }

    if (message.type === "session.ended") {
      setStatusMessage("收到课堂结束广播。");
      return;
    }

    if (message.type === "session.updated") {
      setStatusMessage("课堂名称已自动更新。");
      return;
    }

    if (message.type === "post_class.updated") {
      const status = message.data.status;
      const stage = message.data.post_class_stage;
      setStatusMessage(
        status === "failed"
          ? "课后处理失败，请稍后查看日志。"
          : stage === "notes_ready"
            ? "结构化笔记已生成，图谱和课后产物继续后台处理。"
            : stage === "artifacts_ready"
              ? "课后总结和待办已生成。"
              : stage === "graph_ready"
                ? "最终知识图谱已更新。"
                : stage === "done"
                  ? "课后处理已完成。"
                  : "课后处理状态已更新。",
      );
    }
  }

  // 开始课堂：
  // 1. 调用后端 POST /sessions/start。
  // 2. 用返回的 LectureSession 初始化前端课堂状态。
  // 3. 清空上一节课残留的字幕、时间线、图片和图谱。
  // 4. 用 session_id 连接后端 /ws/{session_id}。
  async function handleStartSession() {
    setIsSessionRequestPending(true);
    setStatusMessage(null);

    try {
      const session = await startSession();
      dispatch({
        type: "session.started",
        session,
      });
      // 新课堂开始后，右侧看板已经切到实时课堂；清空历史选中态，避免左侧
      // 仍高亮某节历史课，让用户误以为当前显示的是历史内容。
      setSelectedHistoryId(null);
      setFocusedSource(null);
      clearHistoryDeepLink();
      setStatusMessage("课堂已开始，正在连接 WebSocket。");
      connectWebSocket(session.session_id);
    } catch (error) {
      setStatusMessage(formatApiError(error, "开始课堂失败"));
    } finally {
      setIsSessionRequestPending(false);
    }
  }

  // 接入已经存在的 recording session：
  // - 前端先开始课堂时，脚本可以自动发现它；
  // - 脚本先自动创建课堂时，前端打开后可以自动或手动接入它。
  async function handleAttachRecordingSession(
    options: { silent?: boolean } = {},
  ) {
    if (state.session?.status === "recording") {
      if (!options.silent) {
        setStatusMessage("当前已经接入录制中的课堂。");
      }
      return;
    }

    setIsAttachRequestPending(true);
    if (!options.silent) {
      setStatusMessage(null);
    }

    try {
      const sessions = await listRecordingSessions();
      const session = sessions[0];
      if (!session) {
        if (!options.silent) {
          setStatusMessage("当前没有正在录制的课堂。");
        }
        return;
      }

      dispatch({
        type: "session.started",
        session,
      });
      setSelectedHistoryId(null);
      setFocusedSource(null);
      clearHistoryDeepLink();
      connectWebSocket(session.session_id);
      setStatusMessage(
        options.silent
          ? `已自动接入录制课堂：${session.session_id}`
          : `已接入录制课堂：${session.session_id}`,
      );
    } catch (error) {
      if (!options.silent) {
        setStatusMessage(formatApiError(error, "接入当前课堂失败"));
      }
    } finally {
      setIsAttachRequestPending(false);
    }
  }

  // 结束课堂：
  // 1. 只有存在当前 session 时才允许调用。
  // 2. 调用后端 POST /sessions/{session_id}/end。
  // 3. 后端会保存本地文件并广播 session.ended。
  //
  // HTTP 响应和 session.ended 广播都可能更新同一状态，因此这里保持幂等：
  // 即使广播先到或后到，最终状态都是 ended + disconnected。
  async function handleEndSession() {
    if (!state.session) {
      return;
    }

    setIsSessionRequestPending(true);
    setStatusMessage(null);

    try {
      if (state.partialTranscript?.text.trim()) {
        try {
          await finalizeTranscriptPreview(
            state.session.session_id,
            state.partialTranscript,
          );
        } catch (error) {
          console.warn("Finalize transcript preview failed before ending session", error);
        }
      }
      const session = await endSession(state.session.session_id);
      dispatch({
        type: "session.ended",
        session,
      });
      setStatusMessage("课堂已结束，核心文件已保存；课后产物正在后台生成。");
      // 保存发生在后端 end 路由里。HTTP 成功返回后刷新历史列表即可看到
      // 刚结束的课堂；silent 避免覆盖“课堂已结束”的主提示。
      void loadHistorySessions({ silent: true });
    } catch (error) {
      setStatusMessage(formatApiError(error, "结束课堂失败"));
    } finally {
      setIsSessionRequestPending(false);
    }
  }

  async function handleRenameSession(title: string, course: string | null) {
    if (!state.session) {
      return;
    }

    setIsRenameRequestPending(true);
    setStatusMessage(null);

    try {
      const session = await updateSessionMetadata(state.session.session_id, {
        title,
        course,
      });
      dispatch({
        type: "session.updated",
        session,
      });
      setHistorySessions((current) =>
        current.map((item) =>
          item.session.session_id === session.session_id
            ? {
                ...item,
                session,
              }
            : item,
        ),
      );
      setStatusMessage("课堂名称已更新。");
    } catch (error) {
      setStatusMessage(formatApiError(error, "更新课堂名称失败"));
    } finally {
      setIsRenameRequestPending(false);
    }
  }

  // 打开历史课程详情：
  // 1. 调用 GET /sessions/{session_id}/history 读取完整课后档案。
  // 2. 关闭现有 WebSocket，保证历史查看模式不会继续消费实时事件。
  // 3. 通过 history.loaded 把历史详情装载进同一套 dashboard 面板。
  //
  // 录制中的课堂不允许切历史，因为那会清空当前实时看板并关闭 socket。
  // 后续如果要支持“边录制边看历史”，应引入独立路由或双看板，而不是复用
  // 当前单看板状态。
  async function handleOpenHistory(
    sessionId: string,
    focusedSourceRef: GlobalSearchSourceRef | null = null,
  ) {
    if (state.session?.status === "recording") {
      setStatusMessage("当前课堂正在录制，结束后再查看历史课程。");
      return;
    }

    setIsHistoryOpening(true);
    setStatusMessage(null);

    try {
      const detail = await getHistorySession(sessionId);
      // 历史课程是只读回放，不需要 WebSocket。关闭旧连接可以防止刚打开历史
      // 后，旧 session 的迟到消息又把右侧面板改回实时内容。
      socketRef.current?.close();
      socketRef.current = null;
      dispatch({
        type: "history.loaded",
        detail,
      });
      setSelectedHistoryId(sessionId);
      setFocusedSource(focusedSourceRef);
      writeHistoryDeepLink(sessionId, focusedSourceRef);
      setStatusMessage(
        focusedSourceRef ? "历史课程已加载，已定位到搜索命中来源。" : "历史课程已加载。",
      );
    } catch (error) {
      setStatusMessage(formatApiError(error, "加载历史课程失败"));
    } finally {
      setIsHistoryOpening(false);
    }
  }

  // 删除历史课堂：
  // 1. 让用户确认，因为后端会删除 data/sessions/{session_id} 整个目录。
  // 2. 调用 DELETE /sessions/{session_id}/history。
  // 3. 从左侧历史列表移除该项。
  // 4. 如果右侧正在展示这节历史课，同步清空 dashboard，避免继续展示已删除数据。
  //
  // 这个操作不影响正在录制的内存课堂；历史列表只包含已经结束保存的课堂。
  async function handleDeleteHistory(sessionId: string) {
    const target = historySessions.find((item) => item.session.session_id === sessionId);
    const title = target?.session.title || sessionId;

    if (!window.confirm(`确定删除历史课堂“${title}”及其本地数据吗？`)) {
      return;
    }

    setDeletingHistoryId(sessionId);
    setStatusMessage(null);

    try {
      await deleteHistorySession(sessionId);
      setHistorySessions((current) =>
        current.filter((item) => item.session.session_id !== sessionId),
      );

      if (selectedHistoryId === sessionId) {
        setSelectedHistoryId(null);
        setFocusedSource(null);
        clearHistoryDeepLink();
      }

      dispatch({
        type: "history.deleted",
        sessionId,
      });
      setStatusMessage("历史课堂已删除，本地数据已清理。");
    } catch (error) {
      setStatusMessage(formatApiError(error, "删除历史课堂失败"));
    } finally {
      setDeletingHistoryId(null);
    }
  }

  async function handleGenerateQuiz() {
    if (!state.session) {
      setStatusMessage("请先选择一节已结束的课堂。");
      return;
    }
    if (state.session.status !== "ended") {
      setStatusMessage("请先结束课堂，再生成课后自测。");
      return;
    }

    setIsQuizGenerating(true);
    setStatusMessage(null);

    try {
      const response = await chatWithAgent({
        session_id: state.session.session_id,
        prompt: "根据这节课生成 3 到 5 道课后自测题",
        mode: "quiz",
        answer_mode: "strict",
      });
      const quizArtifact = response.artifacts.find((artifact) => artifact.type === "quiz");
      const quiz = artifactRecords(quizArtifact);
      dispatch({
        type: "post_class.quiz.generated",
        quiz,
      });
      setStatusMessage(
        quiz.length > 0
          ? "自测题已生成，并显示在课后产物中。"
          : "自测生成完成，但模型没有返回可展示题目。",
      );
    } catch (error) {
      setStatusMessage(formatApiError(error, "生成自测失败"));
    } finally {
      setIsQuizGenerating(false);
    }
  }

  return (
    <main className="app-shell">
      {/* 顶部区域只放课堂控制和产品身份，不承载实时数据。 */}
      <section className="top-bar" aria-label="课堂控制">
        <div className="brand-block">
          <span className="brand-mark">EDU</span>
          <div>
            <h1>课堂实时看板</h1>
            <p>EDU-Mate</p>
          </div>
        </div>
        <ClassroomControls
          session={state.session}
          isBusy={isSessionRequestPending}
          isAttachBusy={isAttachRequestPending}
          isRenameBusy={isRenameRequestPending}
          onStart={handleStartSession}
          onAttach={() => void handleAttachRecordingSession()}
          onEnd={handleEndSession}
          onRename={(title, course) => void handleRenameSession(title, course)}
        />
      </section>

      {/* 状态条展示跨模块状态：session、WebSocket 和事件计数。 */}
      <StatusStrip
        session={state.session}
        websocketStatus={state.websocketStatus}
        eventCount={state.eventCount}
      />

      {statusMessages.length ? (
        <section className="status-message-log" aria-label="运行公告">
          <div className="status-message-header">
            <strong>运行公告</strong>
            <span>
              最近 {statusMessages.length}/{STATUS_NOTICE_LIMIT} 条
            </span>
          </div>
          <ol ref={statusMessageListRef} onScroll={handleStatusNoticeScroll}>
            {[...statusMessages].reverse().map((notice) => (
              <li key={notice.id}>
                <time>{notice.timeLabel}</time>
                <span>{notice.message}</span>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      <section className="content-layout" aria-label="课堂内容">
        {/* 历史栏只负责列表和打开动作；具体历史详情如何变成面板状态，
            仍由 App + reducer 处理，组件本身不 fetch。 */}
        <HistoryPanel
          sessions={historySessions}
          selectedSessionId={selectedHistoryId}
          isLoading={isHistoryLoading}
          isOpening={isHistoryOpening}
          deletingSessionId={deletingHistoryId}
          isOpenDisabled={state.session?.status === "recording"}
          onRefresh={() => void loadHistorySessions()}
          onOpen={handleOpenHistory}
          onDelete={handleDeleteHistory}
        />

        {/* 数据面板共享 App 状态，但组件内部不直接请求后端。 */}
        <section className="dashboard-grid" aria-label="课堂看板内容">
          <RealtimeTranscriptPanel
            focusedSource={focusedSource}
            partialTranscript={state.partialTranscript}
            transcript={state.transcript}
          />
          <VisualOcrPanel
            focusedSource={focusedSource}
            session={state.session}
            visuals={state.visuals}
            onStatusMessage={setStatusMessage}
          />
          <KnowledgeGraphPanel
            focusedSource={focusedSource}
            graph={state.graph}
            status={state.knowledgeGraphStatus}
            transcript={state.transcript}
            visuals={state.visuals}
          />
          <PostClassArtifactsPanel
            artifacts={state.postClassArtifacts}
            canGenerateQuiz={
              state.session?.status === "ended" && state.postClassStatus !== "generating"
            }
            isQuizGenerating={isQuizGenerating}
            onGenerateQuiz={() => void handleGenerateQuiz()}
            status={state.postClassStatus}
          />
          <AgentPanel
            persistedMessages={state.postClassArtifacts.agent_messages}
            session={state.session}
          />
          <GlobalSearchPanel
            isOpenDisabled={state.session?.status === "recording"}
            onOpenSession={(sessionId, sourceRef) => void handleOpenHistory(sessionId, sourceRef)}
          />
        </section>
      </section>
    </main>
  );
}

type HistoryDeepLink = {
  sessionId: string;
  sourceRef: GlobalSearchSourceRef | null;
};

// 从 URL query 中恢复历史课堂定位。
//
// 设计成 query 而不是前端路由，是为了不引入额外依赖，也不改变 Vite 单页应用
// 的部署方式。只有 session_id 是必需的；source_type/source_id 成对出现时才
// 进入具体来源高亮。
function parseHistoryDeepLink(): HistoryDeepLink | null {
  const params = new URLSearchParams(window.location.search);
  const sessionId = params.get("session_id");
  if (!sessionId) {
    return null;
  }

  const sourceType = params.get("source_type");
  const sourceId = params.get("source_id");
  if (!sourceType || !sourceId) {
    return {
      sessionId,
      sourceRef: null,
    };
  }

  const rawTs = params.get("ts");
  const ts = rawTs === null ? null : Number(rawTs);
  return {
    sessionId,
    sourceRef: {
      type: sourceType,
      id: sourceId,
      ts: Number.isFinite(ts) ? ts : null,
      // URL 里只保存定位所需字段。展示文本仍来自历史课堂内容本身；这里留空
      // 是为了满足 GlobalSearchSourceRef 类型，并避免把长文本塞进地址栏。
      text: "",
    },
  };
}

function writeHistoryDeepLink(
  sessionId: string,
  sourceRef: GlobalSearchSourceRef | null,
) {
  const params = new URLSearchParams();
  params.set("session_id", sessionId);
  if (sourceRef) {
    params.set("source_type", sourceRef.type);
    params.set("source_id", sourceRef.id);
    if (typeof sourceRef.ts === "number") {
      params.set("ts", String(sourceRef.ts));
    }
  }
  window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
}

function clearHistoryDeepLink() {
  window.history.replaceState(null, "", window.location.pathname);
}

// 把 service 层抛出的错误转换成用户可读文案。
// ApiError 保留了后端 HTTP status 和 detail；普通 Error 通常来自 fetch
// 网络失败、浏览器 CORS 拒绝等前端侧问题。
function formatApiError(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    return `${fallback}：${error.detail}`;
  }

  if (error instanceof Error) {
    return `${fallback}：${error.message}`;
  }

  return fallback;
}

function formatStatusNoticeTime(date: Date): string {
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function artifactRecords(artifact: AgentArtifact | undefined): Array<Record<string, unknown>> {
  if (!artifact || !Array.isArray(artifact.content)) {
    return [];
  }

  return artifact.content.filter(
    (item): item is Record<string, unknown> =>
      typeof item === "object" && item !== null && !Array.isArray(item),
  );
}

export default App;
