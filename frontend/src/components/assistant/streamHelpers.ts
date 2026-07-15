import type {
  AssistantChannel,
  AssistantEvent,
  AssistantSession,
} from "../../types";
import { parseBackendDateTime } from "../../utils/datetime";
import { parseToolResultPreview, toolStatusFromResult, type ToolCallEntry } from "./types";

/** Pure event-replay / formatting helpers for the assistant chat page.
 *
 * Extracted from the 1800-line AssistantPage so the component file is left with
 * the streaming state machine and render wiring. Everything here is
 * side-effect free: it derives display state from persisted/streamed
 * AssistantEvent rows or formats a timestamp / session label, and never touches
 * React state — which is what makes the mid-stream resume logic
 * (findCurrentAttemptId / buildStreamingFromEvents / rebuildToolCallsMaps)
 * testable without rendering the page. Behaviour is unchanged from the inline
 * versions; only the location moved. */

const CONVERSATION_BOTTOM_THRESHOLD_PX = 48;

/** Parse a tool.result preview string into output + is_error. */
export function parsePreview(previewRaw: unknown): { output: unknown; is_error: boolean } {
  if (typeof previewRaw !== "string") {
    return { output: previewRaw, is_error: false };
  }
  const parsed = parseToolResultPreview(previewRaw, undefined);
  return { output: parsed?.output ?? previewRaw, is_error: parsed?.is_error ?? false };
}

export function isScrolledNearBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= CONVERSATION_BOTTOM_THRESHOLD_PX;
}

/**
 * Whether a live SSE event's `attempt_id` matches the attempt currently being
 * displayed. Guards the live `tool.call` / `tool.result` / `thinking.delta` /
 * `thinking.done` listeners against stale or foreign events — a delayed
 * reconnect replay, a stuck server-side event queue, a second browser tab on
 * the same session — that would otherwise render under the wrong turn (e.g. a
 * finished attempt's tool call flashing beneath a just-sent new message).
 * Events missing `attempt_id` are let through unfiltered: the backend always
 * sets it in practice, so treating an absence as "can't verify, don't drop"
 * is safer than starting to silently swallow events on a field we've never
 * seen missing.
 */
export function isEventForCurrentAttempt(
  payload: Record<string, unknown>,
  currentAttemptId: string,
): boolean {
  const attemptId = typeof payload.attempt_id === "string" ? payload.attempt_id : "";
  if (!attemptId) return true;
  return attemptId === currentAttemptId;
}

export type StreamingContentBlock =
  | { type: "thinking"; turn?: number; content: string }
  | { type: "tool_call"; tool_call_id: string }
  | { type: "text"; content: string };

/**
 * Walk events to find the attempt_id that has started but not yet finished
 * (no matching attempt.completed/failed/stopped). Returns null if no attempt
 * is in progress.
 */
export function findCurrentAttemptId(events: AssistantEvent[]): string | null {
  let current: string | null = null;
  for (const event of events) {
    const aid = typeof event.payload?.attempt_id === "string" ? (event.payload.attempt_id as string) : "";
    if (!aid) continue;
    if (event.event_type === "attempt.started") {
      current = aid;
    } else if (
      event.event_type === "attempt.completed" ||
      event.event_type === "attempt.failed" ||
      event.event_type === "attempt.stopped"
    ) {
      if (aid === current) current = null;
    }
  }
  return current;
}

/**
 * Rebuild the streaming display state (thinking accumulator, thinking blocks,
 * content blocks, latest trace_id) by replaying the in-progress attempt's
 * events. Used to resume mid-stream visibility after a browser refresh.
 */
export function buildStreamingFromEvents(
  events: AssistantEvent[],
  currentAttemptId: string,
): {
  streamingThinking: string;
  thinkingBlocks: Array<{ turn?: number; content: string }>;
  contentBlocks: StreamingContentBlock[];
  traceId: string | null;
} {
  let streamingThinking = "";
  const thinkingBlocks: Array<{ turn?: number; content: string }> = [];
  const contentBlocks: StreamingContentBlock[] = [];
  let traceId: string | null = null;

  for (const event of events) {
    const payload = (event.payload ?? {}) as Record<string, unknown>;
    const attemptId = typeof payload.attempt_id === "string" ? payload.attempt_id : "";
    if (attemptId !== currentAttemptId) continue;

    if (event.event_type === "attempt.started") {
      streamingThinking = "";
      const t = typeof payload.trace_id === "string" ? payload.trace_id : null;
      if (t) traceId = t;
    } else if (event.event_type === "thinking.delta") {
      const delta = typeof payload.delta === "string" ? payload.delta : "";
      streamingThinking += delta;
    } else if (event.event_type === "thinking.done") {
      const turn = typeof payload.turn === "number" ? payload.turn : undefined;
      const thinking = typeof payload.thinking === "string" ? payload.thinking : "";
      if (thinking) {
        thinkingBlocks.push({ turn, content: thinking });
        contentBlocks.push({ type: "thinking", turn, content: thinking });
      }
      streamingThinking = "";
    } else if (event.event_type === "tool.call") {
      const tcId = typeof payload.tool_call_id === "string" ? payload.tool_call_id : "";
      if (tcId) contentBlocks.push({ type: "tool_call", tool_call_id: tcId });
    }
  }

  return { streamingThinking, thinkingBlocks, contentBlocks, traceId };
}

