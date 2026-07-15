import React from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert,
  Button,
  Collapse,
  Drawer,
  Empty,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type {
  CronJobRun,
  CronJobRunTrace,
  StrategySignalAlertTaskStatus,
} from "../types";
import { getCronJobRunTrace, listCronJobRuns } from "../api";
import { buildModelInvocationCollapseItems } from "../hooks/modelInvocation";
import { TraceViewer } from "./TraceViewer";

type Props = {
  jobId: string;
  jobName?: string;
  onClose: () => void;
};

const STATUS_COLORS: Record<string, string> = {
  success: "green",
  error: "red",
  agent_failed: "red",
  pre_failed: "red",
  running: "blue",
  skipped: "orange",
  cancelled: "gray",
};

const DELIVERY_STATUS_COLORS: Record<string, string> = {
  delivered: "green",
  suppressed: "blue",
  skipped: "orange",
  failed: "red",
  none: "default",
};

const SIGNAL_ALERT_STATUS_LABELS: Record<string, string> = {
  task_not_signal_only: "not signal_only",
  task_not_cron_driven: "not cron_driven",
  task_not_running_for_cron_signal: "not running",
  task_lookup_failed: "task lookup failed",
  no_cycle_executed: "no cycle executed",
};

const SIGNAL_ALERT_STATUS_COLORS: Record<string, string> = {
  task_not_signal_only: "red",
  task_not_cron_driven: "orange",
  task_not_running_for_cron_signal: "gold",
  task_lookup_failed: "red",
  no_cycle_executed: "default",
};

/** Self-contained modal showing the most recent ``cron_job_runs`` rows for a job.
 *
 * Surfaces both the task-pipeline columns (``cron_task_kind`` /
 * ``delivery_status``) and the legacy pre-action columns so users can
 * correlate a cron fire to the cycle / debug session / assistant push it
 * created. Each row exposes a "查看 Trace" action that opens a side drawer
 * with the consolidated span tree for the fire (driven by
 * ``GET /assistant/cron-job-runs/{run_id}/trace`` → :class:`TraceViewer`). */
