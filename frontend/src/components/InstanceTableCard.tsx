import { Button, Card, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";

import { pauseInstance, startInstance, stopInstance } from "../api";
import type { InstanceStatus } from "../types";

type Props = {
  instances: InstanceStatus[];
  loading: boolean;
  onMutated: () => void;
};

const MODE_LABEL_MAP: Record<string, string> = {
  paper: "模拟盘",
  live: "实盘",
  backtest: "回测",
};

const STATUS_LABEL_MAP: Record<string, string> = {
  configured: "已配置",
  running: "运行中",
  paused: "已暂停",
  stopped: "已停止",
  error: "异常",
};

const PANEL_CARD_CLASSNAME = "!overflow-hidden !border !border-shell-line !bg-card-bg shadow-shell-card";
const SOFT_TAG_CLASSNAME = "!border-soft-tag-border !bg-soft-tag-bg !text-soft-tag-text";

function statusColor(status: string): string {
  if (status === "running") return "green";
  if (status === "paused") return "gold";
  if (status === "error") return "red";
  if (status === "stopped") return "default";
  return "blue";
}

function formatMode(mode: string): string {
  return MODE_LABEL_MAP[mode] ?? mode;
}

function formatStatus(status: string): string {
  return STATUS_LABEL_MAP[status] ?? status;
}

export function InstanceTableCard({ instances, loading, onMutated }: Props) {
  const columns: ColumnsType<InstanceStatus> = [
    {
      title: "实例",
      dataIndex: "name",
      key: "name",
      render: (value: string, record) => (
        <Space direction="vertical" size={1}>
          <Typography.Text strong>{value}</Typography.Text>
          <Typography.Text className="text-xs" type="secondary">
            {record.instance_id}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "模式",
      dataIndex: "mode",
      key: "mode",
      render: (value: string) => <Tag className={SOFT_TAG_CLASSNAME}>{formatMode(value)}</Tag>,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      render: (value: string) => <Tag color={statusColor(value)}>{formatStatus(value)}</Tag>,
    },
    {
      title: "轮次",
      dataIndex: "cycles",
      key: "cycles",
      align: "right",
      width: 96,
      render: (value: number | null) => value ?? "-",
    },
    {
      title: "操作",
      key: "actions",
      width: 260,
      render: (_, row) => (
        <Space>
          <Button
            className="rounded-xl"
            size="small"
            onClick={async () => {
              await startInstance(row.instance_id);
              onMutated();
            }}
          >
            启动
          </Button>
          <Button
            className="rounded-xl"
            size="small"
            onClick={async () => {
              await pauseInstance(row.instance_id);
              onMutated();
            }}
          >
            暂停
          </Button>
          <Button
            className="rounded-xl"
            size="small"
            danger
            onClick={async () => {
              await stopInstance(row.instance_id);
              onMutated();
            }}
          >
            停止
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <Card className={PANEL_CARD_CLASSNAME} title="实例列表">
      <Table
        rowKey="instance_id"
        loading={loading}
        columns={columns}
        dataSource={instances}
        pagination={false}
        scroll={{ x: 860 }}
      />
    </Card>
  );
}
