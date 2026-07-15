import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { SwarmStatusCard } from "./SwarmStatusCard";
import type { SwarmTaskView } from "../types";

// antd 的 List 用到响应式栅格 → window.matchMedia，jsdom 未实现，需 polyfill。
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

function task(partial: Partial<SwarmTaskView> & Pick<SwarmTaskView, "task_id" | "agent_id" | "status">): SwarmTaskView {
  return {
    depends_on: [],
    summary: null,
    error: null,
    session_id: null,
    started_at: null,
    completed_at: null,
    worker_iterations: 0,
    ...partial,
  };
}

describe("SwarmStatusCard", () => {
  it("渲染各 worker 及其状态标签", () => {
    const tasks: SwarmTaskView[] = [
      task({ task_id: "task-bull", agent_id: "bull_advocate", status: "in_progress" }),
      task({ task_id: "task-bear", agent_id: "bear_advocate", status: "completed" }),
    ];
    render(<SwarmStatusCard tasks={tasks} />);
    expect(screen.getByText("bull_advocate")).toBeInTheDocument();
    expect(screen.getByText("运行中")).toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
  });

  it("workerStatus 覆盖任务自带状态（实时更新）", () => {
    const tasks: SwarmTaskView[] = [
      task({ task_id: "task-risk", agent_id: "risk_officer", status: "pending" }),
    ];
    render(<SwarmStatusCard tasks={tasks} workerStatus={{ "task-risk": "failed" }} />);
    expect(screen.getByText("失败")).toBeInTheDocument();
    expect(screen.queryByText("等待中")).not.toBeInTheDocument();
  });

  it("空任务列表显示占位", () => {
    render(<SwarmStatusCard tasks={[]} />);
    expect(screen.getByText("暂无任务")).toBeInTheDocument();
  });
});
