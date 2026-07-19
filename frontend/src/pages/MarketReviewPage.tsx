import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Empty,
  Radio,
  Segmented,
  Select,
  Space,
  Spin,
  Table,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs, { type Dayjs } from "dayjs";

import {
  ApiError,
  getDragonTigerBoard,
  getFundFlowRanking,
  getMarketBreadth,
  getSectorHeat,
} from "../api";
import { ChangePctTag, formatAmount } from "../components/StockDetailModal";
import { LadderChart } from "../components/LadderChart";
import { MarketSentimentCard } from "../components/MarketSentimentCard";
import { SentimentTimeline } from "../components/SentimentTimeline";
import { usePageRefreshToken } from "../pageRefreshContext";
import type {
  FundFlowData,
  FundFlowRow,
  FundFlowScope,
  LhbData,
  LhbRow,
  MarketBreadthData,
  SectorHeatData,
  SectorHeatRow,
  SectorHeatType,
} from "../types";

// ---- Signed-money cell: 正红负绿, formatted 亿/万 (F1: never fabricate) -------
function SignedAmount({ value }: { value: number | null | undefined }) {
  if (value == null || !Number.isFinite(value)) {
    return <span>—</span>;
  }
  const color = value > 0 ? "#c0322b" : value < 0 ? "#237a3d" : "#3a3a37";
  const sign = value > 0 ? "+" : "";
  return (
    <span style={{ color }} className="tabular-nums">
      {`${sign}${formatAmount(value)}`}
    </span>
  );
}

/** 主力净占比 % — a signed percent number, red-up / green-down. */
function SignedPct({ value }: { value: number | null | undefined }) {
  if (value == null || !Number.isFinite(value)) {
    return <span>—</span>;
  }
  const color = value > 0 ? "#c0322b" : value < 0 ? "#237a3d" : "#3a3a37";
  const sign = value > 0 ? "+" : "";
  return (
    <span style={{ color }} className="tabular-nums">{`${sign}${value.toFixed(2)}%`}</span>
  );
}

