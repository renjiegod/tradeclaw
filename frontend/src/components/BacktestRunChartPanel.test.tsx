import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { BacktestRunChartPanel } from "./BacktestRunChartPanel";
import { getTaskRunChart } from "../api";
import type { BacktestChartSnapshot } from "../types";

vi.mock("../api", () => ({
  getTaskRunChart: vi.fn(),
  // Consumed by the useSymbolNames hook in the symbol dropdown.
  getInstrumentCatalogItem: vi.fn(async (symbol: string) => ({
    symbol,
    display_name: null,
  })),
}));

const klineMock = vi.hoisted(() => {
  type ChartMock = {
    applyNewData: ReturnType<typeof vi.fn>;
    subscribeAction: ReturnType<typeof vi.fn>;
    unsubscribeAction: ReturnType<typeof vi.fn>;
    createIndicator: ReturnType<typeof vi.fn>;
    removeIndicator: ReturnType<typeof vi.fn>;
    setStyles: ReturnType<typeof vi.fn>;
    setOffsetRightDistance: ReturnType<typeof vi.fn>;
    setBarSpace: ReturnType<typeof vi.fn>;
    getBarSpace: ReturnType<typeof vi.fn>;
    scrollToTimestamp: ReturnType<typeof vi.fn>;
    scrollToDataIndex: ReturnType<typeof vi.fn>;
    convertFromPixel: ReturnType<typeof vi.fn>;
    convertToPixel: ReturnType<typeof vi.fn>;
    resize: ReturnType<typeof vi.fn>;
    dispose: ReturnType<typeof vi.fn>;
  };

  const charts: ChartMock[] = [];

  const makeChart = (): ChartMock => {
    const actionHandlers: Record<string, Array<() => void>> = {
      onDataReady: [],
      onScroll: [],
      onZoom: [],
    };
    const chart: ChartMock = {
      subscribeAction: vi.fn((type: string, cb: () => void) => {
        if (actionHandlers[type]) actionHandlers[type].push(cb);
      }),
      unsubscribeAction: vi.fn((type: string, cb: () => void) => {
        const list = actionHandlers[type];
        if (!list) return;
        const i = list.indexOf(cb);
        if (i >= 0) list.splice(i, 1);
      }),
      applyNewData: vi.fn(() => {
        [...actionHandlers.onDataReady].forEach((h) => h());
      }),
      createIndicator: vi.fn((nameOrCfg: unknown, _isStack?: unknown, options?: { id?: string; height?: number }) => {
        return options?.id ?? `auto-pane-${charts.length}-${Math.random().toString(36).slice(2, 7)}`;
      }),
      removeIndicator: vi.fn(),
      setStyles: vi.fn(),
      setOffsetRightDistance: vi.fn(),
      setBarSpace: vi.fn(),
      getBarSpace: vi.fn(() => ({ bar: 8, halfBar: 4, halfGapBar: 0 })),
      scrollToTimestamp: vi.fn(),
      scrollToDataIndex: vi.fn(),
      convertFromPixel: vi.fn(() => ({ dataIndex: 0 })),
      convertToPixel: vi.fn((point: { timestamp?: number; value?: number }) => {
        const v = typeof point?.value === "number" ? point.value : 0;
        return { x: 100, y: 200 - v };
      }),
      resize: vi.fn(),
      dispose: vi.fn(),
    };
    charts.push(chart);
    return chart;
  };

  const init = vi.fn(() => makeChart());
  const dispose = vi.fn();
  const registerStyles = vi.fn();

  return {
    charts,
    reset() {
      charts.length = 0;
      init.mockClear();
      dispose.mockClear();
      registerStyles.mockClear();
    },
    module: { init, dispose, registerStyles },
  };
});

vi.mock("klinecharts", () => ({
  __esModule: true,
  default: klineMock.module,
  init: klineMock.module.init,
  dispose: klineMock.module.dispose,
  registerStyles: klineMock.module.registerStyles,
}));

function buildSnapshot(overrides: Partial<BacktestChartSnapshot> = {}): BacktestChartSnapshot {
  return {
    run: {
      run_id: "run-1",
      task_id: "task-1",
      status: "completed",
      market_profile: "cn_a_share",
      bar_interval: "1d",
      range_start_utc: "2026-01-01T00:00:00",
      range_end_utc: "2026-01-03T00:00:00",
      session_id: "sess-1",
      starting_equity: 100000,
      ending_equity: 101000,
      return_pct: 1,
      error_message: null,
      bars_total: 2,
      bars_completed: 2,
      created_at: "2026-01-01T00:00:00",
      started_at: "2026-01-01T00:00:01",
      finished_at: "2026-01-01T00:00:02",
    },
    symbols: ["600000.SH", "601318.SH"],
    selected_symbol: "600000.SH",
    adjust: "qfq",
    bars: [
      { timestamp: "2026-01-02T00:00:00", open: 10, high: 11, low: 9.5, close: 10.5, volume: 1000, amount: null },
      { timestamp: "2026-01-03T00:00:00", open: 10.5, high: 12, low: 10, close: 11.8, volume: 1500, amount: null },
    ],
    volume_mode: "volume_only",
    trades: [],
    warnings: [],
    ...overrides,
  };
}

