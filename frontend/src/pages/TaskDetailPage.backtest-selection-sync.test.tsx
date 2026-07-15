import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { TaskDetailPage } from "./TaskDetailPage";
import type { TaskStatus } from "../types";
import { getTask, getTaskRun, listCycleRuns, listTaskRuns, pauseTaskRun } from "../api";
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

vi.mock("../components/CreateAgentCard", async () => {
  const { forwardRef } = await import("react");
  return {
    CreateAgentCard: forwardRef(() => <div>create agent card</div>),
  };
});

vi.mock("../components/BacktestRunConfigPanel", () => ({
  BacktestRunConfigPanel: ({ selectedRunId }: { selectedRunId: string | null }) => (
    <div data-testid="selected-backtest-run-id">{selectedRunId ?? "null"}</div>
  ),
}));

let emitBacktestSelection: ((runId: string | null) => void) | null = null;
vi.mock("../components/TaskCycleRunsPanel", () => ({
  TaskCycleRunsPanel: ({
    onBacktestRunSelected,
  }: {
    onBacktestRunSelected?: (runId: string | null) => void;
  }) => {
    emitBacktestSelection = onBacktestRunSelected ?? null;
    return <div>cycle runs panel</div>;
  },
}));

const baseTask: TaskStatus = {
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

describe("TaskDetailPage backtest selection sync", () => {
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
    emitBacktestSelection = null;
    vi.mocked(listCycleRuns).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(getTask).mockImplementation(async (taskId: string) => ({
      ...baseTask,
      task_id: taskId,
    }));
  });

  it("auto-selects newest run, keeps valid manual selection, keeps older selected run via getTaskRun, then falls back when missing, and resets on non-backtest mode", async () => {
    const outletState: {
      instances: TaskStatus[];
    } = {
      instances: [{ ...baseTask }],
    };
    const refresh = vi.fn().mockResolvedValue(undefined);
    vi.mocked(useConsoleOutlet).mockImplementation(() => ({
      approvals: [],
      instances: outletState.instances,
      health: "ok",
      systemState: { kill_switch_enabled: false, task_count: 1, running_count: 0 },
      loading: false,
      dataRefreshFailed: false,
      refresh,
      setSystemState: vi.fn(),
    }));

    const runLists = [
      [{ run_id: "run-newest", status: "running" }, { run_id: "run-older", status: "completed" }],
      [{ run_id: "run-newer-2", status: "running" }, { run_id: "run-older", status: "completed" }],
      [{ run_id: "run-newer-3", status: "running" }, { run_id: "run-newer-2", status: "completed" }],
      [{ run_id: "run-newer-4", status: "running" }, { run_id: "run-newer-3", status: "completed" }],
    ];
    let runListIndex = 0;
    vi.mocked(pauseTaskRun).mockResolvedValue(undefined);
    vi.mocked(getTaskRun).mockImplementation(async (_taskId, runId) => {
      if (runListIndex === 2 && runId === "run-older") {
        return {
          run_id: "run-older",
          session_id: null,
          status: "completed",
          started_at: "2026-01-01T00:00:00Z",
          completed_at: "2026-01-01T00:10:00Z",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:10:00Z",
          task_id: "task-1",
          run_mode: "backtest",
          runtime_params: null,
          details: null,
          clock_mode: "wall",
          cycle_time: null,
          cycle_time_utc: null,
          wall_started_at: "2026-01-01T00:00:00Z",
          wall_finished_at: "2026-01-01T00:10:00Z",
          run_kind: "manual",
          trace_id: null,
          cycle_failed: false,
          failure_message: null,
          completed_phases: null,
        };
      }
      throw new Error("HTTP 404");
    });
    vi.mocked(listTaskRuns).mockImplementation(async () => {
      const current = runLists[Math.min(runListIndex, runLists.length - 1)] as Array<{
        run_id: string;
        status: string;
      }>;
      return {
        items: current.map((item) => ({
          run_id: item.run_id,
          session_id: null,
          status: item.status,
          started_at: "2026-01-01T00:00:00Z",
          completed_at: "2026-01-01T00:10:00Z",
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:10:00Z",
          task_id: "task-1",
          run_mode: "backtest",
          runtime_params: null,
          details: null,
          clock_mode: "wall",
          cycle_time: null,
          cycle_time_utc: null,
          wall_started_at: "2026-01-01T00:00:00Z",
          wall_finished_at: "2026-01-01T00:10:00Z",
          run_kind: "manual",
          trace_id: null,
          cycle_failed: false,
          failure_message: null,
          completed_phases: null,
        })),
        total: current.length,
      };
    });

    const view = render(
      <MemoryRouter initialEntries={["/tasks/task-1?tab=config"]}>
        <Routes>
          <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("selected-backtest-run-id").textContent).toBe("run-newest");
    });

    fireEvent.click(screen.getByRole("tab", { name: "回测运行" }));
    await act(async () => {
      emitBacktestSelection?.("run-older");
    });
    fireEvent.click(screen.getByRole("tab", { name: "配置" }));
    expect(screen.getByTestId("selected-backtest-run-id").textContent).toBe("run-older");

    runListIndex = 1;
    await act(async () => {
      const refreshAction = screen.getByRole("button", { name: /暂停回测|继续回测/ });
      fireEvent.click(refreshAction);
    });

    await waitFor(() => {
      expect(screen.getByTestId("selected-backtest-run-id").textContent).toBe("run-older");
    });

    runListIndex = 2;
    await act(async () => {
      const refreshAction = screen.getByRole("button", { name: /暂停回测|继续回测/ });
      fireEvent.click(refreshAction);
    });

    await waitFor(() => {
      expect(screen.getByTestId("selected-backtest-run-id").textContent).toBe("run-older");
    });

    runListIndex = 3;
    await act(async () => {
      const refreshAction = screen.getByRole("button", { name: /暂停回测|继续回测/ });
      fireEvent.click(refreshAction);
    });

    await waitFor(() => {
      expect(screen.getByTestId("selected-backtest-run-id").textContent).toBe("run-newer-4");
    });

    outletState.instances = [{ ...baseTask, mode: "paper" }];
    await act(async () => {
      view.rerender(
        <MemoryRouter initialEntries={["/tasks/task-1?tab=config"]}>
          <Routes>
            <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
          </Routes>
        </MemoryRouter>,
      );
    });

    await waitFor(() => {
      expect(screen.queryByTestId("selected-backtest-run-id")).not.toBeInTheDocument();
    });
  });
});
