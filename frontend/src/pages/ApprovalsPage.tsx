import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Card,
  DatePicker,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import type { Dayjs } from "dayjs";
import { useNavigate } from "react-router-dom";

import { PageIntro } from "../components/PageIntro";
import { SymbolAutoComplete } from "../components/SymbolSearchSelect";
import { approve, listApprovals, reject } from "../api";
import { PANEL_CARD_CLASSNAME } from "../styles/classNames";
import type { ApprovalQuery, PendingApproval } from "../types";
import { cycleTimePickerToApiIso, formatDateTimeUtc8 } from "../utils/datetime";

const STATUS_META: Record<string, { label: string; color: string }> = {
  pending: { label: "待处理", color: "processing" },
  approved: { label: "已同意", color: "success" },
  rejected: { label: "已拒绝", color: "error" },
  expired: { label: "已过期", color: "default" },
};

const STATUS_OPTIONS = Object.entries(STATUS_META).map(([value, meta]) => ({
  value,
  label: meta.label,
}));

const SOURCE_OPTIONS = [
  { value: "web", label: "网页" },
  { value: "api", label: "API" },
  { value: "feishu_card", label: "飞书卡片" },
];

const ACTION_META: Record<string, { label: string; color: string }> = {
  buy: { label: "买入", color: "green" },
  sell: { label: "卖出", color: "red" },
};

const PAGE_SIZE = 20;
/** Silent background refresh so pending rows stay actionable and resolved rows
 * land without the operator hammering 刷新. Matches the snappier cadence the
 * Approvals surface had before (App-level fast poll). */
const POLL_INTERVAL_MS = 4000;

/** Cosmetic thousands separators on a decimal money string. Never parseFloat —
 * the value is an exact decimal (§金额十进制); fall back to the raw string. */