const sampleTrade = {
  timestamp: "2026-01-02T00:00:00",
  side: "buy" as const,
  price: 10.5,
  quantity: 100,
  intent_id: "intent-1",
  rationale: "definition-instance-graph.macd.golden_cross",
  cycle_run_id: "cycle-1",
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
  Object.defineProperty(window, "ResizeObserver", {
    writable: true,
    value: vi.fn().mockImplementation(() => ({
      observe: vi.fn(),
      unobserve: vi.fn(),
      disconnect: vi.fn(),
    })),
  });
});

beforeEach(() => {
  vi.clearAllMocks();
  klineMock.reset();
});

afterEach(() => {
  cleanup();
});

describe("BacktestRunChartPanel — empty / loading / error states", () => {
  it("shows empty state when selectedRunId is null", () => {
    render(<BacktestRunChartPanel taskId="task-1" selectedRunId={null} />);
    expect(screen.getByText("暂无回测，先发起一次回测")).toBeInTheDocument();
    expect(getTaskRunChart).not.toHaveBeenCalled();
  });

  it("shows loading skeleton while fetching and disables controls", async () => {
    let resolveFetch: ((value: BacktestChartSnapshot) => void) | undefined;
    vi.mocked(getTaskRunChart).mockImplementationOnce(
      () =>
        new Promise<BacktestChartSnapshot>((resolve) => {
          resolveFetch = resolve;
        }),
    );

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);

    expect(await screen.findByTestId("backtest-chart-skeleton")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /刷\s*新/ })).toBeDisabled();

    await act(async () => {
      resolveFetch?.(buildSnapshot());
    });
    await waitFor(() => expect(screen.queryByTestId("backtest-chart-skeleton")).not.toBeInTheDocument());
  });

  it("shows error state with retry button when API rejects", async () => {
    vi.mocked(getTaskRunChart).mockRejectedValueOnce(new Error("upstream broke"));
    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);

    expect(await screen.findByText(/加载回测图表失败/)).toBeInTheDocument();
    expect(screen.getByText(/upstream broke/)).toBeInTheDocument();

    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot());
    fireEvent.click(screen.getByRole("button", { name: /重\s*试/ }));
    await waitFor(() => expect(getTaskRunChart).toHaveBeenCalledTimes(2));
  });
});

