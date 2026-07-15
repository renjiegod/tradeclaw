import { render, screen } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { BacktestOverviewPanel } from "./BacktestOverviewPanel";
import type { BacktestSummary, RunRow, TaskStatus } from "../types";

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

const baseTask: TaskStatus = {
  task_id: "task-1",
  name: "BT 1",
  mode: "backtest",
  description: "",
  status: "completed",
  cycles: null,
  last_error: "",
  data_provider: "mock",
  data_provider_effective: "mock",
  universe: [],
  execution_strategy: "",
  account_id: "",
  model_id: "",
  settings: {},
  enabled_skills: [],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-06T00:00:00Z",
};

const summary: BacktestSummary = {
  schema_version: 1,
  run_id: "run-1",
  range_start_utc: "2026-01-05T00:00:00Z",
  range_end_utc: "2026-01-06T00:00:00Z",
  bar_interval: "1d",
  completed_at: "2026-01-06T07:00:00Z",
  starting_equity: "100000.00",
  ending_equity: "105500.00",
  return_pct: "5.5",
  final_cash: "55500.00",
  final_market_value: "50000.00",
  final_positions: [
    {
      symbol: "600000.SH",
      name: "浦发银行",
      quantity: 100,
      available: 100,
      cost_price: "10.00",
      last_price: "10.50",
      market_value: "1050.00",
      weight_pct: "0.9953",
    },
    {
      symbol: "000001.SZ",
      name: "平安银行",
      quantity: 200,
      available: 200,
      cost_price: "12.00",
      last_price: "12.00",
      market_value: "2400.00",
      weight_pct: "2.2749",
    },
  ],
  trade_count_closed: 4,
  trade_count_open: 1,
  fills_count: 9,
  win_rate: "0.75",
  win_rate_sample_size: 5,
  avg_holding_trading_days: "3.5",
  avg_holding_sample_size: 5,
  max_drawdown_pct: "1.20",
  max_drawdown_peak_at: "2026-01-05T07:00:00Z",
  max_drawdown_trough_at: "2026-01-06T07:00:00Z",
  max_drawdown_peak_equity: "100200.00",
  max_drawdown_trough_equity: "98800.00",
  equity_curve_meta: { downsampled: false, raw_length: 2 },
  equity_curve: [
    { t: "2026-01-05T07:00:00Z", equity: "100100.00" },
    { t: "2026-01-06T07:00:00Z", equity: "105500.00" },
  ],
};

const baseRun: RunRow = {
  run_id: "run-1",
  task_id: "task-1",
  status: "completed",
  market_profile: "cn_a_share",
  bar_interval: "1d",
  range_start_utc: "2026-01-05T00:00:00Z",
  range_end_utc: "2026-01-06T00:00:00Z",
  session_id: "sess-1",
  starting_equity: 100000,
  ending_equity: 105500,
  return_pct: 5.5,
  error_message: null,
  bars_total: 2,
  bars_completed: 2,
  created_at: "2026-01-05T00:00:00Z",
  started_at: "2026-01-05T00:00:00Z",
  finished_at: "2026-01-06T07:00:00Z",
};

