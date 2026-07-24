// frontend/src/components/assistant/MessageContentRenderer.tsx

import { useMemo, useState } from "react";
import { Button, Input, Tag, Tooltip } from "antd";
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
import { AssistantPanel } from "./panels/AssistantPanel";
import { parsePanelSpec, type PanelSpec } from "./panels/panelSpec";

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

// 名为 render_panel 的工具调用不渲染成普通工具卡，而是渲染成醒目的动态面板
// （K线 / 图表 / 知识图谱 / 表格 / 指标卡）。面板规范来自工具**调用参数**
// （tool.input —— arguments 已随 content_blocks 持久化、并随 tool.call 事件到达
// 前端，无需从被截断的 result 里取）。仅当结果非错误且能解析出至少一个有效块
// 时才渲染面板；否则（校验失败 / 半成品）回退到普通工具卡，让错误可见。
const RENDER_PANEL_TOOL = "render_panel";

function panelSpecFromEntry(entry: ToolCallEntry): PanelSpec | null {
  if (entry.tool.name !== RENDER_PANEL_TOOL) return null;
  if (entry.result?.is_error) return null;
  return parsePanelSpec(entry.tool.input);
}

interface MessageContentRendererProps {
  text: string;
  thinking?: string;
  thinkingBlocks?: Array<{ turn?: number; content: string }>;
  contentBlocks?: OrderedContentBlock[];
  toolCalls?: ToolCallEntry[];
  // Wired by AssistantPage: answering resolves the suspended ask_user tool
  // wait via the answer endpoint (fizz-style tool_result) — no synthetic user
  // message. `selected` are chosen option labels; `custom` is free-form text.
  onAnswerUserQuestion?: (
    questionId: string,
    answer: { selected: string[]; custom?: string },
  ) => void;
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
  onAnswer?: (questionId: string, answer: { selected: string[]; custom?: string }) => void;
  isPending: boolean;
}) {
  // Locked immediately on click/confirm so a slow round-trip (session config
  // refresh lagging behind the answer) can't let a double-click answer twice.
  const [submitting, setSubmitting] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [customText, setCustomText] = useState("");
  // Optimistic local recap: shown the instant the user answers, before the
  // backend clears the pending state / the run finishes and reloads the block.
  const [localAnswer, setLocalAnswer] = useState<{ selected: string[]; custom: string } | null>(
    null,
  );
  const multiSelect = Boolean(block.multi_select);

  // Answered = the backend stamped a recap onto the block (reload-safe) OR we
  // just answered locally. Either way the card collapses to a read-only recap
  // — the fizz "selection collapses into the card" behavior, never a separate
  // user bubble. A card that is no longer the pending one (superseded / turn
  // ended without an answer) also renders read-only, but without a selection.
  const answered = Boolean(block.answered) || localAnswer !== null;
  const resolved = answered || !isPending;
  const interactive = isPending && !answered && !submitting;
  const recapSelected = localAnswer?.selected ?? block.selected ?? [];
  const recapCustom = (localAnswer?.custom ?? block.custom ?? "") || "";

  const fire = (answer: { selected: string[]; custom?: string }) => {
    if (!interactive) return;
    const hasAnswer = answer.selected.length > 0 || Boolean(answer.custom?.trim());
    if (!hasAnswer) return;
    setSubmitting(true);
    setLocalAnswer({ selected: answer.selected, custom: answer.custom?.trim() || "" });
    onAnswer?.(block.question_id, { selected: answer.selected, custom: answer.custom?.trim() });
  };
  const answerSingle = (label: string) => fire({ selected: [label] });
  const toggleOption = (label: string) => {
    if (!interactive) return;
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  };
  const submitMulti = () =>
    fire({ selected: Array.from(selected), custom: customText.trim() || undefined });
  const submitCustom = () => fire({ selected: [], custom: customText.trim() });

  if (resolved) {
    const hasRecap = recapSelected.length > 0 || recapCustom.length > 0;
    const recapText =
      [...recapSelected, recapCustom].filter((part) => part && part.length > 0).join("、");
    return (
      <div
        className="rounded-xl border border-shell-line bg-gray-50/70 px-4 py-3 transition-colors"
        data-testid="assistant-user-question"
        data-resolved="true"
      >
        <div className="mb-2 flex items-center gap-2 text-sm text-gray-400">
          <CheckCircleFilled />
          {block.header ? <Tag color="default">{block.header}</Tag> : null}
          <span>{answered ? "该问题已回答" : "该问题已处理"}</span>
        </div>
        <div className={`mb-2 ${MODEL_INVOCATION_PROSE_CLASSNAME} opacity-70`}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{block.question}</ReactMarkdown>
        </div>
        {hasRecap ? (
          <div
            className="flex items-baseline gap-2 text-sm"
            data-testid="assistant-user-question-recap"
          >
            <span className="shrink-0 text-gray-400">你的选择</span>
            <span className="font-medium text-gray-700">{recapText}</span>
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div
      className="rounded-xl border border-blue-200 bg-blue-50/60 px-4 py-3 transition-colors"
      data-testid="assistant-user-question"
      data-resolved="false"
    >
      <div className="mb-2 flex items-center gap-2 text-sm text-blue-700">
        <QuestionCircleOutlined />
        {block.header ? <Tag color="blue">{block.header}</Tag> : null}
        <span>{multiSelect ? "需要你的选择（可多选）" : "需要你的选择"}</span>
      </div>
      <div className={`mb-3 ${MODEL_INVOCATION_PROSE_CLASSNAME}`}>
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
      {multiSelect ? (
        <div className="mt-2 flex justify-end">
          <Button
            size="small"
            type="primary"
            disabled={!interactive || (selected.size === 0 && customText.trim().length === 0)}
            onClick={submitMulti}
          >
            确认选择
          </Button>
        </div>
      ) : null}
      <div className="mt-2 flex items-center gap-2">
        <Input
          size="small"
          placeholder="其他（自定义回答）"
          value={customText}
          disabled={!interactive}
          onChange={(event) => setCustomText(event.target.value)}
          onPressEnter={multiSelect ? submitMulti : submitCustom}
          data-testid="assistant-user-question-custom"
        />
        {!multiSelect ? (
          <Button
            size="small"
            disabled={!interactive || customText.trim().length === 0}
            onClick={submitCustom}
            data-testid="assistant-user-question-custom-send"
          >
            发送
          </Button>
        ) : null}
      </div>
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

  // 把 render_panel 的面板规范解析结果按 tool_call_id memo 化，使 PanelSpec 及
  // 其内部块 / 数组（overlays / nodes / edges）跨渲染保持**引用稳定**。否则
  // parsePanelSpec 在每次渲染都产出新数组引用，会让下游 effect（kline overlay
  // 拉取）与 useMemo（kgraph 布局）在流式 / 父组件重渲染时反复重跑。memo key
  // 只序列化 render_panel 块的入参与错误态（都很小），值比较稳定即命中缓存。
  const renderPanelEntries = orderedBlocks
    .filter(
      (block): block is Extract<OrderedContentBlock, { type: "tool_call" }> =>
        block.type === "tool_call" && block.name === RENDER_PANEL_TOOL,
    )
    .map((block) => ({ id: block.tool_call_id, entry: resolveToolEntry(block) }));
  const panelMemoKey = JSON.stringify(
    renderPanelEntries.map(({ id, entry }) => [id, entry.tool.input, entry.result?.is_error ?? false]),
  );
  const panelSpecById = useMemo(() => {
    const map = new Map<string, PanelSpec | null>();
    for (const { id, entry } of renderPanelEntries) {
      map.set(id, panelSpecFromEntry(entry));
    }
    return map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [panelMemoKey]);

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
        | { kind: "question"; block: AssistantUserQuestionBlock }
        | { kind: "panel"; spec: PanelSpec; key: string };
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
          const spec = panelSpecById.get(block.tool_call_id) ?? null;
          if (spec) {
            // 面板留在过程卡之外醒目呈现（与 user_question 同策略）。
            outside.push({ kind: "panel", spec, key: block.tool_call_id });
          } else {
            steps.push({ kind: "tool_call", tool: entry.tool, result: entry.result });
          }
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
            if (item.kind === "panel") {
              return <AssistantPanel key={`panel-${item.key}-${index}`} spec={item.spec} />;
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
            const spec = panelSpecById.get(block.tool_call_id) ?? null;
            if (spec) {
              return <AssistantPanel key={`panel-${block.tool_call_id}-${index}`} spec={spec} />;
            }
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