describe("BacktestRunChartPanel — successful render and chart wiring", () => {
  it("loads snapshot, applies bars, and creates default indicators (MA + VOL + MACD) on the right panes", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ trades: [sampleTrade] }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const chart = klineMock.charts[0];

    expect(getTaskRunChart).toHaveBeenCalledWith("task-1", "run-1", { symbol: undefined });
    expect(klineMock.module.init).toHaveBeenCalledTimes(1);

    expect(chart.applyNewData).toHaveBeenCalledTimes(1);
    const [bars, more] = chart.applyNewData.mock.calls[0];
    expect((bars as Array<{ close: number }>).length).toBe(2);
    expect((bars as Array<{ close: number }>)[0].close).toBe(10.5);
    expect(more).toBe(false);

    const indicatorCalls = chart.createIndicator.mock.calls;
    const namesCreated = indicatorCalls.map((call) => {
      const arg = call[0] as string | { name: string };
      return typeof arg === "string" ? arg : arg.name;
    });
    expect(namesCreated).toContain("MA");
    expect(namesCreated).toContain("VOL");
    expect(namesCreated).toContain("MACD");

    const maCall = indicatorCalls.find((call) => {
      const arg = call[0] as string | { name: string };
      return (typeof arg === "string" ? arg : arg.name) === "MA";
    })!;
    expect((maCall[2] as { id?: string })?.id).toBe("candle_pane");

    const tradeKey = `${sampleTrade.cycle_run_id}:${sampleTrade.intent_id}:${sampleTrade.timestamp}`;
    await waitFor(() => expect(screen.getByTestId(`trade-marker-${tradeKey}`)).toBeInTheDocument());
    expect(chart.convertToPixel).toHaveBeenCalled();
    expect(chart.scrollToTimestamp).toHaveBeenCalledWith(Date.parse("2026-01-02T00:00:00"), 0);
    expect(screen.getByTestId("backtest-chart-adjust")).toHaveTextContent("前复权");
  });

  it("renders HTML trade markers after data ready without kline registerOverlay", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValue(buildSnapshot({ trades: [sampleTrade] }));

    const { rerender } = render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));

    const tradeKey = `${sampleTrade.cycle_run_id}:${sampleTrade.intent_id}:${sampleTrade.timestamp}`;
    await waitFor(() => expect(screen.getByTestId(`trade-marker-${tradeKey}`)).toBeInTheDocument());

    rerender(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(screen.getByTestId(`trade-marker-${tradeKey}`)).toBeInTheDocument());
  });

  it("renders backtest-window start/end guides snapped to in-window bars and a range caption", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ trades: [] }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const chart = klineMock.charts[0];

    // Window is 2026-01-01 → 2026-01-03; bars are the 02 and 03 trading days,
    // so the start guide snaps to the 02 bar and the end guide to the 03 bar.
    await waitFor(() => expect(screen.getByTestId("backtest-range-marker-start")).toBeInTheDocument());
    expect(screen.getByTestId("backtest-range-marker-end")).toBeInTheDocument();
    expect(screen.getByTestId("backtest-range-marker-start")).toHaveTextContent("回测开始");
    expect(screen.getByTestId("backtest-range-marker-end")).toHaveTextContent("回测结束");

    const startMs = Date.parse("2026-01-02T00:00:00");
    const endMs = Date.parse("2026-01-03T00:00:00");
    const tsCalls = chart.convertToPixel.mock.calls.map((call) => (call[0] as { timestamp?: number }).timestamp);
    expect(tsCalls).toContain(startMs);
    expect(tsCalls).toContain(endMs);

    expect(screen.getByTestId("backtest-chart-range")).toHaveTextContent("2026-01-01 ~ 2026-01-03");
  });

  it("renders 暂无 K 线数据 placeholder and does not call applyNewData when bars is empty", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ bars: [], trades: [] }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    expect(await screen.findByText("暂无 K 线数据")).toBeInTheDocument();
    expect(klineMock.charts.length).toBe(0);
  });

  it("renders 暂无成交 placeholder when trades is empty and creates no HTML trade markers", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ trades: [] }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const chart = klineMock.charts[0];
    expect(chart.scrollToDataIndex).toHaveBeenCalledWith(buildSnapshot().bars.length - 1);
    expect(await screen.findByText("暂无成交")).toBeInTheDocument();
    expect(document.querySelectorAll('[data-testid^="trade-marker-"]')).toHaveLength(0);
  });

  it("snaps trade overlay to the bar trading day when fill time-of-day differs (daily bars)", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(
      buildSnapshot({
        bars: [
          { timestamp: "2026-01-02", open: 10, high: 11, low: 9.5, close: 10.5, volume: 1000, amount: null },
          { timestamp: "2026-01-03", open: 10.5, high: 12, low: 10, close: 11.8, volume: 1500, amount: null },
        ],
        trades: [
          {
            timestamp: "2026-01-02T08:30:00",
            side: "buy" as const,
            price: 10.2,
            quantity: 100,
            intent_id: "i-day",
            cycle_run_id: "c-day",
          },
        ],
      }),
    );

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const chart = klineMock.charts[0];
    const tradeKey = "c-day:i-day:2026-01-02T08:30:00";
    await waitFor(() => expect(screen.getByTestId(`trade-marker-${tradeKey}`)).toBeInTheDocument());

    const barDayMs = Date.parse("2026-01-02");
    const priceCalls = chart.convertToPixel.mock.calls.filter(
      (call) => (call[0] as { timestamp?: number }).timestamp === barDayMs,
    );
    expect(priceCalls.length).toBeGreaterThanOrEqual(1);
    expect(priceCalls.some((call) => (call[0] as { value?: number }).value === 10.2)).toBe(true);
    expect(priceCalls.some((call) => (call[0] as { value?: number }).value === 11)).toBe(true);
    expect(priceCalls.some((call) => (call[0] as { value?: number }).value === 9.5)).toBe(true);
    expect(chart.scrollToTimestamp).toHaveBeenCalledWith(barDayMs, 0);
  });

  it("renders one overlay per valid trade for same-timestamp multi-fills and only renders valid trade rows", async () => {
    const sameTimestampTrades = [
      {
        timestamp: "2026-04-01T00:00:00",
        side: "buy" as const,
        price: 17.4,
        quantity: 100,
        intent_id: "i1",
        cycle_run_id: "c1",
      },
      {
        timestamp: "2026-04-01T00:00:00",
        side: "buy" as const,
        price: 17.5,
        quantity: 200,
        intent_id: "i2",
        cycle_run_id: "c2",
      },
      {
        timestamp: "2026-04-01T00:00:00",
        side: "sell" as const,
        price: 17.6,
        quantity: 100,
        intent_id: "i3",
        cycle_run_id: "c3",
      },
      {
        timestamp: null,
        side: "buy" as const,
        price: 17.7,
        quantity: 50,
        intent_id: "invalid-no-ts",
        cycle_run_id: "c4",
      },
    ];

    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ trades: sameTimestampTrades }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));

    const expectedValidTradeCount = 3;
    await waitFor(() =>
      expect(document.querySelectorAll('[data-testid^="trade-marker-"]').length).toBe(expectedValidTradeCount),
    );
    expect(screen.queryByText("暂无成交")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByTestId(/^trade-row-/).length).toBe(expectedValidTradeCount));
  });

  it("shows rationale in trade detail rows with fallback", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(
      buildSnapshot({
        trades: [
          sampleTrade,
          {
            ...sampleTrade,
            cycle_run_id: "cycle-2",
            intent_id: "intent-2",
            rationale: null,
          },
        ],
      }),
    );

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    await waitFor(() => expect(screen.getAllByTestId(/^trade-row-/).length).toBe(2));

    expect(screen.getByText("definition-instance-graph.macd.golden_cross")).toBeInTheDocument();
    expect(screen.getByTestId("trade-row-cycle-2:intent-2:2026-01-02T00:00:00")).toHaveTextContent("—");
  });

  it("uses prominent text labels 买入/卖出 for trade overlays and legend", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(
      buildSnapshot({
        trades: [
          sampleTrade,
          {
            ...sampleTrade,
            side: "sell",
            cycle_run_id: "cycle-2",
            intent_id: "intent-2",
          },
        ],
      }),
    );

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    await waitFor(() => expect(document.querySelectorAll('[data-testid^="trade-marker-"]').length).toBe(2));

    const buyMarker = screen.getByTestId(`trade-marker-cycle-1:intent-1:${sampleTrade.timestamp}`);
    const sellMarker = screen.getByTestId("trade-marker-cycle-2:intent-2:2026-01-02T00:00:00");
    expect(buyMarker).toHaveTextContent("买入");
    expect(sellMarker).toHaveTextContent("卖出");

    expect((await screen.findAllByText("买入")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("卖出").length).toBeGreaterThanOrEqual(1);
  });

  it("renders the dark warning strip when snapshot.warnings is non-empty", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(
      buildSnapshot({ warnings: ["数据为部分进度快照"], trades: [] }),
    );
    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    expect(await screen.findByText("数据为部分进度快照")).toBeInTheDocument();
  });

  it("shows running badge and partial-data hint when run.status === 'running'", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(
      buildSnapshot({
        run: { ...buildSnapshot().run, status: "running" },
        trades: [],
      }),
    );
    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    expect(await screen.findByText(/进行中|running/)).toBeInTheDocument();
    expect(screen.getByText(/数据为部分进度快照/)).toBeInTheDocument();
  });
});

