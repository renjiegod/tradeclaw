import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, findRecentApiError, listAssistantSessions } from "./api";

function mockFetchOnce(payload: unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("normalizeAssistantSession via listAssistantSessions", () => {
  it("maps the backend `channel_source` field onto source_channel so channel sessions are identifiable", async () => {
    // Mirrors doyoutrade/assistant/repository.py::_derive_channel_source output.
    mockFetchOnce({
      items: [
        {
          session_id: "channel:channel-426f850720ad:ou_3237652123d8002701e8043619c681c1",
          agent_id: "agent_default",
          title: "Every Minute Push",
          status: "idle",
          config: { channel: { channel_id: "channel-426f850720ad", channel_type: "feishu" } },
          channel_source: {
            is_channel_session: true,
            channel_id: "channel-426f850720ad",
            channel_type: "feishu",
          },
          created_at: "2026-04-30T10:20:25",
          updated_at: "2026-05-27T00:50:07",
          last_attempt_id: "attempt-c8f816b96dbd",
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    });

    const result = await listAssistantSessions({ limit: 50 });

    expect(result.items[0].source_channel).toEqual({
      id: "channel-426f850720ad",
      name: null,
      type: "feishu",
    });
  });

  it("leaves source_channel null for a non-channel session", async () => {
    mockFetchOnce({
      items: [
        {
          session_id: "asst-37b9582d65e4",
          agent_id: "agent_default",
          title: "DoYouTrade Agent",
          status: "idle",
          config: { system_prompt_template_id: "main-agent" },
          channel_source: { is_channel_session: false, channel_id: null, channel_type: null },
          created_at: "2026-05-27T00:35:47",
          updated_at: "2026-05-27T00:35:47",
          last_attempt_id: null,
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    });

    const result = await listAssistantSessions({ limit: 50 });

    expect(result.items[0].source_channel).toBeNull();
  });

  it("forwards channel filter query params to the sessions list endpoint", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo) => {
      const url = typeof input === "string" ? input : input.url;
      expect(url).toContain("channel_id=channel-feishu-a");
      return new Response(
        JSON.stringify({ items: [], total: 0, limit: 50, offset: 0 }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    await listAssistantSessions({ limit: 50, channel_id: "channel-feishu-a" });

    expect(fetchMock).toHaveBeenCalled();
  });
});

describe("ApiError parsing", () => {
  it("preserves structured backend error metadata for frontend error dialogs", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            detail: { error_code: "task_not_found", message: "task not found: task-1" },
            error_code: "task_not_found",
            error_type: "HTTPException",
            error_message: "task not found: task-1",
            trace_id: "trace-xyz",
            timestamp: "2026-06-24T03:21:00Z",
            hint: "check task id",
          }),
          {
            status: 404,
            headers: { "Content-Type": "application/json" },
          },
        ),
      ),
    );

    await expect(listAssistantSessions({ limit: 10 })).rejects.toMatchObject({
      name: "ApiError",
      message: "task not found: task-1",
      status: 404,
      traceId: "trace-xyz",
      timestamp: "2026-06-24T03:21:00Z",
      errorCode: "task_not_found",
      errorType: "HTTPException",
      hint: "check task id",
    });

    const recent = findRecentApiError("加载失败：task not found: task-1");
    expect(recent).toBeInstanceOf(ApiError);
    expect(recent?.traceId).toBe("trace-xyz");
  });
});

describe("swarm API", () => {
  it("listSwarmPresets 解出 presets 数组", async () => {
    const { listSwarmPresets } = await import("./api");
    mockFetchOnce({ presets: [{ name: "investment_committee", title: "投资委员会", description: "", agent_count: 4, variables: [] }] });
    const presets = await listSwarmPresets();
    expect(presets).toHaveLength(1);
    expect(presets[0].name).toBe("investment_committee");
  });

  it("startSwarmRun POST 出 preset_name + user_vars 并返回 run", async () => {
    const { startSwarmRun } = await import("./api");
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ id: "swarm-x", preset_name: "investment_committee", status: "running", user_vars: {}, provider: null, model: null, final_report: null, total_input_tokens: 0, total_output_tokens: 0, error: null, created_at: "", completed_at: null, tasks: [] }), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const run = await startSwarmRun("investment_committee", { target: "AAPL", market: "US" });
    expect(run.id).toBe("swarm-x");
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      preset_name: "investment_committee",
      user_vars: { target: "AAPL", market: "US" },
    });
  });
});
