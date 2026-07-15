import { Empty } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";
import * as klinecharts from "klinecharts";

import type { BacktestChartBar, LocalMarketBarsSnapshot, LocalMarketOverlayItem } from "../types";
import type { MainIndicator, SubIndicator } from "./LocalMarketKlineToolbar";

type LocalMarketKlineChartProps = {
  snapshot: LocalMarketBarsSnapshot | null;
  mainIndicator: MainIndicator;
  subIndicator: SubIndicator;
  overlayItems: LocalMarketOverlayItem[];
  /**
   * Fetch a chunk of bars strictly older than `oldestTimestampMs` (the current
   * left-most loaded bar). Returns an empty array once the local store has no
   * more history — which the chart treats as the lazy-load boundary.
   */
  loadOlderBars: (oldestTimestampMs: number) => Promise<BacktestChartBar[]>;
  /** Notify the parent of the new earliest loaded timestamp so it can widen overlay coverage. */
  onEarliestLoaded?: (earliestTimestampMs: number) => void;
};

type KLineChartHandle = ReturnType<typeof klinecharts.init>;

type LoadDataParams = {
  type: string;
  data: { timestamp?: number } | null;
  callback: (dataList: unknown[], more?: boolean) => void;
};

type OverlayMarker = {
  key: string;
  label: string;
  x: number;
  y: number;
  side: string | null;
  kind: string;
};

type ChartPixelLike = Partial<{ x?: number; y?: number }> | Array<Partial<{ x?: number; y?: number }>>;

const CANDLE_PANE_ID = "candle_pane";
const VOLUME_PANE_ID = "doyoutrade_volume_pane";
const SUB_PANE_ID = "doyoutrade_sub_pane";
const DARK_STYLE_NAME = "doyoutrade-dark";
const DEFAULT_BAR_SPACE = 8;
// Bounds for the marquee (Shift + drag) zoom — mirror BacktestRunChartPanel so
// the two K-line surfaces zoom identically. MIN/MAX cap the per-bar pixel width;
// the selection guards reject accidental clicks and degenerate single-bar spans.
const MIN_BAR_SPACE = 2;
const MAX_BAR_SPACE = 60;
const MIN_SELECTION_PIXELS = 6;
const MIN_SELECTION_BARS = 2;
// After a marquee zoom, keep this fraction of the selection's bar count visible as
// context on EACH side, so the zoomed-in view stays connected to neighbouring bars
// instead of clipping to exactly the selection.
const SELECTION_PADDING_RATIO = 0.25;
// A-share bars are stored as naive-UTC; render wall-clock in the Shanghai zone so
// daily labels keep their calendar date and 5m labels show Beijing trading hours.
const CHART_TIMEZONE = "Asia/Shanghai";
const MARKER_LABEL_HEIGHT_PX = 22;
const MARKER_LABEL_GAP_PX = 8;
const MARKER_STACK_STEP_PX = 16;

let darkStyleRegistered = false;

const doyoutradeDarkChartTheme = {
  grid: { show: true, horizontal: { color: "#1f2937" }, vertical: { color: "#1f2937" } },
  candle: {
    bar: {
      upColor: "#ef4444",
      downColor: "#10b981",
      noChangeColor: "#94a3b8",
      upBorderColor: "#ef4444",
      downBorderColor: "#10b981",
      upWickColor: "#ef4444",
      downWickColor: "#10b981",
    },
    priceMark: {
      last: {
        show: true,
        text: { color: "#000000", backgroundColor: "#fbbf24" },
      },
    },
  },
  indicator: {
    lines: [{ color: "#fbbf24" }, { color: "#3b82f6" }, { color: "#a855f7" }, { color: "#ef4444" }],
  },
  xAxis: {
    axisLine: { color: "#1f2937" },
    tickText: { color: "#94a3b8" },
    tickLine: { color: "#1f2937" },
  },
  yAxis: {
    axisLine: { color: "#1f2937" },
    tickText: { color: "#94a3b8" },
    tickLine: { color: "#1f2937" },
  },
  separator: { color: "#1f2937" },
  crosshair: {
    horizontal: { line: { color: "#94a3b8" }, text: { color: "#0b0e14", backgroundColor: "#cbd5e1" } },
    vertical: { line: { color: "#94a3b8" }, text: { color: "#0b0e14", backgroundColor: "#cbd5e1" } },
  },
};

