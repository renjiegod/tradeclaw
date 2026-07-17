import { cleanup, fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { AssistantPage } from "./AssistantPage";

// ``InlineToolCallCard`` (rendered transitively when historical tool
// previews are surfaced) calls ``useNavigate`` for its jump-to-task
// affordance, which requires a router context. Wrap the page render so
// every existing test keeps working without per-case plumbing.
const render: typeof rtlRender = (ui, options) =>
  rtlRender(<MemoryRouter>{ui}</MemoryRouter>, options);
import type { Agent, AssistantMessage, AssistantSession, ModelRouteRow } from "../types";
import {
  createAssistantSession,
  getAssistantSession,
  listAssistantAgents,
  listAssistantChannels,
  listAssistantEvents,
  listAssistantMessages,
  listAssistantSessions,
  listAssistantTraces,
  listModelRoutes,
  listPendingApprovals,
  sendAssistantMessage,
  stopAssistantSession,
  uploadFile,
} from "../api";

vi.mock("../api", () => ({
  assistantEventStreamUrl: vi.fn(() => "http://localhost/assistant-stream"),
  createAssistantSession: vi.fn(),
  getAssistantSession: vi.fn(),
  listAssistantAgents: vi.fn(),
  listAssistantChannels: vi.fn(),
  listAssistantEvents: vi.fn(),
  listAssistantMessages: vi.fn(),
  listAssistantSessions: vi.fn(),
  listAssistantTraces: vi.fn(),
  listModelRoutes: vi.fn(),
  listPendingApprovals: vi.fn(() => Promise.resolve([])),
  sendAssistantMessage: vi.fn(),
  stopAssistantSession: vi.fn(),
  uploadFile: vi.fn(),
}));

vi.mock("../components/assistant/SkillsToolsTab", () => ({
  SkillsToolsTab: () => <div>skills tools</div>,
}));

const session: AssistantSession = {
  session_id: "session-1",
  title: "DoYouTrade Agent",
  status: "idle",
  agent_id: "agent-1",
  config: { model_route_name: "route-default" },
  created_at: "2026-04-29T00:00:00Z",
  updated_at: "2026-04-29T00:00:00Z",
  last_attempt_id: null,
};

const agent: Agent = {
  id: "agent-1",
  name: "Test Agent",
  status: "active",
  system_prompt: "You are a helpful assistant.",
  model_route_name: "route-default",
  tool_names: [],
  skill_names: [],
  max_turns: 6,
  is_default: false,
  is_builtin: false,
  created_at: "2026-04-29T00:00:00Z",
  updated_at: "2026-04-29T00:00:00Z",
};

const route: ModelRouteRow = {
  id: "route-row-1",
  route_name: "route-default",
  provider_id: "provider-1",
  target_model: "model-a",
  settings: null,
  created_at: "2026-04-29T00:00:00Z",
  updated_at: "2026-04-29T00:00:00Z",
};

const assistantMessage: AssistantMessage = {
  message_id: "message-1",
  session_id: "session-1",
  role: "assistant",
  content: "最终回答",
  created_at: "2026-04-29T00:00:00Z",
  linked_attempt_id: null,
  metadata: { thinking: "首先，分析用户问题。\n\n- 检查语言\n- 构建回答" },
};

const newSession: AssistantSession = {
  ...session,
  session_id: "session-2",
  created_at: "2026-04-29T00:01:00Z",
  updated_at: "2026-04-29T00:01:00Z",
};

const messageInputPlaceholder = /例如：写一个 A 股日线策略定义/i;

const channelSession: AssistantSession = {
  ...session,
  session_id: "session-channel-1",
  title: "Feishu DoYouTrade Agent",
  source_channel: {
    id: "channel-feishu-a",
    name: "Feishu Alpha",
    type: "feishu",
  },
};

describe("AssistantPage conversation", () => {
  beforeAll(() => {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    class MockEventSource {
      url: string;

      constructor(url: string) {
        this.url = url;
      }

      addEventListener = vi.fn();
      close = vi.fn();
    }
    Object.defineProperty(window, "EventSource", {
      writable: true,
      value: MockEventSource,
    });
    Object.defineProperty(globalThis, "EventSource", {
      writable: true,
      value: MockEventSource,
    });
  });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listModelRoutes).mockResolvedValue({ items: [route], total: 1, limit: 50, offset: 0 });
    vi.mocked(listAssistantAgents).mockResolvedValue({ items: [agent], total: 1, limit: 50, offset: 0 });
    vi.mocked(listAssistantChannels).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(listAssistantSessions).mockResolvedValue({ items: [session], total: 1, limit: 50, offset: 0 });
    vi.mocked(getAssistantSession).mockResolvedValue(newSession);
    vi.mocked(listAssistantMessages).mockResolvedValue([assistantMessage]);
    vi.mocked(listAssistantEvents).mockResolvedValue([]);
    vi.mocked(listAssistantTraces).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(stopAssistantSession).mockResolvedValue({ stopped: true });
    vi.mocked(createAssistantSession).mockResolvedValue(newSession);
  });

  afterEach(() => {
    cleanup();
    // 渲染模式开关持久化在 localStorage，jsdom 里跨用例共享，必须清掉
    // 以免上一个用例设置的调试模式泄漏到下一个用例。
    localStorage.removeItem("assistant_debug_mode");
  });

  it("surfaces a pending live-trading approval card inline in the chat", async () => {
    vi.mocked(listPendingApprovals).mockResolvedValue([
      {
        approval_id: "appr-live-1",
        intent_id: "intent-live-1",
        status: "pending",
        mode: "live",
        symbol: "601398.SH",
        action: "buy",
        notional: "780",
        created_at: "2026-06-14T02:00:00",
        expires_at: "2026-06-14T03:00:00",
        task_id: "task-live-1",
      },
    ]);

    render(<AssistantPage />);

    // The global pending trade approval is polled on mount and rendered as an
    // inline card (web analog of the Feishu push), independent of session state.
    expect(await screen.findByTestId("assistant-trade-approvals")).toBeInTheDocument();
    expect(await screen.findByText("601398.SH")).toBeInTheDocument();
  });

  it("renders the thinking block above the assistant answer and allows collapsing it (debug mode)", async () => {
    localStorage.setItem("assistant_debug_mode", "true");
    render(<AssistantPage />);

    await waitFor(() => expect(screen.getByText("深度思考已完成")).toBeInTheDocument());

    expect(screen.getByText("首先，分析用户问题。")).toBeInTheDocument();
    expect(screen.getByText("最终回答")).toBeInTheDocument();

    const thinking = screen.getByText("深度思考已完成").closest("section");
    const answer = screen.getByText("最终回答");
    expect(thinking?.compareDocumentPosition(answer) ?? 0).toBe(Node.DOCUMENT_POSITION_FOLLOWING);

    fireEvent.click(screen.getAllByRole("button", { name: "收起思考内容" })[0]!);
    expect(screen.queryByText("首先，分析用户问题。")).not.toBeInTheDocument();
  });

  it("folds thinking into a collapsed process card by default (simple mode)", async () => {
    render(<AssistantPage />);

    await waitFor(() =>
      expect(screen.getByTestId("collapsed-process-card")).toBeInTheDocument(),
    );
    // Simple mode is the default: no per-block thinking card, content folded.
    expect(screen.queryByText("深度思考已完成")).not.toBeInTheDocument();
    expect(screen.queryByText("首先，分析用户问题。")).not.toBeInTheDocument();
    expect(screen.getByText("最终回答")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("process-card-header"));
    expect(screen.getByText("首先，分析用户问题。")).toBeInTheDocument();
  });

  it("switches to per-card rendering when the debug toggle is turned on", async () => {
    render(<AssistantPage />);

    await waitFor(() =>
      expect(screen.getByTestId("collapsed-process-card")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("switch"));

    await waitFor(() => expect(screen.getByText("深度思考已完成")).toBeInTheDocument());
    expect(screen.queryByTestId("collapsed-process-card")).not.toBeInTheDocument();
    expect(localStorage.getItem("assistant_debug_mode")).toBe("true");
  });

  it("createNewSession shows warning when no agent selected", async () => {
    // Return empty agents list so no agent is auto-selected
    vi.mocked(listAssistantAgents).mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
    vi.mocked(listAssistantSessions).mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });

    render(<AssistantPage />);
    await waitFor(() => expect(screen.getByText("选择 Agent")).toBeInTheDocument());

    const newSessionButton = screen.getByRole("button", { name: /新会话/i });
    fireEvent.click(newSessionButton);

    await waitFor(() => {
      expect(screen.queryByText("请先选择一个 Agent")).toBeInTheDocument();
    });
  });

  it("createNewSession calls API with agent_id when agent selected", async () => {
    const mockCreateSession = vi.fn().mockResolvedValue({
      session_id: "new-session-id",
      title: "DoYouTrade Agent",
      status: "idle",
      agent_id: "agent-1",
      config: { model_route_name: "route-default" },
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      last_attempt_id: null,
    });
    vi.mocked(createAssistantSession).mockImplementation(mockCreateSession);
    vi.mocked(listAssistantSessions).mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });

    render(<AssistantPage />);
    await waitFor(() => expect(screen.getByText("选择 Agent")).toBeInTheDocument());

    const newSessionButton = screen.getByRole("button", { name: /新会话/i });
    fireEvent.click(newSessionButton);

    await waitFor(() => {
      expect(mockCreateSession).toHaveBeenCalledWith({
        title: "新会话",
        agent_id: "agent-1",
      });
    });
  });

  it("auto-creates a session on send when none exists (fresh install)", async () => {
    const createdSession: AssistantSession = {
      ...session,
      session_id: "auto-session-1",
      title: "新会话",
    };
    vi.mocked(listAssistantSessions)
      .mockResolvedValueOnce({ items: [], total: 0, limit: 50, offset: 0 })
      .mockResolvedValue({ items: [createdSession], total: 1, limit: 50, offset: 0 });
    vi.mocked(listAssistantMessages).mockResolvedValue([]);
    vi.mocked(createAssistantSession).mockResolvedValue(createdSession);
    vi.mocked(sendAssistantMessage).mockResolvedValue({
      session: createdSession,
      messages: [
        {
          message_id: "u1",
          session_id: "auto-session-1",
          role: "user",
          content: "你好",
          created_at: "2026-04-29T00:00:00Z",
          linked_attempt_id: null,
          metadata: {},
        },
      ],
      trace_id: null,
    });

    render(<AssistantPage />);
    await waitFor(() => expect(screen.getByText("试试这些示例：")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText(messageInputPlaceholder), {
      target: { value: "你好" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发送/i }));

    await waitFor(() =>
      expect(createAssistantSession).toHaveBeenCalledWith({
        title: "新会话",
        agent_id: "agent-1",
      }),
    );
    await waitFor(() =>
      expect(sendAssistantMessage).toHaveBeenCalledWith("auto-session-1", "你好"),
    );
  });

  it("sends attachments as structured metadata, never as a path in the message text", async () => {
    const createdSession: AssistantSession = {
      ...session,
      session_id: "auto-session-1",
      title: "新会话",
    };
    vi.mocked(listAssistantSessions)
      .mockResolvedValueOnce({ items: [], total: 0, limit: 50, offset: 0 })
      .mockResolvedValue({ items: [createdSession], total: 1, limit: 50, offset: 0 });
    vi.mocked(listAssistantMessages).mockResolvedValue([]);
    vi.mocked(createAssistantSession).mockResolvedValue(createdSession);
    vi.mocked(uploadFile).mockResolvedValue({
      status: "ok",
      file_id: "0123456789abcdef0123456789abcdef.pdf",
      filename: "流水.pdf",
      mime_type: "application/pdf",
      size_bytes: 1234,
    });
    vi.mocked(sendAssistantMessage).mockResolvedValue({
      session: createdSession,
      messages: [],
      trace_id: null,
    });

    const { container } = render(<AssistantPage />);
    await waitFor(() => expect(screen.getByText("试试这些示例：")).toBeInTheDocument());

    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["x"], "流水.pdf", { type: "application/pdf" });
    fireEvent.change(fileInput, { target: { files: [file] } });

    // The composer shows a filename chip — never the server path.
    await waitFor(() => expect(screen.getByText("流水.pdf")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText(messageInputPlaceholder), {
      target: { value: "分析这个文件" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发送/i }));

    await waitFor(() =>
      expect(sendAssistantMessage).toHaveBeenCalledWith("auto-session-1", "分析这个文件", [
        {
          file_id: "0123456789abcdef0123456789abcdef.pdf",
          filename: "流水.pdf",
          mime_type: "application/pdf",
          size_bytes: 1234,
        },
      ]),
    );
    // The message text carries the user's words only — no path, no prefix.
    const lastCall = vi.mocked(sendAssistantMessage).mock.calls.at(-1)!;
    expect(lastCall[1]).toBe("分析这个文件");
    expect(lastCall[1]).not.toContain("path:");
    expect(lastCall[1]).not.toContain("Uploaded file");
  });

  it("warns instead of silently no-oping when send has no session and no agent", async () => {
    vi.mocked(listAssistantAgents).mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
    vi.mocked(listAssistantSessions).mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });

    render(<AssistantPage />);
    await waitFor(() => expect(screen.getByText("选择 Agent")).toBeInTheDocument());

    const textarea = screen.getByPlaceholderText(messageInputPlaceholder);
    fireEvent.change(textarea, { target: { value: "你好" } });
    // Button is disabled without a model route; Enter still reaches submit().
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter", keyCode: 13, shiftKey: false });

    await waitFor(() => {
      expect(screen.getByText("请先选择一个 Agent，或点击「新会话」")).toBeInTheDocument();
    });
    expect(createAssistantSession).not.toHaveBeenCalled();
    expect(sendAssistantMessage).not.toHaveBeenCalled();
  });

  it("loads the requested session_id from the URL even when it is outside the first page", async () => {
    const urlSession: AssistantSession = {
      ...newSession,
      title: "Cron session 2",
    };
    const urlMessage: AssistantMessage = {
      ...assistantMessage,
      session_id: "session-2",
      content: "来自 URL 的 cron 会话",
    };
    vi.mocked(listAssistantSessions).mockResolvedValue({ items: [session], total: 1, limit: 50, offset: 0 });
    vi.mocked(getAssistantSession).mockResolvedValue(urlSession);
    vi.mocked(listAssistantMessages).mockImplementation(async (sessionId: string) =>
      sessionId === "session-2" ? [urlMessage] : [assistantMessage],
    );

    rtlRender(
      <MemoryRouter initialEntries={["/assistant?session_id=session-2"]}>
        <AssistantPage />
      </MemoryRouter>,
    );

    await waitFor(() => expect(getAssistantSession).toHaveBeenCalledWith("session-2"));
    expect(screen.getByText("来自 URL 的 cron 会话")).toBeInTheDocument();
  });

  it("switches to the returned session when /new lifecycle command is sent", async () => {
    vi.mocked(sendAssistantMessage).mockResolvedValue({
      session: newSession,
      messages: [],
      trace_id: null,
      lifecycle_command: {
        command: "new",
        previous_session_id: "session-1",
        new_session_id: "session-2",
      },
    });
    vi.mocked(listAssistantSessions)
      .mockResolvedValueOnce({ items: [session], total: 1, limit: 50, offset: 0 })
      .mockResolvedValueOnce({ items: [newSession, session], total: 2, limit: 50, offset: 0 });
    vi.mocked(listAssistantMessages).mockImplementation(async (sessionId: string) =>
      sessionId === "session-2" ? [] : [assistantMessage],
    );
    vi.mocked(listAssistantEvents).mockResolvedValue([]);

    render(<AssistantPage />);
    await waitFor(() => expect(screen.getByText("最终回答")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText(messageInputPlaceholder), { target: { value: "/new" } });
    fireEvent.click(screen.getByRole("button", { name: /发送/i }));

    await waitFor(() => expect(sendAssistantMessage).toHaveBeenCalledWith("session-1", "/new"));
    await waitFor(() =>
      expect(screen.getByText("试试这些示例：")).toBeInTheDocument(),
    );
    expect(screen.getByText("session-2")).toBeInTheDocument();
  });

  it("allows stopping while an agent response is in progress", async () => {
    vi.mocked(sendAssistantMessage).mockReturnValue(new Promise(() => {}));

    render(<AssistantPage />);
    await waitFor(() => expect(screen.getByText("最终回答")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText(messageInputPlaceholder), { target: { value: "生成策略" } });
    fireEvent.click(screen.getByRole("button", { name: /发送/i }));

    const stopButton = await screen.findByRole("button", { name: /停止/i });
    expect(stopButton).not.toBeDisabled();

    fireEvent.click(stopButton);

    await waitFor(() => expect(stopAssistantSession).toHaveBeenCalledWith("session-1"));
  });

  it("refreshes sessions after a user stop so generated titles can be picked up", async () => {
    vi.mocked(sendAssistantMessage).mockRejectedValue(new Error("Assistant stopped by user"));
    vi.mocked(listAssistantSessions)
      .mockResolvedValueOnce({ items: [session], total: 1, limit: 50, offset: 0 })
      .mockResolvedValueOnce({
        items: [{ ...session, title: "盘中信号复盘" }],
        total: 1,
        limit: 50,
        offset: 0,
      });

    render(<AssistantPage />);
    await waitFor(() => expect(screen.getByText("最终回答")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText(messageInputPlaceholder), { target: { value: "帮我复盘今天盘中的量化信号" } });
    fireEvent.click(screen.getByRole("button", { name: /发送/i }));

    await waitFor(() => expect(listAssistantSessions.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(listAssistantSessions.mock.calls.at(-1)?.[0]).toEqual({ limit: 50 });
  });

  it("renders the optimistic user message before the streaming assistant response", async () => {
    vi.mocked(sendAssistantMessage).mockReturnValue(new Promise(() => {}));

    render(<AssistantPage />);
    await waitFor(() => expect(screen.getByText("最终回答")).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText(messageInputPlaceholder), { target: { value: "第二轮问题" } });
    fireEvent.click(screen.getByRole("button", { name: /发送/i }));

    await waitFor(() => expect(screen.getByText("第二轮问题")).toBeInTheDocument());
    const previousAnswer = screen.getByText("最终回答");
    const optimisticUser = screen.getByText("第二轮问题");
    expect(previousAnswer.compareDocumentPosition(optimisticUser)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  it("anchors a new user message at the top of the viewport on submit even if the user had scrolled away", async () => {
    const scrollIntoView = vi.spyOn(window.HTMLElement.prototype, "scrollIntoView");
    vi.mocked(sendAssistantMessage).mockReturnValue(new Promise(() => {}));

    render(<AssistantPage />);
    await waitFor(() => expect(screen.getByText("最终回答")).toBeInTheDocument());
    scrollIntoView.mockClear();

    const conversation = screen.getByTestId("assistant-conversation-scroll");
    Object.defineProperty(conversation, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(conversation, "clientHeight", { configurable: true, value: 400 });
    Object.defineProperty(conversation, "scrollTop", { configurable: true, value: 120 });
    fireEvent.scroll(conversation);

    fireEvent.change(screen.getByPlaceholderText(messageInputPlaceholder), { target: { value: "继续输出" } });
    fireEvent.click(screen.getByRole("button", { name: /发送/i }));

    await waitFor(() => expect(screen.getByText("继续输出")).toBeInTheDocument());
    // The submitted user message scrolls into view at the TOP of the conversation
    // so the user can read what they just asked while the agent streams below.
    await waitFor(() => {
      const startCalls = scrollIntoView.mock.calls.filter(
        ([opts]) => opts && (opts as ScrollIntoViewOptions).block === "start",
      );
      expect(startCalls.length).toBeGreaterThan(0);
    });
    // And the existing "回到底部" affordance is still available because we're
    // no longer pinned to the bottom.
    expect(screen.getByRole("button", { name: "回到底部" })).toBeInTheDocument();
    // The aggressive bottom-anchor should NOT fire while we're pinning the
    // question at the top.
    const endCalls = scrollIntoView.mock.calls.filter(
      ([opts]) => opts && (opts as ScrollIntoViewOptions).block === "end",
    );
    expect(endCalls.length).toBe(0);
  });

  it("does not re-anchor the pending user message to the top after the user scrolls back to the bottom", async () => {
    const scrollIntoView = vi.spyOn(window.HTMLElement.prototype, "scrollIntoView");
    vi.mocked(sendAssistantMessage).mockReturnValue(new Promise(() => {}));

    render(<AssistantPage />);
    await waitFor(() => expect(screen.getByText("最终回答")).toBeInTheDocument());
    scrollIntoView.mockClear();

    const conversation = screen.getByTestId("assistant-conversation-scroll");
    Object.defineProperty(conversation, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(conversation, "clientHeight", { configurable: true, value: 400 });
    Object.defineProperty(conversation, "scrollTop", { configurable: true, value: 120 });
    fireEvent.scroll(conversation);

    fireEvent.change(screen.getByPlaceholderText(messageInputPlaceholder), { target: { value: "继续输出" } });
    fireEvent.click(screen.getByRole("button", { name: /发送/i }));

    await waitFor(() => expect(screen.getByText("继续输出")).toBeInTheDocument());
    await waitFor(() => {
      const startCalls = scrollIntoView.mock.calls.filter(
        ([opts]) => opts && (opts as ScrollIntoViewOptions).block === "start",
      );
      expect(startCalls.length).toBeGreaterThan(0);
    });

    const startCallsBefore = scrollIntoView.mock.calls.filter(
      ([opts]) => opts && (opts as ScrollIntoViewOptions).block === "start",
    ).length;

    // Simulate the user manually scrolling all the way to the bottom while the
    // agent is still streaming. This releases the top-anchor pin — but it
    // MUST NOT re-trigger the top-anchor scroll for the same pending message.
    Object.defineProperty(conversation, "scrollTop", { configurable: true, value: 600 });
    fireEvent.scroll(conversation);

    await Promise.resolve();
    await Promise.resolve();

    const startCallsAfter = scrollIntoView.mock.calls.filter(
      ([opts]) => opts && (opts as ScrollIntoViewOptions).block === "start",
    ).length;
    expect(startCallsAfter).toBe(startCallsBefore);
  });

  it("scrolls to the bottom (not back to the top) when 回到底部 is clicked during streaming", async () => {
    const scrollIntoView = vi.spyOn(window.HTMLElement.prototype, "scrollIntoView");
    vi.mocked(sendAssistantMessage).mockReturnValue(new Promise(() => {}));

    render(<AssistantPage />);
    await waitFor(() => expect(screen.getByText("最终回答")).toBeInTheDocument());

    const conversation = screen.getByTestId("assistant-conversation-scroll");
    Object.defineProperty(conversation, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(conversation, "clientHeight", { configurable: true, value: 400 });
    Object.defineProperty(conversation, "scrollTop", { configurable: true, value: 120 });
    fireEvent.scroll(conversation);

    fireEvent.change(screen.getByPlaceholderText(messageInputPlaceholder), { target: { value: "继续输出" } });
    fireEvent.click(screen.getByRole("button", { name: /发送/i }));

    await waitFor(() => expect(screen.getByText("继续输出")).toBeInTheDocument());
    await waitFor(() => {
      const startCalls = scrollIntoView.mock.calls.filter(
        ([opts]) => opts && (opts as ScrollIntoViewOptions).block === "start",
      );
      expect(startCalls.length).toBeGreaterThan(0);
    });
    scrollIntoView.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "回到底部" }));

    await Promise.resolve();
    await Promise.resolve();

    const startCalls = scrollIntoView.mock.calls.filter(
      ([opts]) => opts && (opts as ScrollIntoViewOptions).block === "start",
    );
    const endCalls = scrollIntoView.mock.calls.filter(
      ([opts]) => opts && (opts as ScrollIntoViewOptions).block === "end",
    );
    expect(endCalls.length).toBeGreaterThan(0);
    expect(startCalls.length).toBe(0);
  });

  it("scrolls to the bottom when the return-to-bottom button is clicked", async () => {
    const scrollIntoView = vi.spyOn(window.HTMLElement.prototype, "scrollIntoView");

    render(<AssistantPage />);
    await waitFor(() => expect(screen.getByText("最终回答")).toBeInTheDocument());
    scrollIntoView.mockClear();

    const conversation = screen.getByTestId("assistant-conversation-scroll");
    Object.defineProperty(conversation, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(conversation, "clientHeight", { configurable: true, value: 400 });
    Object.defineProperty(conversation, "scrollTop", { configurable: true, value: 120 });
    fireEvent.scroll(conversation);

    fireEvent.click(screen.getByRole("button", { name: "回到底部" }));

    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "end" });
  });

  it("shows failed tool cards for historical tool.result previews with status error", async () => {
    vi.mocked(listAssistantMessages).mockResolvedValue([
      {
        ...assistantMessage,
        linked_attempt_id: "attempt-1",
      },
    ]);
    vi.mocked(listAssistantEvents).mockResolvedValue([
      {
        event_id: "evt-1",
        session_id: "session-1",
        event_type: "tool.call",
        payload: {
          attempt_id: "attempt-1",
          tool_call_id: "call-1",
          tool: "bind_strategy_instance_to_task",
          arguments: {},
        },
        created_at: "2026-04-29T00:00:01Z",
      },
      {
        event_id: "evt-2",
        session_id: "session-1",
        event_type: "tool.result",
        payload: {
          attempt_id: "attempt-1",
          tool_call_id: "call-1",
          preview: '{"status":"error","error":"strategy instance not found: "}',
        },
        created_at: "2026-04-29T00:00:02Z",
      },
    ]);

    render(<AssistantPage />);

    // 简洁模式（默认）下失败也必须在折叠态可见：过程卡片头部带失败角标。
    await waitFor(() =>
      expect(screen.getByTestId("process-card-error-tag")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("process-card-error-tag").textContent).toBe("1 个工具失败");

    // 展开后能看到具体失败的工具卡。
    fireEvent.click(screen.getByTestId("process-card-header"));
    await waitFor(() =>
      expect(screen.getByText("bind_strategy_instance_to_task")).toBeInTheDocument(),
    );
    expect(screen.getAllByText("失败")[0]).toBeInTheDocument();
  });

  it("renders a failure banner for a run that failed mid-stream", async () => {
    vi.mocked(listAssistantMessages).mockResolvedValue([
      {
        ...assistantMessage,
        message_id: "message-failed",
        content: "部分已生成的内容",
        metadata: {
          failed: true,
          partial: true,
          error: "read timed out",
          error_type: "ReadTimeout",
          content_blocks: [{ type: "text", content: "部分已生成的内容" }],
        },
      },
    ]);

    render(<AssistantPage />);

    await waitFor(() =>
      expect(screen.getByText("本轮运行失败：ReadTimeout")).toBeInTheDocument(),
    );
    // The streamed partial content is still shown alongside the failure banner.
    expect(screen.getByText("部分已生成的内容")).toBeInTheDocument();
  });

  it("renders source channel labels for channel sessions only", async () => {
    vi.mocked(listAssistantSessions).mockResolvedValue({
      items: [channelSession, session],
      total: 2,
      limit: 50,
      offset: 0,
    });
    vi.mocked(listAssistantMessages).mockResolvedValue([]);

    render(<AssistantPage />);

    await waitFor(() => expect(screen.getByText("当前会话")).toBeInTheDocument());

    const sessionSelect = screen.getAllByRole("combobox")[1];
    fireEvent.mouseDown(sessionSelect);

    expect(await screen.findAllByText(/来自 channel:/)).toHaveLength(2);
    expect(screen.getAllByText(/feishu \/ Feishu Alpha \/ channel-feishu-a/)).toHaveLength(2);
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(screen.getByText("DoYouTrade Agent")).toBeInTheDocument();
  });

  it("renders session created_at and reloads list when channel filter changes", async () => {
    vi.mocked(listAssistantChannels).mockResolvedValue({
      items: [
        {
          id: "channel-feishu-a",
          name: "Feishu Alpha",
          type: "feishu",
          enabled: true,
          agent_id: "agent-1",
          status: "connected",
          last_error: "",
          last_connected_at: null,
          config: {},
          secret_keys: [],
          created_at: "2026-04-29T00:00:00Z",
          updated_at: "2026-04-29T00:00:00Z",
        },
      ],
      total: 1,
    });
    vi.mocked(listAssistantSessions).mockResolvedValue({
      items: [channelSession, session],
      total: 2,
      limit: 50,
      offset: 0,
    });
    vi.mocked(listAssistantMessages).mockResolvedValue([]);

    render(<AssistantPage />);

    await waitFor(() => expect(screen.getAllByText(/创建于 /).length).toBeGreaterThan(0));

    const filterSelect = screen.getAllByRole("combobox")[0];
    fireEvent.mouseDown(filterSelect);
    fireEvent.click(await screen.findByText("Web 会话"));

    await waitFor(() =>
      expect(listAssistantSessions).toHaveBeenCalledWith(
        expect.objectContaining({ source: "web", limit: 50 }),
      ),
    );
  });
});
