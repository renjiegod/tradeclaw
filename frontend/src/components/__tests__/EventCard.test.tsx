import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { EventCard } from "../EventCard";

describe("EventCard", () => {
  it("renders event_type label", () => {
    render(
      <EventCard
        event={{ event_type: "signal_output", payload: { proposal_count: 2 } }}
        isSelected={false}
        onClick={vi.fn()}
      />
    );
    expect(screen.getByText("signal_output")).toBeInTheDocument();
  });

  it("calls onClick when card is clicked", () => {
    const onClick = vi.fn();
    render(
      <EventCard
        event={{ event_type: "signal_tool", payload: {} }}
        isSelected={false}
        onClick={onClick}
      />
    );
    fireEvent.click(screen.getByText("signal_tool").closest("div")!);
    expect(onClick).toHaveBeenCalled();
  });

  it("shows selected state text", () => {
    render(
      <EventCard
        event={{ event_type: "signal_output", payload: {} }}
        isSelected={true}
        onClick={vi.fn()}
      />
    );
    expect(screen.getByText("查看详情 ✓")).toBeInTheDocument();
  });
});
