import * as klinecharts from "klinecharts";

import type { BacktestChartSnapshot, BacktestChartTrade } from "../types";

/** Pure helpers, constants and chart-theme config for {@link BacktestRunChartPanel}.
 *
 * Extracted from the 1100-line panel so the component file is left with the
 * klinecharts instance lifecycle and React wiring only. Everything here is
 * either a constant, a chart-style object, or a side-effect-free transform of
 * snapshot / trade data — none of it touches the chart instance or component
 * state, which keeps the day-key / overlay-anchor logic unit-testable in
 * isolation. */

export type MainIndicator = "MA" | "BOLL" | "none";
export type SubIndicator = "MACD" | "KDJ" | "RSI" | "WR";

export type KLineChartHandle = ReturnType<typeof klinecharts.init>;

export const CANDLE_PANE_ID = "candle_pane";
export const VOLUME_PANE_ID = "doyoutrade_volume_pane";
export const SUB_PANE_ID = "doyoutrade_sub_pane";
export const DARK_STYLE_NAME = "doyoutrade-dark";

export const HIGHLIGHT_DURATION_MS = 3000;

export const DEFAULT_BAR_SPACE = 8;
export const MIN_BAR_SPACE = 2;
export const MAX_BAR_SPACE = 60;
export const ZOOM_STEP = 1.25;
export const BAR_SPACE_EPSILON = 1e-4;
export const MIN_SELECTION_PIXELS = 6;
export const MIN_SELECTION_BARS = 2;
// After a marquee zoom, keep this fraction of the selection's bar count visible as
// context on EACH side, so the zoomed-in view stays connected to neighbouring bars
// instead of clipping to exactly the selection.
export const SELECTION_PADDING_RATIO = 0.25;

/** HTML badge approx height (compact text + py-0.5 + border); keep in sync with marker button classes. */
export const TRADE_LABEL_APPROX_HEIGHT_PX = 22;
export const TRADE_LABEL_GAP_PX = 6;
export const TRADE_LABEL_STACK_STEP_PX = 14;

export const MAIN_OPTIONS: { value: MainIndicator; label: string }[] = [
  { value: "MA", label: "MA" },
  { value: "BOLL", label: "BOLL" },
  { value: "none", label: "隐藏" },
];

export const SUB_OPTIONS: { value: SubIndicator; label: string }[] = [
  { value: "MACD", label: "MACD" },
  { value: "KDJ", label: "KDJ" },
  { value: "RSI", label: "RSI" },
  { value: "WR", label: "WR" },
];

export const STATUS_LABELS: Record<string, string> = {
  pending: "待启动",
  queued: "排队中",
  running: "进行中",
  paused: "已暂停",
  stopped: "已停止",
  completed: "已完成",
  failed: "失败",
  canceled: "已取消",
};

export const TRADE_MARKER_STYLE = {
  buy: {
    backgroundColor: "#16a34a",
    borderColor: "#dcfce7",
    text: "买入",
  },
  sell: {
    backgroundColor: "#dc2626",
    borderColor: "#ffe4e6",
    text: "卖出",
  },
} as const;

export function formatAdjustLabel(adjust: string | null | undefined): string {
  switch ((adjust || "").trim().toLowerCase()) {
    case "hfq":
      return "后复权";
    case "none":
      return "不复权";
    case "qfq":
    default:
      return "前复权";
  }
}

export type TradeHtmlMarker = {
  tradeKey: string;
  side: "buy" | "sell";
  label: string;
  x: number;
  labelY: number;
  stemTop: number;
  stemHeight: number;
};

/** Full-height vertical guide marking where the backtest window opens / closes.
 * ``x`` is recomputed on every viewport change alongside the trade markers. */
export type RangeHtmlMarker = {
  kind: RangeBound;
  label: string;
  x: number;
};

let darkStyleRegistered = false;