/**
 * Rebuild both toolCallsState (session->tool_call_id) and toolCallsByAttempt
 * (attempt_id->tool_call_id) from persisted event rows.
 */
export function rebuildToolCallsMaps(events: AssistantEvent[]): {
  bySession: Record<string, Record<string, ToolCallEntry>>;
  byAttempt: Record<string, Record<string, ToolCallEntry>>;
} {
  const bySession: Record<string, Record<string, ToolCallEntry>> = {};
  const byAttempt: Record<string, Record<string, ToolCallEntry>> = {};
  for (const event of events) {
    if (event.event_type === "tool.call") {
      const p = event.payload;
      const tool_call_id = typeof p.tool_call_id === "string" ? p.tool_call_id : String(p.tool_call_id ?? Date.now());
      const attempt_id = typeof p.attempt_id === "string" ? p.attempt_id : "";
      const tool_name = typeof p.tool === "string" ? p.tool : "";
      const input = (p.arguments ?? {}) as Record<string, unknown>;
      const session_id = event.session_id;
      if (!bySession[session_id]) bySession[session_id] = {};
      if (!byAttempt[attempt_id]) byAttempt[attempt_id] = {};
      const entry: ToolCallEntry = {
        tool: { type: "tool_use", id: tool_call_id, name: tool_name, input, status: "completed" },
        attempt_id,
      };
      bySession[session_id][tool_call_id] = entry;
      byAttempt[attempt_id][tool_call_id] = entry;
    } else if (event.event_type === "tool.result") {
      const p = event.payload;
      const tool_call_id = typeof p.tool_call_id === "string" ? p.tool_call_id : "";
      const attempt_id = typeof p.attempt_id === "string" ? p.attempt_id : "";
      const { output, is_error } = parsePreview(p.preview);
      const session_id = event.session_id;
      if (bySession[session_id]?.[tool_call_id]) {
        const existing = bySession[session_id][tool_call_id];
        bySession[session_id][tool_call_id] = {
          ...existing,
          tool: {
            ...existing.tool,
            status: toolStatusFromResult(existing.tool, { is_error }),
          },
          result: { type: "tool_result", tool_use_id: tool_call_id, output, is_error },
        };
      }
      if (byAttempt[attempt_id]?.[tool_call_id]) {
        const existing = byAttempt[attempt_id][tool_call_id];
        byAttempt[attempt_id][tool_call_id] = {
          ...existing,
          tool: {
            ...existing.tool,
            status: toolStatusFromResult(existing.tool, { is_error }),
          },
          result: { type: "tool_result", tool_use_id: tool_call_id, output, is_error },
        };
      }
    }
  }
  return { bySession, byAttempt };
}

