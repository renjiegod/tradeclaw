import { ReloadOutlined } from "@ant-design/icons";
import { Button, Card, Empty, Modal, Space, Spin, Tag, Typography, message } from "antd";
import { useCallback, useEffect, useState } from "react";

import {
  deleteTaskTrigger,
  listTaskTriggers,
  pauseTaskTrigger,
  resumeTaskTrigger,
  runTaskTrigger,
} from "../api";
import type {
  TaskStatus,
  TaskTrigger,
  TriggerDelivery,
  TriggerStatus,
} from "../types";
import { SOFT_TAG_CLASSNAME } from "../styles/classNames";
import { formatBacktestRange, formatDateTimeUtc8 } from "../utils/datetime";
import { TriggerFormModal } from "./TriggerFormModal";

const EMPTY_HINT = "还没有触发器";

type StatusMeta = { color: "green" | "default" | "red"; label: string };

const STATUS_META: Record<TriggerStatus, StatusMeta> = {
  active: { color: "green", label: "运行中" },
  paused: { color: "default", label: "已暂停" },
  exhausted: { color: "default", label: "已用尽" },
  error: { color: "red", label: "错误" },
};

function statusMeta(status: string): StatusMeta {
  return STATUS_META[status as TriggerStatus] ?? { color: "default", label: status };
}

/** Human-readable schedule summary line for a trigger card. */
function scheduleSummary(trigger: TaskTrigger): string {
  let base: string;
  switch (trigger.schedule_kind) {
    case "interval": {
      const secs = trigger.interval_seconds;
      if (secs == null) {
        base = "周期触发";
      } else if (secs % 60 === 0) {
        base = `每 ${secs / 60} 分钟`;
      } else {
        base = `每 ${secs} 秒`;
      }
      break;
    }
    case "cron": {
      const expr = trigger.cron_expression ?? "—";
      base = `${expr}（${trigger.timezone}）`;
      break;
    }
    case "at": {
      base = `单次 ${formatDateTimeUtc8(trigger.at_iso)}`;
      break;
    }
    case "backtest_range": {
      base = `回测 ${formatBacktestRange(trigger.range_start, trigger.range_end)}`;
      break;
    }
    default: {
      base = trigger.schedule_kind;
    }
  }
  if (trigger.trading_session) {
    base += ` · ${trigger.trading_session}时段`;
  }
  return base;
}

/** Human-readable delivery summary line for a trigger card. */
function deliverySummary(delivery: TriggerDelivery | null): string {
  if (!delivery || delivery.mode === "none") {
    return "不推送";
  }
  const head = delivery.mode === "card" ? "📇 卡片" : "📝 文字";
  const target = delivery.target;
  let dest = "";
  if (target) {
    if (target.channel_id) {
      dest = target.channel_id;
    } else if (target.origin || target.session_id) {
      dest = "当前会话";
    }
  }
  return dest ? `${head} → ${dest}` : head;
}

type TriggerCardProps = {
  trigger: TaskTrigger;
  busy: boolean;
  onEdit: (trigger: TaskTrigger) => void;
  onToggle: (trigger: TaskTrigger) => void;
  onRun: (trigger: TaskTrigger) => void;
  onDelete: (trigger: TaskTrigger) => void;
};

function TriggerCard({ trigger, busy, onEdit, onToggle, onRun, onDelete }: TriggerCardProps) {
  const status = statusMeta(trigger.status);
  // paused → resume action; everything else offers pause.
  const toggleLabel = trigger.status === "paused" ? "恢复" : "暂停";
  return (
    <Card
      size="small"
      className="rounded-xl !border !border-shell-line !bg-card-bg shadow-shell-card"
      data-testid="task-trigger-card"
    >
      <Space direction="vertical" size={6} className="w-full">
        <Space align="center" wrap size={8}>
          <Typography.Text strong>{trigger.name}</Typography.Text>
          <Tag color={trigger.execution_intent === "trade" ? "blue" : "default"}>
            {trigger.execution_intent === "trade" ? "交易" : "仅信号"}
          </Tag>
          <Tag color={status.color}>{status.label}</Tag>
          {!trigger.enabled ? <Tag className={SOFT_TAG_CLASSNAME}>已禁用</Tag> : null}
        </Space>
        <Typography.Text type="secondary" className="!text-xs">
          {scheduleSummary(trigger)}
        </Typography.Text>
        <Typography.Text type="secondary" className="!text-xs">
          {deliverySummary(trigger.delivery_json)}
        </Typography.Text>
        <Typography.Text type="secondary" className="!text-xs">
          下次 {formatDateTimeUtc8(trigger.next_fire_at)} · 上次 {formatDateTimeUtc8(trigger.last_fired_at)}
        </Typography.Text>
        {trigger.last_error ? (
          <Typography.Text className="!text-xs" type="danger">
            {trigger.last_error}
          </Typography.Text>
        ) : null}
        <Space size={8} wrap>
          <Button size="small" disabled={busy} onClick={() => onEdit(trigger)}>
            编辑
          </Button>
          <Button size="small" disabled={busy} onClick={() => onToggle(trigger)}>
            {toggleLabel}
          </Button>
          <Button size="small" disabled={busy} onClick={() => onRun(trigger)}>
            立即运行
          </Button>
          <Button size="small" danger disabled={busy} onClick={() => onDelete(trigger)}>
            删除
          </Button>
        </Space>
      </Space>
    </Card>
  );
}

