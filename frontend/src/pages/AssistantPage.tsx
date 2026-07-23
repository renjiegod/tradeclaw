import {
  BulbOutlined,
  CopyOutlined,
  DownOutlined,
  FormOutlined,
  MenuUnfoldOutlined,
  PaperClipOutlined,
  PlusOutlined,
  RobotOutlined,
  SendOutlined,
  StopOutlined,
  ToolOutlined,
  UpOutlined,
} from "@ant-design/icons";
import { Alert, Button, Card, Drawer, Empty, Input, List, Modal, Select, Space, Spin, Switch, Tabs, Tag, Tooltip, Typography, message } from "antd";
import type { TextAreaRef } from "antd/es/input/TextArea";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  assistantEventStreamUrl,
  createAssistantSession,
  getAssistantSession,
  listAssistantAgents,
  listAssistantChannels,
  listAssistantEvents,
  listAssistantMessages,
  listAssistantSessions,
  answerAssistantQuestion,
  listAssistantTools,
  listModelRoutes,
  listPendingApprovals,
  listPendingAssistantApprovals,
  listPendingAssistantQuestions,
  resolveAssistantApproval,
  sendAssistantMessage,
  stopAssistantSession,
  uploadFile,
} from "../api";
import { useSearchParams } from "react-router-dom";
import { AssistantWelcome } from "../components/assistant/AssistantWelcome";
import { SkillsToolsTab } from "../components/assistant/SkillsToolsTab";
import { serializeSession } from "../components/assistant/serializeSession";
import { TracesPanel } from "../components/TracesPanel";
import { usePageRefreshToken } from "../pageRefreshContext";
import { MODEL_INVOCATION_PROSE_CLASSNAME } from "../styles/classNames";
import type {
  Agent,
  AssistantChannel,
  AssistantEvent,
  AssistantMessage,
  AssistantPendingApproval,
  AssistantSession,
  AssistantUserQuestionBlock,
  MessageAttachment,
  ModelRouteRow,
  PendingApproval,
  TraceSummary,
} from "../types";
import { type ToolCallEntry } from "../components/assistant/types";
import { ApprovalQueueCard } from "../components/ApprovalQueueCard";
import { ToolbarButton } from "../components/ToolbarButton";
import { MessageContentRenderer } from "../components/assistant/MessageContentRenderer";
import { ThinkingSpinner } from "../components/assistant/ThinkingSpinner";
import {
  buildStreamingFromEvents,
  findCurrentAttemptId,
  formatMessageTime,
  formatSessionOptionTitle,
  formatSessionSourceChannelLabel,
  isEventForCurrentAttempt,
  isScrolledNearBottom,
  parsePreview,
  rebuildToolCallsMaps,
} from "../components/assistant/streamHelpers";

const DEFAULT_TITLE = "新会话";

// How many of the session's most-recent events to fetch when reconstructing
// "is a run currently in flight" state (see refreshSessionData below). Must
// stay generous: a single long turn can emit hundreds of `thinking.delta` /
// `message.delta` rows (persisted per token), so a small window can miss the
// in-flight attempt's own `attempt.started` event entirely.
const EVENTS_TAIL_LIMIT = 500;

export function ThinkingBlock({ content, streaming = false }: { content: string; streaming?: boolean }) {
  const [open, setOpen] = useState(true);

  if (!content.trim()) {
    return null;
  }

  const title = streaming ? "深度思考中..." : "深度思考已完成";
  const ToggleIcon = open ? UpOutlined : DownOutlined;

  return (
    <section
      className="relative mb-5 w-full rounded-chat border border-chat-line bg-white px-5 py-4 text-chat-muted shadow-[0_14px_40px_rgba(15,23,42,0.04)]"
      aria-label={title}
    >
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2 text-base font-medium text-chat-muted">
          <BulbOutlined className={streaming ? "animate-pulse text-chat-accent" : "text-chat-muted"} />
          <span className="truncate">{title}</span>
        </div>
        <button
          type="button"
          className="grid h-8 w-8 shrink-0 place-items-center rounded-full text-chat-muted transition hover:bg-chat-hover hover:text-shell-ink"
          aria-label={open ? "收起思考内容" : "展开思考内容"}
          aria-expanded={open}
          onClick={() => setOpen((prev) => !prev)}
        >
          <ToggleIcon />
        </button>
      </div>
      {open ? (
        <>
          <div className={`${MODEL_INVOCATION_PROSE_CLASSNAME} max-w-none text-[15px] leading-8 text-chat-muted`}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
          </div>
          <button
            type="button"
            className="absolute bottom-0 left-1/2 grid h-9 w-9 -translate-x-1/2 translate-y-1/2 place-items-center rounded-full border border-chat-line bg-white text-chat-ink shadow-[0_10px_24px_rgba(15,23,42,0.08)] transition hover:bg-chat-surface"
            aria-label="收起思考内容"
            onClick={() => setOpen(false)}
          >
            <DownOutlined />
          </button>
        </>
      ) : null}
    </section>
  );
}

function MessageBubble({
  item,
  toolCalls = [],
  onAnswerUserQuestion,
  pendingQuestionId,
  debugMode = false,
}: {
  item: AssistantMessage;
  toolCalls?: ToolCallEntry[];
  onAnswerUserQuestion?: (
    questionId: string,
    answer: { selected: string[]; custom?: string },
  ) => void;
  pendingQuestionId?: string | null;
  debugMode?: boolean;
}) {
  const isUser = item.role === "user";
  const thinking =
    typeof item.metadata?.thinking === "string" ? item.metadata.thinking : "";
  const thinkingBlocks = Array.isArray(item.metadata?.thinking_blocks)
    ? item.metadata.thinking_blocks.filter(
        (block): block is { turn?: number; content: string } =>
          typeof block === "object" &&
          block !== null &&
          typeof (block as { content?: unknown }).content === "string",
      )
    : undefined;
  const contentBlocks = Array.isArray(item.metadata?.content_blocks)
    ? item.metadata.content_blocks.filter(
        (
          block,
        ): block is
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
          | AssistantUserQuestionBlock => {
          if (typeof block !== "object" || block === null) return false;
          const value = block as Record<string, unknown>;
          if (value.type === "thinking" || value.type === "text") {
            return typeof value.content === "string";
          }
          if (value.type === "user_question") {
            return typeof value.question_id === "string" && Array.isArray(value.options);
          }
          return value.type === "tool_call" && typeof value.tool_call_id === "string";
        },
      )
    : undefined;
  // A run that failed mid-stream now persists an assistant message flagged failed
  // (with the partial work, if any). Surface it as an error banner so the failure
  // is visible rather than the chat appearing to show only the user's query.
  const failed = item.metadata?.failed === true;
  const errorType =
    typeof item.metadata?.error_type === "string" ? item.metadata.error_type : "";
  const isPartial = item.metadata?.partial === true;
  return (
    <div className={`flex w-full flex-col ${isUser ? "items-end" : "items-start"}`}>
      <div
        className={`${
          isUser
            ? "min-w-0 max-w-[72%] break-words rounded-bubble bg-chat-bubble px-5 py-4 text-lg text-chat-ink"
            : "min-w-0 w-full max-w-[860px] break-words text-chat-ink"
        }`}
      >
        {!isUser ? (
          <>
            {failed ? (
              <Alert
                type="error"
                showIcon
                className="!mb-3"
                message={`本轮运行失败${errorType ? `：${errorType}` : ""}`}
                description={
                  isPartial
                    ? "已生成部分内容，下方为中断前已完成的工作。可重新发送以继续。"
                    : "运行中断，未生成回答。诊断详情见调试会话（trace 已记录）。可重新发送重试。"
                }
              />
            ) : null}
            <MessageContentRenderer
              text={item.content}
              thinking={thinking}
              thinkingBlocks={thinkingBlocks}
              contentBlocks={contentBlocks}
              toolCalls={toolCalls}
              onAnswerUserQuestion={onAnswerUserQuestion}
              pendingQuestionId={pendingQuestionId}
              debugMode={debugMode}
            />
          </>
        ) : (
          <>
            {Array.isArray(item.metadata?.attachments) && item.metadata.attachments.length > 0 ? (
              <div
                className={`flex flex-wrap justify-end gap-2 ${item.content ? "mb-2" : ""}`}
              >
                {item.metadata.attachments.map((att) => (
                  <span
                    key={att.file_id}
                    className="inline-flex max-w-[220px] items-center gap-1 rounded-lg bg-white/60 px-2 py-1 text-sm text-chat-ink"
                    title={att.filename}
                  >
                    <PaperClipOutlined className="shrink-0 text-chat-accent" />
                    <span className="truncate">{att.filename}</span>
                  </span>
                ))}
              </div>
            ) : null}
            {item.content ? (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{item.content}</ReactMarkdown>
            ) : null}
          </>
        )}
      </div>
      <span className="mt-1 px-1 text-xs text-gray-400">
        {formatMessageTime(item.created_at)}
      </span>
    </div>
  );
}

