import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { forwardRef, useImperativeHandle } from "react";
import { Modal } from "antd";

import { TasksPage } from "./TasksPage";
import type { TaskStatus } from "../types";
import {
  deleteTask,
  deleteTasks,
  getTaskDuplicatePreset,
  listStrategyDefinitions,
  listTaskRuns,
  listTaskTriggers,
  listTasksPage,
} from "../api";

const applyCreatePatchMock = vi.fn();

vi.mock("../api", () => ({
  deleteTask: vi.fn(),
  deleteTasks: vi.fn(),
  getTaskDuplicatePreset: vi.fn(),
  listStrategyDefinitions: vi.fn(),
  listTaskRuns: vi.fn(),
  listTaskTriggers: vi.fn(),
  listTasksPage: vi.fn(),
}));

type MockTableProps = {
  tasks: TaskStatus[];
  onDuplicate?: (task: TaskStatus) => void;
  onDelete?: (task: TaskStatus) => void;
  onBulkDelete?: () => void;
  selectedTaskIds?: string[];
  onSelectedTaskIdsChange?: (taskIds: string[]) => void;
};

function renderMockTable(label: string, props: MockTableProps) {
  return (
    <div>
      <div>{`${label}-table`}</div>
      {props.tasks.length > 0 ? (
        <>
          <button type="button" onClick={() => props.onDuplicate?.(props.tasks[0]!)}>
            {`duplicate-${props.tasks[0]!.task_id}`}
          </button>
          <button type="button" onClick={() => props.onDelete?.(props.tasks[0]!)}>
            {`delete-${props.tasks[0]!.task_id}`}
          </button>
          <button type="button" onClick={() => props.onSelectedTaskIdsChange?.(props.tasks.map((task) => task.task_id))}>
            select-all
          </button>
          <button type="button" onClick={() => props.onBulkDelete?.()} disabled={!props.selectedTaskIds?.length}>
            bulk-delete
          </button>
        </>
      ) : null}
    </div>
  );
}

vi.mock("../components/TradingTaskTable", () => ({
  TradingTaskTable: (props: MockTableProps) => renderMockTable("trading", props),
}));

vi.mock("../components/BacktestTaskTable", () => ({
  BacktestTaskTable: (props: MockTableProps) => renderMockTable("backtest", props),
}));

vi.mock("../components/CreateAgentCard", () => ({
  CreateAgentCard: forwardRef(
    (
      props: {
        createInitialValues?: {
          name?: string;
          mode?: string;
          strategy_definition_id?: string;
        } | null;
      },
      ref,
    ) => {
      useImperativeHandle(
        ref,
        () => ({
          openSettingsJsonModal: () => undefined,
          applyCreatePatch: applyCreatePatchMock,
        }),
        [],
      );
      return (
        <div>
          <div>{`dup-name:${props.createInitialValues?.name ?? ""}`}</div>
          <div>{`create-mode:${props.createInitialValues?.mode ?? ""}`}</div>
          <div>{`definition-id:${props.createInitialValues?.strategy_definition_id ?? ""}`}</div>
        </div>
      );
    },
  ),
}));

