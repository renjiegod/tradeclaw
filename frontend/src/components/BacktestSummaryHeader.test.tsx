import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { BacktestSummaryHeader } from "./BacktestSummaryHeader";
import type { BacktestSummary, RunRow, TaskStatus } from "../types";

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
  settings: {},
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-06T00:00:00Z",
};

const finalSummary: BacktestSummary = {
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
  final_positions: [],
  trade_count_closed: 4,
  trade_count_open: 1,
  fills_count: 9,
  win_rate: "0.75",
  win_rate_sample_size: 4,
  avg_holding_trading_days: "3.5",
  avg_holding_sample_size: 5,
  max_drawdown_pct: "1.20",
  max_drawdown_peak_at: "2026-01-05T07:00:00Z",
  max_drawdown_trough_at: "2026-01-06T07:00:00Z",
  max_drawdown_peak_equity: "100200.00",
  max_drawdown_trough_equity: "98800.00",
  equity_curve_meta: { downsampled: false, raw_length: 2 },
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

afterEach(() => {
  cleanup();
});

describe("BacktestSummaryHeader", () => {
  it("renders 5 KPI cards from a finalized summary", () => {
    render(<BacktestSummaryHeader task={{ ...baseTask, backtest_summary: finalSummary }} run={baseRun} />);

    expect(screen.getByText("收益率")).toBeTruthy();
    expect(screen.getByText("期末权益")).toBeTruthy();
    expect(screen.getByText("交易次数")).toBeTruthy();
    expect(screen.getByText("胜率")).toBeTruthy();
    expect(screen.getByText("最大回撤")).toBeTruthy();

    expect(screen.getByText("+5.50%")).toBeTruthy();
    expect(screen.getByText("9")).toBeTruthy();
    expect(screen.getByText("75.00%")).toBeTruthy();
    expect(screen.getByText("-1.20%")).toBeTruthy();
  });

  it("renders the backtest window range and bar interval from the summary", () => {
    render(<BacktestSummaryHeader task={{ ...baseTask, backtest_summary: finalSummary }} run={baseRun} />);

    const range = screen.getByTestId("backtest-range");
    expect(range).toHaveTextContent("回测区间：2026-01-05 ~ 2026-01-06");
    expect(range).toHaveTextContent("周期 1d");
  });

  it("falls back to the run row range when no summary exists yet (running)", () => {
    const runningRun: RunRow = { ...baseRun, status: "running" };
    render(
      <BacktestSummaryHeader
        task={{ ...baseTask, backtest_summary: null, status: "configured" }}
        run={runningRun}
      />,
    );

    expect(screen.getByTestId("backtest-range")).toHaveTextContent("2026-01-05 ~ 2026-01-06");
  });

  it("shows '运行中' badge with live KPIs when no summary but run is active", () => {
    const runningRun: RunRow = { ...baseRun, status: "running", return_pct: 1.25, ending_equity: 101250 };
    render(
      <BacktestSummaryHeader
        task={{ ...baseTask, backtest_summary: null, status: "configured" }}
        run={runningRun}
      />,
    );

    expect(screen.getByText("运行中")).toBeTruthy();
    expect(screen.getByText("+1.25%")).toBeTruthy();
    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThanOrEqual(3);
  });

  it("shows '已失败' badge when status is error and summary partial", () => {
    const partial: BacktestSummary = {
      ...finalSummary,
      trade_count_closed: 0,
      trade_count_open: 0,
      fills_count: 0,
      win_rate: "0",
      win_rate_sample_size: 0,
      avg_holding_sample_size: 0,
      max_drawdown_pct: "0",
      max_drawdown_peak_at: null,
      max_drawdown_trough_at: null,
      max_drawdown_peak_equity: null,
      max_drawdown_trough_equity: null,
    };
    render(
      <BacktestSummaryHeader
        task={{ ...baseTask, backtest_summary: partial, status: "error", last_error: "boom" }}
        run={{ ...baseRun, status: "failed" }}
      />,
    );

    expect(screen.getByText("已失败")).toBeTruthy();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });

  it("prefers latest run terminal status over stale task status", () => {
    render(
      <BacktestSummaryHeader
        task={{ ...baseTask, backtest_summary: finalSummary, status: "error", last_error: "previous failure" }}
        run={{ ...baseRun, status: "completed" }}
      />,
    );

    expect(screen.getAllByText("已完成").length).toBeGreaterThanOrEqual(1);
  });

  it("renders fills_count for 交易次数 and mark-to-market win_rate (no closed FIFO trades)", () => {
    const buyOnly: BacktestSummary = {
      ...finalSummary,
      trade_count_closed: 0,
      trade_count_open: 1,
      fills_count: 1,
      win_rate: "1",
      win_rate_sample_size: 1,
      avg_holding_trading_days: "3",
      avg_holding_sample_size: 1,
      return_pct: "156.06",
      ending_equity: "256058.59",
    };
    render(
      <BacktestSummaryHeader
        task={{ ...baseTask, backtest_summary: buyOnly, status: "completed" }}
        run={{ ...baseRun, status: "completed" }}
      />,
    );
    // 交易次数 should show total fills (1), not "0 / 1".
    expect(screen.getByText("1")).toBeTruthy();
    // 胜率 should compute from mtm sample (1/1 = 100%).
    expect(screen.getByText("100.00%")).toBeTruthy();
  });

  it("collapses to '回测失败' card when error and no summary", () => {
    render(
      <BacktestSummaryHeader
        task={{ ...baseTask, backtest_summary: null, status: "error", last_error: "early failure" }}
        run={{ ...baseRun, status: "failed", error_message: "early failure" }}
      />,
    );
    expect(screen.getByText("回测失败")).toBeTruthy();
    expect(screen.getByText("early failure")).toBeTruthy();
  });
});