describe("BacktestRunChartPanel — indicator switching does not refetch", () => {
  it("switching main indicator (MA → BOLL → 隐藏) only calls removeIndicator/createIndicator on candle_pane and does not refetch or re-applyNewData", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ trades: [] }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const chart = klineMock.charts[0];

    expect(chart.applyNewData).toHaveBeenCalledTimes(1);
    chart.applyNewData.mockClear();
    chart.createIndicator.mockClear();
    chart.removeIndicator.mockClear();
    vi.mocked(getTaskRunChart).mockClear();

    fireEvent.click(screen.getByRole("button", { name: "BOLL" }));
    await waitFor(() => {
      const removed = chart.removeIndicator.mock.calls.some((call) => {
        const arg = call[0] as { paneId?: string };
        return arg?.paneId === "candle_pane";
      });
      expect(removed).toBe(true);
      const createdBoll = chart.createIndicator.mock.calls.some((call) => {
        const nameArg = call[0] as string | { name: string };
        const name = typeof nameArg === "string" ? nameArg : nameArg.name;
        const opts = call[2] as { id?: string } | undefined;
        return name === "BOLL" && opts?.id === "candle_pane";
      });
      expect(createdBoll).toBe(true);
    });

    fireEvent.click(screen.getByRole("button", { name: "隐藏" }));
    await waitFor(() => {
      expect(chart.removeIndicator.mock.calls.length).toBeGreaterThanOrEqual(2);
    });

    expect(chart.applyNewData).not.toHaveBeenCalled();
    expect(getTaskRunChart).not.toHaveBeenCalled();
  });

  it("switching sub indicator (MACD → KDJ → RSI → WR) reuses the same sub pane id", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ trades: [] }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const chart = klineMock.charts[0];

    const macdCall = chart.createIndicator.mock.calls.find((call) => {
      const arg = call[0] as string | { name: string };
      return (typeof arg === "string" ? arg : arg.name) === "MACD";
    });
    expect(macdCall).toBeDefined();
    const subPaneId = (macdCall![2] as { id?: string })?.id;
    expect(subPaneId).toBeDefined();

    chart.createIndicator.mockClear();
    chart.removeIndicator.mockClear();
    vi.mocked(getTaskRunChart).mockClear();

    fireEvent.click(screen.getByRole("button", { name: "KDJ" }));
    await waitFor(() => {
      const removedSub = chart.removeIndicator.mock.calls.some((call) => {
        const arg = call[0] as { paneId?: string };
        return arg?.paneId === subPaneId;
      });
      expect(removedSub).toBe(true);
      const createdKdj = chart.createIndicator.mock.calls.some((call) => {
        const nameArg = call[0] as string | { name: string };
        const name = typeof nameArg === "string" ? nameArg : nameArg.name;
        const opts = call[2] as { id?: string } | undefined;
        return name === "KDJ" && opts?.id === subPaneId;
      });
      expect(createdKdj).toBe(true);
    });

    fireEvent.click(screen.getByRole("button", { name: "RSI" }));
    fireEvent.click(screen.getByRole("button", { name: "WR" }));
    await waitFor(() => {
      const allCreatedSub = chart.createIndicator.mock.calls
        .filter((call) => (call[2] as { id?: string })?.id === subPaneId)
        .map((call) => {
          const arg = call[0] as string | { name: string };
          return typeof arg === "string" ? arg : arg.name;
        });
      expect(allCreatedSub).toContain("RSI");
      expect(allCreatedSub).toContain("WR");
    });

    expect(getTaskRunChart).not.toHaveBeenCalled();
  });
});

