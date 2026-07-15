// frontend/src/components/assistant/__tests__/serializeSession.test.ts

import { describe, expect, it } from "vitest";

import type { Agent, AssistantEvent, AssistantMessage, AssistantSession } from "../../../types";
import { serializeSession } from "../serializeSession";
import type { ToolCallEntry } from "../types";

const session: AssistantSession = {
  session_id: "sess-1",
  title: "Test session",
  status: "idle",
  agent_id: "agent-1",
  config: {},
  source_channel: null,
  created_at: "2026-05-23T01:00:00Z",
  updated_at: "2026-05-23T01:05:00Z",
  last_attempt_id: "attempt-1",
};

const agent: Agent = {
  id: "agent-1",
  name: "Test Agent",
  status: "active",
  system_prompt: "You are a helpful assistant.",
  model_route_name: "default",
  tool_names: ["get_kline", "create_cron_job"],
  skill_names: ["lark-mail"],
  max_turns: 10,
  context_compaction: { strategy: "none" } as Agent["context_compaction"],
  is_default: false,
  is_builtin: false,
  created_at: "2026-05-22T00:00:00Z",
  updated_at: "2026-05-22T00:00:00Z",
};

const messages: AssistantMessage[] = [
  {
    message_id: "msg-1",
    session_id: "sess-1",
    role: "user",
    content: "请帮我查 000001 的最近 30 天 K 线",
    created_at: "2026-05-23T01:00:00Z",
    linked_attempt_id: null,
    metadata: {},
  },
  {
    message_id: "msg-2",
    session_id: "sess-1",
    role: "assistant",
    content: "好的，下面是数据摘要……",
    created_at: "2026-05-23T01:02:00Z",
    linked_attempt_id: "attempt-1",
    metadata: {
      content_blocks: [
        { type: "thinking", turn: 1, content: "我需要调用 get_kline 工具" },
        { type: "tool_call", tool_call_id: "tc-1" },
        { type: "text", content: "好的，下面是数据摘要……" },
      ],
    },
  },
];

const toolCallsByAttempt: Record<string, Record<string, ToolCallEntry>> = {
  "attempt-1": {
    "tc-1": {
      tool: {
        type: "tool_use",
        id: "tc-1",
        name: "get_kline",
        input: { symbol: "000001", days: 30 },
        status: "completed",
      },
      result: {
        type: "tool_result",
        tool_use_id: "tc-1",
        output: { rows: 30, sample: "ok" },
        is_error: false,
      },
      attempt_id: "attempt-1",
    },
  },
};

const events: AssistantEvent[] = [
  {
    event_id: "ev-1",
    session_id: "sess-1",
    event_type: "attempt.started",
    payload: { attempt_id: "attempt-1", trace_id: "trace-xyz" },
    created_at: "2026-05-23T01:01:00Z",
  },
  {
    event_id: "ev-2",
    session_id: "sess-1",
    event_type: "tool.call",
    payload: { attempt_id: "attempt-1", tool: "get_kline", tool_call_id: "tc-1" },
    created_at: "2026-05-23T01:01:30Z",
  },
];

