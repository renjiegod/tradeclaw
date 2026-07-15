import { CopyOutlined, WarningOutlined } from "@ant-design/icons";
import { Button, Card, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo } from "react";
import { useNavigate } from "react-router-dom";

import { useSymbolNames } from "../hooks/useSymbolNames";
import type { TaskStatus } from "../types";
import { formatDateTimeUtc8 } from "../utils/datetime";
import { formatStatus, resolveTaskDisplayStatus, statusColor } from "../utils/taskStatus";
import {
  PANEL_CARD_CLASSNAME,
  SOFT_TAG_CLASSNAME,
  formatMode,
  formatUniverse,
  readDefinitionId,
  sortByCreatedDesc,
  universeSymbolsOf,
} from "./taskTableShared";
import type { TriggerSummary } from "./taskTableShared";

type Props = {
  tasks: TaskStatus[];
  loading: boolean;
  onDuplicate?: (task: TaskStatus) => void;
  onDelete?: (task: TaskStatus) => void;
  onBulkDelete?: () => void;
  /** Per-task trigger rollup; ``undefined`` while the summary is still loading. */
  triggerSummaryByTaskId?: Record<string, TriggerSummary | undefined>;
  selectedTaskIds?: string[];
  onSelectedTaskIdsChange?: (taskIds: string[]) => void;
  pagination?: {
    current: number;
    pageSize: number;
    total: number;
    onChange: (page: number, pageSize: number) => void;
  };
};

function TriggerCell({
  summary,
  taskStatus,
}: {
  summary: TriggerSummary | undefined;
  taskStatus: string;
}) {
  if (summary == null) {
    return <Typography.Text type="secondary">…</Typography.Text>;
  }
  if (summary.total === 0) {
    // A started task does nothing until it owns a trigger — surface that loudly
    // for running tasks, quietly otherwise.
    const running = taskStatus === "running";
    return (
      <Tooltip title="该任务没有触发器，不会自动交易或推送信号。进入详情页「调度与推送」添加触发器。">
        <Tag icon={<WarningOutlined />} color={running ? "warning" : "default"}>
          无触发器
        </Tag>
      </Tooltip>
    );
  }
  const countLabel = summary.active < summary.total
    ? `${summary.active}/${summary.total} 启用`
    : `${summary.total} 个`;
  return (
    <Space direction="vertical" size={1}>
      <Tag className={SOFT_TAG_CLASSNAME}>{countLabel}</Tag>
      {summary.nextFireAt ? (
        <Typography.Text className="text-xs" type="secondary">
          下次 {formatDateTimeUtc8(summary.nextFireAt, "—")}
        </Typography.Text>
      ) : null}
    </Space>
  );
}

/** Live / paper / signal-only task list. Columns lean into ongoing operation —
 * mode, strategy, the task's triggers and how soon they fire next, cycle count —
 * rather than the finished-run metrics the backtest list shows. */
export function TradingTaskTable({
  tasks,
  loading,
  onDuplicate,
  onDelete,
  onBulkDelete,
  triggerSummaryByTaskId,
  selectedTaskIds,
  onSelectedTaskIdsChange,
  pagination,
}: Props) {
  const navigate = useNavigate();
  const sortedTasks = useMemo(() => sortByCreatedDesc(tasks), [tasks]);
  const symbolNames = useSymbolNames(useMemo(() => universeSymbolsOf(tasks), [tasks]));

  const columns: ColumnsType<TaskStatus> = [
    {
      title: "任务",
      dataIndex: "name",
      key: "name",
      render: (value: string, record) => (
        <Space direction="vertical" size={1}>
          <Typography.Text strong>{value}</Typography.Text>
          <Typography.Text className="text-xs" type="secondary">
            {record.task_id}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "模式",
      dataIndex: "mode",
      key: "mode",
      width: 96,
      render: (value: string) => <Tag className={SOFT_TAG_CLASSNAME}>{formatMode(value)}</Tag>,
    },
    {
      title: "策略",
      key: "strategy",
      width: 200,
      render: (_: unknown, record) => {
        const definitionId = readDefinitionId(record.settings);
        return (
          <Space direction="vertical" size={1}>
            {record.strategy_name ? (
              <Typography.Text>{record.strategy_name}</Typography.Text>
            ) : (
              <Typography.Text type="secondary">—</Typography.Text>
            )}
            {definitionId ? (
              <Typography.Text className="text-xs" type="secondary" copyable={{ text: definitionId }}>
                {definitionId}
              </Typography.Text>
            ) : null}
          </Space>
        );
      },
    },
    {
      title: "标的",
      dataIndex: "universe",
      key: "universe",
      width: 160,
      render: (universe: string[]) => formatUniverse(universe, symbolNames),
    },
    {
      title: "触发器",
      key: "triggers",
      width: 160,
      render: (_: unknown, record) => (
        <TriggerCell
          summary={triggerSummaryByTaskId?.[record.task_id]}
          taskStatus={record.status}
        />
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (_value: string, record) => {
        const displayStatus = resolveTaskDisplayStatus(record);
        return <Tag color={statusColor(displayStatus)}>{formatStatus(displayStatus)}</Tag>;
      },
    },
    {
      title: "轮次",
      dataIndex: "cycles",
      key: "cycles",
      align: "right",
      width: 88,
      render: (value: number | null) => value ?? "-",
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (value: string) => formatDateTimeUtc8(value, "—"),
    },
    {
      title: "操作",
      key: "actions",
      width: 180,
      render: (_: unknown, record) => (
        <Space size={8}>
          <Button
            size="small"
            icon={<CopyOutlined />}
            onClick={(event) => {
              event.stopPropagation();
              onDuplicate?.(record);
            }}
          >
            复制
          </Button>
          <Button
            size="small"
            danger
            disabled={record.status === "running"}
            onClick={(event) => {
              event.stopPropagation();
              onDelete?.(record);
            }}
          >
            删除
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <Card
      className={PANEL_CARD_CLASSNAME}
      title="交易任务"
      extra={
        <Button danger disabled={!selectedTaskIds?.length} onClick={() => onBulkDelete?.()}>
          删除选中
        </Button>
      }
    >
      <Table<TaskStatus>
        rowKey="task_id"
        loading={loading}
        columns={columns}
        dataSource={sortedTasks}
        rowSelection={{
          selectedRowKeys: selectedTaskIds,
          onChange: (keys) => onSelectedTaskIdsChange?.(keys.map((key) => String(key))),
          getCheckboxProps: (record) => ({
            disabled: record.status === "running",
          }),
        }}
        pagination={
          pagination
            ? {
                current: pagination.current,
                pageSize: pagination.pageSize,
                total: pagination.total,
                showSizeChanger: true,
                showQuickJumper: true,
                onChange: pagination.onChange,
              }
            : false
        }
        scroll={{ x: 1320 }}
        onRow={(record) => ({
          onClick: () => {
            navigate(`/tasks/${encodeURIComponent(record.task_id)}`);
          },
          style: { cursor: "pointer" },
        })}
      />
    </Card>
  );
}