describe("BacktestRunChartPanel — symbol change / refresh re-fetches", () => {
  it("changing symbol triggers re-fetch and disposes the previous chart", async () => {
    const first = buildSnapshot({ trades: [] });
    const second = buildSnapshot({
      selected_symbol: "601318.SH",
      bars: first.bars,
      trades: [],
    });
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(first).mockResolvedValueOnce(second);

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const firstChart = klineMock.charts[0];

    fireEvent.change(screen.getByLabelText("股票"), { target: { value: "601318.SH" } });
    await waitFor(() => expect(getTaskRunChart).toHaveBeenCalledTimes(2));
    expect(getTaskRunChart).toHaveBeenLastCalledWith("task-1", "run-1", { symbol: "601318.SH" });

    await waitFor(() => expect(firstChart.dispose).toHaveBeenCalled());
    await waitFor(() => expect(klineMock.charts.length).toBe(2));
  });

  it("clicking refresh re-fetches and re-creates the chart", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValue(buildSnapshot({ trades: [] }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const firstChart = klineMock.charts[0];

    const refreshButton = screen.getByRole("button", { name: /刷\s*新/ });
    await waitFor(() => expect(refreshButton).toBeEnabled());
    fireEvent.click(refreshButton);
    await waitFor(() => expect(getTaskRunChart).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(firstChart.dispose).toHaveBeenCalled());
    await waitFor(() => expect(klineMock.charts.length).toBe(2));
  });
});

describe("BacktestRunChartPanel — trade marker click highlights table row", () => {
  it("clicking a trade marker highlights the matching table row and clears after 3 seconds", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ trades: [sampleTrade] }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));

    const tradeKey = `${sampleTrade.cycle_run_id}:${sampleTrade.intent_id}:${sampleTrade.timestamp}`;
    const marker = await screen.findByTestId(`trade-marker-${tradeKey}`);
    const row = await screen.findByTestId(`trade-row-${tradeKey}`);
    expect(row.className).not.toMatch(/bg-amber-500\/20/);

    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      act(() => {
        fireEvent.click(marker);
      });

      expect(screen.getByTestId(`trade-row-${tradeKey}`).className).toMatch(/bg-amber-500\/20/);

      act(() => {
        vi.advanceTimersByTime(3001);
      });

      expect(screen.getByTestId(`trade-row-${tradeKey}`).className).not.toMatch(/bg-amber-500\/20/);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("BacktestRunChartPanel — zoom toolbar (放大/缩小/复位)", () => {
  const ZOOM_STEP = 1.25;
  const DEFAULT_BAR_SPACE = 8;
  const MAX_BAR_SPACE = 60;
  const MIN_BAR_SPACE = 2;

  const lastSetBarSpaceCall = (chart: { setBarSpace: { mock: { calls: unknown[][] } } }): number | undefined => {
    const calls = chart.setBarSpace.mock.calls;
    if (calls.length === 0) return undefined;
    return calls[calls.length - 1][0] as number;
  };

  it("renders 放大 / 缩小 / 复位 buttons enabled at default barSpace", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ trades: [] }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));

    const zoomIn = screen.getByRole("button", { name: "放大" });
    const zoomOut = screen.getByRole("button", { name: "缩小" });
    const reset = screen.getByRole("button", { name: "复位" });

    expect(zoomIn).toBeEnabled();
    expect(zoomOut).toBeEnabled();
    expect(reset).toBeEnabled();
  });

  it("clicking 放大 calls setBarSpace with current * 1.25", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ trades: [] }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const chart = klineMock.charts[0];
    chart.setBarSpace.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    await waitFor(() => expect(chart.setBarSpace).toHaveBeenCalled());
    expect(lastSetBarSpaceCall(chart)).toBeCloseTo(DEFAULT_BAR_SPACE * ZOOM_STEP, 5);
  });

  it("clicking 缩小 calls setBarSpace with current / 1.25", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ trades: [] }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const chart = klineMock.charts[0];
    chart.setBarSpace.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "缩小" }));
    await waitFor(() => expect(chart.setBarSpace).toHaveBeenCalled());
    expect(lastSetBarSpaceCall(chart)).toBeCloseTo(DEFAULT_BAR_SPACE / ZOOM_STEP, 5);
  });

  it("clicking 复位 sets default barSpace and scrolls to last bar", async () => {
    const snap = buildSnapshot({ trades: [] });
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(snap);

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const chart = klineMock.charts[0];

    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    await waitFor(() => expect(chart.setBarSpace).toHaveBeenCalled());
    chart.setBarSpace.mockClear();
    chart.scrollToDataIndex.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "复位" }));
    await waitFor(() => expect(chart.setBarSpace).toHaveBeenCalledWith(DEFAULT_BAR_SPACE));
    expect(chart.scrollToDataIndex).toHaveBeenCalledWith(snap.bars.length - 1);
  });

  it("clamps at MAX and disables 放大 once the limit is reached", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ trades: [] }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const chart = klineMock.charts[0];

    const zoomIn = screen.getByRole("button", { name: "放大" });
    for (let i = 0; i < 20; i += 1) {
      if ((zoomIn as HTMLButtonElement).disabled) break;
      fireEvent.click(zoomIn);
    }

    await waitFor(() => expect(zoomIn).toBeDisabled());
    expect(lastSetBarSpaceCall(chart)).toBeLessThanOrEqual(MAX_BAR_SPACE);
    expect(lastSetBarSpaceCall(chart)).toBeGreaterThanOrEqual(MAX_BAR_SPACE - 0.0001);

    const callsBefore = chart.setBarSpace.mock.calls.length;
    fireEvent.click(zoomIn);
    expect(chart.setBarSpace.mock.calls.length).toBe(callsBefore);
  });

  it("clamps at MIN and disables 缩小 once the limit is reached", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ trades: [] }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const chart = klineMock.charts[0];

    const zoomOut = screen.getByRole("button", { name: "缩小" });
    for (let i = 0; i < 20; i += 1) {
      if ((zoomOut as HTMLButtonElement).disabled) break;
      fireEvent.click(zoomOut);
    }

    await waitFor(() => expect(zoomOut).toBeDisabled());
    expect(lastSetBarSpaceCall(chart)).toBeGreaterThanOrEqual(MIN_BAR_SPACE);
    expect(lastSetBarSpaceCall(chart)).toBeLessThanOrEqual(MIN_BAR_SPACE + 0.0001);

    const callsBefore = chart.setBarSpace.mock.calls.length;
    fireEvent.click(zoomOut);
    expect(chart.setBarSpace.mock.calls.length).toBe(callsBefore);
  });

  it("indicator switching preserves the current zoom level", async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ trades: [] }));

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const chart = klineMock.charts[0];

    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    await waitFor(() => expect(chart.setBarSpace).toHaveBeenCalled());
    const afterFirst = lastSetBarSpaceCall(chart)!;
    expect(afterFirst).toBeCloseTo(DEFAULT_BAR_SPACE * ZOOM_STEP, 5);

    fireEvent.click(screen.getByRole("button", { name: "BOLL" }));
    fireEvent.click(screen.getByRole("button", { name: "KDJ" }));

    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    await waitFor(() =>
      expect(lastSetBarSpaceCall(chart)).toBeCloseTo(afterFirst * ZOOM_STEP, 5),
    );
  });

  it("zoom level resets to default after symbol change rebuilds the chart", async () => {
    const first = buildSnapshot({ trades: [] });
    const second = buildSnapshot({ selected_symbol: "601318.SH", bars: first.bars, trades: [] });
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(first).mockResolvedValueOnce(second);

    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const firstChart = klineMock.charts[0];

    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    await waitFor(() => expect(firstChart.setBarSpace).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText("股票"), { target: { value: "601318.SH" } });
    await waitFor(() => expect(klineMock.charts.length).toBe(2));
    const secondChart = klineMock.charts[1];
    secondChart.setBarSpace.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "放大" }));
    await waitFor(() => expect(secondChart.setBarSpace).toHaveBeenCalled());
    expect(lastSetBarSpaceCall(secondChart)).toBeCloseTo(DEFAULT_BAR_SPACE * ZOOM_STEP, 5);
  });
});

