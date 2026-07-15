import { ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Collapse,
  DatePicker,
  Descriptions,
  Input,
  Modal,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import type { Dayjs } from "dayjs";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { getCycleRunDebugView, listCycleRuns, type ListCycleRunsParams } from "../api";
import { JsonCodeBlock } from "./JsonCodeBlock";
import { FormattedRequestView } from "./FormattedRequestView";
import { FormattedResponseView } from "./FormattedResponseView";
import MarkdownPreview from "./MarkdownPreview";
import { ModelInvocationRequestPanel } from "./ModelInvocationRequestPanel";
import { TabbedJsonPanel } from "./TabbedJsonPanel";
import { TraceViewer } from "./TraceViewer";
import type {
  CycleRunDebugView,
  CycleRunRow,
  PendingApproval,
  TaskStatus,
  ModelInvocationRow,
} from "../types";
import { formatDateTimeUtc8 } from "../utils/datetime";
import {
  fmtMoneyExact,
  formatTradeOperationsFromDetails,
  postCycleAccountFromDetails,
} from "../utils/cycleRunListFormat";
import { modelInvocationTokenSummary, buildModelInvocationCollapseItems } from "../hooks/modelInvocation";

const POLL_INTERVAL_MS = 1500;

function failureErrorFromDetails(details: Record<string, unknown> | null | undefined): Record<string, unknown> | null {
  if (!details || typeof details !== "object") return null;
  const fe = details.failure_error;
  if (fe && typeof fe === "object" && !Array.isArray(fe)) return fe as Record<string, unknown>;
  return null;
}

function statusColor(status: string): "default" | "processing" | "success" | "error" {
  if (status === "completed") return "success";
  if (status === "failed") return "error";
  if (status === "running" || status === "pending") return "processing";
  return "default";
}

function sourceTag(source: string): string {
  if (source === "debug") return "调试运行";
  if (source === "scheduled") return "定时调度";
  if (source === "manual") return "手动触发";
  if (source === "cron") return "Cron";
  return source;
}

function fmtNum(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function sourceColor(source: string): "blue" | "green" | "orange" | "geekblue" | "default" {
  if (source === "debug") return "blue";
  if (source === "scheduled") return "green";
  if (source === "manual") return "orange";
  if (source === "cron") return "geekblue";
  return "default";
}

function traceIdUsable(traceId: string | null | undefined): boolean {
  if (traceId == null) return false;
  const s = traceId.trim().toLowerCase();
  if (!s || s === "-") return false;
  return s.length === 32 && /^[0-9a-f]+$/.test(s);
}

/** Delivery-status → antd Tag color. Semantics copied from
 * CronJobRunHistoryModal.tsx so the push-card tags read identically. */
const DELIVERY_STATUS_COLORS: Record<string, string> = {
  delivered: "green",
  suppressed: "blue",
  skipped: "orange",
  failed: "red",
  none: "default",
};

/** Approval status → {label,color}. Local copy of ApprovalsPage.tsx's map so
 * the read-only approvals table here matches the Approvals page visually. */
const APPROVAL_STATUS_META: Record<string, { label: string; color: string }> = {
  pending: { label: "待处理", color: "processing" },
  approved: { label: "已同意", color: "success" },
  rejected: { label: "已拒绝", color: "error" },
  expired: { label: "已过期", color: "default" },
};

/** Order action → {label,color}. Local copy of ApprovalsPage.tsx's map. */
const APPROVAL_ACTION_META: Record<string, { label: string; color: string }> = {
  buy: { label: "买入", color: "green" },
  sell: { label: "卖出", color: "red" },
};

/** Cosmetic thousands separators on a decimal money string. Never parseFloat —
 * the value is an exact decimal (§金额十进制); fall back to the raw string.
 * Local copy of ApprovalsPage.tsx's formatNotional. */
function formatNotional(raw?: string | null): string {
  if (raw == null || raw === "") return "—";
  const match = /^(-?)(\d+)(\.\d+)?$/.exec(raw.trim());
  if (!match) return raw;
  const [, sign, intPart, fracPart = ""] = match;
  const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${sign}${grouped}${fracPart}`;
}

export function CycleRunDetailBody({
  loading,
  data,
}: {
  loading: boolean;
  data: CycleRunDebugView | null;
}) {
  const navigate = useNavigate();
  const invocationItems = useMemo(
    () => (data?.model_invocations?.length ? buildModelInvocationCollapseItems(data.model_invocations) : []),
    [data?.model_invocations],
  );

  const failureStructured = useMemo(
    () => (data ? failureErrorFromDetails(data.cycle_run.details) : null),
    [data],
  );

  if (loading) {
    return (
      <div className="flex min-h-[240px] items-center justify-center">
        <Spin />
      </div>
    );
  }

  if (!data) {
    return <Typography.Text type="secondary">无法加载该周期详情。</Typography.Text>;
  }

  const { cycle_run: row, session, spans } = data;
  const pd = data.push_detail;
  const showTraceHint = !traceIdUsable(row.trace_id) && spans.length === 0;
  const postCycle = postCycleAccountFromDetails(row.details);

  return (
    <Space direction="vertical" size={16} className="w-full">
      <Typography.Title level={5} className="!mb-0">
        周期摘要
      </Typography.Title>
      <Descriptions
        size="small"
        bordered
        column={{ xs: 1, sm: 2, lg: 3 }}
        items={[
          { key: "run_id", label: "run_id", children: <Typography.Text copyable={{ text: row.run_id }}>{row.run_id}</Typography.Text> },
          {
            key: "status",
            label: "状态",
            children: <Tag color={row.cycle_failed ? "error" : statusColor(row.status)}>{row.status}</Tag>,
          },
          {
            key: "run_kind",
            label: "来源",
            children: <Tag color={sourceColor(row.run_kind)}>{sourceTag(row.run_kind)}</Tag>,
          },
          {
            key: "agent_name",
            label: "策略/任务",
            children: row.agent_name ? (
              row.agent_name
            ) : pd?.strategy.reason ? (
              <Typography.Text type="secondary">{pd.strategy.reason}</Typography.Text>
            ) : (
              "—"
            ),
          },
          {
            key: "trace_id",
            label: "trace_id",
            children: row.trace_id ? (
              <Typography.Text copyable={{ text: row.trace_id }} className="font-mono text-xs">
                {row.trace_id}
              </Typography.Text>
            ) : (
              "—"
            ),
          },
          { key: "session_id", label: "session_id", children: row.session_id ?? "—" },
          {
            key: "wall_started",
            label: "开始 (墙钟 UTC+8)",
            children: formatDateTimeUtc8(row.wall_started_at, row.wall_started_at),
          },
          {
            key: "wall_finished",
            label: "结束 (墙钟 UTC+8)",
            children: row.wall_finished_at ? formatDateTimeUtc8(row.wall_finished_at, row.wall_finished_at) : "—",
          },
          {
            key: "cycle_time",
            label: "逻辑时间 (UTC+8)",
            children:
              (row.cycle_time ?? row.cycle_time_utc) == null
                ? "—"
                : formatDateTimeUtc8(
                    row.cycle_time ?? row.cycle_time_utc,
                    row.cycle_time ?? row.cycle_time_utc,
                  ),
          },
          { key: "clock_mode", label: "时钟", children: row.clock_mode },
          ...(row.code_version || row.code_hash
            ? [
                {
                  key: "code_version",
                  label: "代码版本",
                  children: (
                    <Typography.Text className="font-mono text-xs">
                      {row.code_version ?? "—"}
                      {row.code_hash ? (
                        <Typography.Text type="secondary" className="font-mono text-xs">
                          {" "}({row.code_hash})
                        </Typography.Text>
                      ) : null}
                    </Typography.Text>
                  ),
                },
              ]
            : []),
        ]}
      />
      {row.failure_message ? (
        <Alert type="error" showIcon message="周期失败" description={row.failure_message} />
      ) : null}
      {failureStructured ? (
        <Collapse
          size="small"
          items={[
            {
              key: "failure_error",
              label: "结构化错误详情 (failure_error)",
              children: <JsonCodeBlock value={failureStructured} maxHeight={360} copyable />,
            },
          ]}
        />
      ) : null}
      {postCycle ? (
        <>
          <Typography.Title level={5} className="!mb-0">
            周期结束后账户
          </Typography.Title>
          <Space align="center" wrap>
            <Tag color={postCycle.source === "broker" ? "blue" : "default"}>
              {postCycle.source === "broker" ? "柜台" : "账本"}
            </Tag>
            <Typography.Text type="secondary">采集时间 {postCycle.captured_at}</Typography.Text>
          </Space>
          <Descriptions
            size="small"
            bordered
            column={{ xs: 1, sm: 2, lg: 4 }}
            items={[
              { key: "eq", label: "总资产 (equity)", children: fmtMoneyExact(postCycle.account.equity) },
              { key: "mv", label: "总市值", children: fmtMoneyExact(postCycle.total_market_value) },
              { key: "cash", label: "现金", children: fmtMoneyExact(postCycle.account.cash) },
              {
                key: "pc",
                label: "持仓只数",
                children: String(postCycle.positions?.length ?? 0),
              },
            ]}
          />
          <Table
            size="small"
            pagination={false}
            rowKey={(r) => r.symbol}
            dataSource={postCycle.positions}
            columns={[
              { title: "代码", dataIndex: "symbol", key: "symbol", width: 120 },
              {
                title: "名称",
                dataIndex: "name",
                key: "name",
                render: (v: string | null | undefined) => v ?? "—",
              },
              {
                title: "持仓市值",
                dataIndex: "market_value",
                key: "market_value",
                align: "right" as const,
                render: (v: string | null | undefined) => fmtMoneyExact(v),
              },
              {
                title: "股数",
                dataIndex: "quantity",
                key: "quantity",
                align: "right" as const,
                render: (v: number) => fmtNum(v, 0),
              },
              {
                title: "可用",
                dataIndex: "available",
                key: "available",
                align: "right" as const,
                render: (v: number | null | undefined) => fmtNum(v, 0),
              },
              {
                title: "成本",
                dataIndex: "cost_price",
                key: "cost_price",
                align: "right" as const,
                render: (v: string) => fmtMoneyExact(v),
              },
              {
                title: "现价",
                dataIndex: "last_price",
                key: "last_price",
                align: "right" as const,
                render: (v: string | null | undefined) => fmtMoneyExact(v),
              },
            ]}
          />
        </>
      ) : null}
      {showTraceHint ? (
        <Alert
          type="info"
          showIcon
          message="暂无事件流"
          description="本行没有可用的 OpenTelemetry trace_id（或 trace 尚未写入），因此无法展示该轮的 span。"
        />
      ) : null}

      <Typography.Title level={5} className="!mb-0">
        实际推送的卡片
      </Typography.Title>
      {(pd?.pushed_messages.items.length ?? 0) === 0 ? (
        <Alert
          type="info"
          showIcon
          message="未推送卡片"
          description={pd?.pushed_messages.reason ?? "本周期未产生已推送的卡片。"}
        />
      ) : (
        <Space direction="vertical" size={12} className="w-full">
          {pd!.pushed_messages.items.map((m) => (
            <Card
              key={m.message_id}
              size="small"
              title={
                <Space wrap size={[4, 4]}>
                  <Tag>{m.role}</Tag>
                  {m.source ? <Tag color="geekblue">{m.source}</Tag> : null}
                  {m.channel_target ? <Tag color="blue">{m.channel_target}</Tag> : null}
                  {m.delivery_status ? (
                    <Tag color={DELIVERY_STATUS_COLORS[m.delivery_status] ?? "default"}>
                      {m.delivery_status}
                    </Tag>
                  ) : null}
                  {m.reconstructed ? <Tag color="orange">重建</Tag> : null}
                </Space>
              }
              extra={
                <Typography.Text type="secondary" className="text-xs">
                  {formatDateTimeUtc8(m.created_at)}
                </Typography.Text>
              }
            >
              {m.note ? (
                <Alert
                  type="warning"
                  showIcon
                  className="!mb-2"
                  message={m.note}
                />
              ) : null}
              <MarkdownPreview source={m.content} stripFrontmatter />
            </Card>
          ))}
        </Space>
      )}

      <Typography.Title level={5} className="!mb-0">
        审批与结果回执
      </Typography.Title>
      {(pd?.approvals.items.length ?? 0) === 0 ? (
        <Alert
          type="info"
          showIcon
          message="无审批记录"
          description={pd?.approvals.reason ?? "本周期未产生需要审批的下单意图。"}
        />
      ) : (
        <Table<PendingApproval>
          size="small"
          rowKey="approval_id"
          pagination={false}
          dataSource={pd!.approvals.items}
          scroll={{ x: "max-content" }}
          columns={[
            {
              title: "标的",
              key: "symbol",
              render: (_: unknown, a: PendingApproval) => (
                <Space direction="vertical" size={0}>
                  <Typography.Text className="font-mono text-xs">{a.symbol ?? "—"}</Typography.Text>
                  {a.symbol_name ? (
                    <Typography.Text type="secondary" className="text-xs">
                      {a.symbol_name}
                    </Typography.Text>
                  ) : null}
                </Space>
              ),
            },
            {
              title: "方向",
              key: "action",
              width: 80,
              render: (_: unknown, a: PendingApproval) => {
                const meta = a.action ? APPROVAL_ACTION_META[a.action] : undefined;
                return meta ? <Tag color={meta.color}>{meta.label}</Tag> : a.action ?? "—";
              },
            },
            {
              title: "名义金额",
              key: "notional",
              align: "right" as const,
              render: (_: unknown, a: PendingApproval) => formatNotional(a.notional),
            },
            {
              title: "状态",
              key: "status",
              width: 96,
              render: (_: unknown, a: PendingApproval) => {
                const meta = a.status ? APPROVAL_STATUS_META[a.status] : undefined;
                return meta ? <Tag color={meta.color}>{meta.label}</Tag> : a.status ?? "—";
              },
            },
            {
              title: "来源/处理人",
              key: "resolver",
              render: (_: unknown, a: PendingApproval) => (
                <Space direction="vertical" size={0}>
                  <Typography.Text className="text-xs">{a.decision_source ?? "—"}</Typography.Text>
                  {a.resolver_id ? (
                    <Typography.Text type="secondary" className="text-xs">
                      {a.resolver_id}
                    </Typography.Text>
                  ) : null}
                </Space>
              ),
            },
            {
              title: "决策时间",
              key: "decided_at",
              render: (_: unknown, a: PendingApproval) => {
                const ts = a.decided_at ?? a.resolved_at ?? null;
                return ts ? formatDateTimeUtc8(ts, ts) : "—";
              },
            },
            {
              title: "结果回执",
              key: "receipt",
              render: (_: unknown, a: PendingApproval) => {
                if (a.matched_fill) {
                  const qty = a.matched_fill.quantity;
                  const price = a.matched_fill.price;
                  return (
                    <Tag color="green">
                      成交{qty != null ? ` ${qty}股` : ""}
                      {price != null ? ` @ ${price}` : ""}
                    </Tag>
                  );
                }
                if (a.dispatch_error) {
                  return (
                    <Tooltip title={a.dispatch_error}>
                      <Tag color="red">失败</Tag>
                    </Tooltip>
                  );
                }
                if (a.status === "rejected" || a.status === "expired") {
                  return <Tag>放弃</Tag>;
                }
                return "—";
              },
            },
          ]}
        />
      )}

      <Typography.Title level={5} className="!mb-0">
        推送/编排 Agent
      </Typography.Title>
      {!pd?.composer_agent.agent ? (
        <Alert
          type="info"
          showIcon
          message="无编排/推送 Agent"
          description={pd?.composer_agent.reason ?? "本周期无编排/推送 Agent。"}
        />
      ) : (
        <Descriptions
          size="small"
          bordered
          column={{ xs: 1, sm: 2 }}
          items={[
            {
              key: "agent_name",
              label: "名称",
              children: (
                <Space size={4} wrap>
                  <span>{pd.composer_agent.agent.name}</span>
                  {pd.composer_agent.agent.is_builtin ? <Tag color="gold">固定主智能体</Tag> : null}
                </Space>
              ),
            },
            {
              key: "agent_status",
              label: "状态",
              children: (
                <Tag color={pd.composer_agent.agent.status === "active" ? "success" : "default"}>
                  {pd.composer_agent.agent.status}
                </Tag>
              ),
            },
            {
              key: "model_route",
              label: "模型路由",
              children: pd.composer_agent.agent.model_route_name || "—",
            },
            {
              key: "compose_mode",
              label: "编排模式",
              children: pd.composer_agent.compose_mode ?? "—",
            },
            {
              key: "tools",
              label: "工具",
              children: `${pd.composer_agent.agent.tool_names.length} 个`,
            },
            {
              key: "skills",
              label: "技能",
              children: `${pd.composer_agent.agent.skill_names.length} 个`,
            },
            {
              key: "agent_id",
              label: "agent_id",
              children: (
                <Typography.Text copyable={{ text: pd.composer_agent.agent.id }} className="font-mono text-xs">
                  {pd.composer_agent.agent.id}
                </Typography.Text>
              ),
            },
          ]}
        />
      )}

      <Typography.Title level={5} className="!mb-0">
        落地的助手会话
      </Typography.Title>
      {!pd?.assistant_session.session ? (
        <Alert
          type="info"
          showIcon
          message="未落地助手会话"
          description={pd?.assistant_session.reason ?? "卡片未落地到助手会话。"}
        />
      ) : (
        <Descriptions
          size="small"
          bordered
          column={{ xs: 1, sm: 2 }}
          items={[
            {
              key: "title",
              label: "标题",
              children: pd.assistant_session.session.title ?? "—",
            },
            {
              key: "status",
              label: "状态",
              children: (
                <Tag color={statusColor(pd.assistant_session.session.status)}>
                  {pd.assistant_session.session.status}
                </Tag>
              ),
            },
            {
              key: "agent_id",
              label: "agent_id",
              children: pd.assistant_session.session.agent_id ?? "—",
            },
            {
              key: "session_id",
              label: "session_id",
              children: (
                <Typography.Text
                  copyable={{ text: pd.assistant_session.session.session_id }}
                  className="font-mono text-xs"
                >
                  {pd.assistant_session.session.session_id}
                </Typography.Text>
              ),
            },
            {
              key: "open",
              label: "操作",
              children: (
                <Button
                  size="small"
                  onClick={() =>
                    navigate(
                      `/assistant?session_id=${encodeURIComponent(
                        pd.assistant_session.session!.session_id,
                      )}`,
                    )
                  }
                >
                  打开会话
                </Button>
              ),
            },
          ]}
        />
      )}

      <Collapse
        items={[
          {
            key: "cycle-json",
            label: "周期详情（runtime / details）",
            children: (
              <JsonCodeBlock
                value={{
                  runtime_params: row.runtime_params,
                  details: row.details,
                  completed_phases: row.completed_phases,
                  failure_message: row.failure_message,
                }}
                maxHeight={420}
              />
            ),
          },
        ]}
      />

      <Typography.Title level={5} className="!mb-0">
        关联会话与追踪
      </Typography.Title>
      {!row.session_id ? (
        <Typography.Text type="secondary">本条周期未关联 debug session（无 session_id）。</Typography.Text>
      ) : !session ? (
        <Alert
          type="warning"
          showIcon
          message="会话记录不可用"
          description={`cycle_runs 引用了 session_id=${row.session_id}，但数据库中未找到对应 debug_sessions 行（可能已清理）。事件流仍可按 trace 展示。`}
        />
      ) : (
        <Space direction="vertical" size={16} className="w-full">
          <Descriptions
            size="small"
            bordered
            column={{ xs: 1, sm: 2, lg: 4 }}
            items={[
              {
                key: "status",
                label: "会话状态",
                children: <Tag color={statusColor(session.status)}>{session.status}</Tag>,
              },
              {
                key: "session_id",
                label: "session_id",
                children: (
                  <Typography.Text copyable={{ text: session.session_id }} className="font-mono text-xs">
                    {session.session_id}
                  </Typography.Text>
                ),
              },
              {
                key: "session_run_id",
                label: "会话当前 run_id",
                children: session.run_id ?? "—",
              },
              {
                key: "created",
                label: "创建 (UTC+8)",
                children: formatDateTimeUtc8(session.created_at, session.created_at),
              },
              {
                key: "finished",
                label: "结束 (UTC+8)",
                children: session.finished_at ? formatDateTimeUtc8(session.finished_at, session.finished_at) : "—",
              },
              {
                key: "session_type",
                label: "会话类型",
                children: <Tag color={sourceColor(session.session_type)}>{sourceTag(session.session_type)}</Tag>,
              },
            ]}
          />
          {session.error_message || session.error_type || session.traceback_tail ? (
            <Alert
              type="error"
              showIcon
              message={
                session.error_type
                  ? `会话执行失败 · ${session.error_type}`
                  : "会话执行失败"
              }
              description={
                <Space direction="vertical" size={4} style={{ width: "100%" }}>
                  {session.error_message ? (
                    <Typography.Text>{session.error_message}</Typography.Text>
                  ) : null}
                  {session.traceback_tail ? (
                    <Typography.Paragraph
                      className="font-mono text-xs"
                      style={{
                        marginBottom: 0,
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                      }}
                    >
                      {session.traceback_tail}
                    </Typography.Paragraph>
                  ) : null}
                </Space>
              }
            />
          ) : null}
          <Collapse
            items={[
              {
                key: "effective-config",
                label: "本次生效配置",
                children: <JsonCodeBlock value={session.effective_config} maxHeight={360} />,
              },
              {
                key: "request-overrides",
                label: "输入覆盖参数",
                children: (
                  <JsonCodeBlock value={session.input_overrides ?? {}} maxHeight={360} />
                ),
              },
            ]}
          />
        </Space>
      )}

      <Tabs
        items={[
          {
            key: "events",
            label: `事件流 · 本 run 的 trace（${spans.length} spans）`,
            children: <TraceViewer spans={spans} loading={false} />,
          },
          {
            key: "model",
            label: `模型调用 · 本 run_id（${data.model_invocations.length}）`,
            children: invocationItems.length ? (
              <Collapse items={invocationItems} />
            ) : (
              <Typography.Text type="secondary">暂无模型调用记录</Typography.Text>
            ),
          },
        ]}
      />
    </Space>
  );
}

type CycleRunAppliedQuery = Pick<
  ListCycleRunsParams,
  "q" | "status" | "run_kind" | "started_after" | "started_before"
>;

function wallRangeToTimeFilters(range: [Dayjs, Dayjs] | null): Pick<
  CycleRunAppliedQuery,
  "started_after" | "started_before"
> {
  if (!range?.[0] || !range?.[1]) return {};
  return {
    started_after: range[0].startOf("day").toISOString(),
    started_before: range[1].endOf("day").toISOString(),
  };
}

type CycleRunsTableSectionProps = {
  taskId: string;
  refreshTrigger: number;
  onBacktestRunSelected?: (runId: string | null) => void;
};

function CycleRunsTableSection({ taskId, refreshTrigger, onBacktestRunSelected }: CycleRunsTableSectionProps) {
  const [cycleRuns, setCycleRuns] = useState<CycleRunRow[]>([]);
  const [cycleRunsTotal, setCycleRunsTotal] = useState(0);
  const [loadingCycleRuns, setLoadingCycleRuns] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [appliedQuery, setAppliedQuery] = useState<CycleRunAppliedQuery>({});

  const [draftQ, setDraftQ] = useState("");
  const [draftStatus, setDraftStatus] = useState<string | undefined>(undefined);
  const [draftRunKind, setDraftRunKind] = useState<string | undefined>(undefined);
  const [draftRange, setDraftRange] = useState<[Dayjs, Dayjs] | null>(null);

  const [runDetailOpen, setRunDetailOpen] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [runDebugView, setRunDebugView] = useState<CycleRunDebugView | null>(null);
  const [loadingRunDebug, setLoadingRunDebug] = useState(false);

  const loadCycleRuns = useCallback(async () => {
    setLoadingCycleRuns(true);
    try {
      const res = await listCycleRuns(taskId, {
        limit: pageSize,
        offset: (page - 1) * pageSize,
        ...appliedQuery,
      });
      setCycleRuns(res.items);
      setCycleRunsTotal(res.total);
    } finally {
      setLoadingCycleRuns(false);
    }
  }, [taskId, page, pageSize, appliedQuery]);

  useEffect(() => {
    void loadCycleRuns().catch((error: unknown) => {
      const detail = error instanceof Error ? error.message : String(error);
      message.error(`加载周期记录失败：${detail}`);
    });
  }, [loadCycleRuns, refreshTrigger]);

  const refreshRunDebugViewSilent = useCallback(async () => {
    if (!selectedRunId) return;
    try {
      const data = await getCycleRunDebugView(taskId, selectedRunId);
      setRunDebugView(data);
    } catch {
      // silent poll failure
    }
  }, [taskId, selectedRunId]);

  useEffect(() => {
    if (!runDetailOpen || !selectedRunId) {
      return;
    }
    let cancelled = false;
    setRunDebugView(null);
    setLoadingRunDebug(true);
    void getCycleRunDebugView(taskId, selectedRunId)
      .then((data) => {
        if (!cancelled) setRunDebugView(data);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          const detail = error instanceof Error ? error.message : String(error);
          message.error(`加载周期详情失败：${detail}`);
          setRunDebugView(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingRunDebug(false);
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, runDetailOpen, selectedRunId]);

  const hasRunningCycle = useMemo(() => cycleRuns.some((r) => r.status === "running"), [cycleRuns]);

  useEffect(() => {
    if (!hasRunningCycle) return;
    const timer = window.setInterval(() => {
      void loadCycleRuns().catch(() => undefined);
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [hasRunningCycle, loadCycleRuns]);

  useEffect(() => {
    if (!runDetailOpen || !runDebugView) return;
    if (runDebugView.cycle_run.status !== "running") return;
    const timer = window.setInterval(() => {
      void refreshRunDebugViewSilent();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [runDetailOpen, runDebugView?.cycle_run.status, refreshRunDebugViewSilent]);

  const applyFilters = useCallback(() => {
    const t = draftQ.trim();
    const time = wallRangeToTimeFilters(draftRange);
    const next: CycleRunAppliedQuery = {
      ...(t ? { q: t } : {}),
      ...(draftStatus ? { status: draftStatus } : {}),
      ...(draftRunKind ? { run_kind: draftRunKind } : {}),
      ...time,
    };
    setAppliedQuery(next);
    setPage(1);
  }, [draftQ, draftStatus, draftRunKind, draftRange]);

  const resetFilters = useCallback(() => {
    setDraftQ("");
    setDraftStatus(undefined);
    setDraftRunKind(undefined);
    setDraftRange(null);
    setAppliedQuery({});
    setPage(1);
  }, []);

  // Parse each row's trade operations once per data change. The column render
  // runs on every panel re-render, and the panel polls every 1.5s while a cycle
  // is running, so parsing `details` JSON inline would re-walk fills / intents
  // for every visible row each frame. Keying by run_id (the table rowKey, unique
  // here) collapses that to one parse per row per refresh.
  const tradeOpsByRun = useMemo(() => {
    const map = new Map<string, ReturnType<typeof formatTradeOperationsFromDetails>>();
    for (const row of cycleRuns) {
      map.set(row.run_id, formatTradeOperationsFromDetails(row.details));
    }
    return map;
  }, [cycleRuns]);

  const cycleRunColumns: ColumnsType<CycleRunRow> = useMemo(
    () => [
      {
        title: "run_id",
        dataIndex: "run_id",
        key: "run_id",
        ellipsis: true,
        render: (v: string) => (
          <Typography.Text className="font-mono text-xs" copyable={{ text: v }} ellipsis={{ tooltip: v }}>
            {v}
          </Typography.Text>
        ),
      },
      { title: "状态", dataIndex: "status", key: "status", width: 96 },
      { title: "来源", dataIndex: "run_kind", key: "run_kind", width: 88 },
      { title: "时钟", dataIndex: "clock_mode", key: "clock_mode", width: 96 },
      {
        title: "逻辑时间 (UTC+8)",
        dataIndex: "cycle_time",
        key: "cycle_time",
        width: 168,
        ellipsis: true,
        render: (_: string | null, row: CycleRunRow) => {
          const v = row.cycle_time ?? row.cycle_time_utc ?? null;
          return v == null ? "—" : formatDateTimeUtc8(v, v);
        },
      },
      {
        title: "开始 (墙钟 UTC+8)",
        dataIndex: "wall_started_at",
        key: "wall_started_at",
        width: 168,
        ellipsis: true,
        render: (v: string) => formatDateTimeUtc8(v, v),
      },
      {
        title: "交易操作",
        key: "trade_operations",
        width: 280,
        render: (_: unknown, row: CycleRunRow) => {
          const model = tradeOpsByRun.get(row.run_id) ?? { lines: [] };
          if (!model.lines.length) return "—";
          return (
            <Space direction="vertical" size={2}>
              {model.lines.map((line, idx) => (
                <Typography.Text key={`${row.run_id}-op-${idx}`} className="text-xs">
                  {line}
                </Typography.Text>
              ))}
            </Space>
          );
        },
      },
      {
        title: "session",
        dataIndex: "session_id",
        key: "session_id",
        width: 120,
        ellipsis: true,
        render: (v: string | null) =>
          v ? (
            <Typography.Text className="font-mono text-xs" copyable={{ text: v }} ellipsis={{ tooltip: v }}>
              {v}
            </Typography.Text>
          ) : (
            "—"
          ),
      },
    ],
    [tradeOpsByRun],
  );

  const closeRunDetail = useCallback(() => {
    setRunDetailOpen(false);
    setSelectedRunId(null);
    setRunDebugView(null);
  }, []);

  return (
    <>
      <div className="rounded-2xl border border-shell-line bg-card-bg p-4">
        <Space align="center" wrap className="mb-3 w-full justify-between">
          <Typography.Title level={5} className="!mb-0">
            周期运行记录（cycle_runs）
          </Typography.Title>
          <Button icon={<ReloadOutlined />} onClick={() => void loadCycleRuns()} loading={loadingCycleRuns} size="small">
            刷新
          </Button>
        </Space>
        <Typography.Paragraph type="secondary" className="!mb-3 !text-xs">
          每次 run_cycle 一行。点击行打开详情：事件流仅包含该轮 trace，模型调用仅包含该 run_id。支持按 run_id 片段、状态、来源与开始日期（本地日界）筛选；接口单次最多返回 200 条。
        </Typography.Paragraph>
        <Space wrap className="mb-3 w-full" size={[8, 8]}>
          <Input
            allowClear
            placeholder="run_id 包含"
            value={draftQ}
            onChange={(e) => setDraftQ(e.target.value)}
            className="!w-[min(100%,220px)]"
            onPressEnter={() => applyFilters()}
          />
          <Select
            allowClear
            placeholder="状态"
            value={draftStatus}
            onChange={(v) => setDraftStatus(v)}
            className="!min-w-[120px]"
            options={[
              { value: "running", label: "running" },
              { value: "completed", label: "completed" },
              { value: "failed", label: "failed" },
            ]}
          />
          <Select
            allowClear
            placeholder="来源 run_kind"
            value={draftRunKind}
            onChange={(v) => setDraftRunKind(v)}
            className="!min-w-[140px]"
            options={[
              { value: "scheduled", label: "scheduled" },
              { value: "manual", label: "manual" },
              { value: "debug", label: "debug" },
            ]}
          />
          <DatePicker.RangePicker
            value={draftRange}
            onChange={(dates) => {
              if (dates?.[0] && dates?.[1]) setDraftRange([dates[0], dates[1]]);
              else setDraftRange(null);
            }}
          />
          <Button type="primary" icon={<SearchOutlined />} onClick={() => applyFilters()}>
            查询
          </Button>
          <Button onClick={() => resetFilters()}>重置</Button>
        </Space>
        <Table<CycleRunRow>
          size="small"
          rowKey="run_id"
          loading={loadingCycleRuns}
          columns={cycleRunColumns}
          dataSource={cycleRuns}
          pagination={{
            current: page,
            pageSize,
            total: cycleRunsTotal,
            showSizeChanger: true,
            pageSizeOptions: [10, 20, 50, 100, 200],
            showTotal: (t) => `共 ${t} 条`,
            onChange: (p, ps) => {
              setPage(p);
              setPageSize(ps);
            },
          }}
          scroll={{ x: "max-content" }}
          onRow={(record) => ({
            className: "cursor-pointer",
            onClick: () => {
              setSelectedRunId(record.run_id);
              setRunDetailOpen(true);
              if (record.run_mode === "backtest") {
                onBacktestRunSelected?.(record.run_id);
              }
            },
          })}
        />
      </div>

      <Modal
        title={selectedRunId ? `周期详情 · ${selectedRunId}` : "周期详情"}
        open={runDetailOpen}
        onCancel={closeRunDetail}
        footer={[
          <Button key="close" onClick={closeRunDetail}>
            关闭
          </Button>,
          <Button
            key="refresh"
            icon={<ReloadOutlined />}
            onClick={() => {
              if (!selectedRunId) return;
              setLoadingRunDebug(true);
              void getCycleRunDebugView(taskId, selectedRunId)
                .then(setRunDebugView)
                .catch((error: unknown) => {
                  const detail = error instanceof Error ? error.message : String(error);
                  message.error(`刷新失败：${detail}`);
                })
                .finally(() => setLoadingRunDebug(false));
            }}
          >
            刷新
          </Button>,
        ]}
        width="min(1100px, 94vw)"
        destroyOnClose
        styles={{
          body: { maxHeight: "min(85vh, 880px)", overflowY: "auto" },
        }}
      >
        <CycleRunDetailBody loading={loadingRunDebug} data={runDebugView} />
      </Modal>
    </>
  );
}


type TaskCycleRunsPanelProps = {
  task: TaskStatus;
  refreshTrigger: number;
  onBacktestRunSelected?: (runId: string | null) => void;
};

/** 任务详情页「周期运行」Tab：cycle_runs 列表与详情弹窗。 */
export function TaskCycleRunsPanel({ task, refreshTrigger, onBacktestRunSelected }: TaskCycleRunsPanelProps) {
  return (
    <CycleRunsTableSection
      key={task.task_id}
      taskId={task.task_id}
      refreshTrigger={refreshTrigger}
      onBacktestRunSelected={onBacktestRunSelected}
    />
  );
}