function EventTimeline({ events }: { events: AssistantEvent[] }) {
  const toolEvents = useMemo(
    () =>
      events.filter((event) =>
        ["attempt.started", "tool.call", "tool.result", "attempt.completed", "attempt.failed"].includes(
          event.event_type,
        ),
      ),
    [events],
  );

  if (toolEvents.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 Agent 事件" />;
  }

  return (
    <div className="max-h-64 overflow-y-auto">
      <List
        size="small"
        dataSource={toolEvents}
        renderItem={(event) => {
          const tool = typeof event.payload.tool === "string" ? event.payload.tool : "";
          const span = typeof event.payload.span_id === "string" ? event.payload.span_id : "";
          const preview = typeof event.payload.preview === "string" ? event.payload.preview : "";
          return (
            <List.Item className="!items-start">
              <Space direction="vertical" size={2} className="w-full">
                <Space wrap>
                  <Tag icon={<ToolOutlined />} color={event.event_type.includes("failed") ? "error" : "processing"}>
                    {event.event_type}
                  </Tag>
                  {tool ? <Tag>{tool}</Tag> : null}
                  {span ? <Typography.Text code>{span}</Typography.Text> : null}
                </Space>
                {preview ? (
                  <Typography.Paragraph className="!mb-0 !text-xs" ellipsis={{ rows: 2, expandable: true }}>
                    {preview}
                  </Typography.Paragraph>
                ) : null}
              </Space>
            </List.Item>
          );
        }}
      />
    </div>
  );
}