type TaskTriggersPanelProps = {
  task: TaskStatus;
};

/**
 * 任务详情页「调度与推送」Tab：以卡片流渲染该任务下的子触发器（schedule +
 * execution intent + delivery），并提供新建 / 编辑 / 暂停·恢复 / 立即运行 /
 * 删除 操作。
 */
export function TaskTriggersPanel({ task }: TaskTriggersPanelProps) {
  const taskId = task.task_id;
  const [triggers, setTriggers] = useState<TaskTrigger[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  // editing === undefined → modal closed; null → create; TaskTrigger → edit.
  const [editing, setEditing] = useState<TaskTrigger | null | undefined>(undefined);

  const loadTriggers = useCallback(async () => {
    setLoading(true);
    try {
      const items = await listTaskTriggers(taskId);
      setTriggers(items);
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  const reload = useCallback(() => {
    void loadTriggers().catch((error: unknown) => {
      const detail = error instanceof Error ? error.message : String(error);
      message.error(`加载触发器失败：${detail}`);
    });
  }, [loadTriggers]);

  useEffect(() => {
    reload();
  }, [reload]);

  const handleSaved = useCallback(() => {
    setEditing(undefined);
    reload();
  }, [reload]);

  const handleToggle = useCallback(
    (trigger: TaskTrigger) => {
      setBusyId(trigger.id);
      const action = trigger.status === "paused" ? resumeTaskTrigger : pauseTaskTrigger;
      void action(taskId, trigger.id)
        .then(() => loadTriggers())
        .catch((error: unknown) => {
          const detail = error instanceof Error ? error.message : String(error);
          message.error(`操作失败：${detail}`);
        })
        .finally(() => setBusyId(null));
    },
    [loadTriggers, taskId],
  );

  const handleRun = useCallback(
    (trigger: TaskTrigger) => {
      setBusyId(trigger.id);
      void runTaskTrigger(taskId, trigger.id)
        .then((result) => {
          if (result.run_id) {
            message.success(`已触发 run_id=${result.run_id}`);
          } else {
            message.info("已触发（本次无 run_id）");
          }
          return loadTriggers();
        })
        .catch((error: unknown) => {
          const detail = error instanceof Error ? error.message : String(error);
          message.error(`触发失败：${detail}`);
        })
        .finally(() => setBusyId(null));
    },
    [loadTriggers, taskId],
  );

  const handleDelete = useCallback(
    (trigger: TaskTrigger) => {
      Modal.confirm({
        title: "删除触发器",
        content: `确认删除「${trigger.name}」？此操作不可撤销。`,
        okText: "删除",
        okButtonProps: { danger: true },
        cancelText: "取消",
        onOk: async () => {
          setBusyId(trigger.id);
          try {
            await deleteTaskTrigger(taskId, trigger.id);
            message.success("触发器已删除");
            await loadTriggers();
          } catch (error: unknown) {
            const detail = error instanceof Error ? error.message : String(error);
            message.error(`删除失败：${detail}`);
          } finally {
            setBusyId(null);
          }
        },
      });
    },
    [loadTriggers, taskId],
  );

  return (
    <Card
      className="!border !border-shell-line !bg-card-bg shadow-shell-card"
      title={
        <div className="flex flex-col">
          <Typography.Text strong>调度与推送</Typography.Text>
          <Typography.Text type="secondary" className="!text-xs !font-normal">
            该任务下的触发器（调度 + 执行意图 + 推送）。
          </Typography.Text>
        </div>
      }
      extra={
        <Space size={8}>
          <Button
            size="small"
            type="primary"
            onClick={() => setEditing(null)}
            data-testid="new-trigger-button"
          >
            + 新建触发器
          </Button>
          <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={reload}>
            刷新
          </Button>
        </Space>
      }
      data-testid="task-triggers-panel"
    >
      {loading ? (
        <div className="flex min-h-[200px] items-center justify-center">
          <Spin />
        </div>
      ) : triggers.length === 0 ? (
        <Empty description={EMPTY_HINT} image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <Space direction="vertical" size={12} className="w-full">
          {triggers.map((trigger) => (
            <TriggerCard
              key={trigger.id}
              trigger={trigger}
              busy={busyId === trigger.id}
              onEdit={setEditing}
              onToggle={handleToggle}
              onRun={handleRun}
              onDelete={handleDelete}
            />
          ))}
        </Space>
      )}

      {editing !== undefined && (
        <TriggerFormModal
          taskId={taskId}
          trigger={editing ?? undefined}
          onSaved={handleSaved}
          onClose={() => setEditing(undefined)}
        />
      )}
    </Card>
  );
}
