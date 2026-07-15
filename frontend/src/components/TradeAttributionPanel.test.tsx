import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TradeAttributionPanel } from "./TradeAttributionPanel";
import type { TradeAttribution } from "../types";
import { getTradeAttribution } from "../api";

// TradeAttributionPanel only imports getTradeAttribution from ../api.
vi.mock("../api", () => ({
  getTradeAttribution: vi.fn(),
}));

const attribution: TradeAttribution = {
  summary: {
    round_trips: 3,
    win_count: 2,
    loss_count: 1,
    win_rate: 0.6667,
    // 128,000 元 → rendered as "12.80 万" and red (positive).
    total_realized_pnl: "128000.00",
    avg_win: "90000.00",
    avg_loss: "-52000.00",
    profit_factor: 3.46,
    avg_hold_days: 4.5,
    best: {
      symbol: "600519",
      name: "贵州茅台",
      realized_pnl: "150000.00",
      return_pct: 18.2,
    },
    worst: {
      symbol: "300750",
      name: "宁德时代",
      realized_pnl: "-52000.00",
      return_pct: -6.4,
    },
    open_positions: 1,
  },
  round_trips: [
    {
      symbol: "600519",
      name: "贵州茅台",
      open_date: "2026-06-01",
      close_date: "2026-06-10",
      hold_days: 9,
      qty: 100,
      avg_buy: "1650.00",
      avg_sell: "1800.00",
      // Winner → red.
      realized_pnl: "150000.00",
      return_pct: 18.2,
    },
    {
      symbol: "300750",
      name: "宁德时代",
      open_date: "2026-06-05",
      close_date: "2026-06-08",
      hold_days: 3,
      qty: 200,
      avg_buy: "260.00",
      avg_sell: "234.00",
      // Loser → green, negative money string.
      realized_pnl: "-52000.00",
      return_pct: -6.4,
    },
    {
      symbol: "000001",
      name: "平安银行",
      open_date: "2026-06-11",
      close_date: "2026-06-12",
      // Missing hold_days / return_pct must render "—", never fabricated.
      hold_days: null,
      qty: 500,
      avg_buy: "12.00",
      avg_sell: "12.60",
      realized_pnl: "30000.00",
      return_pct: null,
    },
  ],
  by_symbol: [
    { symbol: "600519", name: "贵州茅台", round_trips: 1, realized_pnl: "150000.00", win_rate: 1 },
    { symbol: "300750", name: "宁德时代", round_trips: 1, realized_pnl: "-52000.00", win_rate: 0 },
  ],
  unparsed: [],
};

