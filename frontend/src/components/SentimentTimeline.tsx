import { ReloadOutlined } from "@ant-design/icons";
import { Button, Card, Empty, Spin, Tooltip, Typography, message } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";

import { getSentimentTimeline } from "../api";
import type { SentimentTimelinePoint } from "../types";

/** Months of history the top-level timeline requests by default. */
const DEFAULT_MONTHS = 3;

const EMPTY_HINT = "暂无情绪周期记录（每日复盘自动累积后出现）";

/**
 * Per-label visual palette for the day cells. A-share convention: red = hot /
 * climax, green = cool / ebb; amber calls out 分歧加剧, grey is neutral. The
 * label keys are the exact strings the backend's ``_classify_sentiment``
 * emits — anything unrecognised falls back to a neutral grey so a new backend
 * label never renders as an invisible/transparent cell.
 */
const LABEL_STYLES: Record<
  string,
  { cell: string; dot: string; text: string; short: string }
> = {
  "退潮/低迷": {
    // 冷绿 — ebb / weak
    cell: "bg-emerald-50 border-emerald-300 hover:bg-emerald-100",
    dot: "bg-emerald-500",
    text: "text-emerald-700",
    short: "退潮",
  },
  中性: {
    // 灰 — neutral
    cell: "bg-neutral-50 border-neutral-300 hover:bg-neutral-100",
    dot: "bg-neutral-400",
    text: "text-neutral-600",
    short: "中性",
  },
  "发酵/活跃": {
    // 橙 — fermenting / active
    cell: "bg-orange-50 border-orange-300 hover:bg-orange-100",
    dot: "bg-orange-500",
    text: "text-orange-700",
    short: "发酵",
  },
  "高潮/亢奋": {
    // 热红 — climax / euphoric
    cell: "bg-red-50 border-red-400 hover:bg-red-100",
    dot: "bg-red-600",
    text: "text-red-700",
    short: "高潮",
  },
  分歧加剧: {
    // 琥珀 — rising divergence
    cell: "bg-amber-50 border-amber-400 hover:bg-amber-100",
    dot: "bg-amber-500",
    text: "text-amber-700",
    short: "分歧",
  },
};

const FALLBACK_STYLE = {
  cell: "bg-neutral-50 border-neutral-300 hover:bg-neutral-100",
  dot: "bg-neutral-400",
  text: "text-neutral-600",
  short: "—",
};

function styleFor(label: string) {
  return LABEL_STYLES[label] ?? FALLBACK_STYLE;
}

/** Ordered legend from coolest → hottest, matching the palette above. */
const LEGEND: { label: string }[] = [
  { label: "退潮/低迷" },
  { label: "中性" },
  { label: "发酵/活跃" },
  { label: "分歧加剧" },
  { label: "高潮/亢奋" },
];

