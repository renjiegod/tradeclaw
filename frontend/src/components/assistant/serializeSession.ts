// frontend/src/components/assistant/serializeSession.ts

import type {
  Agent,
  AssistantEvent,
  AssistantMessage,
  AssistantSession,
  AssistantTool,
} from "../../types";

import type { ToolCallEntry } from "./types";
import { stripReasoningTags } from "./reasoningTags";

export interface SerializeSessionInput {
  session: AssistantSession | null;
  agent: Agent | null;
  messages: AssistantMessage[];
  events: AssistantEvent[];
  toolCallsByAttempt: Record<string, Record<string, ToolCallEntry>>;
  /** Optional full tool catalog (used to print descriptions for tools the agent can call). */
  toolCatalog?: AssistantTool[];
}

function fence(language: string, body: string): string {
  return ["```" + language, body, "```"].join("\n");
}

function jsonFence(value: unknown): string {
  if (value === undefined) {
    return fence("json", "undefined");
  }
  try {
    return fence("json", JSON.stringify(value, null, 2));
  } catch {
    return fence("text", String(value));
  }
}

function nonEmptyString(value: unknown): string {
  return typeof value === "string" && value.trim() ? value : "";
}

function resolveEffectiveSystemPrompt(
  session: AssistantSession | null,
  agent: Agent,
): { prompt: string; source: string } {
  const snapshot = nonEmptyString(session?.config?.system_prompt_snapshot);
  if (snapshot) {
    return { prompt: snapshot, source: "session.config.system_prompt_snapshot" };
  }

  const resolved = nonEmptyString(agent.resolved_system_prompt);
  if (resolved) {
    return { prompt: resolved, source: "agent.resolved_system_prompt" };
  }

  const stored = nonEmptyString(agent.system_prompt);
  return { prompt: stored || "(empty)", source: "agent.system_prompt" };
}

function formatAgentSection(
  session: AssistantSession | null,
  agent: Agent | null,
  toolCatalog?: AssistantTool[],
): string {
  if (!agent) {
    return "## Agent\n\n_(no agent bound to this session)_\n";
  }
  const effectivePrompt = resolveEffectiveSystemPrompt(session, agent);
  const lines: string[] = ["## Agent", ""];
  lines.push(`- id: \`${agent.id}\``);
  lines.push(`- name: ${agent.name}`);
  lines.push(`- status: ${agent.status}`);
  lines.push(`- model: ${agent.model_route_name || "(none)"}`);
  lines.push(`- max_turns: ${agent.max_turns}`);
  if (agent.skill_names?.length) {
    lines.push(`- enabled_skills: ${agent.skill_names.join(", ")}`);
  }
  if (agent.tool_names?.length) {
    lines.push(`- enabled_tools: ${agent.tool_names.join(", ")}`);
  }
  if (agent.tool_configs?.length) {
    const loadModes = agent.tool_configs
      .map((cfg) => `${cfg.name}(${cfg.load_mode})`)
      .join(", ");
    lines.push(`- tool_load_modes: ${loadModes}`);
  }
  lines.push("");
  lines.push("### Effective System Prompt");
  lines.push("");
  lines.push(`source: \`${effectivePrompt.source}\``);
  lines.push("");
  lines.push(fence("text", effectivePrompt.prompt));
  lines.push("");

  if (toolCatalog && agent.tool_names?.length) {
    const byName = new Map(toolCatalog.map((t) => [t.name, t]));
    const matched = agent.tool_names
      .map((name) => byName.get(name))
      .filter((t): t is AssistantTool => Boolean(t));
    if (matched.length > 0) {
      lines.push("### Available Tools (descriptions)");
      lines.push("");
      for (const tool of matched) {
        lines.push(`- **${tool.name}** \`(${tool.category})\` — ${tool.description}`);
      }
      lines.push("");
    }
  }

  return lines.join("\n");
}

function formatSessionSection(session: AssistantSession | null): string {
  if (!session) {
    return "## Session\n\n_(no active session)_\n";
  }
  const lines: string[] = ["## Session", ""];
  lines.push(`- session_id: \`${session.session_id}\``);
  lines.push(`- title: ${session.title || "(untitled)"}`);
  lines.push(`- status: ${session.status}`);
  lines.push(`- agent_id: \`${session.agent_id}\``);
  lines.push(`- created_at: ${session.created_at}`);
  lines.push(`- updated_at: ${session.updated_at}`);
  if (session.last_attempt_id) {
    lines.push(`- last_attempt_id: \`${session.last_attempt_id}\``);
  }
  if (session.source_channel?.id) {
    const sc = session.source_channel;
    lines.push(
      `- source_channel: type=${sc.type ?? "?"} name=${sc.name ?? "?"} id=\`${sc.id}\``,
    );
  }
  lines.push("");
  return lines.join("\n");
}