describe("TradeAttributionPanel", () => {
  beforeAll(() => {
    // antd Empty / Table / Alert rely on matchMedia in jsdom.
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

  it("renders the headline summary: 胜率 and 总已实现盈亏 (red / 亿-万 formatted)", async () => {
    vi.mocked(getTradeAttribution).mockResolvedValue(attribution);

    render(<TradeAttributionPanel />);

    expect(getTradeAttribution).toHaveBeenCalledTimes(1);

    const summary = await screen.findByTestId("trade-attribution-summary");

    // 回合数.
    const roundTripsStat = within(summary)
      .getAllByTestId("trade-attribution-stat")
      .find((s) => s.getAttribute("data-stat") === "回合数");
    expect(roundTripsStat).toBeDefined();
    expect(roundTripsStat?.textContent).toContain("3");

    // 胜率 0.6667 → "66.7%".
    expect(summary.textContent).toContain("66.7%");

    // 总已实现盈亏 128000 → "12.80 万" and coloured red (positive gain).
    const totalStat = within(summary)
      .getAllByTestId("trade-attribution-stat")
      .find((s) => s.getAttribute("data-stat") === "总已实现盈亏");
    expect(totalStat).toBeDefined();
    expect(totalStat?.textContent).toContain("12.80 万");
    const totalValue = totalStat?.querySelector("span[style]") as HTMLElement | null;
    expect(totalValue?.style.color).toBe("rgb(207, 19, 34)"); // #cf1322 red

    // 盈亏比 / 平均持仓天数 / 未平仓 present.
    expect(summary.textContent).toContain("3.46"); // profit factor
    expect(summary.textContent).toContain("4.5"); // avg hold days
  });

  it("renders best / worst highlight cards", async () => {
    vi.mocked(getTradeAttribution).mockResolvedValue(attribution);

    render(<TradeAttributionPanel />);

    const best = await screen.findByTestId("trade-attribution-best");
    expect(best.textContent).toContain("600519");
    expect(best.textContent).toContain("贵州茅台");

    const worst = await screen.findByTestId("trade-attribution-worst");
    expect(worst.textContent).toContain("300750");
    expect(worst.textContent).toContain("宁德时代");
  });

  it("renders a round-trip row: winner PnL is red, loser PnL is green", async () => {
    vi.mocked(getTradeAttribution).mockResolvedValue(attribution);

    render(<TradeAttributionPanel />);

    const table = await screen.findByTestId("trade-attribution-round-trips");

    // Winning round-trip (贵州茅台): pnl "15.00 万" coloured red.
    const winnerRow = within(table).getByText("600519").closest("tr");
    expect(winnerRow).toBeTruthy();
    expect(winnerRow?.textContent).toContain("15.00 万");
    const winnerPnl = Array.from(winnerRow?.querySelectorAll("span[style]") ?? []).find((el) =>
      (el as HTMLElement).textContent?.includes("15.00 万"),
    ) as HTMLElement | undefined;
    expect(winnerPnl?.style.color).toBe("rgb(207, 19, 34)"); // red gain

    // Losing round-trip (宁德时代): negative money "-5.20 万" coloured green.
    const loserRow = within(table).getByText("300750").closest("tr");
    expect(loserRow?.textContent).toContain("-5.20 万");
    const loserPnl = Array.from(loserRow?.querySelectorAll("span[style]") ?? []).find((el) =>
      (el as HTMLElement).textContent?.includes("-5.20 万"),
    ) as HTMLElement | undefined;
    expect(loserPnl?.style.color).toBe("rgb(56, 158, 13)"); // #389e0d green loss

    // Missing hold_days / return_pct on 平安银行 render "—", never fabricated.
    const nullRow = within(table).getByText("000001").closest("tr");
    expect(nullRow?.textContent).toContain("—");
  });

  it("surfaces an honest warning listing unparsed settlement files", async () => {
    vi.mocked(getTradeAttribution).mockResolvedValue({
      ...attribution,
      unparsed: [
        { path: "trades/broker_x_2026-06.csv", reason: "缺少成交方向列" },
        { path: "trades/weird.xlsx", reason: "不支持的文件格式" },
      ],
    });

    render(<TradeAttributionPanel />);

    const alert = await screen.findByTestId("trade-attribution-unparsed");
    expect(alert.textContent).toContain("2 个交割单文件无法解析");
    expect(alert.textContent).toContain("trades/broker_x_2026-06.csv");
    expect(alert.textContent).toContain("缺少成交方向列");
    expect(alert.textContent).toContain("trades/weird.xlsx");
  });

  it("shows the friendly empty state for an account with no round-trips", async () => {
    vi.mocked(getTradeAttribution).mockResolvedValue({
      summary: {
        round_trips: 0,
        win_count: 0,
        loss_count: 0,
        win_rate: null,
        total_realized_pnl: "0",
        avg_win: null,
        avg_loss: null,
        profit_factor: null,
        avg_hold_days: null,
        best: null,
        worst: null,
        open_positions: 0,
      },
      round_trips: [],
      by_symbol: [],
      unparsed: [],
    });

    render(<TradeAttributionPanel />);

    expect(
      await screen.findByText(
        "暂无可归因的交割单（把券商导出的交割单放进 knowledge 的 trades/ 后出现）",
      ),
    ).toBeInTheDocument();
    // No summary strip / tables when empty.
    expect(screen.queryByTestId("trade-attribution-summary")).toBeNull();
    expect(screen.queryByTestId("trade-attribution-round-trips")).toBeNull();
  });
});