export const CronJobRunHistoryModal: React.FC<Props> = ({ jobId, jobName, onClose }) => {
  const navigate = useNavigate();
  const [runs, setRuns] = React.useState<CronJobRun[]>([]);
  const [loading, setLoading] = React.useState(false);

  // Trace drawer state — kept inside this modal so it dismisses with it.
  const [traceRun, setTraceRun] = React.useState<CronJobRun | null>(null);
  const [traceLoading, setTraceLoading] = React.useState(false);
  const [traceError, setTraceError] = React.useState<string | null>(null);
  const [tracePayload, setTracePayload] = React.useState<CronJobRunTrace | null>(null);

  const reload = React.useCallback(async () => {
    setLoading(true);
    try {
      const result = await listCronJobRuns(jobId, 50);
      setRuns(result.items);
    } catch (err) {
      message.error(`加载运行记录失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setLoading(false);
    }
  }, [jobId]);

  React.useEffect(() => {
    void reload();
  }, [reload]);

  const openTrace = React.useCallback(async (run: CronJobRun) => {
    setTraceRun(run);
    setTracePayload(null);
    setTraceError(null);
    setTraceLoading(true);
    try {
      const payload = await getCronJobRunTrace(run.id);
      setTracePayload(payload);
    } catch (err) {
      setTraceError(String(err));
    } finally {
      setTraceLoading(false);
    }
  }, []);

  const closeTrace = React.useCallback(() => {
    setTraceRun(null);
    setTracePayload(null);
    setTraceError(null);
  }, []);

  const openAssistantSession = React.useCallback((sessionId: string) => {
    navigate(`/assistant?session_id=${encodeURIComponent(sessionId)}`);
  }, [navigate]);

  const columns = [
    {
      title: "Fired At",
      dataIndex: "fired_at",
      key: "fired_at",
      render: (v: string | null) => (v ? new Date(v).toLocaleString() : "—"),
    },
    {
      title: "Status",
      dataIndex: "status",
      key: "status",
      render: (status: string) => (
        <Tag color={STATUS_COLORS[status] || "gray"}>{status}</Tag>
      ),
    },
    {
      title: "Trace id",
      dataIndex: "trace_id",
      key: "trace_id",
      render: (v: string | null) =>
        v ? (
          <Typography.Text code copyable={{ text: v }} style={{ fontSize: 11 }}>
            {v}
          </Typography.Text>
        ) : (
          "—"
        ),
    },
    {
      title: "Task kind",
      dataIndex: "cron_task_kind",
      key: "cron_task_kind",
      render: (v: string | null) =>
        v ? <Tag color="purple">{v}</Tag> : <span style={{ color: "#999" }}>legacy</span>,
    },
    {
      title: "Delivery",
      dataIndex: "delivery_status",
      key: "delivery_status",
      render: (v: string | null) =>
        v ? (
          <Tag color={DELIVERY_STATUS_COLORS[v] || "default"}>{v}</Tag>
        ) : (
          <span style={{ color: "#999" }}>—</span>
        ),
    },
    {
      title: "Pre-action",
      key: "pre",
      render: (_: unknown, record: CronJobRun) => {
        if (!record.pre_kind) return <span style={{ color: "#999" }}>—</span>;
        const tagColor =
          record.pre_status === "ok" ? "green" : record.pre_status === "error" ? "red" : "default";
        return (
          <span>
            <Tag>{record.pre_kind}</Tag>
            {record.pre_status && <Tag color={tagColor}>{record.pre_status}</Tag>}
          </span>
        );
      },
    },
    {
      title: "Pre run_id",
      dataIndex: "pre_run_id",
      key: "pre_run_id",
      render: (v: string | null) =>
        v ? (
          <Typography.Text code copyable={{ text: v }} style={{ fontSize: 11 }}>
            {v}
          </Typography.Text>
        ) : (
          "—"
        ),
    },
    {
      title: "Task checks",
      key: "task_checks",
      render: (_: unknown, record: CronJobRun) => {
        const instances = getSignalAlertInstances(record);
        if (instances.length === 0) {
          return <span style={{ color: "#999" }}>—</span>;
        }
        return (
          <Space wrap size={4}>
            {instances.map((inst, idx) => {
              const rawStatus = inst.status ?? "unknown";
              const label = SIGNAL_ALERT_STATUS_LABELS[rawStatus] || rawStatus;
              return (
                <Tag key={`${inst.task_id ?? "?"}-${idx}`} color={SIGNAL_ALERT_STATUS_COLORS[rawStatus] || "default"}>
                  {(inst.task_id ?? "?")}: {label}
                </Tag>
              );
            })}
          </Space>
        );
      },
    },
    {
      title: "Agent session",
      dataIndex: "agent_session_id",
      key: "agent_session_id",
      render: (v: string | null) =>
        v ? (
          <Space size={4}>
            <Typography.Text code copyable={{ text: v }} style={{ fontSize: 11 }}>
              {v}
            </Typography.Text>
            <Button size="small" type="link" onClick={() => openAssistantSession(v)}>
              查看会话
            </Button>
          </Space>
        ) : (
          "—"
        ),
    },
    {
      title: "Error",
      key: "error",
      render: (_: unknown, record: CronJobRun) => {
        const err = record.agent_error || record.pre_error;
        if (!err) return null;
        return (
          <Typography.Text type="danger" style={{ fontSize: 11 }} ellipsis={{ tooltip: err }}>
            {err}
          </Typography.Text>
        );
      },
    },
    {
      title: "Action",
      key: "actions",
      render: (_: unknown, record: CronJobRun) => (
        <Button
          size="small"
          type="link"
          // Enable whenever we have any session id to query — both
          // task-pipeline fires (agent_session_id) and legacy fires
          // (pre_debug_session_id) produce spans.
          disabled={!record.agent_session_id && !record.pre_debug_session_id}
          onClick={() => void openTrace(record)}
        >
          查看 Trace
        </Button>
      ),
    },
  ];

  const drawerTitle = traceRun
    ? `Trace — ${traceRun.cron_task_kind ?? traceRun.pre_kind ?? "cron"} fire (${traceRun.id})`
    : "Trace";

  return (
    <Modal
      title={`Run history — ${jobName ?? jobId}`}
      open
      onCancel={onClose}
      onOk={onClose}
      width={1100}
      footer={null}
    >
      <Table
        rowKey="id"
        dataSource={runs}
        columns={columns}
        loading={loading}
        pagination={false}
        size="small"
        scroll={{ x: "max-content" }}
      />

      <Drawer
        title={drawerTitle}
        open={traceRun !== null}
        onClose={closeTrace}
        width={Math.min(1100, window.innerWidth - 80)}
        destroyOnClose
      >
        {traceError && (
          <Alert
            type="error"
            message="Failed to load trace"
            description={traceError}
            style={{ marginBottom: 12 }}
          />
        )}
        {traceRun?.delivery_status === "failed" && (
          // Delivery error lives on agent_error with a ``delivery_failed:``
          // prefix (cron_manager promotes it there). Surface it loudly in
          // the trace drawer too, because the cron.delivery span carries
          // the same info but it's easy to miss when scanning a tree.
          <Alert
            type="error"
            showIcon
            message="Delivery failed"
            description={
              <Typography.Paragraph
                style={{ marginBottom: 0, whiteSpace: "pre-wrap", fontSize: 12 }}
              >
                {traceRun.agent_error ?? "see the cron.delivery span below"}
              </Typography.Paragraph>
            }
            style={{ marginBottom: 12 }}
          />
        )}
        {tracePayload && (
          <CronTraceContext run={traceRun} payload={tracePayload} />
        )}
        <CronTraceBody payload={tracePayload} loading={traceLoading} />
      </Drawer>
    </Modal>
  );
};

/** Span tree + model-invocation panel for one cron fire. Mirrors the
 * ``TracesPanel`` drawer (assistant session traces) so the two surfaces
 * stay visually consistent: ``TraceViewer`` for the span tree, then the
 * shared ``buildModelInvocationCollapseItems`` factory for the LLM call
 * details (request payload / token usage / latency). */
const CronTraceBody: React.FC<{
  payload: CronJobRunTrace | null;
  loading: boolean;
}> = ({ payload, loading }) => {
  const invocationItems = React.useMemo(
    () =>
      payload && payload.model_invocations.length
        ? buildModelInvocationCollapseItems(payload.model_invocations)
        : [],
    [payload],
  );

  if (payload && payload.spans.length === 0 && !loading) {
    return (
      <Empty
        description={
          <Space direction="vertical" size={2} align="center">
            <span>No spans recorded for this fire.</span>
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              This usually means the cron fire was suppressed or rolled back
              before any spans were exported.
            </Typography.Text>
          </Space>
        }
      />
    );
  }

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <TraceViewer spans={payload ? payload.spans : []} loading={loading} />
      <Typography.Title level={5} style={{ margin: "8px 0 4px" }}>
        模型调用
      </Typography.Title>
      {invocationItems.length > 0 ? (
        <Collapse items={invocationItems} size="small" />
      ) : (
        <Typography.Text type="secondary">暂无模型调用</Typography.Text>
      )}
    </Space>
  );
};

/** Header panel above the span tree summarising what this fire touched —
 * delivery outcome, sessions involved, and per-instance run_ids that
 * ``strategy_signal_alert`` stamped into the run. */
const CronTraceContext: React.FC<{
  run: CronJobRun | null;
  payload: CronJobRunTrace;
}> = ({ run, payload }) => {
  if (!run) return null;
  return (
    <Space direction="vertical" size={6} style={{ marginBottom: 12, width: "100%" }}>
      <Space wrap>
        <Tag color={STATUS_COLORS[run.status] || "default"}>{run.status}</Tag>
        {run.cron_task_kind && <Tag color="purple">{run.cron_task_kind}</Tag>}
        {run.delivery_status && (
          <Tag color={DELIVERY_STATUS_COLORS[run.delivery_status] || "default"}>
            delivery: {run.delivery_status}
          </Tag>
        )}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          Sessions: {payload.session_ids.length === 0 ? "—" : payload.session_ids.join(", ")}
        </Typography.Text>
      </Space>
      {payload.related.length > 0 && (
        <Space wrap size={4}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            Related cycle runs:
          </Typography.Text>
          {payload.related.map((rel, idx) => (
            <Tag key={`${rel.task_id ?? "?"}-${idx}`} style={{ marginRight: 0 }}>
              {rel.task_id ?? "?"}
              {rel.run_id ? (
                <Typography.Text
                  code
                  copyable={{ text: rel.run_id }}
                  style={{ fontSize: 11, marginLeft: 4 }}
                >
                  {rel.run_id}
                </Typography.Text>
              ) : null}
              {rel.status && <span style={{ marginLeft: 4 }}>[{rel.status}]</span>}
            </Tag>
          ))}
        </Space>
      )}
    </Space>
  );
};

type SignalAlertInstance = {
  task_id: string | null;
  status: StrategySignalAlertTaskStatus | null;
};

function getSignalAlertInstances(run: CronJobRun): SignalAlertInstance[] {
  const preResult = run.pre_result_json;
  if (!preResult || typeof preResult !== "object") return [];
  const preData = (preResult as Record<string, unknown>).pre_data;
  if (!preData || typeof preData !== "object") return [];
  const instances = (preData as Record<string, unknown>).instances;
  if (!Array.isArray(instances)) return [];
  return instances.flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const row = entry as Record<string, unknown>;
    const taskId = typeof row.task_id === "string" ? row.task_id : null;
    const status = typeof row.status === "string"
      ? (row.status as StrategySignalAlertTaskStatus)
      : null;
    if (!taskId && !status) return [];
    return [{ task_id: taskId, status }];
  });
}
