import { render as rtlRender, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { InlineToolCallList } from "../InlineToolCallList";
import type { ToolUseBlock, ToolResultBlock, ToolCallEntry } from "../types";

// ``InlineToolCallCard`` calls ``useNavigate`` for the jump-to-task
// affordance, which requires a router context. Wrap every render so the
// list-level tests don't have to know about routing details.
const render: typeof rtlRender = (ui, options) =>
  rtlRender(<MemoryRouter>{ui}</MemoryRouter>, options);

const makeTool = (overrides: Partial<ToolUseBlock> = {}): ToolUseBlock => ({
  type: "tool_use",
  id: "tool-1",
  name: "get_data",
  category: "default",
  input: {},
  status: "completed",
  ...overrides,
});

const makeResult = (overrides: Partial<ToolResultBlock> = {}): ToolResultBlock => ({
  type: "tool_result",
  tool_use_id: "tool-1",
  output: {},
  is_error: false,
  ...overrides,
});

describe("InlineToolCallList", () => {
  it("renders empty when entries array is empty", () => {
    const { container } = render(<InlineToolCallList entries={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders one card per entry", () => {
    const entries: ToolCallEntry[] = [
      { tool: makeTool({ id: "t1", name: "tool_a" }) },
      { tool: makeTool({ id: "t2", name: "tool_b" }) },
    ];
    render(<InlineToolCallList entries={entries} />);
    expect(screen.getByText("tool_a")).toBeInTheDocument();
    expect(screen.getByText("tool_b")).toBeInTheDocument();
  });

  it("renders both tool and result when result exists", () => {
    const entries: ToolCallEntry[] = [
      {
        tool: makeTool({ id: "t1" }),
        result: makeResult({ tool_use_id: "t1" }),
      },
    ];
    render(<InlineToolCallList entries={entries} />);
    expect(screen.getAllByText("已完成")[0]).toBeInTheDocument();
  });
});
