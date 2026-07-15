import { ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, Card, Collapse, Empty, Spin, Table, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";

import { getTradeAttribution } from "../api";
import type {
  TradeAttribution,
  TradeAttributionBySymbol,
  TradeAttributionExtreme,
  TradeAttributionRoundTrip,
} from "../types";
import { ChangePctTag, formatAmount } from "./StockDetailModal";

/** Months of settlement history the panel requests by default. */
const DEFAULT_MONTHS = 6;

const EMPTY_HINT =
  "暂无可归因的交割单（把券商导出的交割单放进 knowledge 的 trades/ 后出现）";

/** Fallback for any missing / non-finite value. Never fabricate a number. */
const DASH = "—";

/**
 * Convert a backend decimal money string into a number for display only.
 * Returns null (→ ``—``) for empty / non-finite input so we never fabricate a
 * value. Money is always rendered through {@link formatAmount}; the raw
 * decimal string stays authoritative for anything else.
 */
function moneyToNumber(value: string | null | undefined): number | null {
  const raw = value?.trim();
  if (!raw) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

/** A-share red-up / green-down colour for a signed money number. */
function pnlColor(value: number | null): string | undefined {
  if (value == null) return undefined;
  if (value > 0) return "#cf1322"; // red — gain
  if (value < 0) return "#389e0d"; // green — loss
  return undefined;
}

/** Render a signed money decimal string as 亿/万 with red-up / green-down. */
function MoneyPnl({ value }: { value: string | null | undefined }) {
  const num = moneyToNumber(value);
  if (num == null) return <span>{DASH}</span>;
  return <span style={{ color: pnlColor(num) }}>{formatAmount(num)}</span>;
}

/** Format a ``0..1`` win-rate ratio as a percentage, or ``—`` when null. */
function formatWinRate(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return `${(value * 100).toFixed(1)}%`;
}

/** Format a bare count, or ``—`` when not a finite number. */
function formatCount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return String(value);
}

/** Format a plain number (profit factor / hold days), or ``—`` when null. */
function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return value.toFixed(digits);
}

/** Trim an authored string, or ``—`` when blank. Never fabricate. */
function orDash(value: string | null | undefined): string {
  const trimmed = value?.trim();
  return trimmed ? trimmed : DASH;
}

/**
 * The 交割单归因 (trade attribution) board for the Knowledge review workbench.
 * Reconstructs realized-PnL round-trips from the broker settlement statements
 * the user dropped into the private knowledge base's ``trades/`` partition and
 * renders: a headline summary strip (回合数 / 胜率 / 总已实现盈亏 / 盈亏比 /
 * 平均持仓天数 / 未平仓 + best/worst cards), a per-round-trip detail table, and
 * a collapsible per-symbol summary.
 *
 * Data comes from {@link getTradeAttribution}; money fields are decimal strings
 * converted to numbers only for display via {@link formatAmount}. It never
 * fabricates values — missing fields render ``—``, an empty base shows a
 * friendly empty state, and files that failed to parse are surfaced honestly in
 * a warning Alert rather than silently dropped.
 */
export function TradeAttributionPanel({ months = DEFAULT_MONTHS }: { months?: number }) {
  const [data, setData] = useState<TradeAttribution | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getTradeAttribution(months);
      setData(res);
    } finally {
      setLoading(false);
    }
  }, [months]);

  useEffect(() => {
    void load().catch((error: unknown) => {
      const msg = error instanceof Error ? error.message : String(error);
      message.error(`加载交割单归因失败：${msg}`);
    });
  }, [load]);

  const roundTrips = data?.round_trips ?? [];
  const bySymbol = data?.by_symbol ?? [];
  const unparsed = data?.unparsed ?? [];
  const summary = data?.summary ?? null;

  const showEmpty =
    !loading && (!summary || (summary.round_trips === 0 && summary.open_positions === 0));

  const subtitle = useMemo(() => {
    if (!summary) return `近 ${months} 个月 · 券商交割单归因`;
    return `近 ${months} 个月 · 共 ${summary.round_trips} 个回合 · 券商交割单归因`;
  }, [summary, months]);

  return (
    <Card
      className="!border !border-shell-line !bg-card-bg shadow-shell-card"
      title={
        <div className="flex flex-col">
          <Typography.Text strong>交割单归因</Typography.Text>
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
              message.error(`加载交割单归因失败：${msg}`);
            })
          }
        >
          刷新
        </Button>
      }
      data-testid="trade-attribution-panel"
    >
      {loading ? (
        <div className="flex min-h-[160px] items-center justify-center">
          <Spin />
        </div>
      ) : showEmpty ? (
        <div className="flex flex-col gap-3">
          {unparsed.length > 0 ? <UnparsedAlert count={unparsed.length} unparsed={unparsed} /> : null}
          <Empty
            description={EMPTY_HINT}
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            data-testid="trade-attribution-empty"
          />
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          {unparsed.length > 0 ? <UnparsedAlert count={unparsed.length} unparsed={unparsed} /> : null}

          {summary ? <SummaryStrip summary={summary} /> : null}

          <RoundTripTable rows={roundTrips} />

          {bySymbol.length > 0 ? (
            <Collapse
              ghost
              data-testid="trade-attribution-by-symbol-collapse"
              items={[
                {
                  key: "by_symbol",
                  label: `按标的汇总（${bySymbol.length}）`,
                  children: <BySymbolTable rows={bySymbol} />,
                },
              ]}
            />
          ) : null}

          <Typography.Text type="secondary" className="!text-[11px]">
            数据来自你导入的券商交割单，仅对已实现盈亏做客观归因复盘，非预测、非买卖建议。
          </Typography.Text>
        </div>
      )}
    </Card>
  );
}