/** 将 ISO 时间转换为"今天 hh:mm:ss"、"昨天 hh:mm:ss" 或 "2026-04-12 10:30:12" */
export function formatMessageTime(isoString: string): string {
  // 使用项目已有工具将字符串强制解析为 UTC（避免 JS Date 将无 Z 后缀的 ISO 字符串当作本地时区）
  const date = parseBackendDateTime(isoString);
  const now = new Date();

  // 提取 message 时间戳的 UTC+8 分量（通过 hour 进位）
  const msgUtcMs = date.getTime();
  const msgHourUtc = new Date(msgUtcMs).getUTCHours();
  const msgMinUtc = new Date(msgUtcMs).getUTCMinutes();
  const msgSecUtc = new Date(msgUtcMs).getUTCSeconds();

  // UTC+8 = UTC + 8h；8h 进位到日期需要单独处理
  let msgHourUtc8 = msgHourUtc + 8;
  let msgDayUtc8 = new Date(msgUtcMs).getUTCDate();
  let msgMonthUtc8 = new Date(msgUtcMs).getUTCMonth();
  let msgYearUtc8 = new Date(msgUtcMs).getUTCFullYear();
  if (msgHourUtc8 >= 24) {
    msgHourUtc8 -= 24;
    msgDayUtc8 += 1;
    // 进位到月
    const daysInMsgMonth = new Date(Date.UTC(msgYearUtc8, msgMonthUtc8 + 1, 0)).getUTCDate();
    if (msgDayUtc8 > daysInMsgMonth) {
      msgDayUtc8 = 1;
      msgMonthUtc8 += 1;
      // 进位到年
      if (msgMonthUtc8 > 11) {
        msgMonthUtc8 = 0;
        msgYearUtc8 += 1;
      }
    }
  }

  // 提取 now 的 UTC+8 分量
  const nowHourUtc = now.getUTCHours();
  const nowMinUtc = now.getUTCMinutes();
  const nowSecUtc = now.getUTCSeconds();
  let nowHourUtc8 = nowHourUtc + 8;
  let nowDayUtc8 = now.getUTCDate();
  let nowMonthUtc8 = now.getUTCMonth();
  let nowYearUtc8 = now.getUTCFullYear();
  if (nowHourUtc8 >= 24) {
    nowHourUtc8 -= 24;
    nowDayUtc8 += 1;
    const daysInNowMonth = new Date(Date.UTC(nowYearUtc8, nowMonthUtc8 + 1, 0)).getUTCDate();
    if (nowDayUtc8 > daysInNowMonth) {
      nowDayUtc8 = 1;
      nowMonthUtc8 += 1;
      if (nowMonthUtc8 > 11) {
        nowMonthUtc8 = 0;
        nowYearUtc8 += 1;
      }
    }
  }

  const pad = (n: number) => String(n).padStart(2, "0");
  const msgHh = pad(msgHourUtc8);
  const msgMm = pad(msgMinUtc);
  const msgSs = pad(msgSecUtc);

  // 判断今天/昨天：用完整的 UTC+8 日期比较，避免月边界问题
  const yesterdayDayUtc8 = nowDayUtc8 - 1;
  let yesterdayDayUtc8Norm = yesterdayDayUtc8;
  let yesterdayMonthUtc8 = nowMonthUtc8;
  let yesterdayYearUtc8 = nowYearUtc8;
  if (yesterdayDayUtc8 < 1) {
    // 退到上月
    yesterdayMonthUtc8 -= 1;
    if (yesterdayMonthUtc8 < 0) {
      yesterdayMonthUtc8 = 11;
      yesterdayYearUtc8 -= 1;
    }
    yesterdayDayUtc8Norm = new Date(Date.UTC(yesterdayYearUtc8, yesterdayMonthUtc8 + 1, 0)).getUTCDate();
  }

  const isToday = msgYearUtc8 === nowYearUtc8 && msgMonthUtc8 === nowMonthUtc8 && msgDayUtc8 === nowDayUtc8;
  const isYesterday = msgYearUtc8 === yesterdayYearUtc8 && msgMonthUtc8 === yesterdayMonthUtc8 && msgDayUtc8 === yesterdayDayUtc8Norm;

  if (isToday) {
    return `今天 ${msgHh}:${msgMm}:${msgSs}`;
  }
  if (isYesterday) {
    return `昨天 ${msgHh}:${msgMm}:${msgSs}`;
  }

  // 其他：绝对时间
  return `${msgYearUtc8}-${pad(msgMonthUtc8 + 1)}-${pad(msgDayUtc8)} ${msgHh}:${msgMm}:${msgSs}`;
}

export function formatSessionSourceChannelLabel(
  session: AssistantSession,
  channelNames?: Map<string, Pick<AssistantChannel, "name" | "type">>,
): string | null {
  const source = session.source_channel;
  if (!source?.id) {
    return null;
  }
  const channelMeta = channelNames?.get(source.id);
  const name = source.name ?? channelMeta?.name ?? null;
  const type = source.type ?? channelMeta?.type ?? null;
  const parts = [type, name, source.id].filter((value): value is string => Boolean(value && value.trim()));
  if (parts.length === 0) {
    return null;
  }
  return parts.join(" / ");
}

export function formatSessionOptionTitle(session: AssistantSession, agentName?: string | null): string {
  const suffix = agentName ? ` · ${agentName}` : "";
  return `${session.title || session.session_id}${suffix}`;
}