interface ToolCallSnapshot {
  tool_call_id: string;
  name: string;
  status: string;
  input: unknown;
  result?: { output: unknown; is_error: boolean };
}

function snapshotToolCall(
  toolCallId: string,
  attemptId: string | null | undefined,
  toolCallsByAttempt: Record<string, Record<string, ToolCallEntry>>,
  fallback?: {
    name?: string;
    arguments?: Record<string, unknown>;
    status?: string;
    result_preview?: string;
    is_error?: boolean;
  },
): ToolCallSnapshot {
  const entry = attemptId ? toolCallsByAttempt[attemptId]?.[toolCallId] : undefined;
  if (entry) {
    return {
      tool_call_id: toolCallId,
      name: entry.tool.name,
      status: entry.tool.status,
      input: entry.tool.input,
      result: entry.result
        ? { output: entry.result.output, is_error: entry.result.is_error }
        : undefined,
    };
  }
  return {
    tool_call_id: toolCallId,
    name: fallback?.name ?? "(unknown tool)",
    status: fallback?.status ?? "unknown",
    input: fallback?.arguments ?? {},
    result:
      fallback?.result_preview !== undefined
        ? { output: fallback.result_preview, is_error: Boolean(fallback?.is_error) }
        : undefined,
  };
}

function formatToolCall(snapshot: ToolCallSnapshot): string {
  const lines: string[] = [];
  lines.push(
    `#### Tool Call: \`${snapshot.name}\`  (id: \`${snapshot.tool_call_id}\`, status: ${snapshot.status})`,
  );
  lines.push("");
  lines.push("**Input:**");
  lines.push("");
  lines.push(jsonFence(snapshot.input));
  lines.push("");
  if (snapshot.result) {
    lines.push(`**Result:** (is_error: ${snapshot.result.is_error})`);
    lines.push("");
    if (typeof snapshot.result.output === "string") {
      lines.push(fence("text", snapshot.result.output));
    } else {
      lines.push(jsonFence(snapshot.result.output));
    }
    lines.push("");
  } else {
    lines.push("**Result:** _(no result captured)_");
    lines.push("");
  }
  return lines.join("\n");
}

function formatMessage(
  message: AssistantMessage,
  index: number,
  toolCallsByAttempt: Record<string, Record<string, ToolCallEntry>>,
): string {
  const lines: string[] = [];
  const attemptId = message.linked_attempt_id ?? "";
  const header = `### Turn ${index + 1} — ${message.role} @ ${message.created_at}` +
    (attemptId ? `  (attempt: \`${attemptId}\`)` : "");
  lines.push(header);
  lines.push("");

  const contentBlocks = message.metadata?.content_blocks;
  if (Array.isArray(contentBlocks) && contentBlocks.length > 0) {
    for (const block of contentBlocks) {
      if (!block || typeof block !== "object") continue;
      if (block.type === "thinking") {
        const turn = typeof block.turn === "number" ? ` (turn ${block.turn})` : "";
        lines.push(`#### Thinking${turn}`);
        lines.push("");
        lines.push(fence("text", block.content || ""));
        lines.push("");
      } else if (block.type === "tool_call") {
        const snapshot = snapshotToolCall(block.tool_call_id, attemptId, toolCallsByAttempt, {
          name: block.name,
          arguments: block.arguments,
          status: block.status,
          result_preview: block.result_preview,
          is_error: block.is_error,
        });
        lines.push(formatToolCall(snapshot));
      } else if (block.type === "text") {
        // Defensive: already-persisted text may still carry inline
        // <think>...</think> markup from providers that don't separate
        // reasoning_content (see doyoutrade/models/reasoning_tags.py).
        const { visible, thinking: inlineThinking } = stripReasoningTags(block.content || "");
        if (inlineThinking) {
          lines.push("#### Thinking (inline)");
          lines.push("");
          lines.push(fence("text", inlineThinking));
          lines.push("");
        }
        lines.push("#### Text");
        lines.push("");
        lines.push(visible);
        lines.push("");
      }
    }
  } else {
    // Fallback when no structured content_blocks: use raw fields.
    const thinking = typeof message.metadata?.thinking === "string" ? message.metadata.thinking : "";
    if (thinking) {
      lines.push("#### Thinking");
      lines.push("");
      lines.push(fence("text", thinking));
      lines.push("");
    }
    if (message.content) {
      const { visible, thinking: inlineThinking } = stripReasoningTags(message.content);
      if (inlineThinking) {
        lines.push("#### Thinking (inline)");
        lines.push("");
        lines.push(fence("text", inlineThinking));
        lines.push("");
      }
      lines.push("#### Content");
      lines.push("");
      lines.push(visible);
      lines.push("");
    }
  }

  // If the assistant emitted plain text but content_blocks didn't include a
  // trailing text block, surface message.content so the final reply isn't lost.
  if (
    message.role === "assistant" &&
    message.content &&
    Array.isArray(contentBlocks) &&
    contentBlocks.length > 0 &&
    !contentBlocks.some(
      (block) => block && typeof block === "object" && block.type === "text" && block.content === message.content,
    )
  ) {
    const { visible, thinking: inlineThinking } = stripReasoningTags(message.content);
    if (inlineThinking) {
      lines.push("#### Thinking (inline)");
      lines.push("");
      lines.push(fence("text", inlineThinking));
      lines.push("");
    }
    lines.push("#### Final Text");
    lines.push("");
    lines.push(visible);
    lines.push("");
  }

  return lines.join("\n");
}

