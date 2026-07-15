import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as klinecharts from "klinecharts";

import { getTaskRunChart } from "../api";
import {
  BAR_SPACE_EPSILON,
  CANDLE_PANE_ID,
  DARK_STYLE_NAME,
  DEFAULT_BAR_SPACE,
  HIGHLIGHT_DURATION_MS,
  MAIN_OPTIONS,
  MAX_BAR_SPACE,
  MIN_BAR_SPACE,
  MIN_SELECTION_BARS,
  MIN_SELECTION_PIXELS,
  SELECTION_PADDING_RATIO,
  STATUS_LABELS,
  SUB_OPTIONS,
  SUB_PANE_ID,
  TRADE_LABEL_APPROX_HEIGHT_PX,
  TRADE_LABEL_GAP_PX,
  TRADE_LABEL_STACK_STEP_PX,
  TRADE_MARKER_STYLE,
  VOLUME_PANE_ID,
  ZOOM_STEP,
  buildKlineBars,
  chartNumericOrNull,
  ensureDarkStyleRegistered,
  formatAdjustLabel,
  formatNumber,
  isValidChartTrade,
  mainIndicatorConfig,
  pickChartPixel,
  planTradeOverlays,
  resolveRangeBoundChartMs,
  resolveTradeOverlayAnchor,
  shortText,
  subIndicatorConfig,
  tradeRowKey,
  tradeSideLabel,
  type KLineChartHandle,
  type MainIndicator,
  type RangeBound,
  type RangeHtmlMarker,
  type SubIndicator,
  type TradeHtmlMarker,
} from "./backtestChartHelpers";
import { formatSymbolWithName, useSymbolNames } from "../hooks/useSymbolNames";
import type { BacktestChartSnapshot, BacktestChartTrade } from "../types";
import { formatBacktestRange } from "../utils/datetime";

type BacktestRunChartPanelProps = {
  taskId: string;
  selectedRunId: string | null;
};

