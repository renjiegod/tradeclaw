// frontend/src/components/assistant/CollapsedProcessCard.tsx
//
// 非调试模式（简洁模式）下的"思考过程"卡片：
//
// - streaming=true：执行过程中只渲染这一张卡，头部是一行随最新事件更新的
//   进度文案（processStageText），不逐条铺开工具调用；
// - streaming=false：执行结束后折叠为摘要头（N 段思考 · M 个工具调用），
//   点击展开后按原始顺序混排 thinking 片段与 InlineToolCallCard 明细。
//
// 调试模式（逐卡渲染）不经过本组件，仍由 MessageContentRenderer 直接铺开。
// 本组件是纯渲染层，不改动 content_blocks / toolCallsByAttempt 等状态数据。

import { useState } from "react";
import { BulbOutlined, DownOutlined, LoadingOutlined, UpOutlined } from "@ant-design/icons";
import { Tag } from "antd";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { MODEL_INVOCATION_PROSE_CLASSNAME } from "../../styles/classNames";
import { InlineToolCallCard } from "./InlineToolCallCard";
import { toolStatusFromResult, type ToolResultBlock, type ToolUseBlock } from "./types";

export type ProcessStep =
  | { kind: "thinking"; content: string }
  | { kind: "text"; content: string }
  | { kind: "tool_call"; tool: ToolUseBlock; result?: ToolResultBlock };

/** 执行中头部单行文案：只看最新一条 step，随事件推进而更换。 */
export function processStageText(steps: ProcessStep[]): string {
  if (steps.length === 0) return "正在准备…";
  const last = steps[steps.length - 1];
  if (last.kind === "thinking") return "深度思考中…";
  if (last.kind === "text") return "整理回复中…";
  const status = toolStatusFromResult(last.tool, last.result);
  if (status === "completed") return `${last.tool.name} 完成`;
  if (status === "error") return `${last.tool.name} 失败`;
  return `${last.tool.name} 调用中…`;
}

/** 结束后折叠头的摘要文案。 */
export function processSummaryText(steps: ProcessStep[]): string {
  const thinkingCount = steps.filter((step) => step.kind === "thinking").length;
  const toolCount = steps.filter((step) => step.kind === "tool_call").length;
  const parts: string[] = [];
  if (thinkingCount > 0) parts.push(`${thinkingCount} 段思考`);
  if (toolCount > 0) parts.push(`${toolCount} 个工具调用`);
  return parts.length > 0 ? `思考过程 · ${parts.join(" · ")}` : "思考过程";
}

/** 失败的工具调用数——失败必须在折叠态就可见，不允许被摘要吞掉。 */
export function processErrorCount(steps: ProcessStep[]): number {
  return steps.filter(
    (step) => step.kind === "tool_call" && toolStatusFromResult(step.tool, step.result) === "error",
  ).length;
}

interface CollapsedProcessCardProps {
  steps: ProcessStep[];
  /** true = 执行中占位卡（spinner + 最新进度文案）；false = 已完成折叠摘要。 */
  streaming?: boolean;
}

export function CollapsedProcessCard({ steps, streaming = false }: CollapsedProcessCardProps) {
  const [open, setOpen] = useState(false);

  if (!streaming && steps.length === 0) return null;

  const headerText = streaming ? processStageText(steps) : processSummaryText(steps);
  const errorCount = processErrorCount(steps);
  const ToggleIcon = open ? UpOutlined : DownOutlined;

  return (
    <section
      className="w-full rounded-chat border border-chat-line bg-white px-5 py-4 text-chat-muted shadow-[0_14px_40px_rgba(15,23,42,0.04)]"
      data-testid="collapsed-process-card"
      data-streaming={streaming ? "true" : "false"}
      data-error-count={errorCount}
      aria-label={headerText}
    >
      <button
        type="button"
        className="flex w-full items-center justify-between gap-3 text-left"
        data-testid="process-card-header"
        aria-expanded={open}
        onClick={() => setOpen((prev) => !prev)}
      >
        <div className="flex min-w-0 items-center gap-2 text-base font-medium text-chat-muted">
          {streaming ? (
            <LoadingOutlined className="text-chat-accent" data-testid="process-card-spinner" />
          ) : (
            <BulbOutlined className="text-chat-muted" />
          )}
          <span className="truncate" data-testid="process-stage-text">
            {headerText}
          </span>
          {errorCount > 0 ? (
            <Tag color="error" className="text-xs" data-testid="process-card-error-tag">
              {errorCount} 个工具失败
            </Tag>
          ) : null}
        </div>
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full text-chat-muted transition hover:bg-chat-hover hover:text-shell-ink">
          <ToggleIcon />
        </span>
      </button>
      {open ? (
        <div
          className="mt-3 flex flex-col gap-3 border-l border-chat-line pl-4"
          data-testid="process-card-details"
        >
          {steps.map((step, index) => {
            if (step.kind === "thinking") {
              return (
                <div
                  key={`step-thinking-${index}`}
                  className={`${MODEL_INVOCATION_PROSE_CLASSNAME} max-w-none text-sm leading-7 text-chat-muted`}
                >
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{step.content}</ReactMarkdown>
                </div>
              );
            }
            if (step.kind === "text") {
              // 工具调用之间的中间过程叙述（非最终回答），保持原始顺序。
              return (
                <div
                  key={`step-text-${index}`}
                  className={`${MODEL_INVOCATION_PROSE_CLASSNAME} max-w-none text-sm leading-7`}
                >
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{step.content}</ReactMarkdown>
                </div>
              );
            }
            return (
              <InlineToolCallCard
                key={`step-tool-${step.tool.id}-${index}`}
                tool={step.tool}
                result={step.result}
              />
            );
          })}
        </div>
      ) : null}
    </section>
  );
}
