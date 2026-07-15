// frontend/src/components/assistant/__tests__/CollapsedProcessCard.test.tsx

import { cleanup, fireEvent, render as rtlRender, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import {
  CollapsedProcessCard,
  processErrorCount,
  processStageText,
  processSummaryText,
  type ProcessStep,
} from "../CollapsedProcessCard";
import type { ToolResultBlock, ToolUseBlock } from "../types";

const render: typeof rtlRender = (ui, options) =>
  rtlRender(<MemoryRouter>{ui}</MemoryRouter>, options);

afterEach(() => {
  cleanup();
});

const makeTool = (overrides: Partial<ToolUseBlock> = {}): ToolUseBlock => ({
  type: "tool_use",
  id: "t1",
  name: "test_tool",
  category: "default",
  input: {},
  status: "completed",
  ...overrides,
});

const errorResult = (toolId: string): ToolResultBlock => ({
  type: "tool_result",
  tool_use_id: toolId,
  output: "boom",
  is_error: true,
});

describe("processStageText", () => {
  it("shows a preparing hint when no steps arrived yet", () => {
    expect(processStageText([])).toBe("正在准备…");
  });

  it("shows thinking when the latest step is a thinking snippet", () => {
    const steps: ProcessStep[] = [
      { kind: "tool_call", tool: makeTool() },
      { kind: "thinking", content: "让我想想" },
    ];
    expect(processStageText(steps)).toBe("深度思考中…");
  });

  it("shows a reply-drafting hint when the latest step is narration text", () => {
    const steps: ProcessStep[] = [
      { kind: "tool_call", tool: makeTool() },
      { kind: "text", content: "先小结一下" },
    ];
    expect(processStageText(steps)).toBe("整理回复中…");
  });

  it("shows the running tool name while a call is in flight", () => {
    const steps: ProcessStep[] = [
      { kind: "tool_call", tool: makeTool({ name: "execute_bash", status: "running" }) },
    ];
    expect(processStageText(steps)).toBe("execute_bash 调用中…");
  });

  it("shows completion / failure for the latest tool call", () => {
    expect(
      processStageText([{ kind: "tool_call", tool: makeTool({ name: "stock_lookup" }) }]),
    ).toBe("stock_lookup 完成");
    expect(
      processStageText([
        {
          kind: "tool_call",
          tool: makeTool({ name: "stock_lookup" }),
          result: errorResult("t1"),
        },
      ]),
    ).toBe("stock_lookup 失败");
  });
});

describe("processSummaryText / processErrorCount", () => {
  it("summarises thinking and tool counts", () => {
    const steps: ProcessStep[] = [
      { kind: "thinking", content: "a" },
      { kind: "thinking", content: "b" },
      { kind: "tool_call", tool: makeTool() },
    ];
    expect(processSummaryText(steps)).toBe("思考过程 · 2 段思考 · 1 个工具调用");
    expect(processSummaryText([])).toBe("思考过程");
  });

  it("counts failed tool calls", () => {
    const steps: ProcessStep[] = [
      { kind: "tool_call", tool: makeTool({ id: "ok" }) },
      { kind: "tool_call", tool: makeTool({ id: "bad" }), result: errorResult("bad") },
    ];
    expect(processErrorCount(steps)).toBe(1);
  });
});

describe("CollapsedProcessCard", () => {
  const steps: ProcessStep[] = [
    { kind: "thinking", content: "先查一下行情" },
    { kind: "tool_call", tool: makeTool({ name: "stock_lookup" }) },
  ];

  it("renders nothing when finished with no steps", () => {
    const { container } = render(<CollapsedProcessCard steps={[]} />);
    expect(container.querySelector("[data-testid=collapsed-process-card]")).toBeNull();
  });

  it("stays collapsed by default and expands on click", () => {
    render(<CollapsedProcessCard steps={steps} />);
    expect(screen.getByTestId("process-stage-text").textContent).toBe(
      "思考过程 · 1 段思考 · 1 个工具调用",
    );
    // Collapsed: details (tool card + thinking body) are not in the DOM.
    expect(screen.queryByTestId("process-card-details")).toBeNull();
    expect(screen.queryByText("stock_lookup")).toBeNull();

    fireEvent.click(screen.getByTestId("process-card-header"));
    expect(screen.getByTestId("process-card-details")).toBeInTheDocument();
    expect(screen.getByText("stock_lookup")).toBeInTheDocument();
    expect(screen.getByText("先查一下行情")).toBeInTheDocument();
  });

  it("shows the live stage line with a spinner while streaming", () => {
    const running: ProcessStep[] = [
      ...steps,
      { kind: "tool_call", tool: makeTool({ id: "t2", name: "execute_bash", status: "running" }) },
    ];
    render(<CollapsedProcessCard steps={running} streaming />);
    expect(screen.getByTestId("process-card-spinner")).toBeInTheDocument();
    expect(screen.getByTestId("process-stage-text").textContent).toBe("execute_bash 调用中…");
    // The streaming placeholder renders even before any step arrives.
    cleanup();
    render(<CollapsedProcessCard steps={[]} streaming />);
    expect(screen.getByTestId("process-stage-text").textContent).toBe("正在准备…");
  });

  it("keeps failed tool calls visible in the collapsed header", () => {
    const withError: ProcessStep[] = [
      { kind: "tool_call", tool: makeTool({ id: "bad" }), result: errorResult("bad") },
    ];
    render(<CollapsedProcessCard steps={withError} />);
    expect(screen.getByTestId("process-card-error-tag").textContent).toBe("1 个工具失败");
  });
});