export function BacktestRunChartPanel({ taskId, selectedRunId }: BacktestRunChartPanelProps) {
  const [snapshot, setSnapshot] = useState<BacktestChartSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [selectedSymbol, setSelectedSymbol] = useState<string | undefined>(undefined);
  const [refreshTick, setRefreshTick] = useState(0);
  const [mainIndicator, setMainIndicator] = useState<MainIndicator>("MA");
  const [subIndicator, setSubIndicator] = useState<SubIndicator>("MACD");
  const [highlightedTradeKey, setHighlightedTradeKey] = useState<string | null>(null);
  const [reasonModalText, setReasonModalText] = useState<string | null>(null);
  const [barSpace, setBarSpace] = useState<number>(DEFAULT_BAR_SPACE);
  const [tradeHtmlMarkers, setTradeHtmlMarkers] = useState<TradeHtmlMarker[]>([]);
  const [rangeHtmlMarkers, setRangeHtmlMarkers] = useState<RangeHtmlMarker[]>([]);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<KLineChartHandle | null>(null);
  const lastMainRef = useRef<MainIndicator>("MA");
  const lastSubRef = useRef<SubIndicator>("MACD");
  const tradeRowRefs = useRef<Map<string, HTMLTableRowElement | null>>(new Map());
  const layoutTradeMarkersRef = useRef<(() => void) | null>(null);
  const dragRectRef = useRef<HTMLDivElement | null>(null);
  const dragStartXRef = useRef<number | null>(null);
  // Teardown for the window listeners of an in-flight drag, so the effect cleanup
  // can detach them if the snapshot swaps (symbol / run / refresh) mid-drag.
  const activeDragCleanupRef = useRef<(() => void) | null>(null);

  const validTrades = useMemo(
    () => (snapshot?.trades ?? []).filter((trade) => isValidChartTrade(trade)),
    [snapshot?.trades],
  );

  useEffect(() => {
    setSelectedSymbol(undefined);
    setSnapshot(null);
    setErrorMessage("");
    setHighlightedTradeKey(null);
    setReasonModalText(null);
    setTradeHtmlMarkers([]);
    setRangeHtmlMarkers([]);
  }, [selectedRunId]);

  useEffect(() => {
    if (!selectedRunId) {
      return;
    }
    let cancelled = false;
    setLoading(true);
    setErrorMessage("");
    setSnapshot(null);
    getTaskRunChart(taskId, selectedRunId, { symbol: selectedSymbol })
      .then((result) => {
        if (cancelled) return;
        setSnapshot(result);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setErrorMessage(err instanceof Error ? err.message : String(err));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, selectedRunId, selectedSymbol, refreshTick]);

  useEffect(() => {
    if (highlightedTradeKey == null) return;
    const row = tradeRowRefs.current.get(highlightedTradeKey);
    if (row && typeof row.scrollIntoView === "function") {
      try {
        row.scrollIntoView({ block: "nearest", behavior: "smooth" });
      } catch {
        // no-op for jsdom or older browsers
      }
    }
    const tid = setTimeout(() => setHighlightedTradeKey(null), HIGHLIGHT_DURATION_MS);
    return () => clearTimeout(tid);
  }, [highlightedTradeKey]);

  useEffect(() => {
    if (!snapshot || snapshot.bars.length === 0) return;
    const container = containerRef.current;
    if (!container) return;

    ensureDarkStyleRegistered();

    const chart = klinecharts.init(container, { styles: DARK_STYLE_NAME, locale: "zh-CN" }) as KLineChartHandle | null;
    if (!chart) return;
    chartRef.current = chart;

    const bars = buildKlineBars(snapshot);

    const layoutTradeMarkers = (): void => {
      const c = chartRef.current;
      if (!c || c !== chart) return;
      const next: TradeHtmlMarker[] = [];
      for (const input of planTradeOverlays(validTrades)) {
        const ts = input.trade.timestamp;
        const price = chartNumericOrNull(input.trade.price);
        if (ts == null || price == null) continue;
        const anchor = resolveTradeOverlayAnchor(input.trade, snapshot.bars);
        const chartTimestampMs = anchor?.chartTimestampMs ?? Date.parse(ts);
        const markerPrice = anchor?.markerPrice ?? price;
        if (!Number.isFinite(chartTimestampMs) || !Number.isFinite(markerPrice)) continue;
        const high = anchor?.barHigh ?? markerPrice;
        const low = anchor?.barLow ?? markerPrice;
        const pricePt = pickChartPixel(
          c.convertToPixel({ timestamp: chartTimestampMs, value: markerPrice }, { paneId: CANDLE_PANE_ID, absolute: true }) as
            | Partial<{ x?: number; y?: number }>
            | Array<Partial<{ x?: number; y?: number }>>,
        );
        const highPt = pickChartPixel(
          c.convertToPixel({ timestamp: chartTimestampMs, value: high }, { paneId: CANDLE_PANE_ID, absolute: true }) as
            | Partial<{ x?: number; y?: number }>
            | Array<Partial<{ x?: number; y?: number }>>,
        );
        const lowPt = pickChartPixel(
          c.convertToPixel({ timestamp: chartTimestampMs, value: low }, { paneId: CANDLE_PANE_ID, absolute: true }) as
            | Partial<{ x?: number; y?: number }>
            | Array<Partial<{ x?: number; y?: number }>>,
        );
        if (!pricePt || !highPt || !lowPt) continue;
        const isBuy = input.trade.side === "buy";
        const stack = input.offsetIndex * TRADE_LABEL_STACK_STEP_PX;
        let labelY: number;
        let stemTop: number;
        let stemHeight: number;
        if (isBuy) {
          labelY = lowPt.y + TRADE_LABEL_GAP_PX + stack;
          stemTop = lowPt.y;
          stemHeight = Math.max(1, labelY - stemTop);
        } else {
          labelY = highPt.y - TRADE_LABEL_GAP_PX - TRADE_LABEL_APPROX_HEIGHT_PX - stack;
          const labelBottom = labelY + TRADE_LABEL_APPROX_HEIGHT_PX;
          stemTop = labelBottom;
          stemHeight = Math.max(1, highPt.y - labelBottom);
        }
        next.push({
          tradeKey: input.tradeKey,
          side: isBuy ? "buy" : "sell",
          label: isBuy ? TRADE_MARKER_STYLE.buy.text : TRADE_MARKER_STYLE.sell.text,
          x: pricePt.x,
          labelY,
          stemTop,
          stemHeight,
        });
      }
      setTradeHtmlMarkers(next);

      const refValue = Number(snapshot.bars[0]?.close ?? 0);
      const rangeNext: RangeHtmlMarker[] = [];
      const pushRange = (kind: RangeBound, ms: number | null, label: string): void => {
        if (ms == null || !Number.isFinite(ms)) return;
        const pt = pickChartPixel(
          c.convertToPixel({ timestamp: ms, value: refValue }, { paneId: CANDLE_PANE_ID, absolute: true }) as
            | Partial<{ x?: number; y?: number }>
            | Array<Partial<{ x?: number; y?: number }>>,
        );
        if (!pt) return;
        rangeNext.push({ kind, label, x: pt.x });
      };
      pushRange(
        "start",
        resolveRangeBoundChartMs(snapshot.run.range_start_utc, snapshot.bars, "start"),
        "回测开始",
      );
      pushRange(
        "end",
        resolveRangeBoundChartMs(snapshot.run.range_end_utc, snapshot.bars, "end"),
        "回测结束",
      );
      setRangeHtmlMarkers(rangeNext);
    };
    layoutTradeMarkersRef.current = layoutTradeMarkers;

    const onViewportChange = (): void => {
      layoutTradeMarkers();
    };

    const onDataReady = (): void => {
      if (chartRef.current !== chart) return;
      try {
        chart.unsubscribeAction("onDataReady", onDataReady);
      } catch {
        // ignore if chart is tearing down
      }

      let latestTradeChartMs: number | null = null;
      for (const input of planTradeOverlays(validTrades)) {
        const ts = input.trade.timestamp;
        const price = chartNumericOrNull(input.trade.price);
        if (ts == null || price == null) continue;
        const anchor = resolveTradeOverlayAnchor(input.trade, snapshot.bars);
        const chartTimestampMs = anchor?.chartTimestampMs ?? Date.parse(ts);
        if (!Number.isFinite(chartTimestampMs)) continue;
        latestTradeChartMs = chartTimestampMs;
      }

      if (bars.length > 0) {
        if (latestTradeChartMs != null) {
          chart.scrollToTimestamp(latestTradeChartMs, 0);
        } else {
          chart.scrollToDataIndex(bars.length - 1);
        }
      }

      requestAnimationFrame(() => {
        if (chartRef.current !== chart) return;
        layoutTradeMarkers();
        chart.resize();
        requestAnimationFrame(() => {
          if (chartRef.current !== chart) return;
          layoutTradeMarkers();
        });
      });
    };

    chart.subscribeAction("onDataReady", onDataReady);
    chart.subscribeAction("onScroll", onViewportChange);
    chart.subscribeAction("onZoom", onViewportChange);
    chart.applyNewData(bars, false);

    chart.setOffsetRightDistance(40);
    chart.setBarSpace(DEFAULT_BAR_SPACE);
    setBarSpace(DEFAULT_BAR_SPACE);

    const mainCfg = mainIndicatorConfig(mainIndicator);
    if (mainCfg) {
      chart.createIndicator(mainCfg, false, { id: CANDLE_PANE_ID });
    }

    chart.createIndicator(
      { name: "VOL", calcParams: [5, 10, 20] },
      false,
      { id: VOLUME_PANE_ID, height: 70 },
    );

    const subCfg = subIndicatorConfig(subIndicator);
    chart.createIndicator(subCfg, false, { id: SUB_PANE_ID, height: 90 });

    lastMainRef.current = mainIndicator;
    lastSubRef.current = subIndicator;

    return () => {
      layoutTradeMarkersRef.current = null;
      setTradeHtmlMarkers([]);
      setRangeHtmlMarkers([]);
      try {
        chart.unsubscribeAction("onDataReady", onDataReady);
        chart.unsubscribeAction("onScroll", onViewportChange);
        chart.unsubscribeAction("onZoom", onViewportChange);
      } catch {
        // chart may be partially torn down
      }
      chartRef.current = null;
      try {
        chart.dispose();
      } catch {
        // disposed already
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshot, validTrades]);

  useEffect(() => {
    if (!snapshot || snapshot.bars.length === 0) return;
    const container = containerRef.current;
    if (!container) return;

    const hideRect = () => {
      if (dragRectRef.current) {
        dragRectRef.current.style.display = "none";
        dragRectRef.current.style.width = "0px";
      }
    };

    const onMouseDown = (e: MouseEvent) => {
      if (!e.shiftKey) return;
      if (!chartRef.current) return;
      const rect = container.getBoundingClientRect();
      const startX = e.clientX - rect.left;
      dragStartXRef.current = startX;
      e.preventDefault();
      e.stopPropagation();
      if (dragRectRef.current) {
        dragRectRef.current.style.left = `${startX}px`;
        dragRectRef.current.style.width = "0px";
        dragRectRef.current.style.display = "block";
      }

      const onMouseMove = (ev: MouseEvent) => {
        const start = dragStartXRef.current;
        if (start == null) return;
        const r = container.getBoundingClientRect();
        const cur = ev.clientX - r.left;
        const left = Math.min(start, cur);
        const width = Math.abs(cur - start);
        if (dragRectRef.current) {
          dragRectRef.current.style.left = `${left}px`;
          dragRectRef.current.style.width = `${width}px`;
        }
      };

      const cleanup = () => {
        activeDragCleanupRef.current = null;
        dragStartXRef.current = null;
        hideRect();
        window.removeEventListener("mousemove", onMouseMove);
        window.removeEventListener("mouseup", onMouseUp);
        window.removeEventListener("keydown", onKey);
      };

      const onMouseUp = (ev: MouseEvent) => {
        const start = dragStartXRef.current;
        const chart = chartRef.current;
        cleanup();
        if (start == null || !chart) return;
        const r = container.getBoundingClientRect();
        const end = ev.clientX - r.left;
        const pixelWidth = Math.abs(end - start);
        if (pixelWidth < MIN_SELECTION_PIXELS) return;
        const leftPx = Math.min(start, end);
        const rightPx = Math.max(start, end);
        const leftConv = chart.convertFromPixel(
          { x: leftPx, y: 0 },
          { paneId: CANDLE_PANE_ID },
        ) as { dataIndex?: number } | null;
        const rightConv = chart.convertFromPixel(
          { x: rightPx, y: 0 },
          { paneId: CANDLE_PANE_ID },
        ) as { dataIndex?: number } | null;
        if (
          !leftConv ||
          !rightConv ||
          leftConv.dataIndex == null ||
          rightConv.dataIndex == null
        ) {
          return;
        }
        const leftIndex = Math.max(0, Math.floor(Number(leftConv.dataIndex)));
        const rightIndex = Math.max(0, Math.floor(Number(rightConv.dataIndex)));
        const barCount = rightIndex - leftIndex + 1;
        if (barCount < MIN_SELECTION_BARS) return;
        const containerWidth = r.width;
        // Size bars so the selection PLUS context padding on both sides fills the view,
        // rather than the selection alone — keeps continuity with neighbouring bars.
        const visibleBars = barCount * (1 + 2 * SELECTION_PADDING_RATIO);
        const target = containerWidth > 0 ? containerWidth / visibleBars : DEFAULT_BAR_SPACE;
        const clamped = Math.min(Math.max(target, MIN_BAR_SPACE), MAX_BAR_SPACE);
        chart.setBarSpace(clamped);
        // Center the selection. scrollToDataIndex lands its argument at the RIGHT edge,
        // so aim half a viewport to the right of the selection midpoint — that puts the
        // midpoint mid-screen with the post-clamp bar count deciding the side padding.
        const midIndex = (leftIndex + rightIndex) / 2;
        const visibleCount = containerWidth > 0 ? containerWidth / clamped : visibleBars;
        chart.scrollToDataIndex(Math.round(midIndex + visibleCount / 2));
        setBarSpace(clamped);
        requestAnimationFrame(() => layoutTradeMarkersRef.current?.());
      };

      const onKey = (ev: KeyboardEvent) => {
        if (ev.key === "Escape") cleanup();
      };

      activeDragCleanupRef.current = cleanup;
      window.addEventListener("mousemove", onMouseMove);
      window.addEventListener("mouseup", onMouseUp);
      window.addEventListener("keydown", onKey);
    };

    // Capture phase: klinecharts binds its own mousedown on an inner div it appends
    // inside this container, and only then attaches the document-level mousemove/mouseup
    // that drive its native pan. Listening in the capture phase lets us stopPropagation
    // BEFORE the event descends to that inner element, so a Shift+drag never also starts
    // klinecharts' pan. A bubble-phase listener fires too late.
    container.addEventListener("mousedown", onMouseDown, true);
    return () => {
      container.removeEventListener("mousedown", onMouseDown, true);
      // Detach any in-flight drag's window listeners; a snapshot swap mid-drag would
      // otherwise orphan them and let the stale mouseup zoom the next chart instance.
      activeDragCleanupRef.current?.();
      hideRect();
      dragStartXRef.current = null;
    };
  }, [snapshot]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (lastMainRef.current === mainIndicator) return;
    chart.removeIndicator({ paneId: CANDLE_PANE_ID });
    const cfg = mainIndicatorConfig(mainIndicator);
    if (cfg) {
      chart.createIndicator(cfg, false, { id: CANDLE_PANE_ID });
    }
    lastMainRef.current = mainIndicator;
    requestAnimationFrame(() => layoutTradeMarkersRef.current?.());
  }, [mainIndicator]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    if (lastSubRef.current === subIndicator) return;
    chart.removeIndicator({ paneId: SUB_PANE_ID });
    chart.createIndicator(subIndicatorConfig(subIndicator), false, { id: SUB_PANE_ID });
    lastSubRef.current = subIndicator;
    requestAnimationFrame(() => layoutTradeMarkersRef.current?.());
  }, [subIndicator]);

  const onRefresh = useCallback(() => setRefreshTick((n) => n + 1), []);

  const applyBarSpace = useCallback((next: number) => {
    const chart = chartRef.current;
    if (!chart) return;
    const clamped = Math.min(Math.max(next, MIN_BAR_SPACE), MAX_BAR_SPACE);
    chart.setBarSpace(clamped);
    setBarSpace(clamped);
    requestAnimationFrame(() => layoutTradeMarkersRef.current?.());
  }, []);

  const onZoomIn = useCallback(() => {
    if (barSpace >= MAX_BAR_SPACE - BAR_SPACE_EPSILON) return;
    applyBarSpace(barSpace * ZOOM_STEP);
  }, [applyBarSpace, barSpace]);

  const onZoomOut = useCallback(() => {
    if (barSpace <= MIN_BAR_SPACE + BAR_SPACE_EPSILON) return;
    applyBarSpace(barSpace / ZOOM_STEP);
  }, [applyBarSpace, barSpace]);

  const onZoomReset = useCallback(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.setBarSpace(DEFAULT_BAR_SPACE);
    const barCount = snapshot?.bars.length ?? 0;
    if (barCount > 0) {
      chart.scrollToDataIndex(barCount - 1);
    }
    setBarSpace(DEFAULT_BAR_SPACE);
    requestAnimationFrame(() => layoutTradeMarkersRef.current?.());
  }, [snapshot?.bars.length]);

  const zoomInDisabled = barSpace >= MAX_BAR_SPACE - BAR_SPACE_EPSILON;
  const zoomOutDisabled = barSpace <= MIN_BAR_SPACE + BAR_SPACE_EPSILON;

  const symbolOptions = useMemo(() => snapshot?.symbols ?? [], [snapshot?.symbols]);
  const symbolNames = useSymbolNames(symbolOptions);
  const currentSymbol = selectedSymbol ?? snapshot?.selected_symbol ?? "";
  const runStatus = snapshot?.run.status ?? "";
  const isRunning = runStatus === "running";
  const statusLabel = STATUS_LABELS[runStatus] ?? runStatus;

  const setRowRef = (key: string) => (el: HTMLTableRowElement | null) => {
    if (el == null) {
      tradeRowRefs.current.delete(key);
    } else {
      tradeRowRefs.current.set(key, el);
    }
  };

  if (!selectedRunId) {
    return (
      <div className="flex min-h-[420px] items-center justify-center rounded-md bg-[#0b0e14] text-sm text-slate-400">
        暂无回测，先发起一次回测
      </div>
    );
  }

  if (errorMessage && !loading) {
    return (
      <div className="rounded-md border border-red-900/40 bg-[#0b0e14] p-4 text-sm">
        <div className="mb-2 font-semibold text-red-300">加载回测图表失败</div>
        <div className="mb-3 text-red-200/80">{errorMessage}</div>
        <button
          type="button"
          onClick={onRefresh}
          className="rounded border border-red-400/40 bg-red-500/10 px-3 py-1 text-red-200 transition hover:bg-red-500/20"
        >
          重试
        </button>
      </div>
    );
  }

  const chartReady = !!snapshot && snapshot.bars.length > 0;
  const noBarsForSelected = !!snapshot && snapshot.bars.length === 0;
  const showTradesEmpty = !!snapshot && validTrades.length === 0;

  return (
    <div className="flex flex-col overflow-hidden rounded-md border border-[#1f2937] bg-[#0b0e14] text-slate-200 shadow-lg">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[#1f2937] bg-[#0b0e14] px-4 py-2">
        <div className="flex items-center gap-3">
          <select
            aria-label="股票"
            className="rounded border border-[#374151] bg-[#1f2937] px-2 py-1 text-sm text-slate-100"
            value={currentSymbol}
            onChange={(e) => setSelectedSymbol(e.target.value || undefined)}
            disabled={loading || symbolOptions.length === 0}
          >
            {symbolOptions.length === 0 ? <option value="">—</option> : null}
            {symbolOptions.map((sym) => (
              <option key={sym} value={sym}>
                {formatSymbolWithName(sym, symbolNames)}
              </option>
            ))}
          </select>
          <span
            className={`rounded px-2 py-0.5 text-xs ${isRunning ? "animate-pulse bg-amber-500/20 text-amber-200" : "bg-slate-700 text-slate-300"}`}
            data-testid="backtest-status-badge"
          >
            {statusLabel || "—"}
          </span>
          {snapshot ? (
            <span className="text-[11px] text-slate-400 tabular-nums" data-testid="backtest-chart-range">
              区间 {formatBacktestRange(snapshot.run.range_start_utc, snapshot.run.range_end_utc)}
            </span>
          ) : null}
          {snapshot ? (
            <span
              className="rounded border border-[#374151] bg-[#111827] px-2 py-0.5 text-[11px] text-slate-300"
              data-testid="backtest-chart-adjust"
            >
              {formatAdjustLabel(snapshot.adjust)}
            </span>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-3 text-xs">
          <div className="flex items-center gap-2 rounded border border-[#374151] bg-[#111827] px-2 py-1 text-[11px] text-slate-300">
            <span className="text-slate-400">标记</span>
            <span className="rounded border border-emerald-300/70 bg-emerald-500 px-1.5 py-0.5 font-semibold text-white">
              买入
            </span>
            <span className="rounded border border-rose-300/70 bg-rose-500 px-1.5 py-0.5 font-semibold text-white">
              卖出
            </span>
            <span className="text-slate-500">点击标记可定位成交</span>
          </div>
          <div className="flex items-center gap-1">
            <span className="text-slate-400">主图</span>
            {MAIN_OPTIONS.map((opt) => {
              const active = mainIndicator === opt.value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => setMainIndicator(opt.value)}
                  className={`rounded px-2 py-0.5 transition ${
                    active ? "bg-[#c98536] text-white" : "bg-[#1f2937] text-slate-300 hover:bg-[#374151]"
                  }`}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
          <div className="flex items-center gap-1">
            <span className="text-slate-400">副图</span>
            {SUB_OPTIONS.map((opt) => {
              const active = subIndicator === opt.value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => setSubIndicator(opt.value)}
                  className={`rounded px-2 py-0.5 transition ${
                    active ? "bg-[#c98536] text-white" : "bg-[#1f2937] text-slate-300 hover:bg-[#374151]"
                  }`}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
          <div className="flex items-center gap-1" title="Shift + 拖动 可框选放大">
            <span className="text-slate-400">缩放</span>
            <button
              type="button"
              onClick={onZoomIn}
              disabled={!chartReady || zoomInDisabled}
              className="rounded bg-[#1f2937] px-2 py-0.5 text-slate-300 transition hover:bg-[#374151] disabled:cursor-not-allowed disabled:opacity-60"
            >
              放大
            </button>
            <button
              type="button"
              onClick={onZoomOut}
              disabled={!chartReady || zoomOutDisabled}
              className="rounded bg-[#1f2937] px-2 py-0.5 text-slate-300 transition hover:bg-[#374151] disabled:cursor-not-allowed disabled:opacity-60"
            >
              缩小
            </button>
            <button
              type="button"
              onClick={onZoomReset}
              disabled={!chartReady}
              className="rounded bg-[#1f2937] px-2 py-0.5 text-slate-300 transition hover:bg-[#374151] disabled:cursor-not-allowed disabled:opacity-60"
            >
              复位
            </button>
          </div>
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            className="rounded border border-[#374151] bg-[#1f2937] px-2 py-0.5 text-slate-200 transition hover:bg-[#374151] disabled:cursor-not-allowed disabled:opacity-60"
          >
            刷新
          </button>
        </div>
      </header>

      {isRunning ? (
        <div className="border-b border-[#1f2937] bg-amber-500/10 px-4 py-1 text-xs text-amber-200">
          数据为部分进度快照，可点击刷新查看最新进度
        </div>
      ) : null}

      {snapshot && snapshot.warnings.length > 0 ? (
        <div className="border-b border-[#1f2937] bg-amber-500/10 px-4 py-1 text-xs text-amber-200">
          {snapshot.warnings.join("; ")}
        </div>
      ) : null}

      <section className="relative" style={{ minHeight: 540 }}>
        {loading ? (
          <div data-testid="backtest-chart-skeleton" className="absolute inset-0 flex animate-pulse items-center justify-center bg-slate-800/40 text-sm text-slate-400">
            加载图表数据中...
          </div>
        ) : null}
        {chartReady ? (
          <div className="relative h-[540px] w-full">
            <div ref={containerRef} className="h-full w-full" aria-label="回测K线图" />
            <div className="pointer-events-none absolute inset-0 z-10 overflow-hidden">
              {rangeHtmlMarkers.map((m) => (
                <div key={`range-${m.kind}`} aria-hidden>
                  <div
                    className="absolute inset-y-0 w-0 -translate-x-1/2 border-l border-dashed border-sky-400/80"
                    style={{ left: m.x }}
                  />
                  <span
                    data-testid={`backtest-range-marker-${m.kind}`}
                    className={`absolute top-1 rounded border border-sky-400/60 bg-sky-500/20 px-1.5 py-0.5 text-[10px] font-semibold text-sky-100 ${
                      m.kind === "start" ? "translate-x-1" : "-translate-x-full -ml-1"
                    }`}
                    style={{ left: m.x }}
                  >
                    {m.label}
                  </span>
                </div>
              ))}
              {tradeHtmlMarkers.map((m) => (
                <div
                  key={`${m.tradeKey}-stem`}
                  className={`absolute w-0 -translate-x-1/2 border-l border-dashed ${
                    m.side === "buy" ? "border-emerald-400/90" : "border-rose-400/90"
                  }`}
                  style={{ left: m.x, top: m.stemTop, height: m.stemHeight }}
                  aria-hidden
                />
              ))}
              {tradeHtmlMarkers.map((m) => {
                const st = m.side === "buy" ? TRADE_MARKER_STYLE.buy : TRADE_MARKER_STYLE.sell;
                return (
                  <button
                    key={m.tradeKey}
                    type="button"
                    data-testid={`trade-marker-${m.tradeKey}`}
                    className="pointer-events-auto absolute -translate-x-1/2 rounded-full border px-1.5 py-0.5 text-[10px] font-bold leading-none text-white shadow-sm"
                    style={{
                      left: m.x,
                      top: m.labelY,
                      backgroundColor: st.backgroundColor,
                      borderColor: st.borderColor,
                    }}
                    onClick={() => setHighlightedTradeKey(m.tradeKey)}
                  >
                    {m.label}
                  </button>
                );
              })}
            </div>
          </div>
        ) : null}
        <div
          ref={dragRectRef}
          data-testid="zoom-selection-rect"
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 border-x border-amber-400/60 bg-amber-500/15"
          style={{ display: "none", left: 0, width: 0 }}
        />
        {noBarsForSelected && !loading ? (
          <div className="flex h-[540px] items-center justify-center text-sm text-slate-400">暂无 K 线数据</div>
        ) : null}
      </section>

      <section className="border-t border-[#1f2937] bg-[#0b0e14]">
        <div className="px-4 py-2 text-sm text-slate-300">成交明细</div>
        <div className="max-h-[260px] overflow-y-auto">
          <table className="w-full table-fixed text-sm">
            <thead className="sticky top-0 bg-[#0b0e14] text-slate-300">
              <tr>
                <th className="w-[16%] px-3 py-2 text-left font-normal">时间</th>
                <th className="w-[8%] px-3 py-2 text-left font-normal">方向</th>
                <th className="w-[12%] px-3 py-2 text-right font-normal">价格</th>
                <th className="w-[12%] px-3 py-2 text-right font-normal">数量</th>
                <th className="w-[38%] px-3 py-2 text-left font-normal">理由</th>
                <th className="w-[14%] px-3 py-2 text-left font-normal">Intent</th>
              </tr>
            </thead>
            <tbody>
              {showTradesEmpty ? (
                <tr>
                  <td colSpan={6} className="px-3 py-6 text-center text-slate-500">
                    暂无成交
                  </td>
                </tr>
              ) : (
                validTrades.map((trade) => {
                  const key = tradeRowKey(trade);
                  const highlighted = highlightedTradeKey === key;
                  return (
                    <tr
                      key={key}
                      ref={setRowRef(key)}
                      data-testid={`trade-row-${key}`}
                      className={`border-t border-[#1f2937] transition-colors ${highlighted ? "bg-amber-500/20" : ""}`}
                    >
                      <td className="px-3 py-1.5 text-slate-100">{trade.timestamp ?? "—"}</td>
                      <td className="px-3 py-1.5">
                        <span
                          className={`inline-flex min-w-[38px] items-center justify-center rounded px-1.5 py-0.5 font-semibold ${
                            trade.side === "buy"
                              ? "border border-emerald-300/60 bg-emerald-500/20 text-emerald-300"
                              : "border border-rose-300/60 bg-rose-500/20 text-rose-300"
                          }`}
                        >
                          {tradeSideLabel(trade.side)}
                        </span>
                      </td>
                      <td className="px-3 py-1.5 text-right text-slate-100">{formatNumber(trade.price)}</td>
                      <td className="px-3 py-1.5 text-right text-slate-100">{formatNumber(trade.quantity, 0)}</td>
                      <td className="px-3 py-1.5 align-top text-slate-200">
                        {trade.rationale ? (
                          <div className="space-y-1">
                            <div className="break-all text-slate-200">{shortText(trade.rationale)}</div>
                            <button
                              type="button"
                              className="rounded border border-amber-400/40 bg-amber-500/10 px-2 py-0.5 text-xs text-amber-200 transition hover:bg-amber-500/20"
                              onClick={() => setReasonModalText(trade.rationale ?? "")}
                            >
                              详情
                            </button>
                          </div>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="px-3 py-1.5 align-top text-slate-300 break-all">
                        {trade.intent_id ? shortText(trade.intent_id, 24) : "—"}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>
      {reasonModalText ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4"
          onClick={() => setReasonModalText(null)}
          role="presentation"
        >
          <div
            className="w-full max-w-3xl rounded-md border border-[#374151] bg-[#0b0e14] shadow-2xl"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-label="理由详情"
          >
            <div className="flex items-center justify-between border-b border-[#1f2937] px-4 py-3">
              <div className="text-sm font-semibold text-slate-100">理由详情</div>
              <button
                type="button"
                className="rounded border border-[#374151] bg-[#1f2937] px-2 py-0.5 text-sm text-slate-200 transition hover:bg-[#374151]"
                onClick={() => setReasonModalText(null)}
              >
                关闭
              </button>
            </div>
            <div className="max-h-[65vh] overflow-auto whitespace-pre-wrap break-all px-4 py-3 text-sm text-slate-200">
              {reasonModalText}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
