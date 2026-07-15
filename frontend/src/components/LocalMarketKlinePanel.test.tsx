import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LocalMarketKlinePanel } from "./LocalMarketKlinePanel";
import {
  getLocalMarketBars,
  getLocalMarketOverlays,
  getLocalMarketSyncJob,
  syncLocalMarketBarsRange,
} from "../api";
import type { LocalMarketBarsSnapshot } from "../types";

vi.mock("../api", () => ({
  getLocalMarketBars: vi.fn(),
  getLocalMarketOverlays: vi.fn(),
  getLocalMarketSyncJob: vi.fn(),
  syncLocalMarketBarsRange: vi.fn(),
}));

const klineMock = vi.hoisted(() => {
  const charts: Array<Record<string, ReturnType<typeof vi.fn>>> = [];
  const init = vi.fn(() => {
    const chart = {
      applyNewData: vi.fn(),
      createIndicator: vi.fn(),
      removeIndicator: vi.fn(),
      setBarSpace: vi.fn(),
      setTimezone: vi.fn(),
      setLoadDataCallback: vi.fn(),
      scrollToRealTime: vi.fn(),
      getDataList: vi.fn(() => []),
      subscribeAction: vi.fn(),
      unsubscribeAction: vi.fn(),
      convertToPixel: vi.fn((point: { value?: number }) => ({ x: 100, y: 200 - Number(point?.value ?? 0) * 10 })),
      convertFromPixel: vi.fn(() => ({ dataIndex: 0 })),
      scrollToDataIndex: vi.fn(),
      dispose: vi.fn(),
    };
    charts.push(chart);
    return chart;
  });
  return {
    charts,
    init,
    registerStyles: vi.fn(),
    dispose: vi.fn(),
    reset() {
      charts.length = 0;
      init.mockClear();
    },
  };
});

vi.mock("klinecharts", () => ({
  __esModule: true,
  default: klineMock,
  init: klineMock.init,
  dispose: klineMock.dispose,
  registerStyles: klineMock.registerStyles,
}));

async function chooseInterval(optionName: string) {
  fireEvent.mouseDown(screen.getAllByRole("combobox")[0]);
  await screen.findByRole("option", { name: optionName });
  const optionContent = screen.getAllByText(optionName).at(-1);
  if (!optionContent) {
    throw new Error(`Option ${optionName} was not rendered`);
  }
  fireEvent.click(optionContent);
}

function buildSnapshot(overrides: Partial<LocalMarketBarsSnapshot> = {}): LocalMarketBarsSnapshot {
  return {
    symbol: "600000.SH",
    interval: "1d",
    provider: "auto",
    adjust: "qfq",
    start: "2026-01-01",
    end: "2026-01-31",
    bars: [
      { timestamp: "2026-01-02T00:00:00", open: 10, high: 11, low: 9.8, close: 10.2, volume: 1000, amount: 10200 },
      { timestamp: "2026-01-03T00:00:00", open: 10.2, high: 11.2, low: 10, close: 10.9, volume: 1200, amount: 13080 },
    ],
    volume_mode: "amount_available",
    summary: {
      bar_count: 2,
      latest_close: 10.9,
      window_change: 0.9,
      window_change_pct: 0.09,
      window_high: 11.2,
      window_low: 9.8,
      amplitude_pct: 0.142857,
      total_volume: 2200,
      total_amount: 23280,
    },
    coverage: {
      requested_start: "2026-01-01",
      requested_end: "2026-01-31",
      covered_segments: [{ start: "2026-01-02", end: "2026-01-03", status: "covered" }],
      missing_segments: [],
    },
    available_overlays: {
      backtest_trades: [{ id: "run-1", run_id: "run-1", label: "demo backtest · run-1" }],
      task_fills: [{ id: "task-1", task_id: "task-1", label: "demo task" }],
      signals: [{ id: "task-1", task_id: "task-1", label: "demo task" }],
    },
    sync_state: null,
    warnings: [],
    ...overrides,
  };
}

