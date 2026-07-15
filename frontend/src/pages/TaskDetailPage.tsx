import {
  AppstoreOutlined,
  ArrowLeftOutlined,
  DatabaseOutlined,
  DollarOutlined,
  ExperimentOutlined,
  FallOutlined,
  FunctionOutlined,
  FundOutlined,
  LineChartOutlined,
  PlayCircleOutlined,
  RiseOutlined,
  StockOutlined,
  SyncOutlined,
  ThunderboltOutlined,
  WalletOutlined,
} from "@ant-design/icons";
import { Button, Card, Col, Modal, Result, Row, Space, Spin, Tabs, Tag, Typography, message } from "antd";
import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import {
  deleteTask,
  getTask,
  getTaskRun,
  listCycleRuns,
  listTaskRuns,
  pauseTask,
  pauseTaskRun,
  resumeTaskRun,
  startTask,
  stopTask,
  stopTaskRun,
} from "../api";
import { BacktestOverviewPanel } from "../components/BacktestOverviewPanel";
import { BacktestRunChartPanel } from "../components/BacktestRunChartPanel";
import { BacktestRunConfigPanel } from "../components/BacktestRunConfigPanel";
import { BacktestSummaryHeader } from "../components/BacktestSummaryHeader";
import { CreateAgentCard, type CreateAgentCardHandle } from "../components/CreateAgentCard";
import { KnowledgeJournalsPanel } from "../components/KnowledgeJournalsPanel";
import { TaskCycleRunsPanel } from "../components/TaskCycleRunsPanel";
import { TaskDebugPanel } from "../components/TaskDebugPanel";
import { TaskMetricTile, type MetricTileTone } from "../components/TaskMetricTile";
import { TaskReviewPanel } from "../components/TaskReviewPanel";
import { TaskTriggersPanel } from "../components/TaskTriggersPanel";
import { useConsoleOutlet } from "../consoleOutletContext";
import { usePageRefreshToken } from "../pageRefreshContext";
import type { CycleRunRow, RunRow, TaskStatus } from "../types";
import { SOFT_TAG_CLASSNAME } from "../styles/classNames";
import { fmtMoneyExact, summarizeAccountMetrics } from "../utils/cycleRunListFormat";
import { formatStatus, resolveBacktestDisplayStatus } from "../utils/taskStatus";

const MODE_LABEL_MAP: Record<string, string> = {
  paper: "模拟盘",
  live: "实盘",
  backtest: "回测",
  signal_only: "信号",
};

function formatMode(mode: string): string {
  return MODE_LABEL_MAP[mode] ?? mode;
}

function canStopTask(status: string): boolean {
  return status === "running" || status === "paused";
}

function isMissingRunError(error: unknown): boolean {
  if (typeof error !== "object" || error == null) return false;
  const maybeStatus = (error as { status?: unknown }).status;
  if (maybeStatus === 404) return true;
  const maybeMessage = (error as { message?: unknown }).message;
  if (typeof maybeMessage !== "string") return false;
  const lowerMessage = maybeMessage.toLowerCase();
  const indicatesNotFoundSemantics = lowerMessage.includes("not found") || lowerMessage.includes("missing");
  if (lowerMessage.includes("http 404")) return true;
  if (lowerMessage.includes("status 404")) return true;
  if (lowerMessage.startsWith("404")) return true;
  if (lowerMessage.includes("404") && indicatesNotFoundSemantics) {
    return true;
  }
  return false;
}

const isMissingTaskError = isMissingRunError;

