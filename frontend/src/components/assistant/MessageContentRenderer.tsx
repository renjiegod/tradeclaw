// frontend/src/components/assistant/MessageContentRenderer.tsx

import { useState } from "react";
import { Button, Tag, Tooltip } from "antd";
import { CheckCircleFilled, ExportOutlined, QuestionCircleOutlined } from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import { useNavigate } from "react-router-dom";
import remarkGfm from "remark-gfm";

import { MODEL_INVOCATION_PROSE_CLASSNAME } from "../../styles/classNames";
import type { AssistantUserQuestionBlock } from "../../types";
import { parseToolResultPreview, toolStatusFromResult, type ToolCallEntry } from "./types";
import { ThinkingBlock as ThinkingBlockComponent } from "../../pages/AssistantPage";
import {
  extractTaskIdFromToolResult,
  findBacktestTaskIdInBlocks,
  isBacktestProducingToolCall,
} from "./backtestNavigation";
import { InlineToolCallList } from "./InlineToolCallList";
import { InlineToolCallCard } from "./InlineToolCallCard";
import { CollapsedProcessCard, type ProcessStep } from "./CollapsedProcessCard";
import { stripReasoningTags } from "./reasoningTags";

type OrderedContentBlock =
  | { type: "thinking"; turn?: number; content: string }
  | {
      type: "tool_call";
      tool_call_id: string;
      name?: string;
      arguments?: Record<string, unknown>;
      category?: string | null;
      status?: "pending" | "running" | "completed" | "error";
      result_preview?: string;
      is_error?: boolean;
    }
  | { type: "text"; content: string }
  | AssistantUserQuestionBlock;

interface MessageContentRendererProps {
  text: string;
  thinking?: string;
  thinkingBlocks?: Array<{ turn?: number; content: string }>;
  contentBlocks?: OrderedContentBlock[];
  toolCalls?: ToolCallEntry[];
  // Wired by AssistantPage: clicking an option sends the
  // `/ask_user <question_id> <label>` protocol message to the session.
  onAnswerUserQuestion?: (questionId: string, label: string) => void;
  // The session's currently pending `ask_user_question` id (from
  // `activeSession.config.pending_user_question.question_id`), or `null`
  // when nothing is pending. `undefined` (caller doesn't track it) falls
  // back to the old always-interactive behavior. Any question block whose
  // id doesn't match renders as a disabled, read-only recap — this is what
  // keeps a superseded/already-answered card from looking clickable after
  // the fact (the backend already treats a stale click as a structured
  // `user_question.stale_answer` event; this just makes that visible
  // instead of silently confusing).
  pendingQuestionId?: string | null;
  // 渲染模式。true（默认，保持既有行为）= 调试模式：逐条铺开每个
  // thinking / tool_call 卡片；false = 简洁模式：把 thinking 与工具调用
  // 折叠进 CollapsedProcessCard，text / user_question 保持原位。
  // 纯渲染层开关，不影响 content_blocks / toolCallsByAttempt 数据。
  debugMode?: boolean;
  // 简洁模式下，本条内容是否仍在流式执行中：为 true 时末尾的过程卡片
  // 显示"执行中占位卡"（spinner + 最新进度文案）而非完成态摘要。
  streaming?: boolean;
}

