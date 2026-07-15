import { CopyOutlined } from "@ant-design/icons";
import { Button, Card, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo } from "react";
import { useNavigate } from "react-router-dom";

import { useSymbolNames } from "../hooks/useSymbolNames";
import type { TaskStatus } from "../types";
import { formatBacktestRange, formatDateTimeUtc8 } from "../utils/datetime";
import { formatStatus, resolveTaskDisplayStatus, statusColor } from "../utils/taskStatus";
import {
  PANEL_CARD_CLASSNAME,
  formatSignedPct,
  formatUniverse,
  readDefinitionId,
  returnPctColor,
  sortByCreatedDesc,
  universeSymbolsOf,
} from "./taskTableShared";

type Props = {
  tasks: TaskStatus[];
  loading: boolean;
  onDuplicate?: (task: TaskStatus) => void;
  onDelete?: (task: TaskStatus) => void;
  onBulkDelete?: () => void;
  latestRunStatusByTaskId?: Record<string, string | undefined>;
  selectedTaskIds?: string[];
  onSelectedTaskIdsChange?: (taskIds: string[]) => void;
  pagination?: {
    current: number;
    pageSize: number;
    total: number;
    onChange: (page: number, pageSize: number) => void;
  };
};

/** Backtest-specific task list. Columns lean into a finished run's report —
 * range, return, drawdown, trade count — rather than the live-trading concerns
 * (triggers, cycles) that the trading list surfaces. */
export function BacktestTaskTable({
  tasks,
  loading,
  onDuplicate,
  onDelete,
  onBulkDelete,
  latestRunStatusByTaskId,
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
      title: "回测股票",
      dataIndex: "universe",
      key: "universe",
      width: 160,
      render: (universe: string[]) => formatUniverse(universe, symbolNames),
    },
    {
      title: "区间",
      key: "range",
      width: 200,
      render: (_: unknown, record) =>
        formatBacktestRange(
          record.backtest_summary?.range_start_utc,
          record.backtest_summary?.range_end_utc,
        ),
    },
    {
      title: "收益率",
      key: "return_pct",
      align: "right",
      width: 110,
      render: (_: unknown, record) => {
        const value = record.backtest_summary?.return_pct ?? null;
        if (value == null) return <Typography.Text type="secondary">—</Typography.Text>;
        return (
          <Typography.Text style={{ color: returnPctColor(value) }}>
            {formatSignedPct(value)}
          </Typography.Text>
        );
      },
    },
    {
      title: "最大回撤",
      key: "max_drawdown_pct",
      align: "right",
      width: 110,
      render: (_: unknown, record) => {
        const value = record.backtest_summary?.max_drawdown_pct ?? null;
        if (value == null) return <Typography.Text type="secondary">—</Typography.Text>;
        const n = Number(value);
        return (
          <Typography.Text type={Number.isFinite(n) && n > 0 ? undefined : "secondary"}>
            {Number.isFinite(n) ? `-${Math.abs(n).toFixed(2)}%` : "—"}
          </Typography.Text>
        );
      },
    },
    {
      title: "交易次数",
      key: "fills_count",
      align: "right",
      width: 96,
      render: (_: unknown, record) => {
        const value = record.backtest_summary?.fills_count;
        return value == null ? (
          <Typography.Text type="secondary">—</Typography.Text>
        ) : (
          <Typography.Text>{value}</Typography.Text>
        );
      },
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (_value: string, record) => {
        const displayStatus = resolveTaskDisplayStatus(record, latestRunStatusByTaskId);
        return <Tag color={statusColor(displayStatus)}>{formatStatus(displayStatus)}</Tag>;
      },
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
      title="回测任务"
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
        scroll={{ x: 1480 }}
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