/** Honest warning that some settlement files could not be parsed. */
function UnparsedAlert({
  count,
  unparsed,
}: {
  count: number;
  unparsed: TradeAttribution["unparsed"];
}) {
  return (
    <Alert
      type="warning"
      showIcon
      className="!border-amber-300"
      data-testid="trade-attribution-unparsed"
      message={`${count} 个交割单文件无法解析：券商列名未识别，未纳入统计`}
      description={
        <div className="flex flex-col gap-0.5 text-xs">
          {unparsed.map((u) => (
            <div key={u.path} className="flex flex-wrap gap-x-2">
              <span className="font-medium text-shell-ink">{orDash(u.path)}</span>
              <span className="text-shell-muted">{orDash(u.reason)}</span>
            </div>
          ))}
        </div>
      }
    />
  );
}

/** The headline summary statistic strip + best / worst extreme cards. */
function SummaryStrip({ summary }: { summary: NonNullable<TradeAttribution["summary"]> }) {
  const totalNum = moneyToNumber(summary.total_realized_pnl);
  const stats: { label: string; node: React.ReactNode }[] = [
    { label: "回合数", node: formatCount(summary.round_trips) },
    { label: "胜率", node: formatWinRate(summary.win_rate) },
    {
      label: "总已实现盈亏",
      node: (
        <span style={{ color: pnlColor(totalNum) }}>
          {totalNum == null ? DASH : formatAmount(totalNum)}
        </span>
      ),
    },
    { label: "盈亏比", node: formatNumber(summary.profit_factor) },
    {
      label: "平均持仓天数",
      node: summary.avg_hold_days == null ? DASH : formatNumber(summary.avg_hold_days, 1),
    },
    { label: "未平仓", node: formatCount(summary.open_positions) },
  ];

  return (
    <div className="flex flex-col gap-3" data-testid="trade-attribution-summary">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        {stats.map((s) => (
          <div
            key={s.label}
            className="flex flex-col gap-1 rounded-lg border border-shell-line bg-white/60 p-3"
            data-testid="trade-attribution-stat"
            data-stat={s.label}
          >
            <span className="text-[11px] text-shell-muted">{s.label}</span>
            <span className="text-base font-semibold text-shell-ink">{s.node}</span>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <ExtremeCard kind="best" extreme={summary.best} />
        <ExtremeCard kind="worst" extreme={summary.worst} />
      </div>
    </div>
  );
}

/** One best / worst round-trip highlight card. */
function ExtremeCard({
  kind,
  extreme,
}: {
  kind: "best" | "worst";
  extreme: TradeAttributionExtreme | null;
}) {
  const title = kind === "best" ? "最赚回合" : "最亏回合";
  return (
    <div
      className="flex flex-col gap-1 rounded-lg border border-shell-line bg-white/60 p-3"
      data-testid={`trade-attribution-${kind}`}
    >
      <span className="text-[11px] text-shell-muted">{title}</span>
      {extreme ? (
        <div className="flex items-center justify-between gap-2">
          <div className="flex flex-col leading-tight">
            <span className="text-sm font-semibold text-shell-ink">{orDash(extreme.symbol)}</span>
            <span className="text-xs text-shell-muted">{orDash(extreme.name)}</span>
          </div>
          <div className="flex items-center gap-2">
            <MoneyPnl value={extreme.realized_pnl} />
            <ChangePctTag value={extreme.return_pct} />
          </div>
        </div>
      ) : (
        <span className="text-sm text-shell-muted">{DASH}</span>
      )}
    </div>
  );
}

/** The per-round-trip detail table (sorted by close date desc; sortable by PnL). */
function RoundTripTable({ rows }: { rows: TradeAttributionRoundTrip[] }) {
  const columns: ColumnsType<TradeAttributionRoundTrip> = [
    {
      title: "标的",
      key: "symbol",
      render: (_: unknown, record) => (
        <div className="flex flex-col leading-tight">
          <span className="font-medium text-shell-ink">{orDash(record.symbol)}</span>
          <span className="text-xs text-shell-muted">{orDash(record.name)}</span>
        </div>
      ),
    },
    { title: "开仓日", dataIndex: "open_date", key: "open_date", render: (v: string) => orDash(v) },
    { title: "平仓日", dataIndex: "close_date", key: "close_date", render: (v: string) => orDash(v) },
    {
      title: "持仓天数",
      dataIndex: "hold_days",
      key: "hold_days",
      align: "right",
      render: (v: number | null) => formatCount(v),
    },
    {
      title: "数量",
      dataIndex: "qty",
      key: "qty",
      align: "right",
      render: (v: number) => formatCount(v),
    },
    {
      title: "买均价",
      dataIndex: "avg_buy",
      key: "avg_buy",
      align: "right",
      render: (v: string) => orDash(v),
    },
    {
      title: "卖均价",
      dataIndex: "avg_sell",
      key: "avg_sell",
      align: "right",
      render: (v: string) => orDash(v),
    },
    {
      title: "已实现盈亏",
      key: "realized_pnl",
      align: "right",
      // Sort by the numeric value of the decimal string; blank / non-finite
      // sort as -Infinity so they sink, never crash the comparator.
      sorter: (a, b) =>
        (moneyToNumber(a.realized_pnl) ?? Number.NEGATIVE_INFINITY) -
        (moneyToNumber(b.realized_pnl) ?? Number.NEGATIVE_INFINITY),
      render: (_: unknown, record) => <MoneyPnl value={record.realized_pnl} />,
    },
    {
      title: "收益率",
      key: "return_pct",
      align: "right",
      render: (_: unknown, record) => <ChangePctTag value={record.return_pct} />,
    },
  ];

  // Default order: newest close first. Copy before sorting so we never mutate
  // the fetched array in place.
  const sorted = [...rows].sort((a, b) => (a.close_date < b.close_date ? 1 : a.close_date > b.close_date ? -1 : 0));

  return (
    <Table<TradeAttributionRoundTrip>
      size="small"
      rowKey={(r) => `${r.symbol}-${r.open_date}-${r.close_date}-${r.qty}-${r.realized_pnl}`}
      columns={columns}
      dataSource={sorted}
      pagination={false}
      scroll={{ x: "max-content" }}
      data-testid="trade-attribution-round-trips"
    />
  );
}

/** The collapsible per-symbol aggregate table. */
function BySymbolTable({ rows }: { rows: TradeAttributionBySymbol[] }) {
  const columns: ColumnsType<TradeAttributionBySymbol> = [
    {
      title: "标的",
      key: "symbol",
      render: (_: unknown, record) => (
        <div className="flex flex-col leading-tight">
          <span className="font-medium text-shell-ink">{orDash(record.symbol)}</span>
          <span className="text-xs text-shell-muted">{orDash(record.name)}</span>
        </div>
      ),
    },
    {
      title: "回合数",
      dataIndex: "round_trips",
      key: "round_trips",
      align: "right",
      render: (v: number) => formatCount(v),
    },
    {
      title: "累计盈亏",
      key: "realized_pnl",
      align: "right",
      render: (_: unknown, record) => <MoneyPnl value={record.realized_pnl} />,
    },
    {
      title: "胜率",
      dataIndex: "win_rate",
      key: "win_rate",
      align: "right",
      render: (v: number | null) => formatWinRate(v),
    },
  ];

  return (
    <Table<TradeAttributionBySymbol>
      size="small"
      rowKey={(r) => r.symbol}
      columns={columns}
      dataSource={rows}
      pagination={false}
      scroll={{ x: "max-content" }}
      data-testid="trade-attribution-by-symbol"
    />
  );
}

export default TradeAttributionPanel;