/** Format 炸板率 (0..1) as a percentage, or ``—`` when not finite. */
function formatRate(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(0)}%`;
}

/** Format a count, or ``—`` when not a finite number (never fabricate a 0). */
function formatCount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return String(value);
}

/** ``2026-05-30`` → ``05-30`` for the compact cell caption. */
function shortDate(date: string): string {
  const parts = date.split("-");
  return parts.length === 3 ? `${parts[1]}-${parts[2]}` : date;
}

/**
 * The per-day sentiment-cycle color band for the top of the Knowledge review
 * workbench. Renders each trading day as one cell coloured by its
 * ``_classify_sentiment`` label; hover (and the sub-row) reveal 涨停家数 /
 * 最高连板 / 炸板率. Pure div + Tailwind — no chart dependency.
 *
 * Data comes from {@link getSentimentTimeline}; it never fabricates values —
 * missing metrics show ``—`` and an empty base shows a friendly empty state.
 */
export function SentimentTimeline({ months = DEFAULT_MONTHS }: { months?: number }) {
  const [items, setItems] = useState<SentimentTimelinePoint[] | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getSentimentTimeline(months);
      setItems(res.items);
    } finally {
      setLoading(false);
    }
  }, [months]);

  useEffect(() => {
    void load().catch((error: unknown) => {
      const msg = error instanceof Error ? error.message : String(error);
      message.error(`加载情绪周期时间线失败：${msg}`);
    });
  }, [load]);

  const showEmpty = !loading && (!items || items.length === 0);

  const subtitle = useMemo(() => {
    if (!items || items.length === 0) return `近 ${months} 个月 · 每日复盘累积`;
    return `近 ${months} 个月 · ${items.length} 个交易日`;
  }, [items, months]);

  return (
    <Card
      className="!border !border-shell-line !bg-card-bg shadow-shell-card"
      title={
        <div className="flex flex-col">
          <Typography.Text strong>情绪周期时间线</Typography.Text>
          <Typography.Text type="secondary" className="!text-xs !font-normal">
            {subtitle}
          </Typography.Text>
        </div>
      }
      extra={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={() =>
            void load().catch((error: unknown) => {
              const msg = error instanceof Error ? error.message : String(error);
              message.error(`加载情绪周期时间线失败：${msg}`);
            })
          }
        >
          刷新
        </Button>
      }
      data-testid="sentiment-timeline"
    >
      {loading ? (
        <div className="flex min-h-[160px] items-center justify-center">
          <Spin />
        </div>
      ) : showEmpty ? (
        <Empty
          description={EMPTY_HINT}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          data-testid="sentiment-timeline-empty"
        />
      ) : (
        <div className="flex flex-col gap-3">
          {/* Legend — coolest (退潮) to hottest (高潮). */}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1" data-testid="sentiment-legend">
            {LEGEND.map(({ label }) => {
              const s = styleFor(label);
              return (
                <span key={label} className="flex items-center gap-1.5 text-xs">
                  <span className={`inline-block h-3 w-3 rounded-sm ${s.dot}`} />
                  <Typography.Text type="secondary" className="!text-xs">
                    {label}
                  </Typography.Text>
                </span>
              );
            })}
          </div>

          {/* The color band: one cell per trading day, ascending by date. */}
          <div className="flex flex-wrap gap-1.5" data-testid="sentiment-band">
            {(items ?? []).map((pt) => (
              <DayCell key={pt.date} point={pt} />
            ))}
          </div>

          <Typography.Text type="secondary" className="!text-[11px]">
            仅描述当日情绪状态，非预测、非买卖建议。
          </Typography.Text>
        </div>
      )}
    </Card>
  );
}

/** One trading-day cell: coloured by label, date caption + hover tooltip. */
function DayCell({ point }: { point: SentimentTimelinePoint }) {
  const s = styleFor(point.label);
  return (
    <Tooltip
      title={
        <div className="flex flex-col gap-0.5 text-xs">
          <span className="font-semibold">
            {point.date} · {point.label}
          </span>
          <span>涨停 {formatCount(point.limit_up_count)} 家</span>
          <span>跌停 {formatCount(point.limit_down_count)} 家</span>
          <span>炸板 {formatCount(point.broken_board_count)} 家</span>
          <span>最高连板 {formatCount(point.max_streak)}</span>
          <span>炸板率 {formatRate(point.broken_board_rate)}</span>
        </div>
      }
    >
      <div
        className={`flex w-[64px] cursor-default flex-col items-center gap-0.5 rounded-md border px-1 py-1.5 transition-colors ${s.cell}`}
        data-testid="sentiment-day-cell"
        data-date={point.date}
        data-label={point.label}
      >
        <span className={`text-[11px] font-medium leading-none ${s.text}`}>{s.short}</span>
        <span className="text-[10px] leading-none text-shell-muted">{shortDate(point.date)}</span>
        {/* Sub-row: the headline metric (涨停家数) without needing hover. */}
        <span className="text-[10px] leading-none text-shell-muted">
          涨{formatCount(point.limit_up_count)}
        </span>
      </div>
    </Tooltip>
  );
}

export default SentimentTimeline;