export function AssistantPage() {
  const pageRefreshToken = usePageRefreshToken();
  const [searchParams, setSearchParams] = useSearchParams();
  const [sessions, setSessions] = useState<AssistantSession[]>([]);
  const [assistantChannels, setAssistantChannels] = useState<AssistantChannel[]>([]);
  const [sessionChannelFilter, setSessionChannelFilter] = useState<string>("all");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [pendingUserMessage, setPendingUserMessage] = useState<AssistantMessage | null>(null);
  const [events, setEvents] = useState<AssistantEvent[]>([]);
  // by session_id -> tool_call_id (DB schema compat)
  const [toolCallsState, setToolCallsState] = useState<Record<string, Record<string, ToolCallEntry>>>({});
  // by attempt_id -> tool_call_id (for message-level rendering)
  const [toolCallsByAttempt, setToolCallsByAttempt] = useState<Record<string, Record<string, ToolCallEntry>>>({});
  // ref to current attempt_id (updated on attempt.started, used during SSE streaming)
  const currentAttemptIdRef = useRef<string>("");
  const [modelRoutes, setModelRoutes] = useState<ModelRouteRow[]>([]);
  const [selectedModelRoute, setSelectedModelRoute] = useState<string | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [streamingContent, setStreamingContent] = useState("");
  const [streamingThinking, setStreamingThinking] = useState("");
  const [streamingThinkingBlocks, setStreamingThinkingBlocks] = useState<Array<{ turn?: number; content: string }>>([]);
  const [streamingContentBlocks, setStreamingContentBlocks] = useState<
    Array<
      | { type: "thinking"; turn?: number; content: string }
      | { type: "tool_call"; tool_call_id: string }
      | { type: "text"; content: string }
    >
  >([]);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  sendingRef.current = sending;
  const [isStopping, setIsStopping] = useState(false);
  const [attachments, setAttachments] = useState<MessageAttachment[]>([]);
  const [activeRightTab, setActiveRightTab] = useState<"traces" | "skills-tools">("traces");
  // <lg 视口下右栏（会话 / Traces）收进抽屉，聊天区独占一列
  const [mobileRailOpen, setMobileRailOpen] = useState(false);
  // 对话渲染模式。默认简洁模式（false）：执行中只显示一个随进度更新的
  // 占位卡片，完成后工具调用折叠进"思考过程"卡片。打开调试模式（true）
  // 恢复旧行为：逐条铺开每个工具调用与思考卡片。仅前端展示层开关，
  // 不影响事件持久化 / 复制会话导出 / trace。
  const [debugRenderMode, setDebugRenderMode] = useState(
    () => localStorage.getItem("assistant_debug_mode") === "true",
  );
  const handleDebugRenderModeChange = useCallback((value: boolean) => {
    setDebugRenderMode(value);
    localStorage.setItem("assistant_debug_mode", String(value));
  }, []);
  const [pendingTraceId, setPendingTraceId] = useState<string | null>(null);
  // Blocking tool-call approval awaiting the user's decision (mid-turn).
  // Fed by the `approval.requested` SSE event; cleared on resolution /
  // timeout / attempt end; recovered after refresh via the pending API.
  const [pendingApproval, setPendingApproval] = useState<AssistantPendingApproval | null>(null);
  // A blocking ask_user_question awaiting the user (mid-turn, fizz-style): the
  // tool call is suspended server-side. Fed by `user_question.asked` SSE while
  // the run is suspended (the persisted content block only lands when the turn
  // finishes); cleared on answer / timeout / attempt end; recovered after a
  // refresh via the pending API. Rendered as a live card at the bottom of the
  // conversation; once the turn completes the persisted block's in-card recap
  // takes over.
  const [livePendingQuestion, setLivePendingQuestion] =
    useState<AssistantUserQuestionBlock | null>(null);
  const attachApprovalListeners = useCallback((stream: EventSource) => {
    stream.addEventListener("approval.requested", (rawEvent) => {
      try {
        const payload = JSON.parse((rawEvent as MessageEvent).data) as AssistantPendingApproval;
        if (payload.approval_id) {
          setPendingApproval(payload);
          setApprovalPrefix(payload.suggested_prefix || "");
        }
      } catch {
        // Ignore malformed live event payloads.
      }
    });
    stream.addEventListener("user_question.asked", (rawEvent) => {
      try {
        const payload = JSON.parse((rawEvent as MessageEvent).data) as Record<string, unknown>;
        const questionId = typeof payload.question_id === "string" ? payload.question_id : "";
        if (!questionId) return;
        setLivePendingQuestion({
          type: "user_question",
          question_id: questionId,
          question: typeof payload.question === "string" ? payload.question : "",
          header: typeof payload.header === "string" ? payload.header : null,
          options: Array.isArray(payload.options)
            ? (payload.options as AssistantUserQuestionBlock["options"])
            : [],
          multi_select: Boolean(payload.multi_select),
        });
      } catch {
        // Ignore malformed live event payloads.
      }
    });
    const clearApproval = () => setPendingApproval(null);
    const clearQuestion = () => setLivePendingQuestion(null);
    stream.addEventListener("approval.resolved", clearApproval);
    stream.addEventListener("approval.timeout", clearApproval);
    stream.addEventListener("user_question.answered", clearQuestion);
    stream.addEventListener("user_question.timeout", clearQuestion);
    stream.addEventListener("attempt.completed", clearApproval);
    stream.addEventListener("attempt.failed", clearApproval);
    stream.addEventListener("attempt.stopped", clearApproval);
    stream.addEventListener("attempt.completed", clearQuestion);
    stream.addEventListener("attempt.failed", clearQuestion);
    stream.addEventListener("attempt.stopped", clearQuestion);
  }, []);
  const handleAnswerUserQuestion = useCallback(
    (questionId: string, answer: { selected: string[]; custom?: string }) => {
      if (!sessionId) return;
      // Optimistically clear the live card; the suspended run resumes on the
      // server and the persisted block will carry the recap after completion.
      setLivePendingQuestion((prev) => (prev?.question_id === questionId ? null : prev));
      void answerAssistantQuestion(sessionId, questionId, answer).catch((error) => {
        message.warning(
          error instanceof Error ? error.message : "该问题已在其它端回答或已超时。",
        );
      });
    },
    [sessionId],
  );
  const [approvalPrefix, setApprovalPrefix] = useState("");
  const handleResolveApproval = useCallback(
    async (
      action: "approve_once" | "approve_always" | "approve_persist" | "reject",
      options?: { reason?: string },
    ) => {
      if (!pendingApproval) return;
      const approvalId = pendingApproval.approval_id;
      const commandPrefix = approvalPrefix.trim();
      setPendingApproval(null);
      setApprovalPrefix("");
      try {
        await resolveAssistantApproval(approvalId, action, {
          reason: options?.reason ?? "",
          command_prefix:
            action === "approve_always" || action === "approve_persist"
              ? commandPrefix
              : "",
        });
      } catch {
        message.warning("该审批已在其它端处理或已超时。");
      }
    },
    [pendingApproval, approvalPrefix],
  );
  const handleRejectApproval = useCallback(() => {
    if (!pendingApproval) return;
    let reason = "";
    Modal.confirm({
      title: "拒绝该操作",
      content: (
        <Input.TextArea
          rows={3}
          placeholder="可选：说明拒绝原因，便于 Agent 调整方案"
          onChange={(event) => {
            reason = event.target.value;
          }}
        />
      ),
      okText: "确认拒绝",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () => handleResolveApproval("reject", { reason: reason.trim() }),
    });
  }, [pendingApproval, handleResolveApproval]);

  // Live-trading order approvals (execution-side QueuedApprovalGate) are a
  // SEPARATE system from the tool-call approval above — they are not bound to
  // any chat session. We surface the GLOBAL pending queue inline in the chat
  // (web analog of the Feishu card push) so an operator can approve/reject a
  // resting order without leaving the conversation. Reuses ApprovalQueueCard,
  // so approve/reject go through the same /approvals endpoints + the card's
  // expiry countdown.
  const [tradeApprovals, setTradeApprovals] = useState<PendingApproval[]>([]);
  const refreshTradeApprovals = useCallback(async () => {
    try {
      const result = await listPendingApprovals();
      setTradeApprovals(Array.isArray(result) ? result : []);
    } catch {
      // A failed poll just leaves the last snapshot; the next tick retries.
    }
  }, []);
  useEffect(() => {
    void refreshTradeApprovals();
    const timer = window.setInterval(() => {
      void refreshTradeApprovals();
    }, 4000);
    return () => window.clearInterval(timer);
  }, [refreshTradeApprovals]);

  const bottomRef = useRef<HTMLDivElement | null>(null);
  const conversationScrollRef = useRef<HTMLDivElement | null>(null);
  const pendingUserMessageRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const inputRef = useRef<TextAreaRef | null>(null);
  const streamRef = useRef<EventSource | null>(null);
  const [isAtConversationBottom, setIsAtConversationBottom] = useState(true);
  // Once we anchor the new user message at the top of the viewport, suppress
  // the aggressive auto-scroll-to-bottom for the rest of this turn — otherwise
  // every streaming delta would yank the viewport down and the user would
  // never get to see their own question while the agent thinks.
  const [pinnedUserMessageId, setPinnedUserMessageId] = useState<string | null>(null);
  // Tracks message_ids we've already scrolled-to-top for, so releasing the pin
  // (e.g. user scrolls to bottom, or clicks 回到底部) cannot re-trigger the
  // top-anchor effect and yank the viewport back up.
  const anchoredUserMessageIdsRef = useRef<Set<string>>(new Set());

  const handleUploadClick = useCallback(() => {
    fileInputRef.current?.click();
  }, []);

  const sessionFilterInitializedRef = useRef(false);
  const requestedSessionId = useMemo(() => {
    const raw = searchParams.get("session_id");
    return typeof raw === "string" && raw.trim() ? raw.trim() : null;
  }, [searchParams]);

  const channelsById = useMemo(
    () => new Map(assistantChannels.map((channel) => [channel.id, channel])),
    [assistantChannels],
  );

  const sessionListParams = useMemo(() => {
    const params: Parameters<typeof listAssistantSessions>[0] = { limit: 50 };
    if (sessionChannelFilter === "web") {
      params.source = "web";
    } else if (sessionChannelFilter !== "all") {
      params.channel_id = sessionChannelFilter;
    }
    return params;
  }, [sessionChannelFilter]);

  const activeSession = useMemo(
    () => sessions.find((session) => session.session_id === sessionId) ?? null,
    [sessionId, sessions],
  );
  // The session's currently pending `ask_user_question` id, if any — read
  // from `config.pending_user_question` (set by the tool, cleared as soon as
  // the user answers). Drives which `user_question` card in the transcript
  // still renders as clickable vs. a read-only "already handled" recap.
  const pendingQuestionId = useMemo(() => {
    const pending = (activeSession?.config as Record<string, unknown> | undefined)?.[
      "pending_user_question"
    ];
    if (pending && typeof pending === "object" && !Array.isArray(pending)) {
      const id = (pending as Record<string, unknown>).question_id;
      return typeof id === "string" && id ? id : null;
    }
    return null;
  }, [activeSession]);
  const activeModelRoute = useMemo(() => {
    const agent =
      agents.find((a) => a.id === selectedAgentId) ??
      agents.find((a) => a.id === activeSession?.agent_id);
    return agent?.model_route_name?.trim() ? agent.model_route_name : null;
  }, [selectedAgentId, agents, activeSession?.agent_id]);

  const mergeSessionRows = useCallback(
    (rows: AssistantSession[], pinned: AssistantSession) => [
      pinned,
      ...rows.filter((row) => row.session_id !== pinned.session_id),
    ],
    [],
  );

  const refreshSessionData = useCallback(async (nextSessionId: string) => {
    const [messageRows, eventRows, sessionRows] = await Promise.all([
      listAssistantMessages(nextSessionId),
      // `tail: true` — we need the session's *current* state (is a run still
      // in flight, and what did it just do), not its earliest history. Without
      // this, a long session silently hands back its oldest events instead of
      // its most recent ones (see EVENTS_TAIL_LIMIT).
      listAssistantEvents(nextSessionId, { tail: true, limit: EVENTS_TAIL_LIMIT }),
      listAssistantSessions(sessionListParams),
    ]);
    setMessages(messageRows);
    // A concurrent refresh (e.g. URL session_id sync re-running the boot
    // effect) must not wipe the optimistic user bubble while submit is in
    // flight — that previously made the first send look like a no-op.
    if (!sendingRef.current) {
      setPendingUserMessage(null);
    }
    setIsAtConversationBottom(true);
    setEvents(eventRows);
    const { bySession, byAttempt } = rebuildToolCallsMaps(eventRows);
    setToolCallsState(bySession);
    setToolCallsByAttempt(byAttempt);

    // If a run is mid-stream (e.g., page refreshed during streaming), replay the
    // unfinished attempt's events so thinking/tool progress is visible right
    // away, then the resume SSE useEffect picks up new deltas from there.
    //
    // Important: trust the session's own status here, NOT just the events list.
    // listAssistantEvents is paginated (default limit=100), so for long runs the
    // `attempt.completed` event can fall outside the page — that would otherwise
    // make findCurrentAttemptId report the run as still in flight and replay a
    // stale streaming snapshot beneath the already-finished message bubble,
    // visually truncating the assistant's reply at the last tool.call in the
    // page (typically the last execute_bash).
    const currentSession =
      sessionRows.items.find((row) => row.session_id === nextSessionId) ?? null;
    const sessionIsRunning = currentSession?.status === "running";
    const currentAttemptId = sessionIsRunning ? findCurrentAttemptId(eventRows) : null;
    if (currentAttemptId) {
      currentAttemptIdRef.current = currentAttemptId;
      const built = buildStreamingFromEvents(eventRows, currentAttemptId);
      setStreamingThinking(built.streamingThinking);
      setStreamingThinkingBlocks(built.thinkingBlocks);
      setStreamingContentBlocks(built.contentBlocks);
      if (built.traceId) setPendingTraceId(built.traceId);
    } else {
      setStreamingThinking("");
      setStreamingThinkingBlocks([]);
      setStreamingContentBlocks([]);
      currentAttemptIdRef.current = "";
    }
  }, [sessionListParams]);

  const refreshSessions = useCallback(async (pinnedSessionId?: string | null) => {
    const rows = await listAssistantSessions(sessionListParams);
    let items = rows.items;
    const pinnedId = typeof pinnedSessionId === "string" && pinnedSessionId.trim()
      ? pinnedSessionId.trim()
      : null;
    if (pinnedId && !items.some((row) => row.session_id === pinnedId)) {
      try {
        const pinned = await getAssistantSession(pinnedId);
        items = mergeSessionRows(items, pinned);
      } catch {
        // Fall back to the current page of sessions if the pinned session
        // no longer exists or cannot be fetched.
      }
    }
    setSessions(items);
    return items;
  }, [mergeSessionRows, sessionListParams]);

  const loadAgents = useCallback(async () => {
    try {
      const result = await listAssistantAgents({ include_inactive: true });
      setAgents(result.items);
    } catch (err) {
      console.error("Failed to load agents:", err);
    }
  }, []);

  const loadChannels = useCallback(async () => {
    try {
      const result = await listAssistantChannels();
      setAssistantChannels(result.items);
    } catch (err) {
      console.error("Failed to load assistant channels:", err);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    void (async () => {
      setLoading(true);
      try {
        const [routeRows, agentRows, channelRows, sessionRows] = await Promise.all([
          listModelRoutes(),
          listAssistantAgents({ include_inactive: true }),
          listAssistantChannels().catch(() => ({ items: [] as AssistantChannel[] })),
          refreshSessions(requestedSessionId),
        ]);
        if (!alive) return;
        setModelRoutes(routeRows.items);
        setAgents(agentRows.items);
        setAssistantChannels(channelRows.items);
        if (!selectedAgentId && agentRows.items.length > 0) {
          const firstActive = agentRows.items.find((a: Agent) => a.status === "active");
          if (firstActive) setSelectedAgentId(firstActive.id);
        }
        const defaultRoute = routeRows.items[0]?.route_name ?? null;
        setSelectedModelRoute(defaultRoute);
        const rows = sessionRows;
        const next = requestedSessionId && rows.some((session) => session.session_id === requestedSessionId)
          ? requestedSessionId
          : (rows[0]?.session_id ?? null);
        if (!alive || !next) return;
        setSessionId(next);
        const existing = rows.find((session) => session.session_id === next);
        if (existing?.agent_id) {
          setSelectedAgentId(existing.agent_id);
        }
        await refreshSessionData(next);
      } catch (error) {
        if (alive) message.error(error instanceof Error ? error.message : String(error));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [refreshSessionData, refreshSessions, requestedSessionId]);

  useEffect(() => {
    if (!sessionFilterInitializedRef.current) {
      sessionFilterInitializedRef.current = true;
      return;
    }
    let alive = true;
    void (async () => {
      try {
        const rows = await refreshSessions();
        if (!alive) return;
        setSessionId((currentSessionId) => {
          if (currentSessionId && rows.some((row) => row.session_id === currentSessionId)) {
            return currentSessionId;
          }
          const nextSessionId = rows[0]?.session_id ?? null;
          if (nextSessionId) {
            void refreshSessionData(nextSessionId);
          } else {
            setMessages([]);
            setPendingUserMessage(null);
            setEvents([]);
            setToolCallsState({});
            setToolCallsByAttempt({});
          }
          return nextSessionId;
        });
      } catch (error) {
        if (alive) message.error(error instanceof Error ? error.message : String(error));
      }
    })();
    return () => {
      alive = false;
    };
  }, [refreshSessionData, refreshSessions, sessionChannelFilter]);

  useEffect(() => {
    if (pageRefreshToken === 0) return;
    void (async () => {
      try {
        await Promise.all([loadAgents(), loadChannels()]);
        const rows = await refreshSessions(sessionId ?? requestedSessionId);
        if (sessionId) {
          await refreshSessionData(sessionId);
          return;
        }
        const nextSessionId =
          requestedSessionId && rows.some((row) => row.session_id === requestedSessionId)
            ? requestedSessionId
            : (rows[0]?.session_id ?? null);
        if (!nextSessionId) return;
        setSessionId(nextSessionId);
        await refreshSessionData(nextSessionId);
      } catch (error) {
        message.error(error instanceof Error ? error.message : String(error));
      }
    })();
  }, [loadAgents, loadChannels, pageRefreshToken, refreshSessionData, refreshSessions, requestedSessionId, sessionId]);

  useEffect(() => {
    if (!sessionId && requestedSessionId) {
      return;
    }
    if (sessionId === requestedSessionId) {
      return;
    }
    const next = new URLSearchParams(searchParams);
    if (sessionId) {
      next.set("session_id", sessionId);
    } else {
      next.delete("session_id");
    }
    setSearchParams(next, { replace: true });
  }, [requestedSessionId, searchParams, sessionId, setSearchParams]);

  // 浏览器刷新或切到一个仍在 streaming 的 session 时，自动订阅 SSE，
  // 实时显示思考/工具调用进度，并在终止事件后刷新最终消息。
  // 等 events 加载完成后再开 SSE，这样 last_event_id 能正确跳过已重放的事件。
  const eventsLen = events.length;
  const lastEventId = events.at(-1)?.event_id ?? null;
  useEffect(() => {
    if (!sessionId) return;
    if (sending) return;
    if (!activeSession || activeSession.status !== "running") return;
    if (eventsLen === 0) return;

    const stream = new EventSource(assistantEventStreamUrl(sessionId, lastEventId));
    streamRef.current = stream;
    attachApprovalListeners(stream);
    let closed = false;
    const close = () => {
      if (closed) return;
      closed = true;
      stream.close();
      if (streamRef.current === stream) streamRef.current = null;
    };
    const closeAndRefresh = () => {
      close();
      setIsStopping(false);
      void Promise.all([refreshSessions(sessionId), refreshSessionData(sessionId)]).catch(() => {});
    };

    stream.addEventListener("attempt.started", (rawEvent) => {
      try {
        const payload = JSON.parse((rawEvent as MessageEvent).data) as Record<string, unknown>;
        const attempt_id = typeof payload.attempt_id === "string" ? payload.attempt_id : "";
        const trace_id = typeof payload.trace_id === "string" ? payload.trace_id : null;
        if (attempt_id) currentAttemptIdRef.current = attempt_id;
        if (trace_id) setPendingTraceId(trace_id);
      } catch {
        // Ignore malformed live event payloads.
      }
    });
    stream.addEventListener("tool.call", (rawEvent) => {
      try {
        const payload = JSON.parse((rawEvent as MessageEvent).data) as Record<string, unknown>;
        if (!isEventForCurrentAttempt(payload, currentAttemptIdRef.current)) {
          console.debug(
            "[assistant] ignoring tool.call for a stale/foreign attempt",
            payload.attempt_id,
            "expected",
            currentAttemptIdRef.current,
          );
          return;
        }
        const tool_call_id = typeof payload.tool_call_id === "string" ? payload.tool_call_id : String(Date.now());
        const attempt_id = typeof payload.attempt_id === "string" ? payload.attempt_id : currentAttemptIdRef.current;
        const tool_name = typeof payload.tool === "string" ? payload.tool : "";
        const toolInput = (payload.arguments ?? {}) as Record<string, unknown>;
        const entry: ToolCallEntry = {
          tool: { type: "tool_use", id: tool_call_id, name: tool_name, input: toolInput, status: "running" },
          attempt_id,
        };
        setToolCallsState((prev) => ({
          ...prev,
          [sessionId]: { ...(prev[sessionId] ?? {}), [tool_call_id]: entry },
        }));
        setToolCallsByAttempt((prev) => ({
          ...prev,
          [attempt_id]: { ...(prev[attempt_id] ?? {}), [tool_call_id]: entry },
        }));
        setStreamingContentBlocks((prev) => [...prev, { type: "tool_call", tool_call_id }]);
      } catch {
        // Ignore malformed live event payloads.
      }
    });
    stream.addEventListener("tool.result", (rawEvent) => {
      try {
        const payload = JSON.parse((rawEvent as MessageEvent).data) as Record<string, unknown>;
        if (!isEventForCurrentAttempt(payload, currentAttemptIdRef.current)) {
          console.debug(
            "[assistant] ignoring tool.result for a stale/foreign attempt",
            payload.attempt_id,
            "expected",
            currentAttemptIdRef.current,
          );
          return;
        }
        const tool_call_id = typeof payload.tool_call_id === "string" ? payload.tool_call_id : "";
        const attempt_id = typeof payload.attempt_id === "string" ? payload.attempt_id : currentAttemptIdRef.current;
        const { output, is_error } = parsePreview(payload.preview);
        if (tool_call_id) {
          setToolCallsState((prev) => {
            const existing = prev[sessionId]?.[tool_call_id];
            if (!existing) return prev;
            return {
              ...prev,
              [sessionId]: {
                ...prev[sessionId],
                [tool_call_id]: {
                  tool: { ...existing.tool, status: is_error ? "error" : "completed" },
                  result: { type: "tool_result", tool_use_id: tool_call_id, output, is_error },
                  attempt_id: existing.attempt_id,
                },
              },
            };
          });
          setToolCallsByAttempt((prev) => {
            const existing = prev[attempt_id]?.[tool_call_id];
            if (!existing) return prev;
            return {
              ...prev,
              [attempt_id]: {
                ...prev[attempt_id],
                [tool_call_id]: {
                  tool: { ...existing.tool, status: is_error ? "error" : "completed" },
                  result: { type: "tool_result", tool_use_id: tool_call_id, output, is_error },
                  attempt_id: existing.attempt_id,
                },
              },
            };
          });
        }
      } catch {
        // Ignore malformed live event payloads.
      }
    });
    stream.addEventListener("thinking.delta", (rawEvent) => {
      try {
        const payload = JSON.parse((rawEvent as MessageEvent).data) as Record<string, unknown>;
        if (!isEventForCurrentAttempt(payload, currentAttemptIdRef.current)) return;
        const delta = typeof payload.delta === "string" ? payload.delta : "";
        if (delta) setStreamingThinking((prev) => prev + delta);
      } catch {
        // Ignore malformed live event payloads.
      }
    });
    stream.addEventListener("thinking.done", (rawEvent) => {
      try {
        const payload = JSON.parse((rawEvent as MessageEvent).data) as Record<string, unknown>;
        if (!isEventForCurrentAttempt(payload, currentAttemptIdRef.current)) return;
        const thinking = typeof payload.thinking === "string" ? payload.thinking : "";
        const turn = typeof payload.turn === "number" ? payload.turn : undefined;
        if (thinking) {
          setStreamingThinkingBlocks((prev) => [...prev, { turn, content: thinking }]);
          setStreamingContentBlocks((prev) => [...prev, { type: "thinking", turn, content: thinking }]);
          setStreamingThinking("");
        }
      } catch {
        // Ignore malformed live event payloads.
      }
    });
    stream.addEventListener("attempt.failed", () => {
      setToolCallsState((prev) => {
        const sessionTools = prev[sessionId] ?? {};
        const updated: Record<string, ToolCallEntry> = {};
        for (const [id, entry] of Object.entries(sessionTools)) {
          if (entry.tool.status === "pending" || entry.tool.status === "running") {
            updated[id] = { ...entry, tool: { ...entry.tool, status: "error" } };
          } else {
            updated[id] = entry;
          }
        }
        return { ...prev, [sessionId]: updated };
      });
      closeAndRefresh();
    });
    stream.addEventListener("attempt.completed", closeAndRefresh);
    stream.addEventListener("attempt.stopped", closeAndRefresh);

    return close;
  }, [sessionId, sending, activeSession?.status, eventsLen, lastEventId, refreshSessions, refreshSessionData, attachApprovalListeners]);

  // Refresh-recovery: a turn suspended on an approval survives a page
  // reload (the SSE event is gone), so re-fetch pending approvals whenever
  // the active session is running.
  useEffect(() => {
    if (!sessionId || activeSession?.status !== "running") {
      setPendingApproval(null);
      setLivePendingQuestion(null);
      return;
    }
    void listPendingAssistantApprovals(sessionId)
      .then((result) => {
        const item = result.items[0] ?? null;
        setPendingApproval(item);
        setApprovalPrefix(item?.suggested_prefix || "");
      })
      .catch(() => {});
    // Same refresh-recovery for a turn suspended on an ask_user_question.
    void listPendingAssistantQuestions(sessionId)
      .then((result) => {
        const item = result.items[0];
        setLivePendingQuestion(
          item
            ? {
                type: "user_question",
                question_id: item.question_id,
                question: item.question ?? "",
                header: item.header ?? null,
                options: Array.isArray(item.options) ? item.options : [],
                multi_select: Boolean(item.multi_select),
              }
            : null,
        );
      })
      .catch(() => {});
  }, [sessionId, activeSession?.status]);

  // When a new user message is submitted, scroll it to the TOP of the
  // conversation viewport so the user can read what they just asked while the
  // agent streams its (often very long) response below. This mirrors how
  // Claude.ai / ChatGPT anchor the question after send.
  useEffect(() => {
    if (!pendingUserMessage) return;
    const id = pendingUserMessage.message_id;
    if (anchoredUserMessageIdsRef.current.has(id)) return;
    const node = pendingUserMessageRef.current;
    if (!node) return;
    anchoredUserMessageIdsRef.current.add(id);
    requestAnimationFrame(() => {
      node.scrollIntoView({ behavior: "smooth", block: "start" });
      setPinnedUserMessageId(id);
      // We're no longer "at the bottom"; the auto-scroll-to-bottom effect
      // below will short-circuit so the anchored question stays visible.
      setIsAtConversationBottom(false);
    });
  }, [pendingUserMessage]);

  useEffect(() => {
    if (!isAtConversationBottom) return;
    // While we're pinning a new user message at the top, do NOT yank the
    // viewport to the bottom on every streaming delta.
    if (pinnedUserMessageId) return;
    // pendingUserMessage is set before the pin effect runs (rAF). Skip the
    // bottom-follow for that frame so we don't race the top-anchor scroll.
    if (pendingUserMessage) return;
    // The empty-state welcome screen is taller than the viewport; scrolling it
    // to the bottom would hide the hero. Keep it pinned to the top until a real
    // message exists.
    if (messages.length === 0 && !sending && !streamingContent) return;
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [
    isAtConversationBottom,
    messages.length,
    pendingUserMessage,
    pinnedUserMessageId,
    sending,
    streamingContent,
    streamingThinking,
    streamingThinkingBlocks.length,
    streamingContentBlocks.length,
  ]);

  // The empty-state welcome screen can be taller than the conversation viewport.
  // Keep it pinned to the top so the hero stays visible and the user scrolls
  // *down* through the examples, instead of inheriting a bottom-anchored
  // scrollTop left over from a previous conversation.
  useEffect(() => {
    if (messages.length > 0 || pendingUserMessage || sending || streamingContent) return;
    const element = conversationScrollRef.current;
    if (element) element.scrollTop = 0;
  }, [messages.length, pendingUserMessage, sending, streamingContent, sessionId]);

  const handleConversationScroll = useCallback(() => {
    const element = conversationScrollRef.current;
    if (!element) return;
    const atBottom = isScrolledNearBottom(element);
    setIsAtConversationBottom(atBottom);
    // If the user manually scrolls to the bottom, they're opting into the
    // follow-the-stream behavior, so release the top-anchor pin.
    if (atBottom) setPinnedUserMessageId(null);
  }, []);

  const scrollConversationToBottom = useCallback(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    setIsAtConversationBottom(true);
    // User explicitly asked to follow the latest output — release the pin
    // so subsequent streaming deltas can auto-scroll again.
    setPinnedUserMessageId(null);
  }, []);

  const createNewSession = useCallback(async () => {
    const agentId = selectedAgentId?.trim();
    if (!agentId) {
      message.warning("请先选择一个 Agent");
      return;
    }
    setLoading(true);
    try {
      const created = await createAssistantSession({
        title: DEFAULT_TITLE,
        agent_id: agentId,
      });
      await refreshSessions(created.session_id);
      setSessionId(created.session_id);
      setMessages([]);
      setPendingUserMessage(null);
      setIsAtConversationBottom(true);
      setPinnedUserMessageId(null);
      setEvents([]);
      setStreamingContent("");
      setStreamingThinking("");
      setStreamingThinkingBlocks([]);
      setStreamingContentBlocks([]);
      setPendingTraceId(null);
      currentAttemptIdRef.current = "";
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(false);
    }
  }, [refreshSessions, selectedAgentId]);

  const [isCopying, setIsCopying] = useState(false);

  const handleCopyConversation = useCallback(async () => {
    if (!sessionId) {
      message.warning("没有可复制的会话");
      return;
    }
    setIsCopying(true);
    try {
      // Pull tool descriptions on demand so the AI doing the analysis can see
      // what the agent had access to, not just the names. Failures here are
      // non-fatal — we still copy without descriptions.
      let toolCatalog;
      try {
        toolCatalog = await listAssistantTools();
      } catch {
        toolCatalog = undefined;
      }
      const agent = agents.find((a) => a.id === activeSession?.agent_id) ?? null;
      const text = serializeSession({
        session: activeSession,
        agent,
        messages,
        events,
        toolCallsByAttempt,
        toolCatalog,
      });
      await navigator.clipboard.writeText(text);
      message.success(`已复制会话内容（${text.length} 字符）`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
    } finally {
      setIsCopying(false);
    }
  }, [activeSession, agents, events, messages, sessionId, toolCallsByAttempt]);

  const handleStop = useCallback(async () => {
    if (!sessionId || isStopping) return;
    setIsStopping(true);
    try {
      const result = await stopAssistantSession(sessionId);
      // Optimistic local status so the send/stop toggle does not flicker back to
      // "发送" while POST /messages is still unwinding (OpenClaw keeps canAbort
      // true until the gateway broadcasts aborted state).
      setSessions((prev) =>
        prev.map((row) =>
          row.session_id === sessionId ? { ...row, status: "idle" } : row,
        ),
      );
      if (result.active === false) {
        message.info("当前没有进行中的 Agent 运行");
        setIsStopping(false);
        setSending(false);
        return;
      }
      if (!sending) {
        void Promise.all([refreshSessions(sessionId), refreshSessionData(sessionId)])
          .catch(() => {})
          .finally(() => setIsStopping(false));
      }
    } catch (error) {
      message.warning(error instanceof Error ? error.message : "停止请求失败，请重试");
      setIsStopping(false);
    }
  }, [isStopping, sessionId]);

  const switchSession = useCallback(
    async (nextSessionId: string) => {
      setSessionId(nextSessionId);
      setLoading(true);
      setPinnedUserMessageId(null);
      try {
        const next = sessions.find((session) => session.session_id === nextSessionId);
        if (next?.agent_id) {
          setSelectedAgentId(next.agent_id);
        }
        await refreshSessionData(nextSessionId);
      } catch (error) {
        message.error(error instanceof Error ? error.message : String(error));
      } finally {
        setLoading(false);
      }
    },
    [refreshSessionData, sessions, agents],
  );

  const submit = useCallback(async (overrideText?: unknown) => {
    // ``overrideText`` is set by programmatic sends (e.g. ask_user_question
    // option clicks). Guard against event objects when ``submit`` is used
    // directly as a DOM handler.
    const override = typeof overrideText === "string" ? overrideText.trim() : "";
    const rawText = override || input.trim();
    const hasText = rawText.length > 0;
    // Programmatic sends (option clicks) never carry attachments.
    const outgoingAttachments = override ? [] : attachments;
    const hasAttachment = outgoingAttachments.length > 0;

    if (!hasText && !hasAttachment) {
      message.warning("请输入消息或上传附件后再发送");
      return;
    }

    // Fresh install / empty session list leaves ``sessionId`` null while the
    // send button can still be enabled (it only gates on model route). Auto-
    // create a session so the first send is not a silent no-op.
    let targetSessionId = sessionId;
    if (!targetSessionId) {
      const agentId =
        selectedAgentId?.trim() ||
        agents.find((a) => a.status === "active")?.id?.trim() ||
        "";
      if (!agentId) {
        message.warning("请先选择一个 Agent，或点击「新会话」");
        return;
      }
      try {
        const created = await createAssistantSession({
          title: DEFAULT_TITLE,
          agent_id: agentId,
        });
        const rows = await refreshSessions(created.session_id);
        if (!rows.some((row) => row.session_id === created.session_id)) {
          setSessions([created, ...rows]);
        }
        setSessionId(created.session_id);
        if (!selectedAgentId) {
          setSelectedAgentId(agentId);
        }
        targetSessionId = created.session_id;
      } catch (error) {
        message.error(error instanceof Error ? error.message : String(error));
        return;
      }
    }

    const isLifecycleCommand = /^\/new\s*$/i.test(rawText);
    if (!activeModelRoute && !isLifecycleCommand) {
      message.warning("当前智能体会话未关联模型，请新建会话并选择要使用的模型。");
      return;
    }

    // Attachments travel as structured data (not a text prefix): the message
    // text is the user's own words only, and the file rides in metadata so the
    // bubble renders a filename chip while the server injects the path for the
    // model. Absolute paths never touch the client here.
    const text = hasText ? rawText : "";

    const optimisticUserMessage: AssistantMessage = {
      message_id: `optimistic-${Date.now()}`,
      session_id: targetSessionId,
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
      linked_attempt_id: null,
      metadata: hasAttachment ? { attachments: outgoingAttachments } : {},
    };

    setSending(true);
    setStreamingContent("");
    setStreamingThinking("");
    setStreamingThinkingBlocks([]);
    setStreamingContentBlocks([]);
    // Explicitly forget the previous attempt rather than relying on
    // refreshSessionData having already cleared it after the last turn — a
    // stray tool.call/thinking event arriving before this turn's own
    // `attempt.started` should never be attributed to a leftover attempt id.
    currentAttemptIdRef.current = "";
    if (!override) setInput("");
    setPendingUserMessage(optimisticUserMessage);
    const lastEventId = events.at(-1)?.event_id ?? null;
    const stream = new EventSource(assistantEventStreamUrl(targetSessionId, lastEventId));
    streamRef.current = stream;
    attachApprovalListeners(stream);
    stream.addEventListener("attempt.started", (rawEvent) => {
      try {
        const payload = JSON.parse((rawEvent as MessageEvent).data) as Record<string, unknown>;
        const attempt_id = typeof payload.attempt_id === "string" ? payload.attempt_id : "";
        const trace_id = typeof payload.trace_id === "string" ? payload.trace_id : null;
        currentAttemptIdRef.current = attempt_id;
        if (trace_id) {
          setPendingTraceId(trace_id);
        }
      } catch {
        // Ignore
      }
    });
    stream.addEventListener("tool.call", (rawEvent) => {
      try {
        const payload = JSON.parse((rawEvent as MessageEvent).data) as Record<string, unknown>;
        setEvents((prev) => [
          ...prev,
          {
            event_id: (rawEvent as MessageEvent).lastEventId || `live-${Date.now()}`,
            session_id: targetSessionId,
            event_type: "tool.call",
            payload,
            created_at: new Date().toISOString(),
          },
        ]);
        if (!isEventForCurrentAttempt(payload, currentAttemptIdRef.current)) {
          console.debug(
            "[assistant] ignoring tool.call for a stale/foreign attempt",
            payload.attempt_id,
            "expected",
            currentAttemptIdRef.current,
          );
          return;
        }
        const tool_call_id = typeof payload.tool_call_id === "string" ? payload.tool_call_id : String(Date.now());
        const attempt_id = typeof payload.attempt_id === "string" ? payload.attempt_id : currentAttemptIdRef.current;
        const tool_name = typeof payload.tool === "string" ? payload.tool : "";
        const input = (payload.arguments ?? {}) as Record<string, unknown>;
        const entry: ToolCallEntry = {
          tool: { type: "tool_use", id: tool_call_id, name: tool_name, input, status: "running" },
          attempt_id,
        };
        setToolCallsState((prev) => ({
          ...prev,
          [targetSessionId]: { ...(prev[targetSessionId] ?? {}), [tool_call_id]: entry },
        }));
        setToolCallsByAttempt((prev) => ({
          ...prev,
          [attempt_id]: { ...(prev[attempt_id] ?? {}), [tool_call_id]: entry },
        }));
        setStreamingContentBlocks((prev) => [...prev, { type: "tool_call", tool_call_id }]);
      } catch {
        // Ignore malformed live event payloads.
      }
    });
    stream.addEventListener("tool.result", (rawEvent) => {
      try {
        const payload = JSON.parse((rawEvent as MessageEvent).data) as Record<string, unknown>;
        setEvents((prev) => [
          ...prev,
          {
            event_id: (rawEvent as MessageEvent).lastEventId || `live-${Date.now()}`,
            session_id: targetSessionId,
            event_type: "tool.result",
            payload,
            created_at: new Date().toISOString(),
          },
        ]);
        if (!isEventForCurrentAttempt(payload, currentAttemptIdRef.current)) {
          console.debug(
            "[assistant] ignoring tool.result for a stale/foreign attempt",
            payload.attempt_id,
            "expected",
            currentAttemptIdRef.current,
          );
          return;
        }
        const tool_call_id = typeof payload.tool_call_id === "string" ? payload.tool_call_id : "";
        const attempt_id = typeof payload.attempt_id === "string" ? payload.attempt_id : currentAttemptIdRef.current;
        const { output, is_error } = parsePreview(payload.preview);
        if (tool_call_id) {
          setToolCallsState((prev) => {
            const existing = prev[targetSessionId]?.[tool_call_id];
            if (!existing) return prev;
            return {
              ...prev,
              [targetSessionId]: {
                ...prev[targetSessionId],
                [tool_call_id]: {
                  tool: { ...existing.tool, status: is_error ? "error" : "completed" },
                  result: { type: "tool_result", tool_use_id: tool_call_id, output, is_error },
                  attempt_id: existing.attempt_id,
                },
              },
            };
          });
          setToolCallsByAttempt((prev) => {
            const existing = prev[attempt_id]?.[tool_call_id];
            if (!existing) return prev;
            return {
              ...prev,
              [attempt_id]: {
                ...prev[attempt_id],
                [tool_call_id]: {
                  tool: { ...existing.tool, status: is_error ? "error" : "completed" },
                  result: { type: "tool_result", tool_use_id: tool_call_id, output, is_error },
                  attempt_id: existing.attempt_id,
                },
              },
            };
          });
        }
      } catch {
        // Ignore malformed live event payloads.
      }
    });
    stream.addEventListener("thinking.delta", (rawEvent) => {
      try {
        const payload = JSON.parse((rawEvent as MessageEvent).data) as Record<string, unknown>;
        if (!isEventForCurrentAttempt(payload, currentAttemptIdRef.current)) return;
        const delta = typeof payload.delta === "string" ? payload.delta : "";
        if (delta) {
          setStreamingThinking((prev) => prev + delta);
        }
      } catch {
        // Ignore malformed event payloads.
      }
    });
    stream.addEventListener("thinking.done", (rawEvent) => {
      try {
        const payload = JSON.parse((rawEvent as MessageEvent).data) as Record<string, unknown>;
        if (!isEventForCurrentAttempt(payload, currentAttemptIdRef.current)) return;
        const thinking = typeof payload.thinking === "string" ? payload.thinking : "";
        const turn = typeof payload.turn === "number" ? payload.turn : undefined;
        setStreamingThinking(thinking);
        if (thinking) {
          setStreamingThinkingBlocks((prev) => [...prev, { turn, content: thinking }]);
          setStreamingContentBlocks((prev) => [...prev, { type: "thinking", turn, content: thinking }]);
          setStreamingThinking("");
        }
      } catch {
        // Ignore malformed event payloads.
      }
    });
    stream.addEventListener("attempt.failed", (rawEvent) => {
      try {
        const payload = JSON.parse((rawEvent as MessageEvent).data) as Record<string, unknown>;
        // Mark all pending/running tools as error for this session
        setToolCallsState((prev) => {
          const sessionTools = prev[targetSessionId] ?? {};
          const updated: Record<string, ToolCallEntry> = {};
          for (const [id, entry] of Object.entries(sessionTools)) {
            if (entry.tool.status === "pending" || entry.tool.status === "running") {
              updated[id] = { ...entry, tool: { ...entry.tool, status: "error" } };
            } else {
              updated[id] = entry;
            }
          }
          return { ...prev, [targetSessionId]: updated };
        });
      } catch {
        // Ignore malformed live event payloads.
      }
    });
    const closeAndRefreshAfterStop = () => {
      stream.close();
      if (streamRef.current === stream) streamRef.current = null;
      setIsStopping(false);
      void Promise.all([refreshSessions(targetSessionId), refreshSessionData(targetSessionId)]).catch(() => {});
    };
    stream.addEventListener("attempt.stopped", closeAndRefreshAfterStop);
    stream.addEventListener("attempt.completed", closeAndRefreshAfterStop);
    try {
      const result = hasAttachment
        ? await sendAssistantMessage(targetSessionId, text, outgoingAttachments)
        : await sendAssistantMessage(targetSessionId, text);
      if (result.lifecycle_command?.command === "new") {
        const nextSessionId = result.session.session_id;
        const rows = await refreshSessions(nextSessionId);
        if (!rows.some((session) => session.session_id === nextSessionId)) {
          setSessions([result.session, ...rows]);
        }
        setSessionId(nextSessionId);
        if (result.session.agent_id) {
          setSelectedAgentId(result.session.agent_id);
        }
        await refreshSessionData(nextSessionId);
        return;
      }
      setMessages((prev) => [...prev, ...result.messages]);
      if (result.trace_id) {
        setPendingTraceId(result.trace_id);
      }
      await Promise.all([refreshSessions(targetSessionId), refreshSessionData(targetSessionId)]);
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      if (msg.includes("stopped by user")) {
        setPendingUserMessage(null);
        setInput(text);
        await Promise.all([refreshSessions(targetSessionId), refreshSessionData(targetSessionId)]);
      } else {
        message.error(msg);
        // A failed run persists an assistant error/partial message before returning
        // 500. Re-fetch so that message appears instead of the chat showing only the
        // user's query (the submit path otherwise never refreshes on failure).
        await Promise.all([refreshSessions(targetSessionId), refreshSessionData(targetSessionId)]).catch(
          () => {},
        );
      }
    } finally {
      stream.close();
      if (streamRef.current === stream) streamRef.current = null;
      setStreamingContent("");
      setStreamingThinking("");
      setStreamingThinkingBlocks([]);
      setStreamingContentBlocks([]);
      setPendingUserMessage(null);
      setSending(false);
      setIsStopping(false);
      setAttachments([]);
    }
  }, [
    activeModelRoute,
    agents,
    attachments,
    events,
    input,
    refreshSessionData,
    refreshSessions,
    selectedAgentId,
    sessionId,
  ]);

  const handlePickExample = useCallback((prompt: string) => {
    setInput(prompt);
    // Defer focus until the textarea has re-rendered with the new value so the
    // caret lands at the end and the field is ready for the user to send.
    requestAnimationFrame(() => {
      const textarea = inputRef.current?.resizableTextArea?.textArea;
      if (textarea) {
        textarea.focus();
        textarea.setSelectionRange(prompt.length, prompt.length);
      }
    });
  }, []);

  const rightRail = (
    <>
        <Card title="会话" className="rounded-3xl border-shell-line bg-card-bg/95">
          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <Typography.Text type="secondary" className="text-xs">Channel 筛选</Typography.Text>
              <Select
                className="w-full"
                value={sessionChannelFilter}
                options={[
                  { value: "all", label: "全部会话" },
                  { value: "web", label: "Web 会话" },
                  ...assistantChannels.map((channel) => ({
                    value: channel.id,
                    label: channel.name?.trim() ? `${channel.name} (${channel.type})` : `${channel.id} (${channel.type})`,
                  })),
                ]}
                onChange={(value) => setSessionChannelFilter(value)}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Typography.Text type="secondary" className="text-xs">当前会话</Typography.Text>
              <Select
                className="w-full [&_.ant-select-selection-item]:truncate"
                value={sessionId ?? undefined}
                optionLabelProp="label"
                options={sessions.map((session) => ({
                  value: session.session_id,
                  label: session.title || "新会话",
                }))}
                optionRender={(option) => {
                  const session = sessions.find((row) => row.session_id === option.value);
                  if (!session) {
                    return <span>{option.label}</span>;
                  }
                  const agent = session.agent_id ? agents.find((a) => a.id === session.agent_id) : null;
                  const sourceChannelLabel = formatSessionSourceChannelLabel(session, channelsById);
                  return (
                    <div className="flex min-w-0 flex-col gap-1 py-0.5">
                      <span className="truncate">{formatSessionOptionTitle(session, agent?.name)}</span>
                      <Typography.Text type="secondary" className="text-xs">
                        创建于 {formatMessageTime(session.created_at)}
                      </Typography.Text>
                      {sourceChannelLabel ? (
                        <Tag color="geekblue" className="mr-0 w-fit max-w-full truncate">
                          来自 channel: {sourceChannelLabel}
                        </Tag>
                      ) : null}
                    </div>
                  );
                }}
                onChange={(value) => void switchSession(value)}
              />
            </div>
            {/* 当前会话元信息：紧凑信息块 */}
            {sessionId ? (
              <div className="flex flex-col gap-2 rounded-2xl border border-shell-line bg-white/60 px-3 py-2.5">
                <div className="flex flex-col gap-0.5">
                  <Typography.Text type="secondary" className="text-xs">
                    当前会话 ID
                  </Typography.Text>
                  <Typography.Text
                    className="font-mono text-xs"
                    copyable={{ text: sessionId }}
                    ellipsis={{ tooltip: sessionId }}
                  >
                    {sessionId}
                  </Typography.Text>
                </div>
                {activeSession ? (
                  <Typography.Text type="secondary" className="text-xs">
                    创建于 {formatMessageTime(activeSession.created_at)}
                  </Typography.Text>
                ) : null}
                <div className="flex flex-wrap items-center gap-1.5">
                  {(() => {
                    const agent = agents.find((a) => a.id === activeSession?.agent_id);
                    return agent ? (
                      <Tag icon={<RobotOutlined />} color="blue" className="mr-0">
                        Agent: {agent.name}
                      </Tag>
                    ) : (
                      <Tag icon={<RobotOutlined />} className="mr-0">
                        —
                      </Tag>
                    );
                  })()}
                  {activeSession && formatSessionSourceChannelLabel(activeSession, channelsById) ? (
                    <Tag color="geekblue" className="mr-0 max-w-full truncate">
                      来自 channel: {formatSessionSourceChannelLabel(activeSession, channelsById)}
                    </Tag>
                  ) : null}
                </div>
              </div>
            ) : null}
            <div className="flex flex-col gap-1.5">
              <Typography.Text type="secondary" className="text-xs">选择 Agent</Typography.Text>
              <Select
                className="w-full"
                placeholder="选择一个 Agent"
                value={selectedAgentId ?? undefined}
                options={agents.map((agent) => ({
                  value: agent.id,
                  label: (
                    <Space>
                      <span>{agent.name}</span>
                      {agent.status === "inactive" && <Tag color="default">inactive</Tag>}
                    </Space>
                  ),
                }))}
                onChange={(value) => setSelectedAgentId(value)}
                notFoundContent="暂无 Agent，请先在「Agent 管理」中创建"
              />
            </div>
          </div>
        </Card>
        <Card
          className="flex min-h-0 flex-1 flex-col rounded-3xl border-shell-line bg-card-bg/95 [&>.ant-card-body]:flex [&>.ant-card-body]:min-h-0 [&>.ant-card-body]:flex-1 [&>.ant-card-body]:flex-col [&_.ant-tabs-nav]:!mb-0 [&_.ant-tabs-nav]:!px-4"
          bodyStyle={{ padding: 0 }}
        >
          <Tabs
            activeKey={activeRightTab}
            onChange={(key) => setActiveRightTab(key as "traces" | "skills-tools")}
            items={[
              { key: "traces", label: "Traces" },
              { key: "skills-tools", label: "Skills & Tools" },
            ]}
            size="small"
          />
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            {activeRightTab === "traces" ? (
              sessionId ? (
                <TracesPanel
                  sessionId={sessionId}
                  newTraceId={pendingTraceId}
                  onNewTraceIdConsumed={() => setPendingTraceId(null)}
                />
              ) : (
                <Empty description="选择一个会话" />
              )
            ) : (
              <SkillsToolsTab />
            )}
          </div>
        </Card>
    </>
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <input
        ref={fileInputRef}
        type="file"
        accept=".txt,.csv,.json,.yaml,.yml,.py,.md,.docx,.xlsx,.xls,.pptx,.pdf,.png,.jpg,.jpeg,.webp"
        style={{ display: "none" }}
        onChange={async (event) => {
          const file = event.target.files?.[0];
          if (!file) return;
          const MAX_SIZE = 50 * 1024 * 1024;
          if (file.size > MAX_SIZE) {
            message.error("文件大小不能超过 50 MB");
            return;
          }
          try {
            const result = await uploadFile(file);
            if (result.status === "ok") {
              setAttachments((prev) => [
                ...prev,
                {
                  file_id: result.file_id,
                  filename: result.filename,
                  mime_type: result.mime_type,
                  size_bytes: result.size_bytes,
                },
              ]);
            }
          } catch (err) {
            message.error(err instanceof Error ? err.message : String(err));
          } finally {
            event.target.value = "";
          }
        }}
      />
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
        <Card
          className="flex min-h-0 flex-col rounded-3xl border-shell-line bg-card-bg/95 [&>.ant-card-body]:flex [&>.ant-card-body]:min-h-0 [&>.ant-card-body]:flex-1 [&>.ant-card-body]:flex-col"
          title={
          <Space>
            <RobotOutlined />
            <span>DoYouTrade Agent</span>
            {activeSession ? <Tag>{activeSession.status}</Tag> : null}
          </Space>
        }
        extra={
          <Space wrap size={4}>
            <Button
              className="lg:!hidden"
              icon={<MenuUnfoldOutlined />}
              onClick={() => setMobileRailOpen(true)}
              title="会话"
              aria-label="会话"
            />
            {activeModelRoute ? (
              <Tag color="blue" className="!hidden md:!inline-block">
                使用的模型: {activeModelRoute}
              </Tag>
            ) : null}
            <Tooltip title="调试模式：逐条展示每个工具调用与思考卡片；关闭后执行过程折叠为单个进度卡片">
              <Space size={4} data-testid="assistant-debug-mode-toggle">
                <BulbOutlined className="text-gray-500 lg:hidden" />
                <span className="hidden text-xs text-gray-500 lg:inline">调试模式</span>
                <Switch
                  size="small"
                  checked={debugRenderMode}
                  onChange={handleDebugRenderModeChange}
                />
              </Space>
            </Tooltip>
            <ToolbarButton
              icon={<CopyOutlined />}
              loading={isCopying}
              disabled={!sessionId}
              onClick={() => void handleCopyConversation()}
              title="复制完整会话（含工具调用、思维链、系统提示词等），用于交给 AI 编程工具分析"
              label="复制会话"
            />
            <ToolbarButton
              icon={<FormOutlined />}
              onClick={() => void createNewSession()}
              label="新会话"
            />
          </Space>
        }
      >
        <Spin
          spinning={loading}
          wrapperClassName="flex min-h-0 flex-1 flex-col [&_.ant-spin-container]:flex [&_.ant-spin-container]:min-h-0 [&_.ant-spin-container]:flex-1 [&_.ant-spin-container]:flex-col"
        >
          <div className="relative flex min-h-0 flex-1 flex-col">
            <div
              ref={conversationScrollRef}
              data-testid="assistant-conversation-scroll"
              onScroll={handleConversationScroll}
              className="flex min-h-0 flex-1 flex-col gap-5 overflow-auto rounded-chat border border-shell-line bg-white px-3 py-4 lg:px-6 lg:py-6"
            >
              {messages.length === 0 && !pendingUserMessage && !sending && !streamingContent ? (
                <div className="w-full">
                  <AssistantWelcome
                    agentName={agents.find((a) => a.id === activeSession?.agent_id)?.name}
                    onPickExample={handlePickExample}
                  />
                </div>
              ) : messages.length === 0 ? null : (
                messages.map((item) => (
                <MessageBubble
                  key={item.message_id}
                  item={item}
                  toolCalls={Object.values(toolCallsByAttempt[item.linked_attempt_id ?? ""] ?? {})}
                  onAnswerUserQuestion={handleAnswerUserQuestion}
                  pendingQuestionId={pendingQuestionId}
                  debugMode={debugRenderMode}
                />
              ))
              )}
              {pendingUserMessage ? (
                <div ref={pendingUserMessageRef}>
                  <MessageBubble
                    key={pendingUserMessage.message_id}
                    item={pendingUserMessage}
                  />
                </div>
              ) : null}
              {(streamingContentBlocks.length > 0 || streamingThinking) &&
              (sending || activeSession?.status === "running") ? (
                <div className="flex w-full justify-start">
                  <div className="min-w-0 w-full max-w-[860px]">
                    <MessageContentRenderer
                      text=""
                      thinking={streamingThinking}
                      contentBlocks={[
                        ...streamingContentBlocks,
                        ...(streamingThinking ? [{ type: "thinking" as const, content: streamingThinking }] : []),
                      ]}
                      toolCalls={Object.values(toolCallsByAttempt[currentAttemptIdRef.current] ?? {})}
                      debugMode={debugRenderMode}
                      streaming
                    />
                  </div>
                </div>
              ) : null}
              {streamingContent ? (
                <MessageBubble
                  item={{
                    message_id: "streaming-assistant",
                    session_id: sessionId ?? "",
                    role: "assistant",
                    content: streamingContent,
                    created_at: new Date().toISOString(),
                    linked_attempt_id: null,
                    metadata: {},
                  }}
                  // 简洁模式下过程已由上方的流式占位卡承载，这里只渲染正文，
                  // 避免同一批工具调用在两张卡里重复出现。
                  toolCalls={
                    debugRenderMode
                      ? Object.values(toolCallsByAttempt[currentAttemptIdRef.current] ?? {})
                      : []
                  }
                  debugMode={debugRenderMode}
                />
              ) : null}
              {livePendingQuestion ? (
                <div className="flex w-full justify-start">
                  <div className="min-w-0 w-full max-w-[860px]">
                    <MessageContentRenderer
                      text=""
                      contentBlocks={[livePendingQuestion]}
                      onAnswerUserQuestion={handleAnswerUserQuestion}
                      pendingQuestionId={livePendingQuestion.question_id}
                    />
                  </div>
                </div>
              ) : null}
              {(sending || activeSession?.status === "running") &&
              (debugRenderMode ||
                !(streamingContentBlocks.length > 0 || streamingThinking)) ? (
                <ThinkingSpinner className="self-start" label="Agent 思考中…" />
              ) : null}
              <div ref={bottomRef} />
            </div>
            {!isAtConversationBottom ? (
              <Button
                className="absolute bottom-4 right-4 shadow-lg"
                shape="circle"
                icon={<DownOutlined />}
                onClick={scrollConversationToBottom}
                aria-label="回到底部"
              />
            ) : null}
          </div>
        </Spin>
        {attachments.length > 0 ? (
          <div className="mb-2 flex flex-wrap items-center gap-2">
            {attachments.map((att) => (
              <Tag
                key={att.file_id}
                closable
                icon={<PaperClipOutlined />}
                onClose={() =>
                  setAttachments((prev) => prev.filter((a) => a.file_id !== att.file_id))
                }
                className="max-w-[240px]"
              >
                <span className="truncate align-middle">{att.filename}</span>
              </Tag>
            ))}
            <Typography.Text type="secondary" className="text-xs">
              已上传
            </Typography.Text>
          </div>
        ) : null}
        {tradeApprovals.length > 0 ? (
          <div className="mt-3" data-testid="assistant-trade-approvals">
            <ApprovalQueueCard
              items={tradeApprovals}
              loading={false}
              onMutated={() => void refreshTradeApprovals()}
            />
          </div>
        ) : null}
        {pendingApproval ? (
          <div
            className="mt-3 rounded-xl border border-red-200 bg-red-50/70 px-4 py-3"
            data-testid="assistant-pending-approval"
          >
            <div className="mb-1 text-sm font-medium text-red-700">
              🔒 操作需要审批：{pendingApproval.description}
            </div>
            {pendingApproval.command_preview ? (
              <pre className="mb-2 max-h-24 overflow-auto rounded bg-white/70 px-2 py-1 text-xs text-gray-700">
                {pendingApproval.command_preview}
              </pre>
            ) : null}
            {pendingApproval.allow_always !== false ? (
              <div className="mb-2">
                <div className="mb-1 text-xs text-gray-600">
                  命令前缀（本会话总是允许 / 写入 settings 可改；留空则按规则记住）
                </div>
                <Input
                  size="small"
                  value={approvalPrefix}
                  onChange={(event) => setApprovalPrefix(event.target.value)}
                  placeholder={pendingApproval.suggested_prefix || "例如 doyoutrade-cli task start:*"}
                  data-testid="assistant-approval-prefix"
                />
              </div>
            ) : null}
            <div className="flex flex-wrap gap-2">
              <Button size="small" type="primary" onClick={() => void handleResolveApproval("approve_once")}>
                允许一次
              </Button>
              {pendingApproval.allow_always !== false ? (
                <>
                  <Button size="small" onClick={() => void handleResolveApproval("approve_always")}>
                    本会话总是允许
                  </Button>
                  <Button size="small" onClick={() => void handleResolveApproval("approve_persist")}>
                    写入 settings
                  </Button>
                </>
              ) : null}
              <Button size="small" danger onClick={() => handleRejectApproval()}>
                拒绝
              </Button>
            </div>
          </div>
        ) : null}
        {/* items-end：附件 / 发送按钮贴输入框底部对齐，不随多行输入被拉伸成高条 */}
        <div className="mt-3 flex items-end gap-2 pb-[env(safe-area-inset-bottom)]">
          <Input.TextArea
            ref={inputRef}
            className="min-w-0 flex-1"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onPressEnter={(event) => {
              if (!event.shiftKey) {
                event.preventDefault();
                void submit();
              }
            }}
            autoSize={{ minRows: 2, maxRows: 5 }}
            placeholder="例如：写一个 A 股日线策略定义，创建实例和组合图，并绑定到已有 backtest task 跑 2025 年回测。"
          />
          <Button
            icon={<PlusOutlined />}
            onClick={() => void handleUploadClick()}
            disabled={sending}
            title="上传文件"
            aria-label="上传文件"
          />
          <Button
            type="primary"
            icon={sending || isStopping || activeSession?.status === "running" ? <StopOutlined /> : <SendOutlined />}
            title={sending || isStopping || activeSession?.status === "running" ? "停止" : "发送"}
            aria-label={sending || isStopping || activeSession?.status === "running" ? "停止" : "发送"}
            loading={isStopping}
            disabled={
              sending || isStopping || activeSession?.status === "running"
                ? isStopping
                : !activeModelRoute
            }
            onClick={
              sending || isStopping || activeSession?.status === "running"
                ? () => void handleStop()
                : () => void submit()
            }
          >
            <span className="hidden sm:inline">
              {sending || isStopping || activeSession?.status === "running" ? "停止" : "发送"}
            </span>
          </Button>
        </div>
      </Card>
      <div className="hidden min-h-0 min-w-0 flex-col gap-4 lg:flex">{rightRail}</div>
      <Drawer
        title="会话 / Traces"
        placement="right"
        open={mobileRailOpen}
        onClose={() => setMobileRailOpen(false)}
        width="min(380px, 92vw)"
        rootClassName="lg:hidden"
      >
        <div className="flex flex-col gap-4">{rightRail}</div>
      </Drawer>
    </div>
    </div>
  );
}