describe("LocalMarketKlinePanel", () => {
  beforeEach(() => {
    klineMock.reset();
    vi.mocked(getLocalMarketOverlays).mockResolvedValue({
      overlay_kind: "backtest_trades",
      source: { id: "run-1", label: "demo source" },
      items: [],
      warnings: [],
    });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  const isoTimestampPattern = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;

  it("renders summary metrics from the local market snapshot", async () => {
    vi.mocked(getLocalMarketBars).mockResolvedValue(buildSnapshot());

    render(<LocalMarketKlinePanel symbol="600000.SH" />);

    expect(await screen.findByText("区间摘要")).toBeInTheDocument();
    expect(screen.getByText("前复权")).toBeInTheDocument();
    expect(screen.getByText("最新价")).toBeInTheDocument();
    await waitFor(() => expect(document.body.textContent).toContain("10.9"));
    await waitFor(() => expect(document.body.textContent).toContain("0.9"));
  });

  it("submits fill-gap sync and polls an async job to completion", async () => {
    vi.mocked(getLocalMarketBars).mockResolvedValue(buildSnapshot());
    vi.mocked(syncLocalMarketBarsRange).mockResolvedValue({
      status: "accepted",
      execution_mode: "async",
      job_id: "job-1",
      mode: "fill_gap",
      requested_range: { start: "2026-01-01", end: "2026-01-31" },
      warnings: [],
    });
    vi.mocked(getLocalMarketSyncJob)
      .mockResolvedValueOnce({
        job_id: "job-1",
        status: "running",
        mode: "fill_gap",
        symbol: "600000.SH",
        interval: "1d",
        provider: "auto",
        adjust: "qfq",
        requested_range: { start: "2026-01-01", end: "2026-01-31" },
        fetched_segments: [],
        upserted_count: 0,
        started_at: null,
        finished_at: null,
        error_code: null,
        error_type: null,
        error_message: null,
        hint: null,
      })
      .mockResolvedValueOnce({
        job_id: "job-1",
        status: "ok",
        mode: "fill_gap",
        symbol: "600000.SH",
        interval: "1d",
        provider: "auto",
        adjust: "qfq",
        requested_range: { start: "2026-01-01", end: "2026-01-31" },
        fetched_segments: [{ start: "2026-01-02", end: "2026-01-02", status: "fetched" }],
        upserted_count: 42,
        started_at: null,
        finished_at: null,
        error_code: null,
        error_type: null,
        error_message: null,
        hint: null,
      });

    render(<LocalMarketKlinePanel symbol="600000.SH" />);
    fireEvent.click((await screen.findAllByRole("button", { name: "补缺口" }))[0]);

    await waitFor(() => expect(getLocalMarketSyncJob).toHaveBeenCalledWith("job-1"));
    await waitFor(() => expect(getLocalMarketSyncJob).toHaveBeenCalledTimes(2), { timeout: 4000 });
    await waitFor(() => expect(screen.getByText("同步完成，写入 42 条")).toBeInTheDocument(), { timeout: 4000 });
  }, 10000);

  it("shows the adjust-drift full-refresh message when an inline sync reports it", async () => {
    vi.mocked(getLocalMarketBars).mockResolvedValue(buildSnapshot());
    vi.mocked(syncLocalMarketBarsRange).mockResolvedValue({
      status: "ok",
      execution_mode: "sync",
      mode: "fill_gap",
      requested_range: { start: "2026-01-01", end: "2026-01-31" },
      fetched_segments: [{ start: "2025-12-01", end: "2026-01-31", status: "fetched" }],
      upserted_count: 120,
      adjust_drift_refreshed: true,
      warnings: ["检测到复权因子变化（除权/除息），已自动全量重刷本地K线缓存"],
    });

    render(<LocalMarketKlinePanel symbol="600000.SH" />);
    fireEvent.click((await screen.findAllByRole("button", { name: "补缺口" }))[0]);

    await waitFor(() =>
      expect(screen.getByText("检测到除权，已自动全量重刷，写入 120 条")).toBeInTheDocument(),
    );
  });

  it("surfaces auto-sync failure details and exposes manual sync actions", async () => {
    vi.mocked(getLocalMarketBars).mockResolvedValue(
      buildSnapshot({
        symbol: "000636.SZ",
        sync_state: {
          symbol: "000636.SZ",
          interval: "1d",
          provider: "auto",
          adjust: "qfq",
          target_start: "2016-06-15T00:00:00+00:00",
          target_end: "2026-06-15T23:59:59.999999+00:00",
          covered_start: null,
          covered_end: null,
          last_success_at: null,
          last_attempt_at: "2026-06-15T14:58:37.382187+00:00",
          last_error_code: "market_data_sync_insufficient_coverage",
          last_error_type: "ValueError",
          last_error_message: "upstream returned insufficient bars for requested sync window",
          retry_count: 104,
          status: "failed",
        },
      }),
    );
    vi.mocked(syncLocalMarketBarsRange).mockResolvedValue({
      status: "ok",
      execution_mode: "sync",
      mode: "fill_gap",
      requested_range: { start: "2026-05-16", end: "2026-06-15" },
      fetched_segments: [{ start: "2026-05-16", end: "2026-06-15", status: "fetched" }],
      upserted_count: 384,
      warnings: [],
    });

    render(<LocalMarketKlinePanel symbol="000636.SZ" />);

    expect(await screen.findByText("本地 K 线自动同步失败")).toBeInTheDocument();
    expect(screen.getByText(/错误码: market_data_sync_insufficient_coverage/)).toBeInTheDocument();
    expect(screen.getByText(/已重试 104 次/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "立即补缺口" }));

    await waitFor(() =>
      expect(syncLocalMarketBarsRange).toHaveBeenCalledWith(
        expect.objectContaining({
          symbol: "000636.SZ",
          interval: "1d",
          mode: "fill_gap",
          adjust: "qfq",
        }),
      ),
    );
  });

  it("uses timezone-aware ISO bounds for 5m reads and manual sync", async () => {
    vi.mocked(getLocalMarketBars)
      .mockResolvedValueOnce(buildSnapshot({ symbol: "000636.SZ" }))
      .mockResolvedValueOnce(
        buildSnapshot({
          symbol: "000636.SZ",
          interval: "5m",
          sync_state: {
            symbol: "000636.SZ",
            interval: "5m",
            provider: "auto",
            adjust: "qfq",
            target_start: "2026-05-16T00:00:00+00:00",
            target_end: "2026-06-15T23:59:59.999999+00:00",
            covered_start: null,
            covered_end: null,
            last_success_at: null,
            last_attempt_at: "2026-06-15T14:58:37.382187+00:00",
            last_error_code: "market_data_bound_invalid",
            last_error_type: "ValueError",
            last_error_message: "timezone-aware ISO bounds are required for 5m bars",
            retry_count: 1,
            status: "failed",
          },
        }),
      );
    vi.mocked(syncLocalMarketBarsRange).mockResolvedValue({
      status: "ok",
      execution_mode: "sync",
      mode: "fill_gap",
      requested_range: {
        start: "2026-05-16T00:00:00.000Z",
        end: "2026-06-15T23:59:59.999Z",
      },
      fetched_segments: [
        {
          start: "2026-05-16T00:00:00.000Z",
          end: "2026-06-15T23:59:59.999Z",
          status: "fetched",
        },
      ],
      upserted_count: 384,
      warnings: [],
    });

    render(<LocalMarketKlinePanel symbol="000636.SZ" />);
    await chooseInterval("5 分钟");

    await waitFor(() => expect(getLocalMarketBars).toHaveBeenCalledTimes(2));
    const intradayCall = vi.mocked(getLocalMarketBars).mock.calls.at(-1)?.[0];
    expect(intradayCall).toMatchObject({
      symbol: "000636.SZ",
      interval: "5m",
      provider: "auto",
      adjust: "qfq",
    });
    expect(intradayCall?.start).toMatch(isoTimestampPattern);
    expect(intradayCall?.end).toMatch(isoTimestampPattern);

    expect(await screen.findByText("本地 K 线自动同步失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "立即补缺口" }));

    await waitFor(() =>
      expect(syncLocalMarketBarsRange).toHaveBeenCalledWith(
        expect.objectContaining({
          symbol: "000636.SZ",
          interval: "5m",
          provider: "auto",
          adjust: "qfq",
          mode: "fill_gap",
          start: expect.stringMatching(isoTimestampPattern),
          end: expect.stringMatching(isoTimestampPattern),
        }),
      ),
    );
  });

  it("anchors overlay markers to the candle range when overlay price is outside the adjusted bar", async () => {
    vi.mocked(getLocalMarketBars).mockResolvedValue(
      buildSnapshot({
        available_overlays: {
          backtest_trades: [{ id: "run-1", run_id: "run-1", label: "demo backtest · run-1" }],
          task_fills: [],
          signals: [],
        },
      }),
    );
    vi.mocked(getLocalMarketOverlays).mockResolvedValue({
      overlay_kind: "backtest_trades",
      source: { id: "run-1", label: "demo source" },
      items: [
        {
          timestamp: "2026-01-03T07:00:00",
          kind: "trade_fill",
          side: "buy",
          price: 1,
          label: "BUY",
          details: {},
        },
      ],
      warnings: [],
    });

    render(<LocalMarketKlinePanel symbol="600000.SH" />);

    await waitFor(() => expect(klineMock.charts.length).toBe(1));
    const chart = klineMock.charts[0];
    await waitFor(() => expect(screen.getByText("BUY")).toBeInTheDocument());
    expect(chart.convertToPixel).toHaveBeenCalledWith(
      expect.objectContaining({
        timestamp: Date.parse("2026-01-03T00:00:00Z"),
        value: 10,
      }),
      expect.objectContaining({ paneId: "candle_pane", absolute: true }),
    );
    expect(screen.getByText("BUY")).toHaveStyle({ top: "108px" });
  });

  it("places sell markers above the candle instead of overlapping it", async () => {
    vi.mocked(getLocalMarketBars).mockResolvedValue(
      buildSnapshot({
        available_overlays: {
          backtest_trades: [{ id: "run-1", run_id: "run-1", label: "demo backtest · run-1" }],
          task_fills: [],
          signals: [],
        },
      }),
    );
    vi.mocked(getLocalMarketOverlays).mockResolvedValue({
      overlay_kind: "backtest_trades",
      source: { id: "run-1", label: "demo source" },
      items: [
        {
          timestamp: "2026-01-03T07:00:00",
          kind: "trade_fill",
          side: "sell",
          price: 20,
          label: "SELL",
          details: {},
        },
      ],
      warnings: [],
    });

    render(<LocalMarketKlinePanel symbol="600000.SH" />);

    await waitFor(() => expect(screen.getByText("SELL")).toBeInTheDocument());
    expect(screen.getByText("SELL")).toHaveStyle({ top: "58px" });
  });

  describe("Shift+drag 框选放大", () => {
    const setup = async () => {
      vi.mocked(getLocalMarketBars).mockResolvedValue(buildSnapshot());
      render(<LocalMarketKlinePanel symbol="600000.SH" />);
      await waitFor(() => expect(klineMock.charts.length).toBe(1));
      const chart = klineMock.charts[0];
      chart.setBarSpace.mockClear();
      chart.scrollToDataIndex.mockClear();
      chart.scrollToRealTime.mockClear();
      chart.convertFromPixel.mockReset();
      return chart;
    };

    it("Shift+拖拽框选区间后放大并居中，选区两侧保留上下文", async () => {
      const chart = await setup();
      chart.convertFromPixel.mockReturnValueOnce({ dataIndex: 10 }).mockReturnValueOnce({ dataIndex: 60 });

      const container = screen.getByLabelText("本地 K 线图");
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
      expect(screen.getByTestId("zoom-selection-rect").style.display).not.toBe("none");

      fireEvent.mouseUp(window, { clientX: 300 });

      // 放大发生：每根 bar 宽度落在允许范围内。
      await waitFor(() => expect(chart.setBarSpace).toHaveBeenCalled());
      const spaceArg = Number(chart.setBarSpace.mock.calls.at(-1)?.[0]);
      expect(spaceArg).toBeGreaterThanOrEqual(2);
      expect(spaceArg).toBeLessThanOrEqual(60);

      // 居中 + 右侧留白：scrollToDataIndex 落点在选区右端(dataIndex=60)之外，说明选区
      // 中点被放到屏幕中央、右边也展示了一部分相邻 K 线（不再像旧逻辑把中点怼到右边界外）。
      const idxArg = Number(chart.scrollToDataIndex.mock.calls.at(-1)?.[0]);
      expect(idxArg).toBeGreaterThan(60);

      expect(screen.getByTestId("zoom-selection-rect").style.display).toBe("none");
    });

    it("普通拖拽（无 Shift）不框选放大，保留 klinecharts 平移手势", async () => {
      const chart = await setup();

      const container = screen.getByLabelText("本地 K 线图");
      fireEvent.mouseDown(container, { shiftKey: false, button: 0, clientX: 100 });
      fireEvent.mouseMove(window, { clientX: 300 });
      fireEvent.mouseUp(window, { clientX: 300 });

      expect(chart.convertFromPixel).not.toHaveBeenCalled();
      expect(chart.setBarSpace).not.toHaveBeenCalled();
      expect(chart.scrollToDataIndex).not.toHaveBeenCalled();
      // The gesture was never hijacked: the selection rect stays hidden.
      expect(screen.getByTestId("zoom-selection-rect").style.display).toBe("none");
    });

    it("拖拽宽度低于阈值时取消，不放大", async () => {
      const chart = await setup();

      const container = screen.getByLabelText("本地 K 线图");
      fireEvent.mouseDown(container, { shiftKey: true, button: 0, clientX: 100 });
      fireEvent.mouseMove(window, { clientX: 102 });
      fireEvent.mouseUp(window, { clientX: 102 });

      expect(chart.convertFromPixel).not.toHaveBeenCalled();
      expect(chart.setBarSpace).not.toHaveBeenCalled();
      expect(chart.scrollToDataIndex).not.toHaveBeenCalled();
    });

    it("按 Escape 取消进行中的框选，后续 mouseup 不放大", async () => {
      const chart = await setup();

      const container = screen.getByLabelText("本地 K 线图");
      fireEvent.mouseDown(container, { shiftKey: true, button: 0, clientX: 100 });
      fireEvent.mouseMove(window, { clientX: 200 });
      fireEvent.keyDown(window, { key: "Escape" });
      fireEvent.mouseUp(window, { clientX: 300 });

      expect(chart.setBarSpace).not.toHaveBeenCalled();
      expect(chart.scrollToDataIndex).not.toHaveBeenCalled();
    });

    it("点击复位恢复默认 barSpace 并回到最新", async () => {
      const chart = await setup();

      fireEvent.click(screen.getByRole("button", { name: "复位" }));

      await waitFor(() => expect(chart.setBarSpace).toHaveBeenCalledWith(8));
      expect(chart.scrollToRealTime).toHaveBeenCalled();
    });

    it("拖拽进行中切换 symbol 时解绑遗留的 window 监听，避免泄漏并防止误缩放新图表", async () => {
      vi.mocked(getLocalMarketBars).mockResolvedValue(buildSnapshot());
      const { rerender } = render(<LocalMarketKlinePanel symbol="600000.SH" />);
      await waitFor(() => expect(klineMock.charts.length).toBe(1));

      const container = screen.getByLabelText("本地 K 线图");
      const addSpy = vi.spyOn(window, "addEventListener");
      const removeSpy = vi.spyOn(window, "removeEventListener");
      addSpy.mockClear();
      fireEvent.mouseDown(container, { shiftKey: true, button: 0, clientX: 100 });
      fireEvent.mouseMove(window, { clientX: 200 });

      // Capture this drag's window handlers so we can assert they get detached.
      const moveHandler = addSpy.mock.calls.find((c) => c[0] === "mousemove")?.[1];
      const upHandler = addSpy.mock.calls.find((c) => c[0] === "mouseup")?.[1];
      expect(typeof moveHandler).toBe("function");
      expect(typeof upHandler).toBe("function");
      removeSpy.mockClear();

      // 拖拽未结束就切换标的：panel 把 snapshot 置 null 再重建，effect cleanup 必须解绑这些 window 监听。
      vi.mocked(getLocalMarketBars).mockResolvedValue(buildSnapshot({ symbol: "601111.SH" }));
      rerender(<LocalMarketKlinePanel symbol="601111.SH" />);

      await waitFor(() => expect(removeSpy).toHaveBeenCalledWith("mousemove", moveHandler));
      expect(removeSpy).toHaveBeenCalledWith("mouseup", upHandler);

      await waitFor(() => expect(klineMock.charts.length).toBe(2));
      const newChart = klineMock.charts[1];
      newChart.setBarSpace.mockClear();
      newChart.scrollToDataIndex.mockClear();
      newChart.convertFromPixel.mockReturnValueOnce({ dataIndex: 10 }).mockReturnValueOnce({ dataIndex: 60 });

      // 监听已解绑，这次 mouseup 不会触发旧 onMouseUp，新图表不被误缩放。
      fireEvent.mouseUp(window, { clientX: 300 });
      expect(newChart.setBarSpace).not.toHaveBeenCalled();
      expect(newChart.scrollToDataIndex).not.toHaveBeenCalled();

      addSpy.mockRestore();
      removeSpy.mockRestore();
    });

    it("Shift+按下在捕获阶段拦截，klinecharts 内层元素收不到 mousedown（不触发原生平移）", async () => {
      vi.mocked(getLocalMarketBars).mockResolvedValue(buildSnapshot());
      render(<LocalMarketKlinePanel symbol="600000.SH" />);
      await waitFor(() => expect(klineMock.charts.length).toBe(1));

      const container = screen.getByLabelText("本地 K 线图");
      // 真实 klinecharts 把 mousedown 冒泡绑在它 appendChild 进容器的内层 div 上；这里用一个
      // 内层子元素 + 冒泡监听来模拟，验证 Shift 时事件在捕获阶段被拦下、根本到不了内层。
      const inner = document.createElement("div");
      container.appendChild(inner);
      const innerMouseDown = vi.fn();
      inner.addEventListener("mousedown", innerMouseDown);

      try {
        // Shift+按下：捕获阶段 stopPropagation，事件不应抵达内层元素（即不会启动平移）。
        fireEvent.mouseDown(inner, { shiftKey: true, button: 0, clientX: 100 });
        expect(innerMouseDown).not.toHaveBeenCalled();
        fireEvent.mouseUp(window, { clientX: 100 });

        // 普通（无 Shift）按下：不拦截，事件正常抵达内层元素（保留平移手势）。
        innerMouseDown.mockClear();
        fireEvent.mouseDown(inner, { shiftKey: false, button: 0, clientX: 100 });
        expect(innerMouseDown).toHaveBeenCalledTimes(1);
        fireEvent.mouseUp(window, { clientX: 100 });
      } finally {
        inner.removeEventListener("mousedown", innerMouseDown);
        container.removeChild(inner);
      }
    });
  });
});
