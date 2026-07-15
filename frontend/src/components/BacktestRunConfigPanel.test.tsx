import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { BacktestRunConfigPanel } from "./BacktestRunConfigPanel";
import { getDebugSession, getTaskRun } from "../api";

vi.mock("../api", () => ({
  getTaskRun: vi.fn(),
  getDebugSession: vi.fn(),
}));

describe("BacktestRunConfigPanel", () => {
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
  afterEach(() => {
    cleanup();
  });

  it("shows empty state when selectedRunId is null", () => {
    render(<BacktestRunConfigPanel taskId="task-1" selectedRunId={null} />);
    expect(screen.getByText("暂无运行配置，先发起一次回测")).toBeInTheDocument();
    expect(getTaskRun).not.toHaveBeenCalled();
    expect(getDebugSession).not.toHaveBeenCalled();
  });

  it("renders effective_config when run and session exist", async () => {
    vi.mocked(getTaskRun).mockResolvedValue({
      run_id: "run-1",
      task_id: "task-1",
      status: "completed",
      market_profile: "cn-a",
      bar_interval: "1d",
      range_start_utc: "2026-01-01T00:00:00Z",
      range_end_utc: "2026-01-10T00:00:00Z",
      session_id: "sess-1",
      starting_equity: 10000,
      ending_equity: 10100,
      return_pct: 1,
      error_message: null,
      bars_total: 10,
      bars_completed: 10,
      created_at: "2026-01-01T00:00:00Z",
      started_at: "2026-01-01T00:00:01Z",
      finished_at: "2026-01-01T00:00:02Z",
    });
    vi.mocked(getDebugSession).mockResolvedValue({
      session_id: "sess-1",
      task_id: "task-1",
      status: "completed",
      run_id: "run-1",
      error_message: "",
      input_overrides: null,
      effective_config: { data_provider: "akshare", bar_interval: "1d" },
      created_at: "2026-01-01T00:00:00Z",
      started_at: "2026-01-01T00:00:01Z",
      finished_at: "2026-01-01T00:00:02Z",
      session_type: "backtest",
      spans: [],
      model_invocations: [],
    });

    render(<BacktestRunConfigPanel taskId="task-1" selectedRunId="run-1" />);

    expect(await screen.findByText("本次生效配置")).toBeInTheDocument();
    expect(screen.getByText(/"data_provider"/)).toBeInTheDocument();
    expect(getTaskRun).toHaveBeenCalledWith("task-1", "run-1");
    expect(getDebugSession).toHaveBeenCalledWith("task-1", "sess-1");
  });

  it("shows empty state when run has no session_id", async () => {
    vi.mocked(getTaskRun).mockResolvedValue({
      run_id: "run-2",
      task_id: "task-1",
      status: "completed",
      market_profile: "cn-a",
      bar_interval: "1d",
      range_start_utc: "2026-01-01T00:00:00Z",
      range_end_utc: "2026-01-10T00:00:00Z",
      session_id: null,
      starting_equity: 10000,
      ending_equity: 10100,
      return_pct: 1,
      error_message: null,
      bars_total: 10,
      bars_completed: 10,
      created_at: "2026-01-01T00:00:00Z",
      started_at: "2026-01-01T00:00:01Z",
      finished_at: "2026-01-01T00:00:02Z",
    });

    render(<BacktestRunConfigPanel taskId="task-1" selectedRunId="run-2" />);

    expect(await screen.findByText("该运行未记录有效配置快照")).toBeInTheDocument();
    expect(getDebugSession).not.toHaveBeenCalled();
  });

  it("shows empty state when session effective_config is null", async () => {
    vi.mocked(getTaskRun).mockResolvedValue({
      run_id: "run-3",
      task_id: "task-1",
      status: "completed",
      market_profile: "cn-a",
      bar_interval: "1d",
      range_start_utc: "2026-01-01T00:00:00Z",
      range_end_utc: "2026-01-10T00:00:00Z",
      session_id: "sess-3",
      starting_equity: 10000,
      ending_equity: 10100,
      return_pct: 1,
      error_message: null,
      bars_total: 10,
      bars_completed: 10,
      created_at: "2026-01-01T00:00:00Z",
      started_at: "2026-01-01T00:00:01Z",
      finished_at: "2026-01-01T00:00:02Z",
    });
    vi.mocked(getDebugSession).mockResolvedValue({
      session_id: "sess-3",
      task_id: "task-1",
      status: "completed",
      run_id: "run-3",
      error_message: "",
      input_overrides: null,
      effective_config: null,
      created_at: "2026-01-01T00:00:00Z",
      started_at: "2026-01-01T00:00:01Z",
      finished_at: "2026-01-01T00:00:02Z",
      session_type: "backtest",
      spans: [],
      model_invocations: [],
    });

    render(<BacktestRunConfigPanel taskId="task-1" selectedRunId="run-3" />);

    expect(await screen.findByText("该运行未记录有效配置快照")).toBeInTheDocument();
  });

  it("shows error state with retry when request fails", async () => {
    vi.mocked(getTaskRun).mockRejectedValue(new Error("boom"));

    render(<BacktestRunConfigPanel taskId="task-1" selectedRunId="run-err" />);

    expect(await screen.findByText("加载运行配置失败")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /重\s*试/ })).toBeInTheDocument();

    const retryButton = screen.getByRole("button", { name: /重\s*试/ });
    vi.mocked(getTaskRun).mockResolvedValueOnce({
      run_id: "run-err",
      task_id: "task-1",
      status: "completed",
      market_profile: "cn-a",
      bar_interval: "1d",
      range_start_utc: "2026-01-01T00:00:00Z",
      range_end_utc: "2026-01-10T00:00:00Z",
      session_id: null,
      starting_equity: 10000,
      ending_equity: 10100,
      return_pct: 1,
      error_message: null,
      bars_total: 10,
      bars_completed: 10,
      created_at: "2026-01-01T00:00:00Z",
      started_at: "2026-01-01T00:00:01Z",
      finished_at: "2026-01-01T00:00:02Z",
    });

    retryButton.click();

    await waitFor(() => {
      expect(screen.getByText("该运行未记录有效配置快照")).toBeInTheDocument();
    });
  });
});
