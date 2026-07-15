import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TaskCycleRunsPanel } from "./TaskCycleRunsPanel";
import type { Agent, CycleRunDebugView, PushDetail, TaskStatus } from "../types";
import { getCycleRunDebugView, listCycleRuns } from "../api";

vi.mock("../api", () => ({
  listCycleRuns: vi.fn(),
  getCycleRunDebugView: vi.fn(),
}));

const navigateMock = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return {
    ...actual,
    useNavigate: () => navigateMock,
  };
});

const taskStub: TaskStatus = {
  task_id: "task-1",
  name: "Task 1",
  mode: "backtest",
  description: "",
  status: "configured",
  cycles: 0,
  last_error: "",
  data_provider: null,
  data_provider_effective: "none",
  universe: [],
  settings: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

/** Render the panel inside a router so CycleRunDetailBody's useNavigate works. */
function renderPanel(props: Parameters<typeof TaskCycleRunsPanel>[0]) {
  return render(
    <MemoryRouter>
      <TaskCycleRunsPanel {...props} />
    </MemoryRouter>,
  );
}

const ZERO_TIMELINE_SUMMARY = {
  total_cycles: 0,
  total_signals_buy: 0,
  total_signals_sell: 0,
  total_signals_hold: 0,
  total_signals_target_exposure: 0,
  total_signals_target_quantity: 0,
  top_hold_tags: {},
  top_buy_tags: {},
  top_sell_tags: {},
  top_target_exposure_tags: {},
  top_target_quantity_tags: {},
  first_cycle_time: null,
  last_cycle_time: null,
  first_buy_cycle_time: null,
  first_sell_cycle_time: null,
  first_target_exposure_cycle_time: null,
  first_target_quantity_cycle_time: null,
  zero_trade: true,
} as const;

function makeAgent(overrides: Partial<Agent> = {}): Agent {
  return {
    id: "agent-1",
    name: "Composer Agent",
    status: "active",
    system_prompt: "",
    model_route_name: "route-x",
    tool_names: ["t1", "t2"],
    skill_names: ["s1"],
    max_turns: 8,
    context_compaction: { enabled: false } as Agent["context_compaction"],
    is_default: false,
    is_builtin: false,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function makePushDetail(overrides: Partial<PushDetail> = {}): PushDetail {
  return {
    resolved_from_kind: "manual",
    strategy: { name: "动量策略", task_id: "task-1", reason: null },
    composer_agent: {
      agent: makeAgent(),
      agent_id: "agent-1",
      compose_mode: "card",
      reason: null,
    },
    assistant_session: {
      session: {
        session_id: "asst-sess-1",
        title: "推送会话",
        status: "completed",
        agent_id: "agent-1",
      },
      reason: null,
    },
    pushed_messages: {
      items: [
        {
          message_id: "msg-1",
          session_id: "asst-sess-1",
          role: "assistant",
          content: "## 推送标题\n买入 600519",
          created_at: "2026-01-01T01:00:00Z",
          source: "cron",
          channel_target: "feishu:chat-1",
          delivery_status: "delivered",
          run_id: "run-001",
          cron_job_run_id: null,
        },
      ],
      reason: null,
    },
    approvals: {
      items: [
        {
          approval_id: "apr-1",
          intent_id: "intent-1",
          status: "approved",
          symbol: "600519",
          symbol_name: "贵州茅台",
          action: "buy",
          notional: "178500.00",
          decision_source: "web",
          resolver_id: "user-7",
          decided_at: "2026-01-01T01:05:00Z",
          matched_fill: {
            quantity: "100",
            price: "1785.00",
            amount: "178500.00",
            filled_at: "2026-01-01T01:06:00Z",
          },
        },
      ],
      total: 1,
      reason: null,
    },
    ...overrides,
  };
}

function makeRow(details: Record<string, unknown> | null) {
  return {
    run_id: "run-001",
    task_id: taskStub.task_id,
    agent_name: "agent",
    session_id: null,
    trace_id: null,
    run_mode: "backtest",
    run_kind: "manual",
    clock_mode: "simulated",
    cycle_time: "2026-01-01T00:00:00Z",
    cycle_time_utc: "2026-01-01T00:00:00Z",
    wall_started_at: "2026-01-01T00:00:00Z",
    wall_finished_at: null,
    runtime_params: null,
    status: "completed",
    details,
    cycle_failed: false,
    failure_message: null,
    completed_phases: [],
    submitted_count: null,
    vetoed_count: null,
    pending_approval_count: null,
    code_version: null,
    code_hash: null,
  };
}

function makeDebugView(
  row: ReturnType<typeof makeRow>,
  push_detail?: PushDetail,
): CycleRunDebugView {
  return {
    cycle_run: row as CycleRunDebugView["cycle_run"],
    session: null,
    spans: [],
    model_invocations: [],
    signal_timeline: [],
    signal_timeline_summary: { ...ZERO_TIMELINE_SUMMARY },
    push_detail,
  };
}

/** Click the table row for run-001. The run_id also appears in the modal title
 * once open, so scope the lookup to the table body row (a <tr>). */
async function clickRunRow() {
  const cells = await screen.findAllByText("run-001");
  const tableRow = cells.map((el) => el.closest("tr")).find((tr): tr is HTMLTableRowElement => tr != null);
  fireEvent.click(tableRow as HTMLTableRowElement);
}

/** Open the cycle detail modal for a single-row table and wait for the body. */
async function openDetail(view: CycleRunDebugView) {
  vi.mocked(listCycleRuns).mockResolvedValue({ items: [view.cycle_run], total: 1 });
  vi.mocked(getCycleRunDebugView).mockResolvedValue(view);
  renderPanel({ task: taskStub, refreshTrigger: 0 });
  await clickRunRow();
  // 周期摘要 header confirms the detail body mounted.
  await screen.findByText("周期摘要");
}

describe("TaskCycleRunsPanel selection contract", () => {
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
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  // Vitest auto-cleanup is off without `globals: true`, so unmount between tests
  // to stop a prior render's table/modal from polluting the next test's queries.
  afterEach(() => {
    cleanup();
  });

  it("invokes onBacktestRunSelected with run_id when a row is clicked", async () => {
    const view = makeDebugView(makeRow(null));
    vi.mocked(listCycleRuns).mockResolvedValue({ items: [view.cycle_run], total: 1 });
    vi.mocked(getCycleRunDebugView).mockResolvedValue(view);
    const onBacktestRunSelected = vi.fn();

    renderPanel({ task: taskStub, refreshTrigger: 0, onBacktestRunSelected });

    const runIdCell = await screen.findByText("run-001");
    expect(onBacktestRunSelected).not.toHaveBeenCalled();

    const rowElement = runIdCell.closest("tr");
    expect(rowElement).not.toBeNull();
    fireEvent.click(rowElement as HTMLTableRowElement);

    await waitFor(() => expect(onBacktestRunSelected).toHaveBeenCalledTimes(1));
    expect(onBacktestRunSelected).toHaveBeenLastCalledWith("run-001");
  });

  it("renders trade operations lines from execution fields", async () => {
    const row = makeRow({
      decisions: [{ symbol: "600519", action: "buy" }],
      decision_execution: [{ quantity_shares: 100, total_notional: "178500" }],
    });
    vi.mocked(listCycleRuns).mockResolvedValue({ items: [row], total: 1 });
    vi.mocked(getCycleRunDebugView).mockResolvedValue(makeDebugView(row));

    renderPanel({ task: taskStub, refreshTrigger: 0 });

    expect(await screen.findByText("买 600519 100股 ¥178,500.00")).toBeInTheDocument();
  });

  it("renders dash when execution payload is missing", async () => {
    const row = makeRow({
      decisions: [{ symbol: "600519", action: "buy" }],
      decision_execution: [],
    });
    vi.mocked(listCycleRuns).mockResolvedValue({ items: [row], total: 1 });
    vi.mocked(getCycleRunDebugView).mockResolvedValue(makeDebugView(row));

    renderPanel({ task: taskStub, refreshTrigger: 0 });

    expect((await screen.findAllByRole("columnheader", { name: "交易操作" })).length).toBeGreaterThan(0);
    expect(await screen.findAllByText("—")).not.toHaveLength(0);
  });

  it("renders pushed cards with markdown body and delivery tag", async () => {
    await openDetail(makeDebugView(makeRow(null), makePushDetail()));

    // Markdown heading rendered from message content.
    expect(await screen.findByText("推送标题")).toBeInTheDocument();
    // Delivery status tag.
    expect(screen.getByText("delivered")).toBeInTheDocument();
    // Channel target tag.
    expect(screen.getByText("feishu:chat-1")).toBeInTheDocument();
  });

  it("shows pushed_messages reason when no cards were pushed", async () => {
    const pd = makePushDetail({
      pushed_messages: { items: [], reason: "本周期未生成卡片（信号为空）。" },
    });
    await openDetail(makeDebugView(makeRow(null), pd));

    expect(await screen.findByText("未推送卡片")).toBeInTheDocument();
    expect(screen.getByText("本周期未生成卡片（信号为空）。")).toBeInTheDocument();
  });

  it("renders approval 成交 receipt with status and resolver", async () => {
    await openDetail(makeDebugView(makeRow(null), makePushDetail()));

    await screen.findByText("审批与结果回执");
    // Receipt tag shows fill quantity @ price (decimal string, not parsed).
    expect(screen.getByText(/成交 100股 @ 1785\.00/)).toBeInTheDocument();
    // Status mapped to label.
    expect(screen.getByText("已同意")).toBeInTheDocument();
    // Resolver id surfaced.
    expect(screen.getByText("user-7")).toBeInTheDocument();
  });

  it("renders approval failure receipt when dispatch errored", async () => {
    const pd = makePushDetail({
      approvals: {
        items: [
          {
            approval_id: "apr-2",
            intent_id: "intent-2",
            status: "approved",
            symbol: "000001",
            action: "sell",
            notional: "5000.00",
            decision_source: "api",
            resolver_id: "user-9",
            decided_at: "2026-01-01T02:00:00Z",
            dispatch_error: "broker rejected: insufficient position",
            dispatch_attempts: 2,
            matched_fill: null,
          },
        ],
        total: 1,
        reason: null,
      },
    });
    await openDetail(makeDebugView(makeRow(null), pd));

    await screen.findByText("审批与结果回执");
    expect(screen.getByText("失败")).toBeInTheDocument();
  });

  it("renders composer agent with name, model, and 固定主智能体 tag when builtin", async () => {
    const pd = makePushDetail({
      composer_agent: {
        agent: makeAgent({ name: "主智能体", is_builtin: true, model_route_name: "gpt-route" }),
        agent_id: "agent-1",
        compose_mode: "prose",
        reason: null,
      },
    });
    await openDetail(makeDebugView(makeRow(null), pd));

    await screen.findByText("推送/编排 Agent");
    expect(screen.getByText("主智能体")).toBeInTheDocument();
    expect(screen.getByText("gpt-route")).toBeInTheDocument();
    expect(screen.getByText("固定主智能体")).toBeInTheDocument();
  });

  it("shows composer_agent reason when no agent is present", async () => {
    const pd = makePushDetail({
      composer_agent: { agent: null, agent_id: null, compose_mode: null, reason: "本周期无编排 Agent（确定性推送）。" },
    });
    await openDetail(makeDebugView(makeRow(null), pd));

    expect(await screen.findByText("无编排/推送 Agent")).toBeInTheDocument();
    expect(screen.getByText("本周期无编排 Agent（确定性推送）。")).toBeInTheDocument();
  });

  it("renders assistant session and navigates on 打开会话", async () => {
    await openDetail(makeDebugView(makeRow(null), makePushDetail()));

    await screen.findByText("落地的助手会话");
    expect(screen.getByText("推送会话")).toBeInTheDocument();

    const openBtn = screen.getByRole("button", { name: "打开会话" });
    fireEvent.click(openBtn);
    expect(navigateMock).toHaveBeenCalledWith("/assistant?session_id=asst-sess-1");
  });

  it("shows assistant_session reason when none landed", async () => {
    const pd = makePushDetail({
      assistant_session: { session: null, reason: "卡片未落地到助手会话（仅外发飞书）。" },
    });
    await openDetail(makeDebugView(makeRow(null), pd));

    expect(await screen.findByText("未落地助手会话")).toBeInTheDocument();
    expect(screen.getByText("卡片未落地到助手会话（仅外发飞书）。")).toBeInTheDocument();
  });

  it("shows the strategy/task name in the cycle summary", async () => {
    const row = { ...makeRow(null), agent_name: "动量策略" };
    const view = makeDebugView(row, makePushDetail());
    vi.mocked(listCycleRuns).mockResolvedValue({ items: [row], total: 1 });
    vi.mocked(getCycleRunDebugView).mockResolvedValue(view);
    renderPanel({ task: taskStub, refreshTrigger: 0 });
    await clickRunRow();

    await screen.findByText("策略/任务");
    // The 策略/任务 summary row surfaces the cycle's agent_name.
    expect(screen.getByText("动量策略")).toBeInTheDocument();
  });

  it("falls back to strategy reason when agent_name is empty", async () => {
    const row = { ...makeRow(null), agent_name: "" };
    const pd = makePushDetail({
      strategy: { name: null, task_id: null, reason: "策略未解析（任务已删除）。" },
    });
    const view = makeDebugView(row, pd);
    vi.mocked(listCycleRuns).mockResolvedValue({ items: [row], total: 1 });
    vi.mocked(getCycleRunDebugView).mockResolvedValue(view);
    renderPanel({ task: taskStub, refreshTrigger: 0 });
    await clickRunRow();

    await screen.findByText("策略/任务");
    expect(screen.getByText("策略未解析（任务已删除）。")).toBeInTheDocument();
  });
});
