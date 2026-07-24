import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { AssistantPanel } from "../AssistantPanel";
import { parsePanelSpec } from "../panelSpec";

afterEach(cleanup);

describe("AssistantPanel", () => {
  it("renders an overall title and stacks table / statcard / markdown blocks", () => {
    const spec = parsePanelSpec({
      title: "组合概览",
      blocks: [
        { type: "markdown", content: "**核心持仓**" },
        {
          type: "table",
          title: "持仓",
          columns: [
            { title: "代码", data_index: "code" },
            { title: "市值", data_index: "mv", align: "right" },
          ],
          rows: [{ code: "600519.SH", mv: 120000 }],
        },
        {
          type: "statcard",
          metrics: [
            { label: "今日盈亏", value: "+3.2%", delta: "1200", delta_dir: "up" },
            { label: "持仓数", value: 5 },
          ],
        },
      ],
    });
    expect(spec).not.toBeNull();
    render(<AssistantPanel spec={spec!} />);

    expect(screen.getByText("组合概览")).toBeInTheDocument();
    expect(screen.getByTestId("assistant-markdown-block")).toBeInTheDocument();
    // block-level title
    expect(screen.getByText("持仓")).toBeInTheDocument();
    // table renders the cell values (as text)
    expect(screen.getByTestId("assistant-table-block")).toBeInTheDocument();
    expect(screen.getByText("600519.SH")).toBeInTheDocument();
    // statcard renders labels + values
    expect(screen.getByTestId("assistant-statcard-block")).toBeInTheDocument();
    expect(screen.getByText("今日盈亏")).toBeInTheDocument();
    expect(screen.getByText("+3.2%")).toBeInTheDocument();
  });

  it("stringifies object cell values instead of crashing", () => {
    const spec = parsePanelSpec({
      blocks: [
        {
          type: "table",
          columns: [{ title: "详情", data_index: "detail" }],
          rows: [{ detail: { nested: 1 } }],
        },
      ],
    });
    render(<AssistantPanel spec={spec!} />);
    expect(screen.getByText('{"nested":1}')).toBeInTheDocument();
  });
});
