import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TaskTriggersPanel } from "./TaskTriggersPanel";
import {
  listTaskTriggers,
  pauseTaskTrigger,
  resumeTaskTrigger,
  runTaskTrigger,
} from "../api";
import type { TaskStatus, TaskTrigger } from "../types";

vi.mock("../api", () => ({
  listTaskTriggers: vi.fn(),
  pauseTaskTrigger: vi.fn(),
  resumeTaskTrigger: vi.fn(),
  runTaskTrigger: vi.fn(),
  deleteTaskTrigger: vi.fn(),
}));

// Stub the form modal so the panel-level wiring (open-on-new) is what we test.
vi.mock("./TriggerFormModal", () => ({
  TriggerFormModal: ({ trigger }: { trigger?: TaskTrigger }) => (
    <div>trigger-form-modal:{trigger ? trigger.id : "create"}</div>
  ),
}));

const TASK: TaskStatus = {
  task_id: "task-1",
  name: "Task One",
  mode: "signal_only",
  description: "",
  status: "running",
  cycles: 0,
  last_error: "",
  data_provider: "qmt",
  data_provider_effective: "qmt",
  universe: ["600519.SH"],
  strategy_name: "S",
  settings: {},
  backtest_summary: null,
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-01T00:00:00Z",
} as unknown as TaskStatus;

function trigger(overrides: Partial<TaskTrigger> = {}): TaskTrigger {
  return {
    id: "trg-1",
    task_id: "task-1",
    name: "Close Signal",
    enabled: true,
    status: "active",
    schedule_kind: "cron",
    interval_seconds: null,
    cron_expression: "50 14 * * mon-fri",
    timezone: "Asia/Shanghai",
    at_iso: null,
    range_start: null,
    range_end: null,
    bar_interval: null,
    trading_session: "ashare",
    delete_after_run: false,
    execution_intent: "signal_only",
    delivery_json: { mode: "card", target: { kind: "session", origin: true } },
    last_fired_at: null,
    next_fire_at: null,
    last_run_id: null,
    last_error: "",
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    ...overrides,
  };
}

describe("TaskTriggersPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
    vi.mocked(listTaskTriggers).mockResolvedValue([trigger()]);
    vi.mocked(pauseTaskTrigger).mockResolvedValue(trigger({ status: "paused" }));
    vi.mocked(resumeTaskTrigger).mockResolvedValue(trigger({ status: "active" }));
    vi.mocked(runTaskTrigger).mockResolvedValue({ run_id: "run-123" });
  });

  afterEach(() => cleanup());

  it("opens the create form when 新建触发器 is clicked", async () => {
    render(<TaskTriggersPanel task={TASK} />);

    await screen.findByText("Close Signal");
    fireEvent.click(screen.getByTestId("new-trigger-button"));

    expect(await screen.findByText("trigger-form-modal:create")).toBeInTheDocument();
  });

  it("pauses an active trigger and reloads", async () => {
    render(<TaskTriggersPanel task={TASK} />);

    await screen.findByText("Close Signal");
    // antd inserts a space between two adjacent CJK chars in button text.
    fireEvent.click(screen.getByRole("button", { name: /暂\s*停/ }));

    await waitFor(() => {
      expect(pauseTaskTrigger).toHaveBeenCalledWith("task-1", "trg-1");
      // initial load + reload after pause
      expect(listTaskTriggers).toHaveBeenCalledTimes(2);
    });
  });

  it("runs a trigger and surfaces the run_id", async () => {
    render(<TaskTriggersPanel task={TASK} />);

    await screen.findByText("Close Signal");
    fireEvent.click(screen.getByRole("button", { name: "立即运行" }));

    await waitFor(() => {
      expect(runTaskTrigger).toHaveBeenCalledWith("task-1", "trg-1");
    });
    expect(await screen.findByText(/已触发 run_id=run-123/)).toBeInTheDocument();
  });

  it("offers 恢复 for a paused trigger", async () => {
    vi.mocked(listTaskTriggers).mockResolvedValueOnce([trigger({ status: "paused" })]);
    render(<TaskTriggersPanel task={TASK} />);

    await screen.findByText("Close Signal");
    fireEvent.click(screen.getByRole("button", { name: /恢\s*复/ }));

    await waitFor(() => {
      expect(resumeTaskTrigger).toHaveBeenCalledWith("task-1", "trg-1");
    });
  });
});