describe("serializeSession", () => {
  it("renders session, agent, conversation, and events", () => {
    const output = serializeSession({
      session,
      agent,
      messages,
      events,
      toolCallsByAttempt,
    });

    expect(output).toContain("# Assistant Session Export");
    expect(output).toContain("## Session");
    expect(output).toContain("sess-1");
    expect(output).toContain("## Agent");
    expect(output).toContain("Test Agent");
    expect(output).toContain("You are a helpful assistant.");
    expect(output).toContain("enabled_skills: lark-mail");
    expect(output).toContain("enabled_tools: get_kline, create_cron_job");
    expect(output).toContain("## Conversation");
    expect(output).toContain("Turn 1 — user");
    expect(output).toContain("Turn 2 — assistant");
    expect(output).toContain("attempt: `attempt-1`");
    expect(output).toContain("#### Thinking (turn 1)");
    expect(output).toContain("我需要调用 get_kline 工具");
    expect(output).toContain("Tool Call: `get_kline`");
    expect(output).toContain("\"symbol\": \"000001\"");
    expect(output).toContain("is_error: false");
    expect(output).toContain("\"rows\": 30");
    expect(output).toContain("好的，下面是数据摘要……");
    expect(output).toContain("## Events Timeline");
    expect(output).toContain("attempt.started");
    expect(output).toContain("tool.call");
    // trace_id from attempt.started should make it through as non-duplicated metadata.
    expect(output).toContain("trace_id=trace-xyz");
    // The full event payload JSON dump should NOT be re-printed (it would
    // duplicate tool inputs/outputs / thinking that the Conversation already
    // shows). Only the timeline summary lines should appear.
    expect(output).not.toContain("\"trace_id\": \"trace-xyz\"");
    expect(output).not.toMatch(/```json[\s\S]*"event_id"/);
  });

  it("exports the effective system prompt snapshot when present", () => {
    const output = serializeSession({
      session: {
        ...session,
        config: {
          system_prompt_snapshot:
            "Effective prompt with runtime tool inventory and skill preload.",
        },
      },
      agent: {
        ...agent,
        system_prompt: "",
        resolved_system_prompt: "Rendered template prompt that is not the session snapshot.",
      },
      messages: [],
      events: [],
      toolCallsByAttempt: {},
    });

    expect(output).toContain("### Effective System Prompt");
    expect(output).toContain("source: `session.config.system_prompt_snapshot`");
    expect(output).toContain("Effective prompt with runtime tool inventory and skill preload.");
    expect(output).not.toContain("Rendered template prompt that is not the session snapshot.");
    expect(output).not.toContain("```text\n(empty)\n```");
  });

  it("filters out thinking.delta / thinking.done from the timeline (already in conversation)", () => {
    const output = serializeSession({
      session,
      agent,
      messages,
      events: [
        ...events,
        {
          event_id: "ev-3",
          session_id: "sess-1",
          event_type: "thinking.delta",
          payload: { attempt_id: "attempt-1", delta: "我需要" },
          created_at: "2026-05-23T01:01:31Z",
        },
        {
          event_id: "ev-4",
          session_id: "sess-1",
          event_type: "thinking.done",
          payload: { attempt_id: "attempt-1", thinking: "我需要调用 get_kline 工具", turn: 1 },
          created_at: "2026-05-23T01:01:32Z",
        },
      ],
      toolCallsByAttempt,
    });
    expect(output).not.toContain("thinking.delta");
    expect(output).not.toContain("thinking.done");
    // The thinking text must still appear exactly once, from the Conversation block.
    const occurrences = output.split("我需要调用 get_kline 工具").length - 1;
    expect(occurrences).toBe(1);
  });

  it("splits inline <think> markup out of a persisted text content block", () => {
    const output = serializeSession({
      session,
      agent,
      messages: [
        {
          message_id: "msg-3",
          session_id: "sess-1",
          role: "assistant",
          content: "<think>internal reasoning</think>the visible answer",
          created_at: "2026-05-23T01:03:00Z",
          linked_attempt_id: "attempt-2",
          metadata: {
            content_blocks: [
              { type: "text", content: "<think>internal reasoning</think>the visible answer" },
            ],
          },
        },
      ],
      events: [],
      toolCallsByAttempt: {},
    });

    expect(output).not.toContain("<think>");
    expect(output).toContain("#### Thinking (inline)");
    expect(output).toContain("internal reasoning");
    expect(output).toContain("the visible answer");
  });

  it("includes tool catalog descriptions when provided", () => {
    const output = serializeSession({
      session,
      agent,
      messages,
      events,
      toolCallsByAttempt,
      toolCatalog: [
        { name: "get_kline", category: "kline", description: "Fetch K-line data" },
        { name: "other_tool", category: "misc", description: "Unrelated" },
      ],
    });

    expect(output).toContain("### Available Tools (descriptions)");
    expect(output).toContain("**get_kline** `(kline)` — Fetch K-line data");
    expect(output).not.toContain("Unrelated");
  });

  it("handles empty conversation", () => {
    const output = serializeSession({
      session,
      agent: null,
      messages: [],
      events: [],
      toolCallsByAttempt: {},
    });
    expect(output).toContain("_(no messages yet)_");
    expect(output).toContain("_(no agent bound to this session)_");
    expect(output).not.toContain("## Raw Events");
  });
});
