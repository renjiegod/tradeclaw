// render_panel 路由回归：名为 render_panel 的 tool_call 被渲染成醒目的
// AssistantPanel（而非工具卡）；结果为错误 / 规范无效时回退到普通工具卡，
// 让错误可见。调试模式与简洁模式两条渲染路径都覆盖。

import { cleanup, render as rtlRender, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { MessageContentRenderer } from "../MessageContentRenderer";

const render: typeof rtlRender = (ui, options) =>
  rtlRender(<MemoryRouter>{ui}</MemoryRouter>, options);

afterEach(cleanup);

// 一个只含 markdown 块的面板规范（无外部依赖，适合渲染断言）。
const panelArgs = {
  title: "组合面板",
  blocks: [{ type: "markdown", content: "面板正文内容" }],
};

function panelToolBlock(overrides: Record<string, unknown> = {}) {
  return {
    type: "tool_call" as const,
    tool_call_id: "tc-panel-1",
    name: "render_panel",
    arguments: panelArgs,
    category: "render",
    status: "completed" as const,
    result_preview: JSON.stringify({ status: "rendered", block_count: 1 }),
    ...overrides,
  };
}

describe("MessageContentRenderer render_panel routing", () => {
  it("renders a valid render_panel tool_call as an AssistantPanel (debug mode)", () => {
    render(<MessageContentRenderer text="" contentBlocks={[panelToolBlock()]} debugMode />);
    expect(screen.getByTestId("assistant-panel")).toBeInTheDocument();
    expect(screen.getByText("组合面板")).toBeInTheDocument();
    expect(screen.getByText("面板正文内容")).toBeInTheDocument();
    // 不应把 render_panel 渲染成普通工具卡（工具名 mono 文本）。
    expect(screen.queryByText("render_panel")).not.toBeInTheDocument();
  });

  it("renders the panel outside the collapsed process card (simple mode)", () => {
    render(<MessageContentRenderer text="" contentBlocks={[panelToolBlock()]} debugMode={false} />);
    expect(screen.getByTestId("assistant-panel")).toBeInTheDocument();
    expect(screen.getByText("面板正文内容")).toBeInTheDocument();
  });

  it("falls back to a tool card when the render_panel result is an error", () => {
    render(
      <MessageContentRenderer
        text=""
        contentBlocks={[
          panelToolBlock({
            is_error: true,
            status: "error",
            result_preview: "[error:invalid_symbol] bad symbol",
          }),
        ]}
        debugMode
      />,
    );
    expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();
    // 错误可见：普通工具卡出现，显示工具名。
    expect(screen.getByText("render_panel")).toBeInTheDocument();
  });

  it("falls back to a tool card when the arguments have no valid blocks", () => {
    render(
      <MessageContentRenderer
        text=""
        contentBlocks={[panelToolBlock({ arguments: { blocks: [{ type: "heatmap" }] } })]}
        debugMode
      />,
    );
    expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();
    expect(screen.getByText("render_panel")).toBeInTheDocument();
  });

  it("still renders a normal (non-panel) tool_call as a tool card", () => {
    render(
      <MessageContentRenderer
        text=""
        contentBlocks={[
          {
            type: "tool_call",
            tool_call_id: "tc-other",
            name: "data_run",
            arguments: { code: "600519.SH" },
            status: "completed",
            result_preview: JSON.stringify({ status: "ok" }),
          },
        ]}
        debugMode
      />,
    );
    expect(screen.queryByTestId("assistant-panel")).not.toBeInTheDocument();
    expect(screen.getByText("data_run")).toBeInTheDocument();
  });
});