function UserQuestionCard({
  block,
  onAnswer,
  isPending,
}: {
  block: AssistantUserQuestionBlock;
  onAnswer?: (questionId: string, label: string) => void;
  isPending: boolean;
}) {
  // Locked immediately on click/confirm so a slow round-trip (session config
  // refresh lagging behind the sent message) can't let a double-click fire
  // the answer twice.
  const [submitting, setSubmitting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const multiSelect = Boolean(block.multi_select);
  const resolved = !isPending;
  const interactive = isPending && !submitting;

  const answerSingle = (label: string) => {
    if (!interactive) return;
    setSubmitting(true);
    onAnswer?.(block.question_id, label);
  };
  const toggleOption = (label: string) => {
    if (!interactive) return;
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  };
  const submitSelection = () => {
    if (!interactive || selected.size === 0) return;
    setSubmitting(true);
    onAnswer?.(block.question_id, Array.from(selected).join("、"));
  };

  return (
    <div
      className={`rounded-xl border px-4 py-3 transition-colors ${
        resolved ? "border-shell-line bg-gray-50/70" : "border-blue-200 bg-blue-50/60"
      }`}
      data-testid="assistant-user-question"
      data-resolved={resolved ? "true" : "false"}
    >
      <div className={`mb-2 flex items-center gap-2 text-sm ${resolved ? "text-gray-400" : "text-blue-700"}`}>
        {resolved ? <CheckCircleFilled /> : <QuestionCircleOutlined />}
        {block.header ? <Tag color={resolved ? "default" : "blue"}>{block.header}</Tag> : null}
        <span>
          {resolved ? "该问题已处理" : multiSelect ? "需要你的选择（可多选）" : "需要你的选择"}
        </span>
      </div>
      <div className={`mb-3 ${MODEL_INVOCATION_PROSE_CLASSNAME} ${resolved ? "opacity-70" : ""}`}>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{block.question}</ReactMarkdown>
      </div>
      <div className="flex flex-wrap gap-2">
        {block.options.map((option) => {
          const isSelected = selected.has(option.label);
          return (
            <Tooltip key={option.label} title={option.description || undefined}>
              <Button
                size="small"
                type={multiSelect && isSelected ? "primary" : "default"}
                disabled={!interactive}
                onClick={() => (multiSelect ? toggleOption(option.label) : answerSingle(option.label))}
              >
                {option.label}
              </Button>
            </Tooltip>
          );
        })}
      </div>
      {multiSelect && !resolved ? (
        <div className="mt-2 flex justify-end">
          <Button
            size="small"
            type="primary"
            disabled={!interactive || selected.size === 0}
            onClick={submitSelection}
          >
            确认选择
          </Button>
        </div>
      ) : null}
      {!resolved ? <div className="mt-2 text-xs text-gray-400">也可以直接输入你的回答</div> : null}
    </div>
  );
}

export function MessageContentRenderer({
  text,
  thinking,
  thinkingBlocks,
  contentBlocks,
  toolCalls,
  onAnswerUserQuestion,
  pendingQuestionId,
  debugMode = true,
  streaming = false,
}: MessageContentRendererProps) {
  const navigate = useNavigate();
  const blocks =
    thinkingBlocks?.filter((block) => typeof block.content === "string" && block.content.trim()) ?? [];
  const toolById = new Map((toolCalls ?? []).map((entry) => [entry.tool.id, entry]));
  const orderedBlocks = contentBlocks?.filter((block) => {
    if (block.type === "tool_call") return Boolean(block.tool_call_id);
    if (block.type === "user_question") {
      return Boolean(block.question_id) && Array.isArray(block.options) && block.options.length > 0;
    }
    return typeof block.content === "string" && block.content.trim();
  }) ?? [];
  const { visible: textVisible, thinking: textInlineThinking } = stripReasoningTags(text ?? "");

  // 把持久化的 tool_call block 解析成 ToolCallEntry：优先取实时 toolCalls
  // 映射（活跃流），否则用 block 自带的 preview 兜底。调试 / 简洁两种
  // 渲染路径共用，保证两种模式看到的是同一份工具调用数据。
  const resolveToolEntry = (block: Extract<OrderedContentBlock, { type: "tool_call" }>): ToolCallEntry => {
    const previewResult = parseToolResultPreview(block.result_preview, block.is_error);
    const fallbackTool = {
      type: "tool_use" as const,
      id: block.tool_call_id,
      name: block.name || block.tool_call_id,
      category: block.category ?? undefined,
      input: block.arguments ?? {},
      status: toolStatusFromResult(
        {
          status: block.status ?? (previewResult ? "completed" : "running"),
        },
        previewResult,
      ),
    };
    return (
      toolById.get(block.tool_call_id) ?? {
        tool: fallbackTool,
        result: previewResult
          ? { ...previewResult, tool_use_id: block.tool_call_id }
          : undefined,
      }
    );
  };

  // Compute the message-footer backtest jump target. Two source paths to
  // try, in order:
  //
  //   1. ``orderedBlocks`` — the persisted ``content_blocks`` payload that
  //      carries inline tool_call metadata (name + arguments + preview).
  //      Most messages use this path; ``findBacktestTaskIdInBlocks`` handles
  //      the task_id extraction.
  //
  //   2. ``toolCalls`` — the legacy in-memory tool-state map kept by the
  //      assistant page. We fall back here so messages that haven't been
  //      persisted yet (active stream) still surface the affordance.
  //
  // The button renders only once per message, anchored at the bottom so
  // it stays reachable even after the user scrolls past the
  // ``run_strategy_backtest`` tool card.
  const footerTaskId: string | null = (() => {
    const fromOrdered = findBacktestTaskIdInBlocks(orderedBlocks);
    if (fromOrdered) return fromOrdered;
    if (!toolCalls) return null;
    let latest: string | null = null;
    for (const entry of toolCalls) {
      const args =
        entry.tool.input && typeof entry.tool.input === "object"
          ? (entry.tool.input as Record<string, unknown>)
          : undefined;
      if (!isBacktestProducingToolCall({ name: entry.tool.name, arguments: args })) continue;
      if (entry.result?.is_error) continue;
      if (entry.tool.status !== "completed") continue;
      // ``arguments.task_id`` shortcut only applies to the native backtest
      // tools — for ``execute_bash`` the only ``task_id`` in input would be
      // a background bash task id, not a backtest task.
      if (entry.tool.name !== "execute_bash" && args) {
        const argId = args["task_id"];
        if (typeof argId === "string" && argId) {
          latest = argId;
          continue;
        }
      }
      const fromOutput = extractTaskIdFromToolResult(entry.result?.output);
      if (fromOutput) latest = fromOutput;
    }
    return latest;
  })();

  const footer = footerTaskId ? (
    <div
      className="flex items-center justify-end pt-1"
      data-testid="message-backtest-jump"
    >
      <Button
        type="link"
        size="small"
        icon={<ExportOutlined />}
        onClick={() => navigate(`/tasks/${encodeURIComponent(footerTaskId)}`)}
      >
        查看回测任务详情
      </Button>
    </div>
  ) : null;

  if (orderedBlocks.length > 0) {
    // Defensive fallback: when content_blocks omits the final answer text
    // (e.g. backend returned only intermediate text + tool_calls, or persisted
    // a max_turns_reached / partial run), the assistant's reply would visually
    // "cut off" at the last tool_call. Append item.content as a trailing text
    // block if the existing blocks don't already end with it.
    const trimmedText = textVisible.trim();
    const lastTextBlock = [...orderedBlocks].reverse().find((b) => b.type === "text") as
      | { type: "text"; content: string }
      | undefined;
    const lastTextContent = stripReasoningTags(lastTextBlock?.content ?? "").visible.trim();
    const shouldAppendFallbackText = Boolean(trimmedText) && trimmedText !== lastTextContent;

    if (!debugMode) {
      // 简洁模式：一次 Agent loop（一条消息）只出现一张过程卡。thinking /
      // tool_call / 工具调用之间的中间叙述文本全部按原始顺序收进
      // CollapsedProcessCard；只有两类内容留在卡外：
      //   1. 收尾的最终回答文本（最后一个过程块之后的 text）；
      //   2. user_question 问答卡（必须保持可交互，不能折叠）。
      // text 里剥出的内联 <think> 片段同样并入过程卡。
      type OutsideItem =
        | { kind: "text"; content: string }
        | { kind: "question"; block: AssistantUserQuestionBlock };
      const steps: ProcessStep[] = [];
      const outside: OutsideItem[] = [];
      // 最后一个过程块（thinking / tool_call）的位置：其后的 text 是最终回答。
      let lastProcessBlockIndex = -1;
      orderedBlocks.forEach((block, index) => {
        if (block.type === "thinking" || block.type === "tool_call") {
          lastProcessBlockIndex = index;
        }
      });
      orderedBlocks.forEach((block, index) => {
        if (block.type === "thinking") {
          steps.push({ kind: "thinking", content: block.content });
        } else if (block.type === "tool_call") {
          const entry = resolveToolEntry(block);
          steps.push({ kind: "tool_call", tool: entry.tool, result: entry.result });
        } else if (block.type === "user_question") {
          outside.push({ kind: "question", block });
        } else {
          const { visible: blockVisibleText, thinking: blockInlineThinking } = stripReasoningTags(
            block.content,
          );
          if (blockInlineThinking) {
            steps.push({ kind: "thinking", content: blockInlineThinking });
          }
          if (!blockVisibleText.trim()) return;
          if (index < lastProcessBlockIndex) {
            steps.push({ kind: "text", content: blockVisibleText });
          } else {
            outside.push({ kind: "text", content: blockVisibleText });
          }
        }
      });
      return (
        <div className="flex flex-col gap-3">
          {steps.length > 0 || streaming ? (
            <CollapsedProcessCard steps={steps} streaming={streaming} />
          ) : null}
          {outside.map((item, index) => {
            if (item.kind === "question") {
              const isPending =
                pendingQuestionId === undefined
                  ? true
                  : pendingQuestionId === item.block.question_id;
              return (
                <UserQuestionCard
                  key={`question-${item.block.question_id}-${index}`}
                  block={item.block}
                  onAnswer={onAnswerUserQuestion}
                  isPending={isPending}
                />
              );
            }
            return (
              <div key={`text-${index}`} className={`${MODEL_INVOCATION_PROSE_CLASSNAME}`}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{item.content}</ReactMarkdown>
              </div>
            );
          })}
          {shouldAppendFallbackText ? (
            <div key="text-fallback" className={`${MODEL_INVOCATION_PROSE_CLASSNAME}`}>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{textVisible}</ReactMarkdown>
            </div>
          ) : null}
          {footer}
        </div>
      );
    }

    return (
      <div className="flex flex-col gap-3">
        {orderedBlocks.map((block, index) => {
          if (block.type === "thinking") {
            return (
              <ThinkingBlockComponent
                key={`thinking-${block.turn ?? index}-${index}`}
                content={block.content}
              />
            );
          }
          if (block.type === "tool_call") {
            const entry = resolveToolEntry(block);
            return (
              <InlineToolCallCard
                key={`tool-${block.tool_call_id}-${index}`}
                tool={entry.tool}
                result={entry.result}
              />
            );
          }
          if (block.type === "user_question") {
            const isPending =
              pendingQuestionId === undefined ? true : pendingQuestionId === block.question_id;
            return (
              <UserQuestionCard
                key={`question-${block.question_id}-${index}`}
                block={block}
                onAnswer={onAnswerUserQuestion}
                isPending={isPending}
              />
            );
          }
          const { visible: blockVisibleText, thinking: blockInlineThinking } = stripReasoningTags(
            block.content,
          );
          return (
            <div key={`text-${index}`} className="flex flex-col gap-3">
              {blockInlineThinking && <ThinkingBlockComponent content={blockInlineThinking} />}
              <div className={`${MODEL_INVOCATION_PROSE_CLASSNAME}`}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{blockVisibleText}</ReactMarkdown>
              </div>
            </div>
          );
        })}
        {shouldAppendFallbackText ? (
          <div key="text-fallback" className={`${MODEL_INVOCATION_PROSE_CLASSNAME}`}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{textVisible}</ReactMarkdown>
          </div>
        ) : null}
        {footer}
      </div>
    );
  }
  if (!debugMode) {
    // 简洁模式 fallback（无 content_blocks 的旧消息 / 活跃流早期）：把
    // thinking 与 toolCalls 全部折叠进一个过程卡，正文照常渲染。
    const fallbackSteps: ProcessStep[] = [
      ...(blocks.length > 0
        ? blocks.map((block): ProcessStep => ({ kind: "thinking", content: block.content }))
        : thinking
          ? [{ kind: "thinking", content: thinking } satisfies ProcessStep]
          : []),
      ...(textInlineThinking
        ? [{ kind: "thinking", content: textInlineThinking } satisfies ProcessStep]
        : []),
      ...(toolCalls ?? []).map(
        (entry): ProcessStep => ({ kind: "tool_call", tool: entry.tool, result: entry.result }),
      ),
    ];
    return (
      <div className="flex flex-col gap-3">
        {(fallbackSteps.length > 0 || streaming) && (
          <CollapsedProcessCard steps={fallbackSteps} streaming={streaming} />
        )}
        {textVisible && (
          <div className={`${MODEL_INVOCATION_PROSE_CLASSNAME}`}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{textVisible}</ReactMarkdown>
          </div>
        )}
        {footer}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      {blocks.length > 0
        ? blocks.map((block, index) => (
            <ThinkingBlockComponent
              key={`${block.turn ?? index}-${index}`}
              content={block.content}
            />
          ))
        : thinking && <ThinkingBlockComponent content={thinking} />}
      {textInlineThinking && <ThinkingBlockComponent content={textInlineThinking} />}
      {toolCalls && toolCalls.length > 0 && (
        <InlineToolCallList entries={toolCalls} />
      )}
      {textVisible && (
        <div className={`${MODEL_INVOCATION_PROSE_CLASSNAME}`}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{textVisible}</ReactMarkdown>
        </div>
      )}
      {footer}
    </div>
  );
}