function ensureDarkStyleRegistered(): void {
  if (darkStyleRegistered) return;
  const fn = (klinecharts as unknown as { registerStyles?: (name: string, styles: unknown) => void }).registerStyles;
  if (typeof fn === "function") fn(DARK_STYLE_NAME, doyoutradeDarkChartTheme);
  darkStyleRegistered = true;
}

/**
 * Parse a backend bar timestamp into epoch ms. Daily bars arrive as a bare
 * calendar date (`YYYY-MM-DD`) and intraday bars as a naive datetime
 * (`YYYY-MM-DDTHH:MM:SS`) that is actually UTC — append `Z` so the browser does
 * not reinterpret it in the local zone (which previously shifted daily labels to
 * `08:00`). Marker positioning and the chart series must share this parsing.
 */
export function parseBarTimestampMs(value: string): number {
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return Date.parse(value);
  if (value.includes("T") && !/[zZ]$/.test(value) && !/[+-]\d{2}:?\d{2}$/.test(value)) {
    return Date.parse(`${value}Z`);
  }
  return Date.parse(value);
}

function dayKeyFromMs(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

function utcDayKey(value: string): string {
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const ms = parseBarTimestampMs(value);
  if (!Number.isFinite(ms)) return value.slice(0, 10);
  return dayKeyFromMs(ms);
}

function mainIndicatorConfig(main: MainIndicator) {
  if (main === "MA") return { name: "MA", calcParams: [5, 10, 30, 60] };
  if (main === "BOLL") return { name: "BOLL", calcParams: [20, 2] };
  return null;
}

function subIndicatorConfig(sub: SubIndicator) {
  switch (sub) {
    case "MACD":
      return { name: "MACD", calcParams: [12, 26, 9] };
    case "KDJ":
      return { name: "KDJ", calcParams: [9, 3, 3] };
    case "RSI":
      return { name: "RSI", calcParams: [6, 12, 24] };
    case "WR":
      return { name: "WR", calcParams: [6, 10, 14] };
  }
}

function buildKlineBars(bars: BacktestChartBar[], useTurnover: boolean) {
  return bars.map((bar: BacktestChartBar) => ({
    timestamp: parseBarTimestampMs(bar.timestamp),
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
    volume: bar.volume,
    turnover: useTurnover ? bar.amount ?? undefined : undefined,
  }));
}

function markerColor(kind: string, side: string | null): string {
  if (kind === "signal") return side === "sell" ? "#f97316" : "#2563eb";
  return side === "sell" ? "#dc2626" : "#16a34a";
}

function chartNumberOrNull(value: unknown): number | null {
  const num = typeof value === "number" ? value : Number(value);
  return Number.isFinite(num) ? num : null;
}

function pickChartPixel(raw: ChartPixelLike | null | undefined): { x: number; y: number } | null {
  const point = Array.isArray(raw) ? raw[0] : raw;
  if (!point || typeof point.x !== "number" || typeof point.y !== "number") return null;
  if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) return null;
  return { x: point.x, y: point.y };
}

function resolveOverlayMarkerPrice(item: LocalMarketOverlayItem, bar: BacktestChartBar): number | null {
  const low = chartNumberOrNull(bar.low);
  const high = chartNumberOrNull(bar.high);
  const close = chartNumberOrNull(bar.close);
  const price = chartNumberOrNull(item.price);
  if (price != null && low != null && high != null) {
    return Math.min(Math.max(price, low), high);
  }
  if (price != null) return price;
  if (close != null) return close;
  if (low != null && high != null) return (low + high) / 2;
  return null;
}