function formatNotional(raw?: string | null): string {
  if (raw == null || raw === "") return "—";
  const match = /^(-?)(\d+)(\.\d+)?$/.exec(raw.trim());
  if (!match) return raw;
  const [, sign, intPart, fracPart = ""] = match;
  const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${sign}${grouped}${fracPart}`;
}

type Props = {
  /** Bubble a global refresh (e.g. the nav pending badge) after a decision. */
  onMutated?: () => void;
};

export function ApprovalsPage({ onMutated }: Props) {
  const navigate = useNavigate();
  const [items, setItems] = useState<PendingApproval[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [acting, setActing] = useState<string | null>(null);

  // Filters.
  const [statuses, setStatuses] = useState<string[]>([]);
  const [symbol, setSymbol] = useState("");
  const [keyword, setKeyword] = useState("");
  const [source, setSource] = useState<string | undefined>(undefined);
  const [range, setRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);

  const query = useMemo<ApprovalQuery>(() => {
    const start = range?.[0];
    const end = range?.[1];
    return {
      status: statuses.length ? statuses : undefined,
      symbol: symbol.trim() || undefined,
      q: keyword.trim() || undefined,
      decision_source: source,
      // RangePicker is day-granular; widen to the full UTC+8 day before the
      // util converts the wall time to a UTC instant for the naive columns.
      created_after: start ? cycleTimePickerToApiIso(start.startOf("day")) : undefined,
      created_before: end ? cycleTimePickerToApiIso(end.endOf("day")) : undefined,
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    };
  }, [statuses, symbol, keyword, source, range, page]);

  const load = useCallback(
    async (opts?: { silent?: boolean }) => {
      if (!opts?.silent) setLoading(true);
      try {
        const res = await listApprovals(query);
        setItems(res.items);
        setTotal(res.total);
      } catch {
        if (!opts?.silent) message.error("加载审批列表失败");
      } finally {
        if (!opts?.silent) setLoading(false);
      }
    },
    [query],
  );

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void load({ silent: true }).catch(() => {});
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  // A new filter set should land the operator on page 1, not a now-empty page N.
  useEffect(() => {
    setPage(1);
  }, [statuses, symbol, keyword, source, range]);

  const resolveApproval = useCallback(
    async (approvalId: string, kind: "approve" | "reject") => {
      setActing(approvalId);
      try {
        if (kind === "approve") {
          await approve(approvalId);
        } else {
          await reject(approvalId);
        }
        onMutated?.();
        await load({ silent: true });
      } catch {
        message.warning("该审批已被处理或已过期。");
        await load({ silent: true });
      } finally {
        setActing(null);
      }
    },
    [load, onMutated],
  );

  const resetFilters = useCallback(() => {
    setStatuses([]);
    setSymbol("");
    setKeyword("");
    setSource(undefined);
    setRange(null);
  }, []);

  const columns = useMemo<ColumnsType<PendingApproval>>(
    () => [
      {
        title: "创建时间",
        dataIndex: "created_at",
        width: 170,
        render: (value: string | null | undefined) => (
          <span className="whitespace-nowrap font-mono text-xs">
            {formatDateTimeUtc8(value, "—")}
          </span>
        ),
      },
      {
        title: "标的 / 方向",
        key: "symbol",
        render: (_: unknown, row: PendingApproval) => {
          const action = (row.action ?? "").toLowerCase();
          const meta = ACTION_META[action];
          return (
            <Space size={6}>
              <Typography.Text strong>
                {row.symbol_name ? `${row.symbol_name} ` : ""}
                <span className="font-mono">{row.symbol ?? "—"}</span>
              </Typography.Text>
              {meta ? <Tag color={meta.color}>{meta.label}</Tag> : null}
            </Space>
          );
        },
      },
      {
        title: "名义金额",
        dataIndex: "notional",
        align: "right",
        width: 120,
        render: (value: string | null | undefined) => (
          <span className="font-mono">{formatNotional(value)}</span>
        ),
      },
      {
        title: "状态",
        dataIndex: "status",
        width: 96,
        render: (value: string | null | undefined) => {
          const meta = STATUS_META[(value ?? "").toLowerCase()];
          return meta ? <Tag color={meta.color}>{meta.label}</Tag> : <span>{value ?? "—"}</span>;
        },
      },
      {
        title: "来源 / 处理人",
        key: "decision",
        width: 150,
        render: (_: unknown, row: PendingApproval) => {
          if (!row.decision_source && !row.resolver_id) {
            return <Typography.Text type="secondary">—</Typography.Text>;
          }
          return (
            <Space direction="vertical" size={0}>
              {row.decision_source ? <Tag>{row.decision_source}</Tag> : null}
              {row.resolver_id ? (
                <Typography.Text className="text-xs" type="secondary">
                  {row.resolver_id}
                </Typography.Text>
              ) : null}
            </Space>
          );
        },
      },
      {
        title: "决策时间",
        key: "decided_at",
        width: 170,
        render: (_: unknown, row: PendingApproval) => {
          const ts = row.decided_at ?? row.resolved_at;
          return (
            <span className="whitespace-nowrap font-mono text-xs">
              {ts ? formatDateTimeUtc8(ts, "—") : "—"}
            </span>
          );
        },
      },
      {
        title: "操作",
        key: "actions",
        width: 180,
        render: (_: unknown, row: PendingApproval) => {
          const status = (row.status ?? "").toLowerCase();
          const decidable = status === "" || status === "pending";
          return (
            <Space size={6} wrap>
              {decidable ? (
                <>
                  <Button
                    type="primary"
                    size="small"
                    className="rounded-lg"
                    loading={acting === row.approval_id}
                    onClick={() => void resolveApproval(row.approval_id, "approve")}
                  >
                    同意
                  </Button>
                  <Button
                    danger
                    size="small"
                    className="rounded-lg"
                    loading={acting === row.approval_id}
                    onClick={() => void resolveApproval(row.approval_id, "reject")}
                  >
                    拒绝
                  </Button>
                </>
              ) : row.reason ? (
                <Tooltip title={row.reason}>
                  <Typography.Text className="text-xs" type="secondary" ellipsis>
                    {row.reason}
                  </Typography.Text>
                </Tooltip>
              ) : null}
              {row.task_id ? (
                <Button
                  size="small"
                  className="rounded-lg"
                  onClick={() => navigate(`/tasks/${encodeURIComponent(row.task_id as string)}`)}
                >
                  任务
                </Button>
              ) : null}
            </Space>
          );
        },
      },
    ],
    [acting, navigate, resolveApproval],
  );

  return (
    <>
      <PageIntro
        title="Approvals"
        description="集中查看全部审批（待处理与历史），支持按状态、标的、来源、时间筛选与关键词搜索。"
      />
      <Card className={PANEL_CARD_CLASSNAME} title="审批记录">
        <Space direction="vertical" size={12} className="w-full">
          <Space size={8} wrap>
            <Select
              mode="multiple"
              allowClear
              placeholder="状态"
              options={STATUS_OPTIONS}
              value={statuses}
              onChange={setStatuses}
              style={{ minWidth: 200 }}
              maxTagCount="responsive"
            />
            <SymbolAutoComplete
              placeholder="股票代码 / 名称搜索"
              value={symbol}
              onChange={setSymbol}
              style={{ width: 220 }}
            />
            <Select
              allowClear
              placeholder="来源"
              options={SOURCE_OPTIONS}
              value={source}
              onChange={(value) => setSource(value)}
              style={{ width: 130 }}
            />
            <DatePicker.RangePicker
              value={range as [Dayjs, Dayjs] | null}
              onChange={(value) => setRange(value as [Dayjs | null, Dayjs | null] | null)}
              allowEmpty={[true, true]}
            />
            <Input.Search
              allowClear
              placeholder="搜索请求ID / 意图 / 任务"
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              onSearch={() => void load()}
              style={{ width: 240 }}
            />
            <Button onClick={resetFilters}>重置</Button>
            <Button onClick={() => void load()}>刷新</Button>
          </Space>
          <Table<PendingApproval>
            rowKey="approval_id"
            size="small"
            loading={loading}
            dataSource={items}
            columns={columns}
            pagination={{
              current: page,
              pageSize: PAGE_SIZE,
              total,
              showSizeChanger: false,
              showTotal: (count) => `共 ${count} 条`,
              onChange: (next) => setPage(next),
            }}
          />
        </Space>
      </Card>
    </>
  );
}