const baseTask: TaskStatus = {
  task_id: "task-1",
  name: "Task 1",
  mode: "paper",
  description: "",
  status: "configured",
  cycles: null,
  last_error: "",
  data_provider: "mock",
  data_provider_effective: "mock",
  universe: [],
  settings: {},
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("TasksPage tabs + list flows", () => {
  let modalConfirmSpy: ReturnType<typeof vi.spyOn>;

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
    modalConfirmSpy = vi.spyOn(Modal, "confirm").mockImplementation((config) => {
      void config.onOk?.();
      return {
        destroy: vi.fn(),
        update: vi.fn(),
      } as never;
    });
    vi.mocked(deleteTask).mockResolvedValue(undefined);
    vi.mocked(deleteTasks).mockResolvedValue(undefined);
    vi.mocked(listStrategyDefinitions).mockResolvedValue({ items: [] });
    vi.mocked(listTaskTriggers).mockResolvedValue([]);
    vi.mocked(listTasksPage).mockResolvedValue({
      items: [baseTask],
      total: 1,
      limit: 20,
      offset: 0,
    });
  });

  afterEach(() => {
    modalConfirmSpy.mockRestore();
    cleanup();
  });

  it("defaults to the trading tab and queries the non-backtest modes", async () => {
    render(<TasksPage onMutated={vi.fn()} />);
    await waitFor(() => {
      expect(listTasksPage).toHaveBeenCalled();
    });
    expect(screen.getByText("trading-table")).toBeInTheDocument();
    expect(listTasksPage).toHaveBeenCalledWith(
      expect.objectContaining({ modes: ["paper", "live", "signal_only"] }),
    );
  });

  it("switches to the backtest tab and queries the backtest mode", async () => {
    render(<TasksPage onMutated={vi.fn()} />);
    await waitFor(() => {
      expect(listTasksPage).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByText("回测"));

    await waitFor(() => {
      expect(screen.getByText("backtest-table")).toBeInTheDocument();
      expect(listTasksPage).toHaveBeenCalledWith(expect.objectContaining({ modes: ["backtest"] }));
    });
  });

  it("prefills strategy definition binding from duplicate preset API", async () => {
    vi.mocked(getTaskDuplicatePreset).mockResolvedValue({
      name: "graph-a-copy",
      mode: "paper",
      description: "desc",
      data_provider: "mock",
      universe_symbols: ["000001.SZ"],
      strategy: {
        definition_id: "sd-main",
        parameter_overrides: { lookback: 20 },
        execution_profile: "default",
      },
    });

    const view = render(<TasksPage onMutated={vi.fn()} />);
    await waitFor(() => {
      expect(listTasksPage).toHaveBeenCalled();
    });
    fireEvent.click(view.getAllByRole("button", { name: "duplicate-task-1" })[0]!);

    await waitFor(() => {
      expect(getTaskDuplicatePreset).toHaveBeenCalledWith("task-1");
      expect(screen.getByText("dup-name:graph-a-copy")).toBeInTheDocument();
      expect(screen.getByText("definition-id:sd-main")).toBeInTheDocument();
    });
  });

  it("keeps backtest latest-run patch after preset prefill", async () => {
    vi.mocked(getTaskDuplicatePreset).mockResolvedValue({
      name: "backtest-copy",
      mode: "backtest",
      description: "",
      data_provider: "mock",
      universe_symbols: [],
      strategy: {
        definition_id: "sd-default",
        parameter_overrides: {},
        execution_profile: "default",
      },
    });
    vi.mocked(listTaskRuns).mockResolvedValue({
      total: 1,
      items: [
        {
          run_id: "run-1",
          task_id: "task-1",
          status: "completed",
          market_profile: "cn_a_share",
          bar_interval: "1d",
          range_start_utc: "2024-01-01T00:00:00Z",
          range_end_utc: "2024-01-31T00:00:00Z",
          session_id: null,
          starting_equity: null,
          ending_equity: null,
          return_pct: null,
          error_message: null,
          bars_total: 0,
          bars_completed: 0,
          created_at: "2026-01-01T00:00:00Z",
          started_at: null,
          finished_at: null,
        },
      ],
    });

    vi.mocked(listTasksPage).mockResolvedValueOnce({
      items: [{ ...baseTask, mode: "backtest" }],
      total: 1,
      limit: 20,
      offset: 0,
    });
    const view = render(<TasksPage onMutated={vi.fn()} />);
    await waitFor(() => {
      expect(listTasksPage).toHaveBeenCalled();
    });
    fireEvent.click(view.getAllByRole("button", { name: "duplicate-task-1" })[0]!);

    await waitFor(() => {
      expect(listTaskRuns).toHaveBeenCalledWith("task-1", { limit: 1, offset: 0 });
      expect(applyCreatePatchMock).toHaveBeenCalledWith(
        expect.objectContaining({
          backtest_market_profile: "cn_a_share",
          backtest_bar_interval: "1d",
        }),
        { onlyWhenEmpty: true },
      );
    });
  });

  it("deletes a single non-running task and refreshes the list", async () => {
    const view = render(<TasksPage onMutated={vi.fn()} />);
    await waitFor(() => {
      expect(listTasksPage).toHaveBeenCalled();
    });

    fireEvent.click(view.getByRole("button", { name: "delete-task-1" }));

    await waitFor(() => {
      expect(deleteTask).toHaveBeenCalledWith("task-1");
      expect(listTasksPage).toHaveBeenCalledTimes(2);
    });
  });

  it("deletes selected non-running tasks in bulk and refreshes the list", async () => {
    vi.mocked(listTasksPage).mockResolvedValueOnce({
      items: [
        baseTask,
        { ...baseTask, task_id: "task-2", name: "Task 2", status: "paused" },
      ],
      total: 2,
      limit: 20,
      offset: 0,
    });

    const view = render(<TasksPage onMutated={vi.fn()} />);
    await waitFor(() => {
      expect(listTasksPage).toHaveBeenCalled();
    });

    fireEvent.click(view.getByRole("button", { name: "select-all" }));
    fireEvent.click(view.getByRole("button", { name: "bulk-delete" }));

    await waitFor(() => {
      expect(deleteTasks).toHaveBeenCalledWith(["task-1", "task-2"]);
      expect(listTasksPage).toHaveBeenCalledTimes(2);
    });
  });
});
