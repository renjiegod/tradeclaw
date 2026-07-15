import { render, screen, waitFor } from "@testing-library/react";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { forwardRef, useImperativeHandle } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { TaskDetailPage } from "./TaskDetailPage";
import type { TaskStatus } from "../types";
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

vi.mock("../components/TaskCycleRunsPanel", () => ({
  TaskCycleRunsPanel: () => <div>cycle runs panel</div>,
}));

vi.mock("../components/TaskDebugPanel", () => ({
  TaskDebugPanel: () => <div>debug panel</div>,
}));

vi.mock("../components/BacktestRunChartPanel", () => ({
  BacktestRunChartPanel: () => <div>backtest chart panel</div>,
}));

vi.mock("../components/CreateAgentCard", () => ({
  CreateAgentCard: forwardRef((_props, ref) => {
    useImperativeHandle(
      ref,
      () => ({
        openSettingsJsonModal: () => undefined,
      }),
      []
    );
    return <button type="button">保存</button>;
  }),
}));

const baseTask: TaskStatus = {
  task_id: "task-1",
  name: "Task 1",
  mode: "paper",
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

function renderPage(task: TaskStatus, tab: "config" | "cycle_runs" | "debug" = "config") {
  vi.mocked(useConsoleOutlet).mockReturnValue({
    approvals: [],
    instances: [task],
    health: "ok",
    systemState: { kill_switch_enabled: false, task_count: 1, running_count: 0 },
    loading: false,
    dataRefreshFailed: false,
    refresh: vi.fn().mockResolvedValue(undefined),
    setSystemState: vi.fn(),
  });

  return render(
    <MemoryRouter initialEntries={[`/tasks/${task.task_id}?tab=${tab}`]}>
      <Routes>
        <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
      </Routes>
    </MemoryRouter>
  );
}

describe("TaskDetailPage backtest config tab", () => {
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
    vi.mocked(listTaskRuns).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(listCycleRuns).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(getTask).mockImplementation(async (taskId: string) => ({
      ...baseTask,
      task_id: taskId,
      mode: "backtest",
    }));
  });

  it("shows empty read-only text for backtest config with no selected run", async () => {
    renderPage({ ...baseTask, mode: "backtest" }, "config");
    expect(await screen.findByText("暂无运行配置，先发起一次回测")).toBeInTheDocument();
  });

  it("does not show raw json edit button in backtest config tab", () => {
    renderPage({ ...baseTask, mode: "backtest" }, "config");
    expect(screen.queryByText("编辑原始 JSON…")).not.toBeInTheDocument();
  });

  it("shows chart tab for backtest tasks", async () => {
    renderPage({ ...baseTask, mode: "backtest" }, "cycle_runs");
    await waitFor(() => {
      expect(screen.getAllByText("回测图表").length).toBeGreaterThan(0);
    });
  });

  it("keeps edit UI for non-backtest config tab", async () => {
    renderPage({ ...baseTask, mode: "paper" }, "config");
    expect(await screen.findByRole("button", { name: "保存" })).toBeInTheDocument();
  });
});
