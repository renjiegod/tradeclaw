import { Alert } from "antd";

import type { MarketBreadthData } from "../types";

/**
 * Map a rule-based sentiment ``label`` to a warm/cool palette per the A-share
 * red-up / green-down convention plus the temperature-thermometer framing:
 * 退潮/低迷 = cool/green, 中性 = neutral gray, 分歧加剧 = amber,
 * 发酵/活跃 = orange, 高潮/亢奋 = hot red. Unknown labels fall back to neutral.
 */
function sentimentPalette(label: string): { bg: string; border: string; ink: string } {
  if (label.includes("退潮") || label.includes("低迷")) {
    return { bg: "#e8f5ec", border: "#95d5a8", ink: "#237a3d" };
  }
  if (label.includes("高潮") || label.includes("亢奋")) {
    return { bg: "#fde8e8", border: "#f0a3a3", ink: "#c0322b" };
  }
  if (label.includes("发酵") || label.includes("活跃")) {
    return { bg: "#fdefe0", border: "#f2c07a", ink: "#c9611f" };
  }
  if (label.includes("分歧")) {
    return { bg: "#fdf6e3", border: "#e6d08a", ink: "#a8811a" };
  }
  // 中性 / unknown
  return { bg: "#f2f2f0", border: "#d0d0cc", ink: "#5c5c58" };
}

/** One labeled breadth stat tile. */
function StatTile({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "up" | "down" | "neutral";
}) {
  const color = tone === "up" ? "#c0322b" : tone === "down" ? "#237a3d" : "#3a3a37";
  return (
    <div className="flex min-w-[84px] flex-col items-center rounded-xl border border-shell-line bg-white/70 px-3 py-2">
      <span className="text-xs text-shell-muted">{label}</span>
      <span className="text-lg font-semibold tabular-nums" style={{ color }}>
        {value}
      </span>
    </div>
  );
}

/**
 * 情绪温度计卡: large sentiment label with state-driven color, the four core
 * breadth stats, and the fixed compliance disclaimer (always visible — it is a
 * regulatory requirement and must never be hidden).
 */
export function MarketSentimentCard({ data }: { data: MarketBreadthData }) {
  const { sentiment } = data;
  const palette = sentimentPalette(sentiment.label);
  const brokenPct = Number.isFinite(data.broken_board_rate)
    ? `${(data.broken_board_rate * 100).toFixed(1)}%`
    : "—";

  return (
    <div
      className="flex flex-col gap-4 rounded-2xl border p-5"
      style={{ backgroundColor: palette.bg, borderColor: palette.border }}
      aria-label="情绪温度计"
    >
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-col gap-1">
          <span className="text-xs uppercase tracking-wide text-shell-muted">情绪温度计</span>
          <span
            className="text-3xl font-bold leading-tight"
            style={{ color: palette.ink }}
            data-testid="sentiment-label"
          >
            {sentiment.label}
          </span>
          <span className="text-sm text-shell-muted">{sentiment.reason}</span>
        </div>
      </div>

      <div className="flex flex-wrap gap-3">
        <StatTile label="涨停" value={String(data.limit_up_count)} tone="up" />
        <StatTile label="跌停" value={String(data.limit_down_count)} tone="down" />
        <StatTile label="炸板" value={String(data.broken_board_count)} tone="neutral" />
        <StatTile label="炸板率" value={brokenPct} tone="neutral" />
        <StatTile label="最高连板" value={`${data.max_streak} 板`} tone="up" />
      </div>

      <Alert
        type="info"
        showIcon
        className="rounded-xl"
        message="合规提示"
        description={sentiment.disclaimer}
        data-testid="sentiment-disclaimer"
      />
    </div>
  );
}