function resolveOverlayMarkerY(
  item: LocalMarketOverlayItem,
  index: number,
  bounds: {
    highY: number;
    lowY: number;
  },
): number {
  const { highY, lowY } = bounds;
  const stack = (index % 3) * MARKER_STACK_STEP_PX;
  if (item.side === "buy") {
    return lowY + MARKER_LABEL_GAP_PX + stack;
  }
  return highY - MARKER_LABEL_GAP_PX - MARKER_LABEL_HEIGHT_PX - stack;
}

export function LocalMarketKlineChart({
  snapshot,
  mainIndicator,
  subIndicator,
  overlayItems,
  loadOlderBars,
  onEarliestLoaded,
}: LocalMarketKlineChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<KLineChartHandle | null>(null);
  const layoutMarkersRef = useRef<(() => void) | null>(null);
  // Marquee-zoom (Shift + drag) selection rectangle + drag origin in container px.
  const dragRectRef = useRef<HTMLDivElement | null>(null);
  const dragStartXRef = useRef<number | null>(null);
  // Teardown for the window listeners of an in-flight drag, so the effect cleanup
  // can detach them if the snapshot swaps (symbol / interval / refresh) mid-drag.
  const activeDragCleanupRef = useRef<(() => void) | null>(null);
  const mainIndicatorIdRef = useRef<string | null>(null);
  const subIndicatorIdRef = useRef<string | null>(null);
  const loadingOlderRef = useRef(false);
  // Keep the load callbacks current without re-registering them on the chart.
  const loadOlderRef = useRef(loadOlderBars);
  loadOlderRef.current = loadOlderBars;
  const onEarliestLoadedRef = useRef(onEarliestLoaded);
  onEarliestLoadedRef.current = onEarliestLoaded;
  const volumeModeRef = useRef(snapshot?.volume_mode);
  volumeModeRef.current = snapshot?.volume_mode;

  const [chartReady, setChartReady] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  // Accumulated bars currently held by the chart (initial window + lazy-loaded
  // history). Drives overlay marker anchoring so older markers appear as the
  // user scrolls back.
  const [chartBars, setChartBars] = useState<BacktestChartBar[]>([]);
  const [markers, setMarkers] = useState<OverlayMarker[]>([]);

  useEffect(() => {
    if (!snapshot || snapshot.bars.length === 0) return;
    const container = containerRef.current;
    if (!container) return;

    ensureDarkStyleRegistered();
    const chart = klinecharts.init(container, { styles: DARK_STYLE_NAME, locale: "zh-CN" }) as KLineChartHandle | null;
    if (!chart) return;
    chartRef.current = chart;
    const useTurnover = snapshot.volume_mode === "amount_available";

    chart.setTimezone?.(CHART_TIMEZONE);
    // `more = true` lets klinecharts fire the forward load callback when the user
    // scrolls past the left edge; the callback decides when history is exhausted.
    chart.applyNewData(buildKlineBars(snapshot.bars, useTurnover), true);
    chart.setBarSpace(DEFAULT_BAR_SPACE);
    chart.scrollToRealTime?.(0);
    chart.createIndicator({ name: "VOL", calcParams: [5, 10, 20] }, false, { id: VOLUME_PANE_ID, height: 70 });

    chart.setLoadDataCallback?.(({ type, data, callback }: LoadDataParams) => {
      if (type !== "forward") {
        callback([], false);
        return;
      }
      if (loadingOlderRef.current) {
        callback([], true);
        return;
      }
      const oldestMs = typeof data?.timestamp === "number" ? data.timestamp : null;
      if (oldestMs == null) {
        callback([], false);
        return;
      }
      loadingOlderRef.current = true;
      setLoadingOlder(true);
      loadOlderRef.current(oldestMs)
        .then((older) => {
          const hasMore = older.length > 0;
          callback(buildKlineBars(older, volumeModeRef.current === "amount_available"), hasMore);
          if (older.length > 0) {
            setChartBars((prev) => [...older, ...prev]);
            onEarliestLoadedRef.current?.(parseBarTimestampMs(older[0].timestamp));
          }
        })
        .catch(() => {
          // Surface no more data rather than wedging the loading flag; the
          // sidebar sync controls remain the path to fetch unsynced history.
          callback([], false);
        })
        .finally(() => {
          loadingOlderRef.current = false;
          setLoadingOlder(false);
        });
    });

    const chartApi = chart as KLineChartHandle & {
      subscribeAction?: (type: string, handler: () => void) => void;
      unsubscribeAction?: (type: string, handler: () => void) => void;
    };
    const relayout = () => layoutMarkersRef.current?.();
    // Must call on chart instance — detached methods lose `this` and break _chartStore access.
    chartApi.subscribeAction?.("onScroll", relayout);
    chartApi.subscribeAction?.("onZoom", relayout);
    chartApi.subscribeAction?.("onDataReady", relayout);

    setChartBars(snapshot.bars);
    setChartReady(true);

    return () => {
      chartRef.current = null;
      layoutMarkersRef.current = null;
      mainIndicatorIdRef.current = null;
      subIndicatorIdRef.current = null;
      loadingOlderRef.current = false;
      setChartReady(false);
      setLoadingOlder(false);
      setMarkers([]);
      chartApi.unsubscribeAction?.("onScroll", relayout);
      chartApi.unsubscribeAction?.("onZoom", relayout);
      chartApi.unsubscribeAction?.("onDataReady", relayout);
      try {
        chart.dispose();
      } catch {
        // ignore dispose races
      }
    };
  }, [snapshot]);

  // Shift + drag a horizontal range to zoom into it. klinecharts owns the plain
  // left-drag (pan), so the gesture is only hijacked while Shift is held, leaving
  // pan / scroll / wheel-zoom untouched. Mirrors BacktestRunChartPanel.
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
        if (!leftConv || !rightConv || leftConv.dataIndex == null || rightConv.dataIndex == null) {
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
        requestAnimationFrame(() => layoutMarkersRef.current?.());
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
    // inside this container (Event → _chartContainer), and only then attaches the
    // document-level mousemove/mouseup that drive its native pan. Listening in the
    // capture phase lets us stopPropagation BEFORE the event descends to that inner
    // element, so a Shift+drag never also starts klinecharts' pan. A bubble-phase
    // listener fires too late — after the inner handler has already begun panning.
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

  const handleResetZoom = useCallback(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.setBarSpace(DEFAULT_BAR_SPACE);
    chart.scrollToRealTime?.(0);
    requestAnimationFrame(() => layoutMarkersRef.current?.());
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !chartReady) return;
    if (mainIndicatorIdRef.current) {
      chart.removeIndicator?.(CANDLE_PANE_ID, mainIndicatorIdRef.current);
      mainIndicatorIdRef.current = null;
    }
    const cfg = mainIndicatorConfig(mainIndicator);
    if (cfg) {
      chart.createIndicator(cfg, false, { id: CANDLE_PANE_ID });
      mainIndicatorIdRef.current = cfg.name;
    }
    layoutMarkersRef.current?.();
  }, [mainIndicator, chartReady]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !chartReady) return;
    if (subIndicatorIdRef.current) {
      chart.removeIndicator?.(SUB_PANE_ID, subIndicatorIdRef.current);
      subIndicatorIdRef.current = null;
    }
    const cfg = subIndicatorConfig(subIndicator);
    chart.createIndicator(cfg, false, { id: SUB_PANE_ID, height: 90 });
    subIndicatorIdRef.current = cfg.name;
    layoutMarkersRef.current?.();
  }, [subIndicator, chartReady]);

  useEffect(() => {
    layoutMarkersRef.current = () => {
      const chart = chartRef.current;
      if (!chart || chartBars.length === 0) {
        setMarkers([]);
        return;
      }
      const nextMarkers = overlayItems.flatMap((item, index) => {
        const bar = chartBars.find((row) => utcDayKey(row.timestamp) === utcDayKey(item.timestamp));
        if (!bar) return [];
        const markerPrice = resolveOverlayMarkerPrice(item, bar);
        if (markerPrice == null) return [];
        const barMs = parseBarTimestampMs(bar.timestamp);
        const pixel = pickChartPixel(chart.convertToPixel(
          {
            timestamp: barMs,
            value: markerPrice,
          },
          { paneId: CANDLE_PANE_ID, absolute: true },
        ) as ChartPixelLike | undefined);
        const high = chartNumberOrNull(bar.high);
        const low = chartNumberOrNull(bar.low);
        if (!pixel || high == null || low == null) return [];
        const highPixel = pickChartPixel(chart.convertToPixel(
          {
            timestamp: barMs,
            value: high,
          },
          { paneId: CANDLE_PANE_ID, absolute: true },
        ) as ChartPixelLike | undefined);
        const lowPixel = pickChartPixel(chart.convertToPixel(
          {
            timestamp: barMs,
            value: low,
          },
          { paneId: CANDLE_PANE_ID, absolute: true },
        ) as ChartPixelLike | undefined);
        if (!highPixel || !lowPixel) return [];
        return [
          {
            key: `${item.kind}:${item.timestamp}:${index}`,
            label: item.label,
            x: pixel.x,
            y: resolveOverlayMarkerY(item, index, { highY: highPixel.y, lowY: lowPixel.y }),
            side: item.side,
            kind: item.kind,
          },
        ];
      });
      setMarkers(nextMarkers);
    };
    layoutMarkersRef.current();
  }, [overlayItems, chartBars, chartReady]);

  if (!snapshot || snapshot.bars.length === 0) {
    return (
      <div className="flex h-full min-h-[420px] items-center justify-center rounded-xl border border-slate-800 bg-slate-950">
        <Empty description="暂无本地 K 线" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      </div>
    );
  }

  return (
    <div className="relative h-full min-h-[420px] overflow-hidden rounded-xl border border-slate-800 bg-slate-950">
      <div ref={containerRef} className="h-full w-full" aria-label="本地 K 线图" />
      {/* Marquee-zoom selection rectangle, driven imperatively during Shift + drag. */}
      <div
        ref={dragRectRef}
        data-testid="zoom-selection-rect"
        aria-hidden="true"
        className="pointer-events-none absolute inset-y-0 border-x border-amber-400/60 bg-amber-500/15"
        style={{ display: "none", left: 0, width: 0 }}
      />
      <div className="pointer-events-none absolute left-2 top-2 flex items-center gap-2">
        <span className="rounded-full bg-slate-900/80 px-2 py-0.5 text-[11px] text-slate-300 shadow">
          Shift + 拖动 框选放大
        </span>
        <button
          type="button"
          onClick={handleResetZoom}
          className="pointer-events-auto rounded-full bg-slate-900/80 px-2 py-0.5 text-[11px] text-slate-200 shadow transition hover:bg-slate-800"
        >
          复位
        </button>
        {loadingOlder ? (
          <span className="rounded-full bg-slate-900/80 px-2 py-0.5 text-[11px] text-slate-300 shadow">
            加载更早数据…
          </span>
        ) : null}
      </div>
      <div className="pointer-events-none absolute inset-0">
        {markers.map((marker) => (
          <div
            key={marker.key}
            className="absolute -translate-x-1/2 rounded-full border px-2 py-0.5 text-[11px] font-medium text-white shadow"
            style={{
              left: marker.x,
              top: marker.y,
              backgroundColor: markerColor(marker.kind, marker.side),
              borderColor: "#e2e8f0",
            }}
          >
            {marker.label}
          </div>
        ))}
      </div>
    </div>
  );
}
