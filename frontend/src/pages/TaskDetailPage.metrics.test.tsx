import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { TaskDetailPage } from "./TaskDetailPage";
import type { CycleRunRow, TaskStatus } from "../types";
import { getTask, listCycleRuns, listTaskRuns } from "../api";
import { useConsoleOutlet } from "../consoleOutletContext";

vi.mock("../api", () => ({
  deleteTask: vi.fn(),
  getTask: vi.fn(),
  getTaskRun: vi.fn(),
  listCycleRuns: vi.fn(),
  listTaskRuns: vi.fn(),
  pauseTask: vi.fn(),
  pauseTaskRun: vi.fn(),
  resumeTaskRun: vi.fn(),
  startTask: vi.fn(),
  stopTask: vi.fn(),
  stopTaskRun: vi.fn(),
}));

vi.mock("../consoleOutletContext", () => ({
  useConsoleOutlet: vi.fn(),
}));

vi.mock("../components/TaskDebugPanel", () => ({
  TaskDebugPanel: () => <div>debug panel</div>,
}));
vi.mock("../components/TaskCycleRunsPanel", () => ({
  TaskCycleRunsPanel: () => <div>cycle runs panel</div>,
}));
vi.mock("../components/TaskTriggersPanel", () => ({
  TaskTriggersPanel: () => <div>triggers panel</div>,
}));
vi.mock("../components/TaskReviewPanel", () => ({
  TaskReviewPanel: () => <div>review panel</div>,
}));
vi.mock("../components/KnowledgeJournalsPanel", () => ({
  KnowledgeJournalsPanel: () => <div>journals panel</div>,
}));
vi.mock("../components/CreateAgentCard", async () => {
  const { forwardRef } = await import("react");
  return { CreateAgentCard: forwardRef(() => <div>create agent card</div>) };
});

const baseTask: TaskStatus = {
  task_id: "task-1",
  name: "动量轮动",
  mode: "paper",
  description: "",
  status: "running",
  cycles: 3,
  last_error: "",
  data_provider: null,
  data_provider_effective: "qmt",
  universe: ["600519.SH"],
  strategy_name: "SuperTrend 跟随",
  settings: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-06T00:00:00Z",
};

function snapshotRow(runId: string, cycleTime: string, equity: string): CycleRunRow {
  return {
    run_id: runId,
    cycle_time: cycleTime,
    details: {
      post_cycle_account: {
        source: "ledger",
        captured_at: cycleTime,
        account: { cash: "0", equity },
        total_market_value: "0",
        positions: [],
      },
    },
  } as unknown as CycleRunRow;
}

function renderPage(task: TaskStatus) {
  vi.mocked(useConsoleOutlet).mockReturnValue({
    approvals: [],
    instances: [task],
    health: "ok",
    systemState: { kill_switch_enabled: false, task_count: 1, running_count: 1 },
    loading: false,
    dataRefreshFailed: false,
    refresh: vi.fn().mockResolvedValue(undefined),
    setSystemState: vi.fn(),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any);

  return render(
    <MemoryRouter initialEntries={[`/tasks/${task.task_id}?tab=debug`]}>
      <Routes>
        <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("TaskDetailPage account metric tiles", () => {
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

  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getTask).mockImplementation(async (taskId: string) => ({ ...baseTask, task_id: taskId }));
    vi.mocked(listTaskRuns).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(listCycleRuns).mockResolvedValue({
      items: [
        snapshotRow("run-a", "2026-01-01T00:00:00Z", "100000"),
        snapshotRow("run-b", "2026-01-06T00:00:00Z", "110000"),
      ],
      total: 2,
    });
  });

  it("derives 起始/当前权益 and signed 总盈亏 from the cycle-run snapshots", async () => {
    renderPage(baseTask);

    expect(await screen.findByText("起始权益")).toBeTruthy();
    expect(screen.getByText("当前权益")).toBeTruthy();
    expect(screen.getByText("总盈亏")).toBeTruthy();

    expect(await screen.findByText("100,000.00")).toBeTruthy();
    expect(screen.getByText("110,000.00")).toBeTruthy();
    expect(screen.getByText("+10,000.00")).toBeTruthy();
    expect(screen.getByText("(+10.00%)")).toBeTruthy();
  });

  it("shows the running status pill and pill-style metadata", async () => {
    renderPage(baseTask);

    // Status pill next to the title.
    expect(await screen.findByText("运行中")).toBeTruthy();
    // Mode + strategy + symbol + cycles meta chips.
    expect(screen.getByText("模拟盘")).toBeTruthy();
    expect(screen.getByText("SuperTrend 跟随")).toBeTruthy();
    expect(screen.getByText("600519.SH")).toBeTruthy();
    expect(screen.getByText("轮次 3")).toBeTruthy();
  });

  it("renders '—' placeholders when no cycle run has an account snapshot", async () => {
    vi.mocked(listCycleRuns).mockResolvedValue({ items: [], total: 0 });
    renderPage(baseTask);

    expect(await screen.findByText("起始权益")).toBeTruthy();
    // All three values collapse to the em-dash placeholder.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });
});
