import { useEffect, useState } from "react";

import { MarkdownContent } from "./MarkdownContent";
import { chatWithAgent } from "../services/agentApi";
import { ApiError } from "../services/api";
import type {
  AgentAnswerMode,
  AgentArtifact,
  AgentIntent,
  AgentMessage,
  AgentSourceRef,
} from "../types/agent";
import type { LectureSession } from "../types/classroom";
import { formatClassTime } from "../utils/time";

// 课堂 Agent 面板。
//
// 它只依赖当前看板里选中的 session：
// - 实时课堂：session 来自 POST /sessions/start。
// - 历史课堂：session 来自 GET /sessions/{session_id}/history 后的 reducer 状态。
//
// 组件不读取 transcript/timeline/graph 属性，因为 Agent 的资料读取统一放在后端；
// 前端只负责把 prompt 和 session_id 发给 /agent/chat，并展示响应。
type AgentPanelProps = {
  session: LectureSession | null;
  persistedMessages?: Array<Record<string, unknown>>;
};

const intentLabels: Record<string, string> = {
  qa: "问答",
  summary: "总结",
  todos: "待办",
  quiz: "自测",
};

const maxVisibleSourceRefs = 3;
const sourcePreviewChars = 180;

export function AgentPanel({ session, persistedMessages = [] }: AgentPanelProps) {
  // prompt 是输入框草稿；messages 是本面板的轻量聊天记录。它们不进入全局
  // classroomReducer，因为 Agent 对话不参与实时事件合并。
  const [prompt, setPrompt] = useState("");
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [answerMode, setAnswerMode] = useState<AgentAnswerMode>("strict");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setMessages(persistedMessages.map(normalizeAgentMessage));
  }, [session?.session_id, persistedMessages]);

  async function submitAgentPrompt(nextPrompt = prompt, mode: AgentIntent = "auto") {
    const trimmedPrompt = nextPrompt.trim();
    // 没有选中课堂时禁用提交；后端 Agent 必须拿到 session_id 才能读取课堂资料。
    if (!session || !trimmedPrompt || isLoading) {
      return;
    }

    setIsLoading(true);
    setError(null);
    // 先把用户消息追加到本地，让界面立即响应。若请求失败，只显示错误，不回滚
    // 用户输入记录，方便用户改写后重试。
    setMessages((current) => [
      ...current,
      {
        role: "user",
        content: trimmedPrompt,
      },
    ]);

    try {
      // 后端会根据 session_id 自行判断读取内存课堂还是历史课堂文件。
      const response = await chatWithAgent({
        session_id: session.session_id,
        prompt: trimmedPrompt,
        mode,
        answer_mode: mode === "qa" || mode === "auto" ? answerMode : "strict",
      });
      setMessages((current) => [
        ...current,
        {
          role: "assistant",
          content: response.answer,
          intent: response.intent,
          artifacts: response.artifacts,
          source_refs: response.source_refs,
          warnings: response.warnings,
        },
      ]);
      // 成功后清空输入框；快捷按钮提交时 nextPrompt 不等于当前 prompt，也同样安全。
      setPrompt("");
    } catch (caughtError) {
      setError(formatAgentError(caughtError));
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <section className="panel agent-panel" aria-labelledby="agent-title">
      <div className="panel-header">
        <div>
          <h2 id="agent-title">课堂 Agent</h2>
          <span>{session ? session.title : "未选择课堂"}</span>
        </div>
      </div>

      <div className="agent-body">
        <label className="agent-mode-toggle">
          <input
            checked={answerMode === "grounded"}
            disabled={!session || isLoading}
            onChange={(event) =>
              setAnswerMode(event.target.checked ? "grounded" : "strict")
            }
            type="checkbox"
          />
          <span>允许模型补充解释</span>
        </label>

        <div className="agent-messages" aria-live="polite">
          {/* aria-live 让辅助技术能感知新回答。Markdown 只走受控 React 节点，
              不把模型输出作为 HTML 注入页面。 */}
          {messages.length === 0 ? (
            <div className="agent-empty">选择课堂后即可提问</div>
          ) : (
            messages.map((message, index) => (
              <article className={`agent-message ${message.role}`} key={index}>
                <div className="agent-message-top">
                  <strong>{message.role === "user" ? "你" : "Agent"}</strong>
                  {message.intent ? <span>{intentLabels[message.intent]}</span> : null}
                </div>
                <MarkdownContent content={message.content} />
                {/* artifacts 是结构化结果，例如 todos/quiz。第一版用折叠详情
                    保持紧凑展示，后续可替换为专门的卡片/列表组件。 */}
                {message.artifacts?.length ? (
                  <div className="agent-artifacts">
                    {message.artifacts.map((artifact, artifactIndex) => (
                      <ArtifactView artifact={artifact} key={artifactIndex} />
                    ))}
                  </div>
                ) : null}
                {/* source_refs 是 Agent 可信度的关键：用户能看到回答来自哪段字幕、
                    OCR 或知识节点。未来这里可以扩展为点击跳转到时间线。 */}
                {message.source_refs?.length ? (
                  <SourceRefs refs={message.source_refs} />
                ) : null}
                {message.warnings?.length ? (
                  <ul className="agent-warnings">
                    {message.warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                ) : null}
              </article>
            ))
          )}
        </div>

        {error ? <div className="agent-error">{error}</div> : null}

        <form
          className="agent-input-row"
          onSubmit={(event) => {
            event.preventDefault();
            void submitAgentPrompt();
          }}
        >
          <input
            disabled={!session || isLoading}
            onChange={(event) => setPrompt(event.target.value)}
            placeholder="问问这节课"
            value={prompt}
          />
          <button className="primary-button" disabled={!session || !prompt.trim() || isLoading}>
            {isLoading ? "处理中" : "发送"}
          </button>
        </form>
      </div>
    </section>
  );
}

function normalizeAgentMessage(message: Record<string, unknown>): AgentMessage {
  // 把后端保存的宽松 JSON 还原成 AgentPanel 使用的消息形态。
  const role = message.role === "assistant" ? "assistant" : "user";
  return {
    role,
    content: typeof message.content === "string" ? message.content : "",
    intent:
      message.intent === "qa" ||
      message.intent === "summary" ||
      message.intent === "todos" ||
      message.intent === "quiz"
        ? message.intent
        : undefined,
    artifacts: Array.isArray(message.artifacts)
      ? (message.artifacts as AgentMessage["artifacts"])
      : undefined,
    source_refs: Array.isArray(message.source_refs)
      ? (message.source_refs as AgentMessage["source_refs"])
      : undefined,
    warnings: Array.isArray(message.warnings)
      ? message.warnings.map(String)
      : undefined,
  };
}

function ArtifactView({ artifact }: { artifact: AgentArtifact }) {
  // 这里根据 artifact.type 做轻量结构化展示。后端已经把 summary/todos/quiz
  // 拆成稳定类型，前端无需理解技能内部逻辑，只需要按产物类型选择渲染方式。
  return (
    <details>
      <summary>{artifact.title}</summary>
      {artifact.type === "summary" ? (
        <SummaryArtifact content={artifact.content} />
      ) : artifact.type === "todos" ? (
        <TodoArtifact content={artifact.content} />
      ) : artifact.type === "quiz" ? (
        <QuizArtifact content={artifact.content} />
      ) : (
        <pre>{formatArtifactContent(artifact.content)}</pre>
      )}
    </details>
  );
}

function SummaryArtifact({ content }: { content: AgentArtifact["content"] }) {
  return <p className="artifact-summary">{formatArtifactContent(content)}</p>;
}

function TodoArtifact({ content }: { content: AgentArtifact["content"] }) {
  const items = Array.isArray(content) ? content : [];

  if (items.length === 0) {
    return <p className="artifact-empty">没有待办候选</p>;
  }

  return (
    <ul className="artifact-list">
      {items.map((item, index) => (
        <li key={index}>
          <strong>{stringValue(item.title, "未命名待办")}</strong>
          <span>
            置信度 {numberValue(item.confidence, 0).toFixed(2)}
            {item.due_time ? ` · 截止 ${String(item.due_time)}` : ""}
          </span>
        </li>
      ))}
    </ul>
  );
}

function QuizArtifact({ content }: { content: AgentArtifact["content"] }) {
  const items = Array.isArray(content) ? content : [];

  if (items.length === 0) {
    return <p className="artifact-empty">没有自测题</p>;
  }

  return (
    <ol className="artifact-list quiz-list">
      {items.map((item, index) => (
        <li key={index}>
          <strong>{stringValue(item.question, `第 ${index + 1} 题`)}</strong>
          <span>答案：{stringValue(item.answer, "暂无答案")}</span>
          {item.explanation ? <p>{String(item.explanation)}</p> : null}
        </li>
      ))}
    </ol>
  );
}

function formatArtifactContent(content: AgentArtifact["content"]): string {
  return typeof content === "string" ? content : JSON.stringify(content, null, 2);
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function numberValue(value: unknown, fallback: number): number {
  return typeof value === "number" ? value : fallback;
}

function SourceRefs({ refs }: { refs: AgentSourceRef[] }) {
  // 来源列表目前只展示类型、时间和文本；不拼后端文件路径，避免前端绕过 API
  // 直接依赖 data/sessions 的本地存储结构。
  const visibleRefs = refs.slice(0, maxVisibleSourceRefs);
  const hiddenCount = refs.length - visibleRefs.length;

  return (
    <div className="agent-source-list">
      {visibleRefs.map((ref) => (
        <div className="agent-source" key={`${ref.type}-${ref.id}`}>
          <span>
            {ref.type}
            {typeof ref.ts === "number" ? ` · ${formatClassTime(ref.ts)}` : ""}
          </span>
          <p>{compactSourceText(ref.text ?? "")}</p>
        </div>
      ))}
      {hiddenCount > 0 ? (
        <div className="agent-source-more">还有 {hiddenCount} 条来源已折叠</div>
      ) : null}
    </div>
  );
}

function compactSourceText(text: string): string {
  const normalized = text.replace(/\s+/g, " ").trim();
  if (normalized.length <= sourcePreviewChars) {
    return normalized;
  }
  return `${normalized.slice(0, sourcePreviewChars).trimEnd()}...`;
}

function formatAgentError(error: unknown): string {
  // ApiError 来自 services/api.ts，保留了后端返回的 detail。普通错误对象通常是
  // 网络、跨域或浏览器侧异常。
  if (error instanceof ApiError) {
    return error.detail;
  }

  if (error instanceof Error) {
    return error.message;
  }

  return "Agent 请求失败";
}