/**
 * Extract only the bits of an event payload that are NOT already shown in the
 * Conversation section, so the timeline adds context without re-printing tool
 * inputs / outputs / thinking that the conversation already covers.
 */
function nonDuplicateEventMeta(event: AssistantEvent): string[] {
  const payload = (event.payload ?? {}) as Record<string, unknown>;
  const extras: string[] = [];
  const traceId = typeof payload.trace_id === "string" ? payload.trace_id : "";
  if (traceId) extras.push(`trace_id=${traceId}`);
  if (event.event_type === "attempt.failed" || event.event_type === "attempt.stopped") {
    const error = typeof payload.error === "string" ? payload.error : "";
    const reason = typeof payload.reason === "string" ? payload.reason : "";
    if (error) extras.push(`error=${error}`);
    if (reason) extras.push(`reason=${reason}`);
  }
  if (event.event_type === "tool.result") {
    const isError = payload.is_error === true || payload.status === "error";
    if (isError) extras.push("is_error=true");
  }
  return extras;
}

function formatEventsAppendix(events: AssistantEvent[]): string {
  if (events.length === 0) {
    return "";
  }
  // Skip event types whose payload is wholly duplicated inside the Conversation
  // section (tool input/output, thinking text). Keep lifecycle events so the
  // timeline still shows ordering and trace/error metadata.
  const timelineTypes = new Set([
    "attempt.started",
    "attempt.completed",
    "attempt.failed",
    "attempt.stopped",
    "tool.call",
    "tool.result",
  ]);
  const filtered = events.filter((event) => timelineTypes.has(event.event_type));
  if (filtered.length === 0) {
    return "";
  }
  const lines: string[] = [
    "## Events Timeline",
    "",
    "_Ordering / lifecycle metadata only. Tool inputs, outputs, and thinking are in the Conversation section above._",
    "",
  ];
  for (const event of filtered) {
    const summary: string[] = [event.created_at, event.event_type];
    const tool = typeof event.payload?.tool === "string" ? event.payload.tool : "";
    const attemptId = typeof event.payload?.attempt_id === "string" ? event.payload.attempt_id : "";
    const toolCallId = typeof event.payload?.tool_call_id === "string" ? event.payload.tool_call_id : "";
    if (tool) summary.push(`tool=${tool}`);
    if (toolCallId) summary.push(`tool_call_id=${toolCallId}`);
    if (attemptId) summary.push(`attempt=${attemptId}`);
    summary.push(...nonDuplicateEventMeta(event));
    lines.push(`- ${summary.join(" | ")}`);
  }
  lines.push("");
  return lines.join("\n");
}

export function serializeSession(input: SerializeSessionInput): string {
  const { session, agent, messages, events, toolCallsByAttempt, toolCatalog } = input;
  const parts: string[] = [];
  parts.push("# Assistant Session Export");
  parts.push("");
  parts.push(`_Generated at ${new Date().toISOString()}_`);
  parts.push("");
  parts.push(formatSessionSection(session));
  parts.push(formatAgentSection(session, agent, toolCatalog));
  parts.push("## Conversation");
  parts.push("");
  if (messages.length === 0) {
    parts.push("_(no messages yet)_");
    parts.push("");
  } else {
    messages.forEach((message, index) => {
      parts.push(formatMessage(message, index, toolCallsByAttempt));
    });
  }
  const eventsBlock = formatEventsAppendix(events);
  if (eventsBlock) {
    parts.push(eventsBlock);
  }
  return parts.join("\n").replace(/\n{3,}/g, "\n\n");
}
