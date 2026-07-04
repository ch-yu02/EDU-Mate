import { useMemo, useState } from "react";

import { EmptyState } from "./EmptyState";
import { MarkdownContent } from "./MarkdownContent";
import type { PostClassStatus, SessionPostClassArtifacts } from "../types/classroom";

// 课后产物面板。
//
// 这个组件只展示已经由历史详情 API 返回的产物，不主动 fetch，也不直接拼
// data/sessions 路径。这样前端仍然只依赖后端 API 契约；后续如果要做“下载
// summary.md”或“打开 quiz.json”，可以在 service 层新增明确接口。
type ArtifactTab = "summary" | "todos" | "quiz";

type PostClassArtifactsPanelProps = {
  artifacts: SessionPostClassArtifacts;
  canGenerateQuiz?: boolean;
  isQuizGenerating?: boolean;
  onGenerateQuiz?: () => void;
  status?: PostClassStatus;
};

const tabs: Array<{ id: ArtifactTab; label: string }> = [
  { id: "summary", label: "总结" },
  { id: "todos", label: "待办" },
  { id: "quiz", label: "自测" },
];

export function PostClassArtifactsPanel({
  artifacts,
  canGenerateQuiz = false,
  isQuizGenerating = false,
  onGenerateQuiz,
  status = "idle",
}: PostClassArtifactsPanelProps) {
  // 三类产物共用一个紧凑的 tab 状态。默认显示总结，因为它是学生打开历史课
  // 后最常见的第一入口。
  const [activeTab, setActiveTab] = useState<ArtifactTab>("summary");
  // 计数用于按钮上给用户一个直接信号：当前历史课堂到底生成了哪些产物。
  const counts = useMemo(
    () => ({
      summary: artifacts.summary_markdown ? 1 : 0,
      todos: artifacts.todos.length,
      quiz: artifacts.quiz.length,
    }),
    [artifacts],
  );
  const totalCount = counts.summary + counts.todos + counts.quiz;
  const isGenerating = status === "generating";
  const isFailed = status === "failed";
  // 空态文案区分自动产物和主动产物：
  // - summary/todos 在结束课堂时由后端自动生成；
  // - quiz 在当前面板里主动生成，生成后会保存到 quiz.json 并立刻显示。
  const emptyLabel =
    activeTab === "quiz" ? "点击生成自测后显示" : "结束课堂后自动生成";
  const panelStatus = isGenerating
    ? "生成中"
    : isFailed
      ? "生成失败"
      : totalCount > 0
        ? "已生成"
        : "暂无产物";
  const contentEmptyLabel = isGenerating
    ? "课后产物生成中"
    : isFailed
      ? "课后产物生成失败，可稍后刷新历史课堂"
      : emptyLabel;

  return (
    <section className="panel post-class-panel" aria-labelledby="post-class-title">
      <div className="panel-header">
        <div>
          <h2 id="post-class-title">课后产物</h2>
          <span>{panelStatus}</span>
        </div>
        <div className="segmented-control compact">
          {tabs.map((tab) => (
            <button
              className={activeTab === tab.id ? "active" : ""}
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              type="button"
            >
              {tab.label}
              {counts[tab.id] > 0 ? ` ${counts[tab.id]}` : ""}
            </button>
          ))}
        </div>
      </div>

      <div className="post-class-content">
        {activeTab === "quiz" ? (
          <div className="post-class-actions">
            <button
              className="primary-button"
              disabled={!canGenerateQuiz || isQuizGenerating}
              onClick={onGenerateQuiz}
              type="button"
            >
              {isQuizGenerating
                ? "生成中"
                : counts.quiz > 0
                  ? "重新生成自测"
                  : "生成自测"}
            </button>
            <span>
              {canGenerateQuiz
                ? "根据当前课堂资料生成 3-5 道自测题"
                : "结束课堂或打开历史课堂后可生成自测"}
            </span>
          </div>
        ) : null}

        {/* 旧历史课堂可能没有这些文件；正在录制的课堂也不会有历史产物。 */}
        {(isGenerating || isFailed) && totalCount === 0 ? (
          <EmptyState label={contentEmptyLabel} />
        ) : activeTab === "summary" ? (
          <SummaryView summary={artifacts.summary_markdown} />
        ) : activeTab === "todos" ? (
          <TodoView todos={artifacts.todos} />
        ) : (
          <QuizView quiz={artifacts.quiz} />
        )}
      </div>
    </section>
  );
}

function SummaryView({ summary }: { summary?: string | null }) {
  if (!summary) {
    return <EmptyState label="暂无总结" />;
  }

  return (
    <MarkdownContent
      className="post-class-summary post-class-summary-markdown"
      content={summary}
    />
  );
}

function TodoView({ todos }: { todos: Array<Record<string, unknown>> }) {
  // todos.json 目前由规则版 TodoDetectiveSkill 生成。字段仍然比较宽松，所以
  // 这里用 stringValue/numberValue 做防御式展示，避免坏数据把 UI 撞崩。
  if (todos.length === 0) {
    return <EmptyState label="暂无待办" />;
  }

  return (
    <ul className="post-class-list">
      {todos.map((todo, index) => (
        <li key={index}>
          <strong>{stringValue(todo.title, "未命名待办")}</strong>
          <span>置信度 {numberValue(todo.confidence, 0).toFixed(2)}</span>
        </li>
      ))}
    </ul>
  );
}

function QuizView({ quiz }: { quiz: Array<Record<string, unknown>> }) {
  // quiz.json 当前是短答题列表。它不会随结束课堂自动生成，而是用户在本面板
  // 主动触发后写入。未来如果支持选择题，可以在这里扩展 options 的结构化
  // 展示，而不需要改历史详情读取流程。
  if (quiz.length === 0) {
    return <EmptyState label="暂无自测题" />;
  }

  return (
    <ol className="post-class-list">
      {quiz.map((item, index) => (
        <li key={index}>
          <strong>{stringValue(item.question, `第 ${index + 1} 题`)}</strong>
          <span>答案：{stringValue(item.answer, "暂无答案")}</span>
          {item.explanation ? <p>{String(item.explanation)}</p> : null}
        </li>
      ))}
    </ol>
  );
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function numberValue(value: unknown, fallback: number): number {
  return typeof value === "number" ? value : fallback;
}