/** Signed money: ``1234.5`` → ``+1,234.50`` / ``-1234.5`` → ``-1,234.50``. */
function fmtSignedMoney(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v < 0 ? "-" : v > 0 ? "+" : "";
  return `${sign}${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** Signed percent (input already in percent units): ``5.5`` → ``+5.50%``. */
function fmtSignedPct(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : v < 0 ? "-" : "";
  return `${sign}${Math.abs(v).toFixed(digits)}%`;
}

function modeIcon(mode: string): ReactNode {
  switch (mode) {
    case "backtest":
      return <LineChartOutlined />;
    case "live":
      return <DollarOutlined />;
    case "paper":
      return <ExperimentOutlined />;
    case "signal_only":
      return <ThunderboltOutlined />;
    default:
      return <AppstoreOutlined />;
  }
}

type StatusTone = { dot: string; pill: string; pulse: boolean };

function statusPillTone(status: string): StatusTone {
  switch (status) {
    case "running":
      return { dot: "bg-emerald-500", pill: "border-emerald-200 bg-emerald-50 text-emerald-700", pulse: true };
    case "paused":
      return { dot: "bg-amber-500", pill: "border-amber-200 bg-amber-50 text-amber-700", pulse: false };
    case "error":
      return { dot: "bg-rose-500", pill: "border-rose-200 bg-rose-50 text-rose-700", pulse: false };
    case "completed":
      return { dot: "bg-sky-500", pill: "border-sky-200 bg-sky-50 text-sky-700", pulse: false };
    default:
      // configured / stopped / unknown — a quiet neutral pill.
      return { dot: "bg-slate-400", pill: "border-slate-200 bg-slate-50 text-slate-600", pulse: false };
  }
}

/** Inline status badge shown next to the task title: a colored dot (pulsing
 * while running) + the localized status label. */
function StatusPill({ status }: { status: string }) {
  const tone = statusPillTone(status);
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${tone.pill}`}
    >
      <span className="relative flex h-1.5 w-1.5">
        {tone.pulse ? (
          <span className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-60 ${tone.dot}`} />
        ) : null}
        <span className={`relative inline-flex h-1.5 w-1.5 rounded-full ${tone.dot}`} />
      </span>
      {formatStatus(status)}
    </span>
  );
}

/** Pill-style metadata chip used in the task-detail header (mode / strategy /
 * symbols / cycles / data source). */
function MetaTag({ icon, children }: { icon?: ReactNode; children: ReactNode }) {
  return (
    <Tag className={`${SOFT_TAG_CLASSNAME} !m-0 !inline-flex !items-center !gap-1 !rounded-full !px-2.5 !py-0.5`}>
      {icon}
      <span>{children}</span>
    </Tag>
  );
}

export function TaskDetailPage() {
  const pageRefreshToken = usePageRefreshToken();
  const { taskId } = useParams<{ taskId: string }>();
  const [searchParams] = useSearchParams();
  const tabFromUrl = searchParams.get("tab");
  const { instances, loading, refresh } = useConsoleOutlet();
  const navigate = useNavigate();
  const [detailTab, setDetailTab] = useState("debug");
  const [selectedBacktestRunId, setSelectedBacktestRunId] = useState<string | null>(null);
  const [backtestRun, setBacktestRun] = useState<RunRow | null>(null);
  const [backtestRunLoading, setBacktestRunLoading] = useState(false);
  const [backtestRunActing, setBacktestRunActing] = useState(false);
  const [backtestTaskDetail, setBacktestTaskDetail] = useState<TaskStatus | null>(null);
  const [directTask, setDirectTask] = useState<TaskStatus | null>(null);
  const [directTaskState, setDirectTaskState] = useState<"idle" | "loading" | "found" | "not_found">("idle");
  const selectedBacktestRunIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!taskId) {
      setDirectTask(null);
      setDirectTaskState("idle");
      return;
    }
    let cancelled = false;
    setDirectTaskState("loading");
    void getTask(taskId)
      .then((task) => {
        if (cancelled) return;
        setDirectTask(task);
        setDirectTaskState("found");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (isMissingTaskError(error)) {
          setDirectTask(null);
          setDirectTaskState("not_found");
          return;
        }
        const detail = error instanceof Error ? error.message : String(error);
        message.error(`加载任务详情失败：${detail}`);
        setDirectTaskState((prev) => (prev === "found" ? "found" : "not_found"));
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, pageRefreshToken]);

  const instance = useMemo(() => {
    const fromInstances = instances.find((i) => i.task_id === taskId);
    if (fromInstances) return fromInstances;
    if (directTask && directTask.task_id === taskId) return directTask;
    return undefined;
  }, [instances, directTask, taskId]);
  const backtestTask: TaskStatus | undefined = useMemo(() => {
    if (!instance) return undefined;
    if (instance.mode !== "backtest") return instance;
    return backtestTaskDetail ?? instance;
  }, [instance, backtestTaskDetail]);
  const displayStatus = useMemo(() => {
    if (!instance) return "";
    if (instance.mode !== "backtest") return instance.status;
    return resolveBacktestDisplayStatus(backtestTask?.status ?? instance.status, backtestRun?.status);
  }, [instance, backtestTask?.status, backtestRun?.status]);

  useEffect(() => {
    const defaultTab = instance?.mode === "backtest" ? "overview" : "debug";
    if (instance?.mode === "backtest" && tabFromUrl === "debug") {
      setDetailTab(defaultTab);
      return;
    }
    if (
      tabFromUrl === "debug" ||
      tabFromUrl === "cycle_runs" ||
      tabFromUrl === "triggers" ||
      tabFromUrl === "review" ||
      tabFromUrl === "config" ||
      tabFromUrl === "chart" ||
      tabFromUrl === "overview"
    ) {
      setDetailTab(tabFromUrl);
    } else {
      setDetailTab(defaultTab);
    }
  }, [tabFromUrl, taskId, instance?.mode]);
  const [cycleRunsRefreshTrigger, setCycleRunsRefreshTrigger] = useState(0);
  const [reviewRows, setReviewRows] = useState<CycleRunRow[]>([]);
  const configCardRef = useRef<CreateAgentCardHandle>(null);

  // Headline equity tiles for live / paper / signal tasks come from the same
  // cycle-run account snapshots the 复盘 panel reads — first vs latest equity.
  // null = no cycle has captured a snapshot yet (render an explicit "—").
  const accountMetrics = useMemo(
    () => (instance && instance.mode !== "backtest" ? summarizeAccountMetrics(reviewRows) : null),
    [instance, reviewRows],
  );

  useEffect(() => {
    selectedBacktestRunIdRef.current = selectedBacktestRunId;
  }, [selectedBacktestRunId]);

  const loadLatestBacktestRun = useCallback(
    async (taskIdValue: string) => {
      setBacktestRunLoading(true);
      try {
        const runs = await listTaskRuns(taskIdValue, { limit: 50, offset: 0 });
        const newestRun = runs.items[0] ?? null;
        setBacktestRun(newestRun);
        const currentSelectedRunId = selectedBacktestRunIdRef.current;
        if (currentSelectedRunId == null) {
          setSelectedBacktestRunId(newestRun?.run_id ?? null);
          return;
        }
        const selectionInFetchedPage = runs.items.some((run) => run.run_id === currentSelectedRunId);
        if (selectionInFetchedPage) {
          setSelectedBacktestRunId(currentSelectedRunId);
          return;
        }
        try {
          await getTaskRun(taskIdValue, currentSelectedRunId);
          setSelectedBacktestRunId(currentSelectedRunId);
        } catch (error: unknown) {
          if (isMissingRunError(error)) {
            setSelectedBacktestRunId(newestRun?.run_id ?? null);
            return;
          }
          setSelectedBacktestRunId(currentSelectedRunId);
        }
      } finally {
        setBacktestRunLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (!taskId || instance?.mode !== "backtest") {
      setSelectedBacktestRunId(null);
      setBacktestRun(null);
      return;
    }
    void loadLatestBacktestRun(taskId).catch((error: unknown) => {
      const detail = error instanceof Error ? error.message : String(error);
      message.error(`加载回测运行失败：${detail}`);
    });
  }, [taskId, instance?.mode, loadLatestBacktestRun, pageRefreshToken]);

  useEffect(() => {
    if (!taskId || instance?.mode !== "backtest") {
      setBacktestTaskDetail(null);
      return;
    }
    let cancelled = false;
    void getTask(taskId)
      .then((detail) => {
        if (!cancelled) setBacktestTaskDetail(detail);
      })
      .catch((error: unknown) => {
        const detailMsg = error instanceof Error ? error.message : String(error);
        message.error(`加载回测概览失败：${detailMsg}`);
      });
    return () => {
      cancelled = true;
    };
  }, [
    taskId,
    pageRefreshToken,
    instance?.mode,
    instance?.status,
    instance?.cycles,
    backtestRun?.status,
    backtestRun?.bars_completed,
  ]);

  useEffect(() => {
    if (pageRefreshToken === 0) return;
    setCycleRunsRefreshTrigger((n) => n + 1);
  }, [pageRefreshToken]);

  // 复盘 (account review) is for live / paper tasks — load the full cycle-run
  // series once (one call carries every row's full details incl.
  // post_cycle_account); the panel sorts and filters from there.
  useEffect(() => {
    if (!taskId || instance?.mode === "backtest") {
      setReviewRows([]);
      return;
    }
    let cancelled = false;
    void listCycleRuns(taskId, { limit: 200, offset: 0 })
      .then((res) => {
        if (!cancelled) setReviewRows(res.items);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const detail = error instanceof Error ? error.message : String(error);
        message.error(`加载复盘数据失败：${detail}`);
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, instance?.mode, pageRefreshToken, cycleRunsRefreshTrigger]);

  if (!instance && (loading || directTaskState === "loading" || directTaskState === "idle")) {
    return (
      <div className="flex min-h-[240px] items-center justify-center">
        <Spin size="large" />
      </div>
    );
  }

  if (!taskId || !instance) {
    return (
      <Result
        status="404"
        title="未找到任务"
        subTitle="该任务可能已删除，或链接无效。"
        extra={
          <Button type="primary" onClick={() => navigate("/tasks")}>
            返回任务列表
          </Button>
        }
      />
    );
  }

  const pnlChange = accountMetrics?.change ?? null;
  const pnlNegative = pnlChange != null && pnlChange < 0;
  const pnlPositive = pnlChange != null && pnlChange > 0;
  const pnlTone: MetricTileTone = pnlPositive ? "emerald" : pnlNegative ? "rose" : "slate";
  const pnlValueClass = pnlPositive ? "!text-emerald-600" : pnlNegative ? "!text-rose-600" : "";

  return (
    <Space direction="vertical" size={16} className="w-full">
      <div>
        <Button type="link" className="!px-0 !text-shell-ink" icon={<ArrowLeftOutlined />} onClick={() => navigate("/tasks")}>
          返回任务列表
        </Button>
      </div>

      <Card className="!overflow-hidden !border !border-shell-line !bg-card-bg shadow-shell-card">
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <Typography.Title level={4} className="!mb-0">
                  {instance.name}
                </Typography.Title>
                <StatusPill status={displayStatus} />
              </div>
              <Typography.Text type="secondary" copyable={{ text: instance.task_id }} className="font-mono text-xs">
                {instance.task_id}
              </Typography.Text>
            </div>
            <Space wrap>
              {instance.mode !== "backtest" ? (
                <>
                  <Button
                    className="rounded-xl"
                    type={instance.status === "running" ? "default" : "primary"}
                    onClick={async () => {
                      if (instance.status === "running") {
                        await pauseTask(instance.task_id);
                      } else {
                        await startTask(instance.task_id);
                      }
                      await refresh();
                    }}
                  >
                    {instance.status === "running" ? "暂停" : "启动"}
                  </Button>
                  <Button
                    className="rounded-xl"
                    danger
                    disabled={!canStopTask(instance.status)}
                    title={!canStopTask(instance.status) ? "请先启动任务后再停止" : undefined}
                    onClick={async () => {
                      await stopTask(instance.task_id);
                      await refresh();
                    }}
                  >
                    停止
                  </Button>
                </>
              ) : (
                <>
                  <Button
                    className="rounded-xl"
                    loading={backtestRunActing || backtestRunLoading}
                    disabled={!backtestRun || !["running", "paused"].includes(backtestRun.status)}
                    onClick={async () => {
                      if (!backtestRun) return;
                      setBacktestRunActing(true);
                      try {
                        if (backtestRun.status === "running") {
                          await pauseTaskRun(instance.task_id, backtestRun.run_id);
                        } else {
                          await resumeTaskRun(instance.task_id, backtestRun.run_id);
                        }
                        await loadLatestBacktestRun(instance.task_id);
                        setCycleRunsRefreshTrigger((n) => n + 1);
                      } finally {
                        setBacktestRunActing(false);
                      }
                    }}
                  >
                    {backtestRun?.status === "running" ? "暂停回测" : "继续回测"}
                  </Button>
                  <Button
                    className="rounded-xl"
                    danger
                    loading={backtestRunActing}
                    disabled={!backtestRun || !["running", "paused", "pending"].includes(backtestRun.status)}
                    onClick={async () => {
                      if (!backtestRun) return;
                      setBacktestRunActing(true);
                      try {
                        await stopTaskRun(instance.task_id, backtestRun.run_id);
                        await loadLatestBacktestRun(instance.task_id);
                        setCycleRunsRefreshTrigger((n) => n + 1);
                      } finally {
                        setBacktestRunActing(false);
                      }
                    }}
                  >
                    停止回测
                  </Button>
                </>
              )}
              <Button
                className="rounded-xl"
                danger
                type="primary"
                onClick={() => {
                  Modal.confirm({
                    title: "删除任务",
                    content: `确定删除「${instance.name}」吗？该操作不可恢复，持久化记录将被移除。`,
                    okText: "删除",
                    okButtonProps: { danger: true },
                    cancelText: "取消",
                    onOk: async () => {
                      try {
                        await deleteTask(instance.task_id);
                        message.success("已删除任务");
                        await refresh();
                        navigate("/tasks");
                      } catch (err) {
                        const detail = err instanceof Error ? err.message : String(err);
                        message.error(`删除失败：${detail}`);
                      }
                    },
                  });
                }}
              >
                删除
              </Button>
            </Space>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <MetaTag icon={modeIcon(instance.mode)}>{formatMode(instance.mode)}</MetaTag>
            {instance.strategy_name ? (
              <MetaTag icon={<FunctionOutlined />}>{instance.strategy_name}</MetaTag>
            ) : null}
            {instance.universe.slice(0, 6).map((sym) => (
              <MetaTag key={sym} icon={<StockOutlined />}>
                {sym}
              </MetaTag>
            ))}
            {instance.universe.length > 6 ? <MetaTag>+{instance.universe.length - 6}</MetaTag> : null}
            <MetaTag icon={<SyncOutlined />}>轮次 {instance.cycles ?? "—"}</MetaTag>
            {instance.data_provider_effective && instance.data_provider_effective !== "none" ? (
              <MetaTag icon={<DatabaseOutlined />}>{instance.data_provider_effective}</MetaTag>
            ) : null}
            {instance.mode === "backtest" ? (
              <MetaTag icon={<PlayCircleOutlined />}>
                运行状态：{backtestRunLoading ? "加载中…" : backtestRun?.status ?? "未启动"}
              </MetaTag>
            ) : null}
          </div>
        </div>
      </Card>

      {instance.mode === "backtest" ? (
        backtestTask ? <BacktestSummaryHeader task={backtestTask} run={backtestRun} /> : null
      ) : (
        <Row gutter={[12, 12]}>
          <Col xs={24} sm={8}>
            <TaskMetricTile
              tone="violet"
              icon={<WalletOutlined />}
              label="起始权益"
              value={accountMetrics ? fmtMoneyExact(accountMetrics.startEquity) : "—"}
            />
          </Col>
          <Col xs={24} sm={8}>
            <TaskMetricTile
              tone="sky"
              icon={<FundOutlined />}
              label="当前权益"
              value={accountMetrics ? fmtMoneyExact(accountMetrics.endEquity) : "—"}
              sub={accountMetrics ? `覆盖 ${accountMetrics.pointCount} 个周期` : undefined}
            />
          </Col>
          <Col xs={24} sm={8}>
            <TaskMetricTile
              tone={pnlTone}
              icon={pnlNegative ? <FallOutlined /> : <RiseOutlined />}
              label="总盈亏"
              value={accountMetrics ? fmtSignedMoney(accountMetrics.change) : "—"}
              valueClassName={pnlValueClass}
              sub={accountMetrics ? `(${fmtSignedPct(accountMetrics.changePct)})` : undefined}
            />
          </Col>
        </Row>
      )}

      <Tabs
        activeKey={detailTab}
        onChange={setDetailTab}
        destroyOnHidden={false}
        className="[&_.ant-tabs-nav]:!mb-4 [&_.ant-tabs-tab]:!text-[15px] [&_.ant-tabs-tab-btn]:!font-medium"
        tabBarExtraContent={
          detailTab === "config" && instance.mode !== "backtest" ? (
            <Button
              type="default"
              size="small"
              title="在弹窗中编辑完整 settings；保存时 ReAct 轮数与信号工具仍以表单为准覆盖同名字段。"
              onClick={() => configCardRef.current?.openSettingsJsonModal()}
            >
              编辑原始 JSON…
            </Button>
          ) : null
        }
        items={[
          ...(instance.mode === "backtest" && backtestTask
            ? [
                {
                  key: "overview",
                  label: "概览",
                  children: (
                    <BacktestOverviewPanel task={backtestTask} run={backtestRun} />
                  ),
                },
              ]
            : []),
          ...(instance.mode === "backtest"
            ? []
            : [
                {
                  key: "debug",
                  label: "调试",
                  children: (
                    <TaskDebugPanel
                      task={instance}
                      onDebugSessionCreated={() => setCycleRunsRefreshTrigger((n) => n + 1)}
                    />
                  ),
                },
              ]),
          {
            key: "cycle_runs",
            label: instance.mode === "backtest" ? "回测运行" : "周期运行",
            children: (
              <TaskCycleRunsPanel
                task={instance}
                refreshTrigger={cycleRunsRefreshTrigger}
                onBacktestRunSelected={instance.mode === "backtest" ? setSelectedBacktestRunId : undefined}
              />
            ),
          },
          ...(instance.mode === "backtest"
            ? []
            : [
                {
                  key: "triggers",
                  label: "调度与推送",
                  children: <TaskTriggersPanel task={instance} />,
                },
              ]),
          ...(instance.mode === "backtest"
            ? []
            : [
                {
                  key: "review",
                  label: "复盘",
                  children: (
                    <div className="flex flex-col gap-4">
                      <TaskReviewPanel rows={reviewRows} />
                      <KnowledgeJournalsPanel />
                    </div>
                  ),
                },
              ]),
          ...(instance.mode === "backtest"
            ? [
                {
                  key: "chart",
                  label: "回测图表",
                  children: (
                    <BacktestRunChartPanel taskId={instance.task_id} selectedRunId={selectedBacktestRunId} />
                  ),
                },
              ]
            : []),
          {
            key: "config",
            label: "配置",
            children: (
              instance.mode === "backtest" ? (
                <BacktestRunConfigPanel taskId={instance.task_id} selectedRunId={selectedBacktestRunId} />
              ) : (
                <CreateAgentCard
                  ref={configCardRef}
                  mode="edit"
                  editTask={instance}
                  hideCardTitle
                  settingsJsonButtonPlacement="none"
                  onCreated={() => void refresh()}
                />
              )
            ),
          },
        ]}
      />
    </Space>
  );
}