export const doyoutradeDarkChartTheme = {
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

export function ensureDarkStyleRegistered(): void {
  if (darkStyleRegistered) return;
  const fn = (klinecharts as unknown as { registerStyles?: (name: string, styles: unknown) => void }).registerStyles;
  if (typeof fn === "function") {
    fn(DARK_STYLE_NAME, doyoutradeDarkChartTheme);
  }
  darkStyleRegistered = true;
}

export function mainIndicatorConfig(main: MainIndicator) {
  if (main === "MA") return { name: "MA", calcParams: [5, 10, 30, 60] };
  if (main === "BOLL") return { name: "BOLL", calcParams: [20, 2] };
  return null;
}

export function subIndicatorConfig(sub: SubIndicator) {
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

export function tradeRowKey(trade: BacktestChartTrade): string {
  return `${trade.cycle_run_id}:${trade.intent_id ?? ""}:${trade.timestamp ?? ""}`;
}

export function formatNumber(value: number | null | undefined, fractionDigits = 4): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toLocaleString("zh-CN", { maximumFractionDigits: fractionDigits });
}

export function tradeSideLabel(side: string): string {
  if (side === "buy") return "买入";
  if (side === "sell") return "卖出";
  return side || "—";
}

export function shortText(value: string, maxChars = 88): string {
  const normalized = value.trim();
  if (normalized.length <= maxChars) return normalized;
  return `${normalized.slice(0, maxChars)}...`;
}

export function buildKlineBars(snapshot: BacktestChartSnapshot) {
  const useTurnover = snapshot.volume_mode === "amount_available";
  return snapshot.bars.map((bar) => ({
    timestamp: Date.parse(bar.timestamp),
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
    volume: bar.volume,
    turnover: useTurnover ? bar.amount ?? undefined : undefined,
  }));
}

export type TradeOverlayInput = {
  trade: BacktestChartTrade;
  tradeKey: string;
  offsetIndex: number;
};

export function chartNumericOrNull(v: unknown): number | null {
  if (v == null) return null;
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

export function isValidChartTrade(trade: BacktestChartTrade): boolean {
  if (!trade.timestamp) return false;
  if (trade.side !== "buy" && trade.side !== "sell") return false;
  return chartNumericOrNull(trade.price) != null && chartNumericOrNull(trade.quantity) != null;
}

/** Calendar day in UTC (YYYY-MM-DD). Fills are naive UTC on the server; avoid local parsing drift. */
export function tradeCalendarDayKeyUtc(tradeTimestamp: string): string | null {
  const s = tradeTimestamp.trim();
  if (!s) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
  let iso = s;
  if (/^\d{4}-\d{2}-\d{2}T/.test(s) && !/[zZ]$/.test(s) && !/[+-]\d{2}:?\d{2}$/.test(s)) {
    iso = `${s}Z`;
  }
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return null;
  return new Date(ms).toISOString().slice(0, 10);
}

export function barCalendarDayKey(barTimestamp: string): string {
  const s = barTimestamp.trim();
  if (!s) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
  let iso = s;
  if (/^\d{4}-\d{2}-\d{2}T/.test(s) && !/[zZ]$/.test(s) && !/[+-]\d{2}:?\d{2}$/.test(s)) {
    iso = `${s}Z`;
  }
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return s.slice(0, 10);
  return new Date(ms).toISOString().slice(0, 10);
}

export type RangeBound = "start" | "end";

/** Snap a backtest-window bound (``range_start_utc`` / ``range_end_utc``) to the
 * chart timestamp of the nearest in-window bar so the marker lands on a real
 * candle rather than floating between days. Bars arrive sorted ascending;
 * parsing mirrors ``buildKlineBars`` (raw ``Date.parse``) so the returned ms
 * aligns with the axis, while day-matching reuses ``barCalendarDayKey`` (UTC
 * calendar day) — the same convention the trade overlays use. Returns ``null``
 * when no bar falls on the relevant side of the bound (no marker drawn). */
export function resolveRangeBoundChartMs(
  bound: string | null | undefined,
  bars: BacktestChartSnapshot["bars"],
  edge: RangeBound,
): number | null {
  if (!bound || bars.length === 0) return null;
  const boundDay = barCalendarDayKey(bound);
  if (!boundDay) return null;
  let lastOnOrBefore: number | null = null;
  for (const bar of bars) {
    const raw = bar.timestamp;
    if (raw == null || String(raw).trim() === "") continue;
    const ms = Date.parse(String(raw));
    if (!Number.isFinite(ms)) continue;
    const barDay = barCalendarDayKey(String(raw));
    if (edge === "start") {
      // First bar on/after the window start (warmup bars sit before it).
      if (barDay >= boundDay) return ms;
    } else if (barDay <= boundDay) {
      lastOnOrBefore = ms;
    } else {
      break;
    }
  }
  return edge === "end" ? lastOnOrBefore : null;
}

export function resolveTradeOverlayAnchor(
  trade: BacktestChartTrade,
  bars: BacktestChartSnapshot["bars"],
): { chartTimestampMs: number; markerPrice: number; barHigh: number; barLow: number } | null {
  if (!trade.timestamp || bars.length === 0) return null;
  const day = tradeCalendarDayKeyUtc(trade.timestamp);
  if (!day) return null;
  for (const bar of bars) {
    const rawTs = bar.timestamp;
    if (rawTs == null || String(rawTs).trim() === "") continue;
    if (barCalendarDayKey(String(rawTs)) !== day) continue;
    const chartTimestampMs = Date.parse(String(rawTs));
    if (!Number.isFinite(chartTimestampMs)) continue;
    const low = Number(bar.low);
    const high = Number(bar.high);
    const price = Number(trade.price);
    if (!Number.isFinite(low) || !Number.isFinite(high)) continue;
    const markerPrice = Number.isFinite(price) ? Math.min(Math.max(price, low), high) : (low + high) / 2;
    return { chartTimestampMs, markerPrice, barHigh: high, barLow: low };
  }
  return null;
}

export function planTradeOverlays(trades: BacktestChartTrade[]): TradeOverlayInput[] {
  const groupCounters = new Map<string, number>();
  const result: TradeOverlayInput[] = [];
  for (const trade of trades) {
    const groupKey = `${trade.timestamp}|${trade.side}`;
    const offsetIndex = groupCounters.get(groupKey) ?? 0;
    groupCounters.set(groupKey, offsetIndex + 1);
    result.push({
      trade,
      tradeKey: tradeRowKey(trade),
      offsetIndex,
    });
  }
  return result;
}

export function pickChartPixel(
  raw: Partial<{ x?: number; y?: number }> | Array<Partial<{ x?: number; y?: number }>> | null | undefined,
): { x: number; y: number } | null {
  const o = Array.isArray(raw) ? raw[0] : raw;
  if (!o || typeof o.x !== "number" || typeof o.y !== "number") return null;
  if (!Number.isFinite(o.x) || !Number.isFinite(o.y)) return null;
  return { x: o.x, y: o.y };
}
