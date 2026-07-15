// frontend/src/components/assistant/__tests__/InlineToolCallCard.test.tsx

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InlineToolCallCard } from "../InlineToolCallCard";
import type { ToolUseBlock, ToolResultBlock } from "../types";

// RTL doesn't auto-unmount across renders in the same file by default and
// the existing tests in this suite predated that need. The new jump-button
// tests all assert against the same Chinese label so the prior DOM bleeds
// across them — pin the cleanup contract explicitly here.
afterEach(() => {
  cleanup();
});

// ``useNavigate`` is the only react-router-dom surface used by the card.
// Mock it directly so tests don't need a real Router wrapper — the
// navigate call is the contract we care about.
const navigateMock = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return {
    ...actual,
    useNavigate: () => navigateMock,
  };
});

const baseTool: ToolUseBlock = {
  type: "tool_use",
  id: "tool-1",
  name: "get_kline_data",
  category: "kline",
  input: { symbol: "000001", days: 30 },
  status: "running",
};

const result: ToolResultBlock = {
  type: "tool_result",
  tool_use_id: "tool-1",
  output: { candles: [], length: 0 },
  is_error: false,
};

describe("InlineToolCallCard", () => {
  it("renders tool name and category label", () => {
    render(<InlineToolCallCard tool={baseTool} />);
    expect(screen.getByText("get_kline_data")).toBeInTheDocument();
    expect(screen.getByText("K线查询")).toBeInTheDocument();
  });

  it("shows running status by default", () => {
    render(<InlineToolCallCard tool={baseTool} />);
    expect(screen.getAllByText("调用中")[0]).toBeInTheDocument();
  });

  it("shows completed status when tool is completed", () => {
    render(
      <InlineToolCallCard
        tool={{ ...baseTool, status: "completed" }}
        result={result}
      />
    );
    expect(screen.getAllByText("已完成")[0]).toBeInTheDocument();
  });

  it("shows error status when result is error even if tool status is completed", () => {
    render(
      <InlineToolCallCard
        tool={{ ...baseTool, status: "completed" }}
        result={{ ...result, is_error: true, output: { status: "error", error: "boom" } }}
      />
    );
    expect(screen.getAllByText("失败")[0]).toBeInTheDocument();
  });

  it("renders input JSON when expanded", () => {
    render(
      <InlineToolCallCard
        tool={baseTool}
        defaultExpanded
      />
    );
    expect(screen.getByText(/000001/)).toBeInTheDocument();
  });

  it("renders string output as markdown (headings, lists, fenced code)", () => {
    const markdown = [
      "## 回测报告",
      "",
      "- 收益率: **+7.90%**",
      "- 最大回撤: -2.10%",
      "",
      "```json",
      JSON.stringify({ status: "ok" }),
      "```",
    ].join("\n");
    render(
      <InlineToolCallCard
        tool={{ ...baseTool, status: "completed" }}
        result={{
          type: "tool_result",
          tool_use_id: "tool-1",
          output: markdown,
          is_error: false,
        }}
        defaultExpanded
      />,
    );
    // Markdown wrapper is mounted instead of the JSON-stringified blob.
    expect(screen.getByTestId("tool-result-markdown")).toBeInTheDocument();
    // Heading is rendered as an <h2>, not as a raw "## ..." text node.
    const heading = screen.getByRole("heading", { level: 2 });
    expect(heading.textContent).toContain("回测报告");
    // Bold span survives markdown conversion.
    const bold = screen.getByText("+7.90%");
    expect(bold.tagName.toLowerCase()).toBe("strong");
  });

  it("falls back to JSON view for non-string outputs", () => {
    render(
      <InlineToolCallCard
        tool={{ ...baseTool, status: "completed" }}
        result={{
          type: "tool_result",
          tool_use_id: "tool-1",
          output: { candles: [], length: 0 },
          is_error: false,
        }}
        defaultExpanded
      />,
    );
    expect(screen.queryByTestId("tool-result-markdown")).toBeNull();
    expect(screen.getByText(/candles/)).toBeInTheDocument();
  });

  it("uses default theme for unknown category", () => {
    render(
      <InlineToolCallCard
        tool={{ ...baseTool, category: "unknown_category" }}
      />
    );
    expect(screen.getByText("工具")).toBeInTheDocument();
  });

  describe("backtest jump button", () => {
    const backtestTool: ToolUseBlock = {
      type: "tool_use",
      id: "tool-bt",
      name: "run_strategy_backtest",
      category: "strategy",
      input: { instance_id: "si-1", universe: ["600522.SH"] },
      status: "completed",
    };

    it("renders jump button when run_strategy_backtest result carries task_id (parsed object)", () => {
      navigateMock.mockClear();
      const objResult: ToolResultBlock = {
        type: "tool_result",
        tool_use_id: "tool-bt",
        output: {
          status: "ok",
          backtest_job: { task_id: "task-abc", run_id: "btjob-1" },
        },
        is_error: false,
      };
      render(<InlineToolCallCard tool={backtestTool} result={objResult} />);
      const button = screen.getByText("查看回测任务详情");
      fireEvent.click(button);
      expect(navigateMock).toHaveBeenCalledWith("/tasks/task-abc");
    });

    it("extracts task_id from a fenced ```json``` envelope when output is a string", () => {
      navigateMock.mockClear();
      const text =
        "Backtest completed: run_id=btjob-1 status=completed.\n\n" +
        "```json\n" +
        JSON.stringify({
          status: "ok",
          backtest_job: { task_id: "task-from-string", run_id: "btjob-1" },
        }) +
        "\n```";
      const stringResult: ToolResultBlock = {
        type: "tool_result",
        tool_use_id: "tool-bt",
        output: text,
        is_error: false,
      };
      render(<InlineToolCallCard tool={backtestTool} result={stringResult} />);
      fireEvent.click(screen.getByText("查看回测任务详情"));
      expect(navigateMock).toHaveBeenCalledWith("/tasks/task-from-string");
    });

    it("falls back to tool.input.task_id when output is truncated past the JSON", () => {
      navigateMock.mockClear();
      // Simulate a tool_result that was truncated before reaching task_id.
      const truncated: ToolResultBlock = {
        type: "tool_result",
        tool_use_id: "tool-bt",
        output: "Backtest completed: run_id=btjob-1.\n\n```json\n{ \"status\":...",
        is_error: false,
      };
      const toolWithTaskInInput: ToolUseBlock = {
        ...backtestTool,
        input: { task_id: "task-from-input" },
      };
      render(
        <InlineToolCallCard
          tool={toolWithTaskInInput}
          result={truncated}
        />,
      );
      fireEvent.click(screen.getByText("查看回测任务详情"));
      expect(navigateMock).toHaveBeenCalledWith("/tasks/task-from-input");
    });

    it("hides the button when the call is still running", () => {
      const runningResult: ToolResultBlock = {
        type: "tool_result",
        tool_use_id: "tool-bt",
        output: { status: "ok", backtest_job: { task_id: "task-abc" } },
        is_error: false,
      };
      render(
        <InlineToolCallCard
          tool={{ ...backtestTool, status: "running" }}
          result={runningResult}
        />,
      );
      expect(screen.queryByText("查看回测任务详情")).toBeNull();
    });

    it("hides the button when the call errored", () => {
      const errorResult: ToolResultBlock = {
        type: "tool_result",
        tool_use_id: "tool-bt",
        output: { status: "error" },
        is_error: true,
      };
      render(<InlineToolCallCard tool={backtestTool} result={errorResult} />);
      expect(screen.queryByText("查看回测任务详情")).toBeNull();
    });

    it("never renders the button for non-backtest tools", () => {
      const someResult: ToolResultBlock = {
        type: "tool_result",
        tool_use_id: "tool-1",
        output: { task_id: "task-irrelevant" },
        is_error: false,
      };
      render(
        <InlineToolCallCard
          tool={{ ...baseTool, status: "completed" }}
          result={someResult}
        />,
      );
      expect(screen.queryByText("查看回测任务详情")).toBeNull();
    });
  });
});