describe("BacktestOverviewPanel", () => {
  it("renders 9 metric cards, equity chart, and positions sorted by market value desc", () => {
    const { container } = render(
      <BacktestOverviewPanel task={{ ...baseTask, backtest_summary: summary }} run={baseRun} />,
    );

    expect(screen.getByText("起始权益")).toBeTruthy();
    expect(screen.getByText("期末权益")).toBeTruthy();
    expect(screen.getByText("收益率")).toBeTruthy();
    expect(screen.getByText("最终现金")).toBeTruthy();
    expect(screen.getByText("最终市值")).toBeTruthy();
    expect(screen.getByText("交易次数")).toBeTruthy();
    expect(screen.getByText("胜率")).toBeTruthy();
    expect(screen.getByText("平均持仓(交易日)")).toBeTruthy();
    expect(screen.getByText("最大回撤")).toBeTruthy();
    expect(screen.getByText("9")).toBeTruthy();
    expect(screen.getByText(/已平仓\s*4.*持仓\s*1/)).toBeTruthy();

    expect(screen.getByTestId("backtest-equity-chart")).toBeTruthy();

    expect(screen.getByText("600000.SH")).toBeTruthy();
    expect(screen.getByText("000001.SZ")).toBeTruthy();

    const headerCells = container.querySelectorAll("th");
    let symbolHeaderIndex = -1;
    headerCells.forEach((cell, i) => {
      if (cell.textContent?.includes("代码")) symbolHeaderIndex = i;
    });
    expect(symbolHeaderIndex).toBeGreaterThanOrEqual(0);

    const rows = container.querySelectorAll("tbody tr");
    const firstSymbolCell = rows[0]?.querySelectorAll("td")[symbolHeaderIndex];
    expect(firstSymbolCell?.textContent?.includes("000001.SZ")).toBe(true);
  });

  it("renders placeholder when no summary and run is active", () => {
    const runningRun: RunRow = { ...baseRun, status: "running", bars_completed: 1, bars_total: 5 };
    render(
      <BacktestOverviewPanel
        task={{ ...baseTask, backtest_summary: null, status: "configured" }}
        run={runningRun}
      />,
    );
    expect(screen.getByText(/回测尚未结束/)).toBeTruthy();
    expect(screen.getByText(/1\s*\/\s*5/)).toBeTruthy();
  });

  it("buy-only run shows fills_count and mtm win_rate (regression for 0/1 dash bug)", () => {
    const buyOnly: BacktestSummary = {
      ...summary,
      trade_count_closed: 0,
      trade_count_open: 1,
      fills_count: 1,
      win_rate: "1",
      win_rate_sample_size: 1,
      avg_holding_trading_days: "3",
      avg_holding_sample_size: 1,
    };
    render(
      <BacktestOverviewPanel
        task={{ ...baseTask, backtest_summary: buyOnly }}
        run={baseRun}
      />,
    );
    expect(screen.getAllByText("交易次数").length).toBeGreaterThan(0);
    // fills_count appears at the trade count card (主值 "1").
    expect(screen.getAllByText("1").length).toBeGreaterThan(0);
    // Subline shows the FIFO breakdown so users still see closed/open at a glance.
    expect(screen.getAllByText(/已平仓.*0.*持仓.*1/).length).toBeGreaterThan(0);
    // mtm win_rate (1/1) → 100.00%.
    expect(screen.getByText("100.00%")).toBeTruthy();
  });

  it("shows downsampled tag when equity_curve_meta.downsampled is true", () => {
    const downsampled: BacktestSummary = {
      ...summary,
      equity_curve_meta: { downsampled: true, raw_length: 12345 },
    };
    render(
      <BacktestOverviewPanel
        task={{ ...baseTask, backtest_summary: downsampled }}
        run={baseRun}
      />,
    );
    expect(screen.getByText(/已下采样/)).toBeTruthy();
    expect(screen.getByText(/12345/)).toBeTruthy();
  });

  it("shows empty curve state when starting equity is invalid", () => {
    const invalidStartingEquity: BacktestSummary = {
      ...summary,
      starting_equity: "0",
    };
    render(
      <BacktestOverviewPanel
        task={{ ...baseTask, backtest_summary: invalidStartingEquity }}
        run={baseRun}
      />,
    );
    expect(screen.getAllByText("暂无权益曲线").length).toBeGreaterThan(0);
  });

  it("shows empty curve state when fewer than two valid points remain", () => {
    const dirtyCurve: BacktestSummary = {
      ...summary,
      equity_curve: [
        { t: "2026-01-05T07:00:00Z", equity: "100100.00" },
        { t: "", equity: "not-a-number" },
      ],
    };
    render(
      <BacktestOverviewPanel
        task={{ ...baseTask, backtest_summary: dirtyCurve }}
        run={baseRun}
      />,
    );
    expect(screen.getAllByText("暂无权益曲线").length).toBeGreaterThan(0);
  });
});
