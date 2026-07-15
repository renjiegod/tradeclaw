import { Card, Col, Empty, Row, Table, Tag, Tooltip, Typography } from "antd";
import { useMemo } from "react";
import {
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from "recharts";

import { useSymbolNames } from "../hooks/useSymbolNames";
import type {
  BacktestSummary,
  BacktestSummaryEquityPoint,
  BacktestSummaryExitReasonStat,
  BacktestSummaryFinalPosition,
  BacktestSummarySymbolStat,
  BacktestSummaryTagStat,
  RunRow,
  TaskStatus,
} from "../types";

type Props = {
  task: TaskStatus;
  run: RunRow | null;
};

function fmtMoneyExact(v: string | number | null | undefined): string {
  if (v == null) return "—";
  if (typeof v === "string") {
    const t = v.trim();
    if (!t) return "—";
    const n = Number(t);
    if (!Number.isFinite(n)) return v;
    return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  if (Number.isNaN(v)) return "—";
  return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtSignedPct(v: string | number | null | undefined, digits = 2): string {
  if (v == null) return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  const sign = n > 0 ? "+" : n < 0 ? "-" : "";
  return `${sign}${Math.abs(n).toFixed(digits)}%`;
}

function fmtRatioAsPct(v: string | null | undefined, digits = 2): string {
  if (v == null) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function fmtIntegerString(v: string | number | null | undefined, digits = 2): string {
  if (v == null) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return n.toFixed(digits);
}

/** Render an optional bare-percent string (already in percent units) with a
 * fixed digit count. ``null``/``undefined`` map to 「—」 — these mean
 * "undefined" (e.g. Sharpe with zero stdev), not "zero". */
function fmtOptionalPct(v: string | null | undefined, digits = 2): string {
  if (v == null) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${n.toFixed(digits)}%`;
}

/** Render an optional unitless ratio (Sharpe / Calmar / profit_factor)
 * with a fixed digit count. Same null-handling as ``fmtOptionalPct``. */
function fmtOptionalRatio(v: string | null | undefined, digits = 2): string {
  if (v == null) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return n.toFixed(digits);
}

/** Render an optional signed money string with locale formatting. ``null``
 * → 「—」. Used for ``avg_win_pnl`` / ``avg_loss_pnl``. */
function fmtOptionalSignedMoney(v: string | null | undefined): string {
  if (v == null) return "—";
  const t = v.trim();
  if (!t) return "—";
  const n = Number(t);
  if (!Number.isFinite(n)) return "—";
  const sign = n < 0 ? "-" : "";
  const formatted = Math.abs(n).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${sign}${formatted}`;
}

function moneyValueFromString(v: string | null | undefined): number {
  if (v == null) return Number.NEGATIVE_INFINITY;
  const n = Number(v);
  return Number.isFinite(n) ? n : Number.NEGATIVE_INFINITY;
}

const METRIC_CARD_CLASS = "!border !border-shell-line !bg-card-bg shadow-shell-card !rounded-xl";

function MetricCard({ title, value, hint }: { title: string; value: React.ReactNode; hint?: string }) {
  return (
    <Card size="small" className={METRIC_CARD_CLASS} variant="borderless">
      <div className="text-xs text-shell-muted">
        {title}
        {hint ? (
          <Tooltip title={hint}>
            <span className="ml-1 cursor-help text-shell-muted">ⓘ</span>
          </Tooltip>
        ) : null}
      </div>
      <div className="mt-1 text-base font-semibold tabular-nums">{value}</div>
    </Card>
  );
}

type EquitySeriesPoint = {
  t: string;
  equity: number;
  returnPct: number;
};

function buildEquitySeries(
  curve: BacktestSummaryEquityPoint[],
  startingEquity: string | number | null | undefined,
): EquitySeriesPoint[] {
  const base = Number(startingEquity);
  if (!Number.isFinite(base) || base <= 0) return [];
  return curve
    .map((p) => {
      const equity = Number(p.equity);
      if (!p.t || !Number.isFinite(equity)) return null;
      return {
        t: p.t,
        equity,
        returnPct: ((equity / base) - 1) * 100,
      };
    })
    .filter((p): p is EquitySeriesPoint => p != null);
}

function fmtAxisTime(value: string): string {
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) return value;
  const d = new Date(ms);
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${month}-${day}`;
}

function fmtFullTime(value: string): string {
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) return value;
  const d = new Date(ms);
  return d.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function EquityCurve({ summary }: { summary: BacktestSummary }) {
  const curve = summary.equity_curve ?? [];
  const series = useMemo(
    () => buildEquitySeries(curve, summary.starting_equity),
    [curve, summary.starting_equity],
  );

  if (series.length < 2) {
    return (
      <Card size="small" className="!rounded-xl !border !border-shell-line !bg-card-bg">
        <Empty description="暂无权益曲线" />
      </Card>
    );
  }

  return (
    <Card
      size="small"
      className="!rounded-xl !border !border-shell-line !bg-card-bg"
      title={
        <div className="flex items-center justify-between">
          <span>权益曲线</span>
          {summary.equity_curve_meta.downsampled ? (
            <Tag color="orange" data-testid="bt-equity-downsampled-tag">
              {`已下采样（原始 ${summary.equity_curve_meta.raw_length} 点）`}
            </Tag>
          ) : null}
        </div>
      }
    >
      <div data-testid="backtest-equity-chart" className="h-[220px] w-full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={series} margin={{ top: 8, right: 16, left: 8, bottom: 8 }}>
            <XAxis dataKey="t" tickFormatter={fmtAxisTime} minTickGap={24} />
            <YAxis tickFormatter={(v) => fmtSignedPct(v, 2)} width={68} />
            <RechartsTooltip
              formatter={(value: number, name: string, payload: { payload: EquitySeriesPoint }) => {
                if (name === "returnPct") return [fmtSignedPct(value, 2), "累计收益率"];
                if (name === "equity") return [fmtMoneyExact(payload.payload.equity), "权益"];
                return [String(value), name];
              }}
              labelFormatter={(label: string) => `时间：${fmtFullTime(label)}`}
            />
            <Line
              type="monotone"
              dataKey="returnPct"
              name="returnPct"
              stroke="#1677ff"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}

function PositionsTable({ positions }: { positions: BacktestSummaryFinalPosition[] }) {
  const sorted = useMemo(
    () =>
      [...positions].sort(
        (a, b) => moneyValueFromString(b.market_value) - moneyValueFromString(a.market_value),
      ),
    [positions],
  );

  return (
    <Card
      size="small"
      className="!rounded-xl !border !border-shell-line !bg-card-bg"
      title="最终持仓"
    >
      <Table<BacktestSummaryFinalPosition>
        rowKey="symbol"
        size="small"
        pagination={false}
        dataSource={sorted}
        columns={[
          {
            title: "代码",
            dataIndex: "symbol",
            render: (v: string) => (
              <Typography.Text copyable={{ text: v }} className="font-mono">
                {v}
              </Typography.Text>
            ),
          },
          { title: "名称", dataIndex: "name", render: (v: string | null | undefined) => v ?? "—" },
          {
            title: "数量",
            dataIndex: "quantity",
            sorter: (a, b) => a.quantity - b.quantity,
          },
          {
            title: "成本价",
            dataIndex: "cost_price",
            sorter: (a, b) => Number(a.cost_price) - Number(b.cost_price),
            render: (v: string) => fmtMoneyExact(v),
          },
          {
            title: "最新价",
            dataIndex: "last_price",
            sorter: (a, b) => Number(a.last_price ?? 0) - Number(b.last_price ?? 0),
            render: (v: string | null | undefined) => fmtMoneyExact(v),
          },
          {
            title: "市值",
            dataIndex: "market_value",
            defaultSortOrder: "descend",
            sorter: (a, b) => moneyValueFromString(a.market_value) - moneyValueFromString(b.market_value),
            render: (v: string | null | undefined) => fmtMoneyExact(v),
          },
        ]}
      />
    </Card>
  );
}

function describeMaxDrawdown(summary: BacktestSummary): string | undefined {
  if (!summary.max_drawdown_peak_at || !summary.max_drawdown_trough_at) return undefined;
  return `${fmtMoneyExact(summary.max_drawdown_peak_equity)} → ${fmtMoneyExact(summary.max_drawdown_trough_equity)}`;
}

function MetricsGrid({ summary }: { summary: BacktestSummary }) {
  const winRateSample = summary.win_rate_sample_size ?? 0;
  const avgHoldingSample = summary.avg_holding_sample_size ?? 0;
  const winRateValue = winRateSample === 0 ? "—" : fmtRatioAsPct(summary.win_rate);
  const avgHoldingValue =
    avgHoldingSample === 0 ? "—" : fmtIntegerString(summary.avg_holding_trading_days);
  const mddValue =
    summary.max_drawdown_peak_at == null
      ? "—"
      : `-${Number(summary.max_drawdown_pct).toFixed(2)}%`;
  const fillsCount = summary.fills_count ?? 0;
  const tradeBreakdown = (
    <span className="text-xs text-shell-muted ml-2 font-normal">
      已平仓 {summary.trade_count_closed ?? 0} · 持仓 {summary.trade_count_open ?? 0}
    </span>
  );

  return (
    <Row gutter={[12, 12]}>
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard title="起始权益" value={fmtMoneyExact(summary.starting_equity)} />
      </Col>
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard title="期末权益" value={fmtMoneyExact(summary.ending_equity)} />
      </Col>
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard title="收益率" value={fmtSignedPct(summary.return_pct)} />
      </Col>
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard title="最终现金" value={fmtMoneyExact(summary.final_cash)} />
      </Col>
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard title="最终市值" value={fmtMoneyExact(summary.final_market_value)} />
      </Col>
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard
          title="交易次数"
          value={
            <span className="inline-flex items-baseline">
              <span>{fillsCount}</span>
              {tradeBreakdown}
            </span>
          }
          hint="买入 + 卖出 的总成交笔数；副信息为 FIFO 视角的『已平仓』和『仍持仓符号』"
        />
      </Col>
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard
          title="胜率"
          value={winRateValue}
          hint={
            winRateSample === 0
              ? "暂无可统计的交易（既无已平仓 trade，也无可 mark-to-market 的持仓）"
              : `已平仓 + 持仓 mark-to-market 的盈利占比，N=${winRateSample}`
          }
        />
      </Col>
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard
          title="平均持仓(交易日)"
          value={avgHoldingValue}
          hint={
            avgHoldingSample === 0
              ? "暂无成交"
              : `按交易日计；包含已平仓与未平仓 lot（未平仓以回测末日为退出时刻），N=${avgHoldingSample}`
          }
        />
      </Col>
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard title="最大回撤" value={mddValue} hint={describeMaxDrawdown(summary)} />
      </Col>
    </Row>
  );
}

function RiskMetricsGrid({ summary }: { summary: BacktestSummary }) {
  // All fields here are optional on the type — older persisted summaries
  // pre-date the metric extension. Skip the entire section when every value
  // is missing so the panel doesn't show eight 「—」 cards in a row.
  const hasAny =
    summary.annual_return_pct != null ||
    summary.volatility_annual_pct != null ||
    summary.sharpe != null ||
    summary.sortino != null ||
    summary.calmar != null ||
    summary.profit_factor != null ||
    summary.avg_win_pnl != null ||
    summary.avg_loss_pnl != null ||
    summary.profit_loss_ratio != null ||
    (summary.max_consecutive_losses != null && summary.max_consecutive_losses > 0);
  if (!hasAny) return null;

  return (
    <Card
      size="small"
      className="!rounded-xl !border !border-shell-line !bg-card-bg"
      title="风险调整与盈亏统计"
      data-testid="backtest-risk-metrics"
    >
      <Row gutter={[12, 12]}>
        <Col xs={24} sm={12} md={8} lg={6}>
          <MetricCard
            title="年化收益"
            value={fmtOptionalPct(summary.annual_return_pct)}
            hint="CAGR：基于区间起末权益与自然日跨度（365.25 天/年）计算。"
          />
        </Col>
        <Col xs={24} sm={12} md={8} lg={6}>
          <MetricCard
            title="年化波动率"
            value={fmtOptionalPct(summary.volatility_annual_pct)}
            hint="期内 bar-to-bar 简单收益的样本标准差，按 bars-per-year 年化。"
          />
        </Col>
        <Col xs={24} sm={12} md={8} lg={6}>
          <MetricCard
            title="Sharpe"
            value={fmtOptionalRatio(summary.sharpe)}
            hint="无风险利率 0，按年化波动率归一。等权益（波动率=0）时为 —。"
          />
        </Col>
        <Col xs={24} sm={12} md={8} lg={6}>
          <MetricCard
            title="Sortino"
            value={fmtOptionalRatio(summary.sortino)}
            hint="只使用下行偏差。无下行交易时为 —。"
          />
        </Col>
        <Col xs={24} sm={12} md={8} lg={6}>
          <MetricCard
            title="Calmar"
            value={fmtOptionalRatio(summary.calmar)}
            hint="年化收益 / |最大回撤|。最大回撤为 0 时为 —。"
          />
        </Col>
        <Col xs={24} sm={12} md={8} lg={6}>
          <MetricCard
            title="盈亏因子"
            value={fmtOptionalRatio(summary.profit_factor)}
            hint="总盈利 / |总亏损|。无亏损交易时为 — （不是 ∞）。"
          />
        </Col>
        <Col xs={24} sm={12} md={8} lg={6}>
          <MetricCard
            title="平均盈利 / 亏损"
            value={`${fmtOptionalSignedMoney(summary.avg_win_pnl)} / ${fmtOptionalSignedMoney(summary.avg_loss_pnl)}`}
            hint="按笔平均，仅统计已平仓 trades。"
          />
        </Col>
        <Col xs={24} sm={12} md={8} lg={6}>
          <MetricCard
            title="盈亏比"
            value={fmtOptionalRatio(summary.profit_loss_ratio)}
            hint="|平均盈利| / |平均亏损|。无任一边时为 —。"
          />
        </Col>
        <Col xs={24} sm={12} md={8} lg={6}>
          <MetricCard
            title="最大连亏"
            value={
              summary.max_consecutive_losses != null
                ? String(summary.max_consecutive_losses)
                : "—"
            }
            hint="按 exit_time 排序的全局连亏笔数；跨标的合并计算。"
          />
        </Col>
      </Row>
    </Card>
  );
}

function BySymbolTable({ rows }: { rows: BacktestSummarySymbolStat[] }) {
  const symbolNames = useSymbolNames(rows.map((row) => row.symbol));
  if (!rows.length) return null;
  return (
    <Card
      size="small"
      className="!rounded-xl !border !border-shell-line !bg-card-bg"
      title="按标的拆解（按 |PnL| 排序）"
      data-testid="backtest-by-symbol"
    >
      <Table<BacktestSummarySymbolStat>
        rowKey="symbol"
        size="small"
        pagination={false}
        dataSource={rows}
        columns={[
          {
            title: "标的",
            dataIndex: "symbol",
            render: (v: string) => (
              <Typography.Text copyable={{ text: v }} className="font-mono">
                {v}
              </Typography.Text>
            ),
          },
          {
            title: "名称",
            key: "name",
            render: (_: unknown, record) => symbolNames[record.symbol] ?? "—",
          },
          {
            title: "平仓笔数",
            dataIndex: "trade_count_closed",
            sorter: (a, b) => a.trade_count_closed - b.trade_count_closed,
          },
          {
            title: "PnL",
            dataIndex: "pnl",
            defaultSortOrder: "descend",
            sorter: (a, b) => Number(a.pnl) - Number(b.pnl),
            render: (v: string) => (
              <span
                className={
                  Number(v) > 0
                    ? "text-emerald-600"
                    : Number(v) < 0
                      ? "text-rose-600"
                      : undefined
                }
              >
                {fmtOptionalSignedMoney(v)}
              </span>
            ),
          },
          {
            title: "胜率",
            dataIndex: "win_rate",
            sorter: (a, b) => Number(a.win_rate) - Number(b.win_rate),
            render: (v: string) => fmtRatioAsPct(v),
          },
          {
            title: "平均持仓(交易日)",
            dataIndex: "avg_holding_trading_days",
            sorter: (a, b) =>
              Number(a.avg_holding_trading_days) - Number(b.avg_holding_trading_days),
            render: (v: string) => fmtIntegerString(v),
          },
        ]}
      />
    </Card>
  );
}

const EXIT_REASON_LABELS: Record<string, string> = {
  signal: "信号",
  stop_loss: "止损",
  take_profit: "止盈",
  trailing_stop: "移动止损",
  roi: "ROI",
  circuit_breaker: "熔断",
};

function ByExitReasonTable({ rows }: { rows: BacktestSummaryExitReasonStat[] }) {
  if (!rows.length) return null;
  return (
    <Card
      size="small"
      className="!rounded-xl !border !border-shell-line !bg-card-bg"
      title="按退出原因拆解（按 |PnL| 排序）"
      data-testid="backtest-by-exit-reason"
    >
      <Table<BacktestSummaryExitReasonStat>
        rowKey="exit_reason"
        size="small"
        pagination={false}
        dataSource={rows}
        columns={[
          {
            title: "退出原因",
            dataIndex: "exit_reason",
            render: (v: string) => EXIT_REASON_LABELS[v] ?? v,
          },
          {
            title: "平仓笔数",
            dataIndex: "trade_count_closed",
            sorter: (a, b) => a.trade_count_closed - b.trade_count_closed,
          },
          {
            title: "PnL",
            dataIndex: "pnl",
            defaultSortOrder: "descend",
            sorter: (a, b) => Number(a.pnl) - Number(b.pnl),
            render: (v: string) => (
              <span
                className={
                  Number(v) > 0
                    ? "text-emerald-600"
                    : Number(v) < 0
                      ? "text-rose-600"
                      : undefined
                }
              >
                {fmtOptionalSignedMoney(v)}
              </span>
            ),
          },
          {
            title: "胜率",
            dataIndex: "win_rate",
            sorter: (a, b) => Number(a.win_rate) - Number(b.win_rate),
            render: (v: string) => fmtRatioAsPct(v),
          },
          {
            title: "平均持仓(交易日)",
            dataIndex: "avg_holding_trading_days",
            sorter: (a, b) =>
              Number(a.avg_holding_trading_days) - Number(b.avg_holding_trading_days),
            render: (v: string) => fmtIntegerString(v),
          },
        ]}
      />
    </Card>
  );
}

function ByTagTable({ rows }: { rows: BacktestSummaryTagStat[] }) {
  if (!rows.length) return null;
  return (
    <Card
      size="small"
      className="!rounded-xl !border !border-shell-line !bg-card-bg"
      title="按入场因子拆解（按 |PnL| 排序）"
      data-testid="backtest-by-tag"
    >
      <Table<BacktestSummaryTagStat>
        rowKey="tag"
        size="small"
        pagination={false}
        dataSource={rows}
        columns={[
          {
            title: "入场因子",
            dataIndex: "tag",
            render: (v: string) => v,
          },
          {
            title: "平仓笔数",
            dataIndex: "trade_count_closed",
            sorter: (a, b) => a.trade_count_closed - b.trade_count_closed,
          },
          {
            title: "PnL",
            dataIndex: "pnl",
            defaultSortOrder: "descend",
            sorter: (a, b) => Number(a.pnl) - Number(b.pnl),
            render: (v: string) => (
              <span
                className={
                  Number(v) > 0
                    ? "text-emerald-600"
                    : Number(v) < 0
                      ? "text-rose-600"
                      : undefined
                }
              >
                {fmtOptionalSignedMoney(v)}
              </span>
            ),
          },
          {
            title: "胜率",
            dataIndex: "win_rate",
            sorter: (a, b) => Number(a.win_rate) - Number(b.win_rate),
            render: (v: string) => fmtRatioAsPct(v),
          },
          {
            title: "平均持仓(交易日)",
            dataIndex: "avg_holding_trading_days",
            sorter: (a, b) =>
              Number(a.avg_holding_trading_days) - Number(b.avg_holding_trading_days),
            render: (v: string) => fmtIntegerString(v),
          },
        ]}
      />
    </Card>
  );
}

export function BacktestOverviewPanel({ task, run }: Props) {
  const summary = task.backtest_summary ?? null;

  if (!summary) {
    const completed = run?.bars_completed ?? 0;
    const total = run?.bars_total ?? 0;
    return (
      <Card className="!rounded-xl !border !border-shell-line !bg-card-bg">
        <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
          <Typography.Text strong>回测尚未结束</Typography.Text>
          <Typography.Text type="secondary">
            完成后将在此处显示概览数据。当前进度：{`${completed} / ${total}`}
          </Typography.Text>
        </div>
      </Card>
    );
  }

  return (
    <div data-testid="backtest-overview-panel" className="flex flex-col gap-4">
      <MetricsGrid summary={summary} />
      <RiskMetricsGrid summary={summary} />
      <EquityCurve summary={summary} />
      <BySymbolTable rows={summary.by_symbol ?? []} />
      <ByExitReasonTable rows={summary.by_exit_reason ?? []} />
      <ByTagTable rows={summary.by_tag ?? []} />
      <PositionsTable positions={summary.final_positions} />
    </div>
  );
}
