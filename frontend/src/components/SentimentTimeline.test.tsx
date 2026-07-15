import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { SentimentTimeline } from "./SentimentTimeline";
import type { SentimentTimeline as SentimentTimelineData } from "../types";
import { getSentimentTimeline } from "../api";

// SentimentTimeline only imports getSentimentTimeline from ../api.
vi.mock("../api", () => ({
  getSentimentTimeline: vi.fn(),
}));

const timeline: SentimentTimelineData = {
  items: [
    {
      date: "2026-05-28",
      label: "退潮/低迷",
      limit_up_count: 18,
      limit_down_count: 22,
      broken_board_count: 30,
      broken_board_rate: 0.45,
      max_streak: 3,
    },
    {
      date: "2026-05-29",
      label: "发酵/活跃",
      limit_up_count: 62,
      limit_down_count: 4,
      broken_board_count: 12,
      broken_board_rate: 0.18,
      max_streak: 5,
    },
    {
      date: "2026-05-30",
      label: "高潮/亢奋",
      limit_up_count: 91,
      limit_down_count: 2,
      broken_board_count: 9,
      broken_board_rate: 0.1,
      max_streak: 7,
    },
  ],
};

describe("SentimentTimeline", () => {
  beforeAll(() => {
    // antd Tooltip/Empty rely on matchMedia in jsdom.
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
  });

  afterEach(cleanup);

  it("renders one cell per trading day with date + 涨停家数 and its label", async () => {
    vi.mocked(getSentimentTimeline).mockResolvedValue(timeline);

    render(<SentimentTimeline months={3} />);

    // Requests the configured window.
    expect(getSentimentTimeline).toHaveBeenCalledWith(3);

    // One cell per day appears (band renders after the fetch resolves).
    const cells = await screen.findAllByTestId("sentiment-day-cell");
    expect(cells).toHaveLength(3);

    // The 高潮 day carries the right label + 涨停家数 (91) — asserted via the
    // cell's data attributes and its sub-row caption.
    const climaxCell = cells.find((c) => c.getAttribute("data-date") === "2026-05-30");
    expect(climaxCell).toBeDefined();
    expect(climaxCell?.getAttribute("data-label")).toBe("高潮/亢奋");
    expect(climaxCell?.textContent).toContain("涨91");
    expect(climaxCell?.textContent).toContain("05-30");

    // The 退潮 day is present with its 涨停家数 (18) — no fabricated values.
    const ebbCell = cells.find((c) => c.getAttribute("data-date") === "2026-05-28");
    expect(ebbCell?.getAttribute("data-label")).toBe("退潮/低迷");
    expect(ebbCell?.textContent).toContain("涨18");

    // Legend surfaces every classifier label.
    const legend = await screen.findByTestId("sentiment-legend");
    expect(legend.textContent).toContain("退潮/低迷");
    expect(legend.textContent).toContain("发酵/活跃");
    expect(legend.textContent).toContain("高潮/亢奋");
    expect(legend.textContent).toContain("分歧加剧");
    expect(legend.textContent).toContain("中性");
  });

  it("shows the friendly empty state when no cycles have been recorded", async () => {
    vi.mocked(getSentimentTimeline).mockResolvedValue({ items: [] });

    render(<SentimentTimeline months={3} />);

    expect(
      await screen.findByText("暂无情绪周期记录（每日复盘自动累积后出现）"),
    ).toBeInTheDocument();
    // No day cells and no band when empty.
    expect(screen.queryByTestId("sentiment-day-cell")).toBeNull();
    expect(screen.queryByTestId("sentiment-band")).toBeNull();
  });
});
