import { Card, Col, Row, Table, Tag, Tooltip, Typography } from "antd";
import { useMemo } from "react";
import {
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { CycleRunRow, PostCycleAccountPositionRow } from "../types";
import { formatDateTimeUtc8 } from "../utils/datetime";
import {
  buildAccountReviewPoints as buildReviewPoints,
  fmtMoneyExact,
  summarizeAccountPoints,
  type AccountReviewPoint as ReviewPoint,
} from "../utils/cycleRunListFormat";

type Props = {
  /** Full cycle-run series for the task (any order); the panel sorts and filters. */
  rows: CycleRunRow[];
};

/** Signed percent (input already in percent units, e.g. ``5.5`` → ``+5.50%``). */
function fmtSignedPct(v: string | number | null | undefined, digits = 2): string {
  if (v == null) return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "—";
  const sign = n > 0 ? "+" : n < 0 ? "-" : "";
  return `${sign}${Math.abs(n).toFixed(digits)}%`;
}

/** Signed money (input is a decimal string / number, e.g. ``-1234.5`` → ``-1,234.50``). */
function fmtSignedMoney(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v < 0 ? "-" : v > 0 ? "+" : "";
  const formatted = Math.abs(v).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${sign}${formatted}`;
}

function fmtNum(n: number | null | undefined, digits = 0): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function fmtAxisTime(value: string): string {
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) return value;
  const d = new Date(ms);
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${month}-${day}`;
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

/** Equity for the chart. ``returnPct`` is cumulative vs the first qualifying point. */
type EquitySeriesPoint = {
  t: string;
  equity: number;
  returnPct: number;
};

function buildEquitySeries(points: ReviewPoint[]): EquitySeriesPoint[] {
  const base = points[0]?.equity;
  if (base == null || !Number.isFinite(base) || base <= 0) {
    return points.map((p) => ({ t: p.cycleTime, equity: p.equity, returnPct: 0 }));
  }
  return points.map((p) => ({
    t: p.cycleTime,
    equity: p.equity,
    returnPct: (p.equity / base - 1) * 100,
  }));
}

function PeriodSummary({ points }: { points: ReviewPoint[] }) {
  const first = points[0];
  const last = points[points.length - 1];
  // Same single source as the task-detail equity tiles — no drift between the
  // 复盘 区间盈亏/收益率 and the header 总盈亏. Parent only renders this for a
  // non-empty series, so the summary is always set.
  const summary = summarizeAccountPoints(points);
  if (!summary) return null;
  const { startEquity, endEquity, change, changePct } = summary;

  return (
    <Row gutter={[12, 12]} data-testid="task-review-summary">
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard title="起始权益" value={fmtMoneyExact(startEquity)} />
      </Col>
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard title="期末权益" value={fmtMoneyExact(endEquity)} />
      </Col>
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard
          title="区间盈亏"
          value={
            <span
              className={change > 0 ? "text-emerald-600" : change < 0 ? "text-rose-600" : undefined}
            >
              {fmtSignedMoney(change)}
            </span>
          }
          hint="期末权益 − 起始权益（区间内首末两个带账户快照的周期）。"
        />
      </Col>
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard
          title="区间收益率"
          value={
            <span
              className={
                changePct != null && changePct > 0
                  ? "text-emerald-600"
                  : changePct != null && changePct < 0
                    ? "text-rose-600"
                    : undefined
              }
            >
              {fmtSignedPct(changePct)}
            </span>
          }
        />
      </Col>
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard title="覆盖周期数" value={String(points.length)} hint="带账户快照的 cycle run 数量。" />
      </Col>
      <Col xs={24} sm={12} md={8} lg={8}>
        <MetricCard
          title="时间区间 (UTC+8)"
          value={
            <span className="text-sm">
              {formatDateTimeUtc8(first.cycleTime)} → {formatDateTimeUtc8(last.cycleTime)}
            </span>
          }
          hint="首个 → 最新 cycle 的逻辑时间。"
        />
      </Col>
    </Row>
  );
}

function fmtFullTime(value: string): string {
  return formatDateTimeUtc8(value, value);
}

function EquityTrend({ points }: { points: ReviewPoint[] }) {
  const series = useMemo(() => buildEquitySeries(points), [points]);

  return (
    <Card
      size="small"
      className="!rounded-xl !border !border-shell-line !bg-card-bg"
      title="账户权益走势"
    >
      {series.length < 2 ? (
        <Typography.Text type="secondary">至少需要两个带账户快照的周期才能绘制走势。</Typography.Text>
      ) : (
        <div data-testid="task-review-equity-chart" className="h-[220px] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={series} margin={{ top: 8, right: 16, left: 8, bottom: 8 }}>
              <XAxis dataKey="t" tickFormatter={fmtAxisTime} minTickGap={24} />
              <YAxis tickFormatter={(v) => fmtMoneyExact(v)} width={80} />
              <RechartsTooltip
                formatter={(value: number, name: string, payload: { payload: EquitySeriesPoint }) => {
                  if (name === "equity") return [fmtMoneyExact(value), "权益"];
                  if (name === "returnPct") return [fmtSignedPct(payload.payload.returnPct, 2), "累计收益率"];
                  return [String(value), name];
                }}
                labelFormatter={(label: string) => `时间：${fmtFullTime(label)}`}
              />
              <Line
                type="monotone"
                dataKey="equity"
                name="equity"
                stroke="#1677ff"
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </Card>
  );
}

function moneyValueFromString(v: string | null | undefined): number {
  if (v == null) return Number.NEGATIVE_INFINITY;
  const n = Number(v);
  return Number.isFinite(n) ? n : Number.NEGATIVE_INFINITY;
}

function LatestPositions({ point }: { point: ReviewPoint }) {
  const equity = point.equity;
  const sorted = useMemo(
    () =>
      [...point.postCycle.positions].sort(
        (a, b) => moneyValueFromString(b.market_value) - moneyValueFromString(a.market_value),
      ),
    [point.postCycle.positions],
  );

  return (
    <Card
      size="small"
      className="!rounded-xl !border !border-shell-line !bg-card-bg"
      title={
        <div className="flex items-center justify-between gap-2">
          <span>最新持仓</span>
          <Typography.Text type="secondary" className="text-xs">
            截至 {formatDateTimeUtc8(point.cycleTime)} · {point.postCycle.source === "broker" ? "柜台" : "账本"}
          </Typography.Text>
        </div>
      }
    >
      <Table<PostCycleAccountPositionRow>
        size="small"
        rowKey={(r) => r.symbol}
        pagination={false}
        dataSource={sorted}
        locale={{ emptyText: "当前空仓" }}
        columns={[
          {
            title: "代码",
            dataIndex: "symbol",
            key: "symbol",
            width: 120,
            render: (v: string) => (
              <Typography.Text copyable={{ text: v }} className="font-mono">
                {v}
              </Typography.Text>
            ),
          },
          {
            title: "名称",
            dataIndex: "name",
            key: "name",
            render: (v: string | null | undefined) => v ?? "—",
          },
          {
            title: "股数",
            dataIndex: "quantity",
            key: "quantity",
            align: "right" as const,
            sorter: (a, b) => a.quantity - b.quantity,
            render: (v: number) => fmtNum(v, 0),
          },
          {
            title: "成本价",
            dataIndex: "cost_price",
            key: "cost_price",
            align: "right" as const,
            sorter: (a, b) => Number(a.cost_price) - Number(b.cost_price),
            render: (v: string) => fmtMoneyExact(v),
          },
          {
            title: "最新价",
            dataIndex: "last_price",
            key: "last_price",
            align: "right" as const,
            sorter: (a, b) => Number(a.last_price ?? 0) - Number(b.last_price ?? 0),
            render: (v: string | null | undefined) => fmtMoneyExact(v),
          },
          {
            title: "市值",
            dataIndex: "market_value",
            key: "market_value",
            align: "right" as const,
            defaultSortOrder: "descend",
            sorter: (a, b) => moneyValueFromString(a.market_value) - moneyValueFromString(b.market_value),
            render: (v: string | null | undefined) => fmtMoneyExact(v),
          },
          {
            title: "占比",
            key: "weight",
            align: "right" as const,
            render: (_: unknown, record) => {
              const mv = Number(record.market_value ?? "");
              if (!Number.isFinite(mv) || !Number.isFinite(equity) || equity <= 0) return "—";
              return `${((mv / equity) * 100).toFixed(2)}%`;
            },
          },
        ]}
      />
    </Card>
  );
}

/** 任务详情页「复盘」Tab：从 cycle_runs 的 post_cycle_account 快照构建账户复盘视图。 */
export function TaskReviewPanel({ rows }: Props) {
  const points = useMemo(() => buildReviewPoints(rows), [rows]);

  if (points.length === 0) {
    return (
      <Card className="!border !border-shell-line !bg-card-bg shadow-shell-card">
        <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
          <Typography.Text strong>暂无复盘数据</Typography.Text>
          <Typography.Text type="secondary">该任务还没有带账户快照的 cycle run。</Typography.Text>
        </div>
      </Card>
    );
  }

  const latest = points[points.length - 1];

  return (
    <div data-testid="task-review-panel" className="flex flex-col gap-4">
      <Card
        size="small"
        className="!rounded-xl !border !border-shell-line !bg-card-bg"
        title={
          <div className="flex items-center justify-between gap-2">
            <span>区间概览</span>
            <Tag color={latest.postCycle.source === "broker" ? "blue" : "default"}>
              {latest.postCycle.source === "broker" ? "柜台快照" : "账本快照"}
            </Tag>
          </div>
        }
      >
        <PeriodSummary points={points} />
      </Card>
      <EquityTrend points={points} />
      <LatestPositions point={latest} />
    </div>
  );
}