describe("BacktestRunChartPanel — Shift+drag range zoom", () => {
  const setupChart = async () => {
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(buildSnapshot({ trades: [] }));
    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const chart = klineMock.charts[0];
    chart.setBarSpace.mockClear();
    chart.scrollToDataIndex.mockClear();
    chart.convertFromPixel.mockReset();
    return chart;
  };

  it("Shift+drag selects an X-axis range and zooms centered with context on both sides", async () => {
    const chart = await setupChart();
    chart.convertFromPixel
      .mockReturnValueOnce({ dataIndex: 10 })
      .mockReturnValueOnce({ dataIndex: 60 });

    const container = screen.getByLabelText("回测K线图");
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      right: 800,
      bottom: 400,
      width: 800,
      height: 400,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);
    fireEvent.mouseDown(container, { shiftKey: true, button: 0, clientX: 100 });
    fireEvent.mouseMove(window, { clientX: 200 });
    fireEvent.mouseUp(window, { clientX: 300 });

    await waitFor(() => expect(chart.setBarSpace).toHaveBeenCalled());
    const spaceArg = Number(chart.setBarSpace.mock.calls.at(-1)?.[0]);
    expect(spaceArg).toBeGreaterThanOrEqual(2);
    expect(spaceArg).toBeLessThanOrEqual(60);
    // Selection right edge is dataIndex=60; a scroll target beyond it means the midpoint
    // is centered and neighbouring bars stay visible on the right (no clip to the edge).
    const idxArg = Number(chart.scrollToDataIndex.mock.calls.at(-1)?.[0]);
    expect(idxArg).toBeGreaterThan(60);
  });

  it("plain drag (no Shift) does not call setBarSpace from the panel", async () => {
    const chart = await setupChart();

    const container = screen.getByLabelText("回测K线图");
    fireEvent.mouseDown(container, { shiftKey: false, button: 0, clientX: 100 });
    fireEvent.mouseMove(window, { clientX: 300 });
    fireEvent.mouseUp(window, { clientX: 300 });

    expect(chart.setBarSpace).not.toHaveBeenCalled();
    expect(chart.scrollToDataIndex).not.toHaveBeenCalled();
    expect(chart.convertFromPixel).not.toHaveBeenCalled();
  });

  it("Shift+drag with width below MIN_SELECTION_PIXELS cancels and does not zoom", async () => {
    const chart = await setupChart();

    const container = screen.getByLabelText("回测K线图");
    fireEvent.mouseDown(container, { shiftKey: true, button: 0, clientX: 100 });
    fireEvent.mouseMove(window, { clientX: 102 });
    fireEvent.mouseUp(window, { clientX: 102 });

    expect(chart.convertFromPixel).not.toHaveBeenCalled();
    expect(chart.setBarSpace).not.toHaveBeenCalled();
    expect(chart.scrollToDataIndex).not.toHaveBeenCalled();
  });

  it("Shift+drag resolving to fewer than 2 bars cancels", async () => {
    const chart = await setupChart();
    chart.convertFromPixel
      .mockReturnValueOnce({ dataIndex: 25 })
      .mockReturnValueOnce({ dataIndex: 25 });

    const container = screen.getByLabelText("回测K线图");
    fireEvent.mouseDown(container, { shiftKey: true, button: 0, clientX: 100 });
    fireEvent.mouseMove(window, { clientX: 200 });
    fireEvent.mouseUp(window, { clientX: 200 });

    expect(chart.setBarSpace).not.toHaveBeenCalled();
    expect(chart.scrollToDataIndex).not.toHaveBeenCalled();
  });

  it("pressing Escape during a Shift+drag cancels and the subsequent mouseup does not zoom", async () => {
    const chart = await setupChart();

    const container = screen.getByLabelText("回测K线图");
    fireEvent.mouseDown(container, { shiftKey: true, button: 0, clientX: 100 });
    fireEvent.mouseMove(window, { clientX: 200 });
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.mouseUp(window, { clientX: 300 });

    expect(chart.setBarSpace).not.toHaveBeenCalled();
    expect(chart.scrollToDataIndex).not.toHaveBeenCalled();
  });

  it("the selection rect is hidden after a successful range zoom", async () => {
    const chart = await setupChart();
    chart.convertFromPixel
      .mockReturnValueOnce({ dataIndex: 10 })
      .mockReturnValueOnce({ dataIndex: 60 });

    const container = screen.getByLabelText("回测K线图");
    fireEvent.mouseDown(container, { shiftKey: true, button: 0, clientX: 100 });
    fireEvent.mouseMove(window, { clientX: 200 });

    const rect = screen.getByTestId("zoom-selection-rect");
    expect(rect.style.display).not.toBe("none");

    fireEvent.mouseUp(window, { clientX: 300 });

    await waitFor(() => expect(chart.setBarSpace).toHaveBeenCalled());
    expect(screen.getByTestId("zoom-selection-rect").style.display).toBe("none");
  });

  it("intercepts Shift+mousedown in the capture phase so the inner chart element never starts a pan", async () => {
    await setupChart();
    const container = screen.getByLabelText("回测K线图");
    // klinecharts binds its mousedown on an inner div it appends inside the container;
    // model that with a child + bubble listener and assert capture-phase stopPropagation.
    const inner = document.createElement("div");
    container.appendChild(inner);
    const innerMouseDown = vi.fn();
    inner.addEventListener("mousedown", innerMouseDown);

    try {
      // Shift: intercepted in capture phase, event never reaches the inner element (no pan).
      fireEvent.mouseDown(inner, { shiftKey: true, button: 0, clientX: 100 });
      expect(innerMouseDown).not.toHaveBeenCalled();
      fireEvent.mouseUp(window, { clientX: 100 });

      // No Shift: not intercepted, event reaches the inner element (native pan preserved).
      innerMouseDown.mockClear();
      fireEvent.mouseDown(inner, { shiftKey: false, button: 0, clientX: 100 });
      expect(innerMouseDown).toHaveBeenCalledTimes(1);
      fireEvent.mouseUp(window, { clientX: 100 });
    } finally {
      inner.removeEventListener("mousedown", innerMouseDown);
      container.removeChild(inner);
    }
  });

  it("detaches an in-flight drag's window listeners when the snapshot rebuilds (symbol change) mid-drag", async () => {
    const first = buildSnapshot({ trades: [] });
    const second = buildSnapshot({ selected_symbol: "601318.SH", bars: first.bars, trades: [] });
    vi.mocked(getTaskRunChart).mockResolvedValueOnce(first).mockResolvedValueOnce(second);
    render(<BacktestRunChartPanel taskId="task-1" selectedRunId="run-1" />);
    await waitFor(() => expect(klineMock.charts.length).toBe(1));

    const container = screen.getByLabelText("回测K线图");
    const addSpy = vi.spyOn(window, "addEventListener");
    const removeSpy = vi.spyOn(window, "removeEventListener");
    addSpy.mockClear();
    fireEvent.mouseDown(container, { shiftKey: true, button: 0, clientX: 100 });
    fireEvent.mouseMove(window, { clientX: 200 });

    const moveHandler = addSpy.mock.calls.find((c) => c[0] === "mousemove")?.[1];
    const upHandler = addSpy.mock.calls.find((c) => c[0] === "mouseup")?.[1];
    expect(typeof moveHandler).toBe("function");
    expect(typeof upHandler).toBe("function");
    removeSpy.mockClear();

    // Switch symbol mid-drag → panel sets snapshot null then rebuilds; effect cleanup
    // must detach the in-flight drag's window listeners.
    fireEvent.change(screen.getByLabelText("股票"), { target: { value: "601318.SH" } });

    await waitFor(() => expect(removeSpy).toHaveBeenCalledWith("mousemove", moveHandler));
    expect(removeSpy).toHaveBeenCalledWith("mouseup", upHandler);

    addSpy.mockRestore();
    removeSpy.mockRestore();
  });
});
