import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AssistantWelcome } from "../AssistantWelcome";

describe("AssistantWelcome", () => {
  afterEach(() => cleanup());

  it("renders the hero with the agent name and example groups", () => {
    render(<AssistantWelcome agentName="Vibe-Trading" onPickExample={() => {}} />);

    expect(screen.getByRole("heading", { name: "Vibe-Trading" })).toBeInTheDocument();
    expect(screen.getByText("试试这些示例：")).toBeInTheDocument();
    expect(screen.getByText("策略与回测")).toBeInTheDocument();
    expect(screen.getByText("数据与分析")).toBeInTheDocument();
  });

  it("falls back to the default agent name when none is provided", () => {
    render(<AssistantWelcome onPickExample={() => {}} />);
    expect(screen.getByRole("heading", { name: "DoYouTrade Agent" })).toBeInTheDocument();
  });

  it("hands the full prompt back when an example card is clicked", () => {
    const onPick = vi.fn();
    render(<AssistantWelcome onPickExample={onPick} />);

    fireEvent.click(screen.getByRole("button", { name: /双均线交叉策略/ }));

    expect(onPick).toHaveBeenCalledTimes(1);
    expect(onPick.mock.calls[0][0]).toContain("000001.SZ");
  });
});