function plainNumber(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

/** Turn a friendly ApiError / Error into a short display line. */
function errorLine(err: unknown): string {
  if (err instanceof ApiError) {
    return err.message || err.errorCode || `HTTP ${err.status}`;
  }
  return err instanceof Error ? err.message : String(err);
}

const INDIVIDUAL_PERIODS = ["今日", "3日", "5日", "10日"];
const SECTOR_PERIODS = ["今日", "5日", "10日"];
const SECTOR_TYPES = ["概念", "行业", "地域"];

// A numeric sorter that keeps null/absent values at the low end so a
// descending click surfaces real numbers first (matches WatchlistPage).
function numSorter<T>(get: (row: T) => number | null | undefined) {
  return (a: T, b: T) => {
    const av = get(a);
    const bv = get(b);
    const an = av == null || !Number.isFinite(av) ? null : av;
    const bn = bv == null || !Number.isFinite(bv) ? null : bv;
    if (an == null && bn == null) return 0;
    if (an == null) return -1;
    if (bn == null) return 1;
    return an - bn;
  };
}

export function MarketReviewPage() {
  const pageRefreshToken = usePageRefreshToken();
  const [tradeDate, setTradeDate] = useState<Dayjs>(() => dayjs());

  // --- 情绪 / 涨停面板 / 连板梯队 (breadth) ---
  const [breadth, setBreadth] = useState<MarketBreadthData | null>(null);
  const [breadthLoading, setBreadthLoading] = useState(true);
  const [breadthError, setBreadthError] = useState<{ code: string | null; text: string } | null>(null);

  // --- 龙虎榜 (lhb) ---
  const [lhb, setLhb] = useState<LhbData | null>(null);
  const [lhbLoading, setLhbLoading] = useState(true);
  const [lhbError, setLhbError] = useState<string | null>(null);
  const [lhbRange, setLhbRange] = useState<"single" | "recent3">("single");

  // --- 资金流 (fund-flow) ---
  const [fundFlow, setFundFlow] = useState<FundFlowData | null>(null);
  const [fundFlowLoading, setFundFlowLoading] = useState(true);
  const [fundFlowError, setFundFlowError] = useState<string | null>(null);
  const [scope, setScope] = useState<FundFlowScope>("individual");
  const [period, setPeriod] = useState<string>("今日");
  const [sectorType, setSectorType] = useState<string>("概念");

  // --- 题材热度 (sector-heat) ---
  const [sectorHeat, setSectorHeat] = useState<SectorHeatData | null>(null);
  const [sectorHeatLoading, setSectorHeatLoading] = useState(true);
  const [sectorHeatError, setSectorHeatError] = useState<string | null>(null);
  const [heatType, setHeatType] = useState<SectorHeatType>("concept");

  const dateStr = useMemo(() => tradeDate.format("YYYY-MM-DD"), [tradeDate]);

  // ------------------------------------------------------------------ loaders
  const loadBreadth = useCallback(async () => {
    setBreadthLoading(true);
    setBreadthError(null);
    try {
      const data = await getMarketBreadth({ date: dateStr });
      setBreadth(data);
    } catch (err) {
      setBreadth(null);
      if (err instanceof ApiError && err.errorCode === "market_breadth_empty") {
        setBreadthError({
          code: "market_breadth_empty",
          text: "无数据（可能非交易日或盘后未更新），请换一个交易日再试。",
        });
      } else {
        setBreadthError({ code: err instanceof ApiError ? err.errorCode : null, text: errorLine(err) });
      }
    } finally {
      setBreadthLoading(false);
    }
  }, [dateStr]);

  const loadLhb = useCallback(async () => {
    setLhbLoading(true);
    setLhbError(null);
    try {
      const params =
        lhbRange === "recent3"
          ? { start: tradeDate.subtract(2, "day").format("YYYY-MM-DD"), end: dateStr }
          : { date: dateStr };
      const data = await getDragonTigerBoard(params);
      setLhb(data);
    } catch (err) {
      setLhb(null);
      if (err instanceof ApiError && err.errorCode === "lhb_empty") {
        setLhbError("该区间无龙虎榜数据（可能非交易日或盘后未更新）。");
      } else {
        setLhbError(errorLine(err));
      }
    } finally {
      setLhbLoading(false);
    }
  }, [dateStr, lhbRange, tradeDate]);

  const loadFundFlow = useCallback(async () => {
    setFundFlowLoading(true);
    setFundFlowError(null);
    try {
      const data = await getFundFlowRanking({
        scope,
        period,
        sector_type: scope === "sector" ? sectorType : undefined,
        top: 30,
      });
      setFundFlow(data);
    } catch (err) {
      setFundFlow(null);
      if (err instanceof ApiError && err.errorCode === "fund_flow_empty") {
        setFundFlowError("当前口径暂无资金流数据，请换个周期或口径重试。");
      } else {
        setFundFlowError(errorLine(err));
      }
    } finally {
      setFundFlowLoading(false);
    }
  }, [period, scope, sectorType]);

  const loadSectorHeat = useCallback(async () => {
    setSectorHeatLoading(true);
    setSectorHeatError(null);
    try {
      const data = await getSectorHeat({ sector_type: heatType, top: 30 });
      setSectorHeat(data);
    } catch (err) {
      setSectorHeat(null);
      if (err instanceof ApiError && err.errorCode === "sector_heat_empty") {
        setSectorHeatError("当前板块口径暂无热度数据，请换一个板块类型或稍后重试。");
      } else {
        setSectorHeatError(errorLine(err));
      }
    } finally {
      setSectorHeatLoading(false);
    }
  }, [heatType]);

  useEffect(() => {
    void loadBreadth();
  }, [loadBreadth, pageRefreshToken]);

  useEffect(() => {
    void loadLhb();
  }, [loadLhb, pageRefreshToken]);

  useEffect(() => {
    void loadFundFlow();
  }, [loadFundFlow, pageRefreshToken]);

  useEffect(() => {
    void loadSectorHeat();
  }, [loadSectorHeat, pageRefreshToken]);

  // When switching scope, snap period into the scope's allowed set (sector has
  // no 3日) so we never send an invalid_period the backend would reject.
  const onScopeChange = useCallback((next: FundFlowScope) => {
    setScope(next);
    setPeriod((prev) => {
      const allowed = next === "sector" ? SECTOR_PERIODS : INDIVIDUAL_PERIODS;
      return allowed.includes(prev) ? prev : "今日";
    });
  }, []);

  // ------------------------------------------------------------------ columns
  const lhbColumns: ColumnsType<LhbRow> = [
    {
      title: "名称 / 代码",
      key: "name",
      render: (_v, row) => (
        <div className="flex flex-col">
          <span className="font-medium text-shell-ink">{row.name || "—"}</span>
          <span className="text-xs text-shell-muted">{row.symbol || row.code || "—"}</span>
        </div>
      ),
    },
    { title: "上榜日", dataIndex: "on_date", key: "on_date", render: (v: string) => v || "—" },
    { title: "上榜原因", dataIndex: "reason", key: "reason", render: (v: string) => v || "—" },
    {
      title: "涨跌幅",
      key: "change_pct",
      sorter: numSorter<LhbRow>((r) => r.change_pct),
      render: (_v, row) => <ChangePctTag value={row.change_pct} />,
    },
    {
      title: "龙虎榜净买额",
      key: "net_buy_amount",
      defaultSortOrder: "descend",
      sorter: numSorter<LhbRow>((r) => r.net_buy_amount),
      render: (_v, row) => <SignedAmount value={row.net_buy_amount} />,
    },
    {
      title: "买入额",
      key: "buy_amount",
      sorter: numSorter<LhbRow>((r) => r.buy_amount),
      render: (_v, row) => formatAmount(row.buy_amount),
    },
    {
      title: "卖出额",
      key: "sell_amount",
      sorter: numSorter<LhbRow>((r) => r.sell_amount),
      render: (_v, row) => formatAmount(row.sell_amount),
    },
    { title: "解读", dataIndex: "interpretation", key: "interpretation", render: (v: string) => v || "—" },
  ];

  const fundFlowColumns: ColumnsType<FundFlowRow> = [
    {
      title: "名称 / 代码",
      key: "name",
      render: (_v, row) => (
        <div className="flex flex-col">
          <span className="font-medium text-shell-ink">{row.name || "—"}</span>
          <span className="text-xs text-shell-muted">{row.symbol || row.code || "—"}</span>
        </div>
      ),
    },
    {
      title: "最新价",
      key: "latest_price",
      sorter: numSorter<FundFlowRow>((r) => r.latest_price),
      render: (_v, row) => plainNumber(row.latest_price),
    },
    {
      title: "涨跌幅",
      key: "change_pct",
      sorter: numSorter<FundFlowRow>((r) => r.change_pct),
      render: (_v, row) => <ChangePctTag value={row.change_pct} />,
    },
    {
      title: "主力净流入",
      key: "main_net_amount",
      defaultSortOrder: "descend",
      sorter: numSorter<FundFlowRow>((r) => r.main_net_amount),
      render: (_v, row) => <SignedAmount value={row.main_net_amount} />,
    },
    {
      title: "主力净占比",
      key: "main_net_pct",
      sorter: numSorter<FundFlowRow>((r) => r.main_net_pct),
      render: (_v, row) => <SignedPct value={row.main_net_pct} />,
    },
    {
      title: "超大单净流入",
      key: "super_large_net_amount",
      sorter: numSorter<FundFlowRow>((r) => r.super_large_net_amount),
      render: (_v, row) => <SignedAmount value={row.super_large_net_amount} />,
    },
    {
      title: "大单净流入",
      key: "large_net_amount",
      sorter: numSorter<FundFlowRow>((r) => r.large_net_amount),
      render: (_v, row) => <SignedAmount value={row.large_net_amount} />,
    },
    ...(scope === "sector"
      ? [
          {
            title: "领涨股",
            dataIndex: "lead_stock",
            key: "lead_stock",
            render: (v: string | null) => v || "—",
          } as ColumnsType<FundFlowRow>[number],
        ]
      : []),
  ];

  const sectorHeatColumns: ColumnsType<SectorHeatRow> = [
    {
      title: "板块名称",
      key: "board_name",
      render: (_v, row) => (
        <span className="font-medium text-shell-ink">{row.board_name || "—"}</span>
      ),
    },
    {
      title: "涨跌幅",
      key: "change_pct",
      defaultSortOrder: "descend",
      sorter: numSorter<SectorHeatRow>((r) => r.change_pct),
      render: (_v, row) => <ChangePctTag value={row.change_pct} />,
    },
    {
      title: "领涨股",
      key: "leader_stock",
      render: (_v, row) =>
        row.leader_stock ? (
          <div className="flex flex-col">
            <span className="text-shell-ink">{row.leader_stock}</span>
            <span className="text-xs">
              <ChangePctTag value={row.leader_change_pct} />
            </span>
          </div>
        ) : (
          <span>—</span>
        ),
    },
    {
      title: "上涨 / 下跌家数",
      key: "up_down",
      render: (_v, row) => (
        <span className="tabular-nums">
          <span style={{ color: "#c0322b" }}>{row.up_count ?? "—"}</span>
          {" / "}
          <span style={{ color: "#237a3d" }}>{row.down_count ?? "—"}</span>
        </span>
      ),
    },
    {
      title: "换手率",
      key: "turnover_rate",
      sorter: numSorter<SectorHeatRow>((r) => r.turnover_rate),
      render: (_v, row) =>
        row.turnover_rate == null || !Number.isFinite(row.turnover_rate)
          ? "—"
          : `${row.turnover_rate.toFixed(2)}%`,
    },
    {
      title: "总市值",
      key: "total_mv",
      sorter: numSorter<SectorHeatRow>((r) => r.total_mv),
      render: (_v, row) => formatAmount(row.total_mv),
    },
  ];

  const periodOptions = scope === "sector" ? SECTOR_PERIODS : INDIVIDUAL_PERIODS;

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-col">
          <Typography.Title level={3} className="!mb-0">
            市场复盘
          </Typography.Title>
          <Typography.Text type="secondary">
            情绪周期 · 涨停面板 · 连板梯队 · 情绪温度计 · 龙虎榜 · 题材热度 · 资金流排名（仅供研究，非投资建议）
          </Typography.Text>
        </div>
        <Space>
          <span className="text-sm text-shell-muted">交易日</span>
          <DatePicker
            value={tradeDate}
            onChange={(value) => {
              if (value) setTradeDate(value);
            }}
            allowClear={false}
            disabledDate={(d) => d.isAfter(dayjs(), "day")}
          />
        </Space>
      </div>

      {/* 情绪温度计 + 连板梯队 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="市场情绪" className="rounded-2xl">
          {breadthLoading ? (
            <div className="flex justify-center py-10">
              <Spin />
            </div>
          ) : breadthError ? (
            <Alert
              type={breadthError.code === "market_breadth_empty" ? "info" : "error"}
              showIcon
              message={breadthError.code === "market_breadth_empty" ? "暂无数据" : "加载失败"}
              description={breadthError.text}
              action={
                <Button size="small" onClick={() => void loadBreadth()}>
                  重试
                </Button>
              }
            />
          ) : breadth ? (
            <MarketSentimentCard data={breadth} />
          ) : (
            <Empty description="无数据" />
          )}
        </Card>

        <Card title="连板梯队" className="rounded-2xl">
          {breadthLoading ? (
            <div className="flex justify-center py-10">
              <Spin />
            </div>
          ) : breadth ? (
            <LadderChart ladder={breadth.ladder} />
          ) : (
            <Empty
              description={breadthError?.text ?? "无连板梯队数据"}
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          )}
        </Card>
      </div>

      {/* 情绪周期 — 跨月的情绪节奏，给上面的当日情绪温度计提供历史背景。
          与当日快照互补：温度计是「今天几度」，时间轴是「处在周期哪一段」。 */}
      <SentimentTimeline months={3} />

      {/* 龙虎榜 */}
      <Card
        title="龙虎榜"
        className="rounded-2xl"
        extra={
          <Segmented
            size="small"
            value={lhbRange}
            onChange={(v) => setLhbRange(v as "single" | "recent3")}
            options={[
              { label: "当日", value: "single" },
              { label: "近3日", value: "recent3" },
            ]}
          />
        }
      >
        {lhbError ? (
          <Alert
            type="info"
            showIcon
            className="mb-3"
            message="暂无数据"
            description={lhbError}
            action={
              <Button size="small" onClick={() => void loadLhb()}>
                重试
              </Button>
            }
          />
        ) : null}
        <Table<LhbRow>
          rowKey={(row, idx) => `${row.symbol || row.code}-${row.on_date}-${idx}`}
          size="small"
          loading={lhbLoading}
          columns={lhbColumns}
          dataSource={lhb?.latest ?? []}
          pagination={false}
          scroll={{ x: "max-content" }}
          locale={{ emptyText: lhbError ? " " : <Empty description="无龙虎榜数据" /> }}
        />
      </Card>

      {/* 题材热度 */}
      <Card
        title="题材热度"
        className="rounded-2xl"
        extra={
          <Radio.Group
            size="small"
            value={heatType}
            onChange={(e) => setHeatType(e.target.value as SectorHeatType)}
            optionType="button"
            buttonStyle="solid"
            options={[
              { label: "概念", value: "concept" },
              { label: "行业", value: "industry" },
            ]}
          />
        }
      >
        {sectorHeatError ? (
          <Alert
            type="info"
            showIcon
            className="mb-3"
            message="暂无数据"
            description={sectorHeatError}
            action={
              <Button size="small" onClick={() => void loadSectorHeat()}>
                重试
              </Button>
            }
          />
        ) : null}
        <Table<SectorHeatRow>
          rowKey={(row, idx) => `${row.board_code || row.board_name}-${idx}`}
          size="small"
          loading={sectorHeatLoading}
          columns={sectorHeatColumns}
          dataSource={sectorHeat?.latest ?? []}
          pagination={false}
          scroll={{ x: "max-content" }}
          locale={{ emptyText: sectorHeatError ? " " : <Empty description="无题材热度数据" /> }}
        />
      </Card>

      {/* 资金流排名 */}
      <Card
        title="资金流排名"
        className="rounded-2xl"
        extra={
          <Space wrap>
            <Radio.Group
              size="small"
              value={scope}
              onChange={(e) => onScopeChange(e.target.value as FundFlowScope)}
              optionType="button"
              buttonStyle="solid"
              options={[
                { label: "个股", value: "individual" },
                { label: "板块", value: "sector" },
              ]}
            />
            <Select
              size="small"
              value={period}
              onChange={setPeriod}
              style={{ width: 92 }}
              options={periodOptions.map((p) => ({ label: p, value: p }))}
            />
            {scope === "sector" ? (
              <Select
                size="small"
                value={sectorType}
                onChange={setSectorType}
                style={{ width: 92 }}
                options={SECTOR_TYPES.map((s) => ({ label: s, value: s }))}
              />
            ) : null}
          </Space>
        }
      >
        {fundFlowError ? (
          <Alert
            type="info"
            showIcon
            className="mb-3"
            message="暂无数据"
            description={fundFlowError}
            action={
              <Button size="small" onClick={() => void loadFundFlow()}>
                重试
              </Button>
            }
          />
        ) : null}
        <Table<FundFlowRow>
          rowKey={(row, idx) => `${row.symbol || row.code || row.name}-${idx}`}
          size="small"
          loading={fundFlowLoading}
          columns={fundFlowColumns}
          dataSource={fundFlow?.latest ?? []}
          pagination={false}
          scroll={{ x: "max-content" }}
          locale={{ emptyText: fundFlowError ? " " : <Empty description="无资金流数据" /> }}
        />
      </Card>
    </div>
  );
}
