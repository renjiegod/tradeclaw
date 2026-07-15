import { Card, Row, Col, Statistic, Tag, Tooltip, Typography } from "antd";

import type { BacktestSummary, RunRow, TaskStatus } from "../types";
import { formatBacktestRange } from "../utils/datetime";
import { resolveBacktestDisplayStatus } from "../utils/taskStatus";

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
  const abs = Math.abs(n).toFixed(digits);
  return `${sign}${abs}%`;
}

function fmtRatioAsPct(v: string | null | undefined, digits = 2): string {
  if (v == null) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function isRunActive(status: string | null | undefined): boolean {
  return status === "running" || status === "paused" || status === "pending";
}

const KPI_CARD_CLASS =
  "!border !border-shell-line !bg-card-bg shadow-shell-card !rounded-xl";

function KpiCard({
  title,
  children,
  hint,
}: {
  title: string;
  children: React.ReactNode;
  hint?: string;
}) {
  return (
    <Card size="small" className={KPI_CARD_CLASS} variant="borderless">
      <div className="text-xs text-shell-muted">
        {title}
        {hint ? (
          <Tooltip title={hint}>
            <span className="ml-1 cursor-help text-shell-muted">ⓘ</span>
          </Tooltip>
        ) : null}
      </div>
      <div className="mt-1 text-lg font-semibold tabular-nums">{children}</div>
    </Card>
  );
}

function describeMaxDrawdown(summary: BacktestSummary): string | undefined {
  if (!summary.max_drawdown_peak_at || !summary.max_drawdown_trough_at) return undefined;
  const peak = fmtMoneyExact(summary.max_drawdown_peak_equity);
  const trough = fmtMoneyExact(summary.max_drawdown_trough_equity);
  return `峰值 ${peak} (${summary.max_drawdown_peak_at}) → 谷值 ${trough} (${summary.max_drawdown_trough_at})`;
}

export function BacktestSummaryHeader({ task, run }: Props) {
  const summary = task.backtest_summary ?? null;
  const status = resolveBacktestDisplayStatus(task.status, run?.status);
  const isError = status === "error";
  const isCompleted = status === "completed" && summary != null;
  const isRunning = !summary && isRunActive(run?.status);

  if (isError && !summary) {
    return (
      <Card className="!border !border-red-300 !bg-red-50 !rounded-xl">
        <div className="flex items-center justify-between">
          <div>
            <Typography.Text strong className="!text-red-700">
              回测失败
            </Typography.Text>
            <div className="mt-1 text-sm text-red-700">
              {task.last_error || run?.error_message || "未提供错误信息"}
            </div>
          </div>
          <Tag color="red">已失败</Tag>
        </div>
      </Card>
    );
  }

  let badge: React.ReactNode = null;
  if (isCompleted) {
    badge = <Tag color="green">已完成</Tag>;
  } else if (isError) {
    badge = <Tag color="red">已失败</Tag>;
  } else if (isRunning) {
    badge = <Tag color="processing">运行中</Tag>;
  }

  const closedCount = summary?.trade_count_closed ?? null;
  const openCount = summary?.trade_count_open ?? null;
  const winRateSample = summary?.win_rate_sample_size ?? 0;
  const winRateValue =
    summary == null
      ? "—"
      : winRateSample === 0
        ? "—"
        : fmtRatioAsPct(summary.win_rate);
  const mddValue =
    summary == null
      ? "—"
      : !summary.max_drawdown_peak_at
        ? "—"
        : `-${Number(summary.max_drawdown_pct).toFixed(2)}%`;
  const returnValue =
    summary != null
      ? fmtSignedPct(summary.return_pct)
      : run?.return_pct != null
        ? fmtSignedPct(run.return_pct)
        : "—";
  const equityValue =
    summary != null
      ? fmtMoneyExact(summary.ending_equity)
      : run?.ending_equity != null
        ? fmtMoneyExact(run.ending_equity)
        : "—";
  const tradeValue =
    summary != null
      ? `${summary.fills_count ?? 0}`
      : "—";
  const tradeHint =
    summary != null
      ? `共成交 ${summary.fills_count ?? 0} 笔（含买入与卖出）。已平仓 ${closedCount ?? 0} / 仍持仓 ${openCount ?? 0}`
      : undefined;
  const winRateHint =
    summary != null
      ? winRateSample === 0
        ? "暂无可统计的交易（既无已平仓 trade，也无可 mark-to-market 的持仓）"
        : `已平仓 + 持仓 mark-to-market 的盈利占比，N=${winRateSample}`
      : undefined;

  // Window bounds come from the finalized summary; fall back to the run row
  // while the backtest is still in flight (no summary yet).
  const rangeStart = summary?.range_start_utc ?? run?.range_start_utc ?? null;
  const rangeEnd = summary?.range_end_utc ?? run?.range_end_utc ?? null;
  const barInterval = summary?.bar_interval ?? run?.bar_interval ?? null;
  const hasRange = (rangeStart ?? "") !== "" || (rangeEnd ?? "") !== "";

  return (
    <div data-testid="backtest-summary-header">
      <Row gutter={[12, 12]}>
        <Col xs={24} sm={12} md={8} lg={5}>
          <KpiCard title="收益率">
            <Statistic value={returnValue} valueStyle={{ fontSize: "1.125rem", fontWeight: 600 }} />
          </KpiCard>
        </Col>
        <Col xs={24} sm={12} md={8} lg={5}>
          <KpiCard title="期末权益">{equityValue}</KpiCard>
        </Col>
        <Col xs={24} sm={12} md={8} lg={4}>
          <KpiCard title="交易次数" hint={tradeHint}>
            {tradeValue}
          </KpiCard>
        </Col>
        <Col xs={24} sm={12} md={8} lg={5}>
          <KpiCard title="胜率" hint={winRateHint}>
            {winRateValue}
          </KpiCard>
        </Col>
        <Col xs={24} sm={12} md={8} lg={5}>
          <KpiCard
            title="最大回撤"
            hint={summary ? describeMaxDrawdown(summary) : undefined}
          >
            {mddValue}
          </KpiCard>
        </Col>
      </Row>
      {badge || hasRange ? (
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
          {badge}
          {hasRange ? (
            <Typography.Text
              type="secondary"
              className="text-xs tabular-nums"
              data-testid="backtest-range"
            >
              回测区间：{formatBacktestRange(rangeStart, rangeEnd)}
              {barInterval ? ` · 周期 ${barInterval}` : ""}
            </Typography.Text>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
