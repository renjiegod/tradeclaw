import { Button, Card, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";

import { pauseInstance, startInstance, stopInstance } from "../api";
import type { InstanceStatus } from "../types";

type Props = {
  instances: InstanceStatus[];
  loading: boolean;
  onMutated: () => void;
};

function statusColor(status: string): string {
  if (status === "running") return "green";
  if (status === "paused") return "gold";
  if (status === "error") return "red";
  if (status === "stopped") return "default";
  return "blue";
}

export function InstanceTableCard({ instances, loading, onMutated }: Props) {
  const columns: ColumnsType<InstanceStatus> = [
    {
      title: "Agent",
      dataIndex: "name",
      key: "name",
      render: (value: string, record) => (
        <Space direction="vertical" size={1}>
          <Typography.Text strong>{value}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {record.instance_id}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: "Mode",
      dataIndex: "mode",
      key: "mode",
      render: (value: string) => <Tag className="soft-tag">{value}</Tag>,
    },
    {
      title: "Status",
      dataIndex: "status",
      key: "status",
      render: (value: string) => <Tag color={statusColor(value)}>{value}</Tag>,
    },
    {
      title: "Cycles",
      dataIndex: "cycles",
      key: "cycles",
      align: "right",
      width: 96,
      render: (value: number | null) => value ?? "-",
    },
    {
      title: "Actions",
      key: "actions",
      width: 260,
      render: (_, row) => (
        <Space>
          <Button
            size="small"
            onClick={async () => {
              await startInstance(row.instance_id);
              onMutated();
            }}
          >
            Start
          </Button>
          <Button
            size="small"
            onClick={async () => {
              await pauseInstance(row.instance_id);
              onMutated();
            }}
          >
            Pause
          </Button>
          <Button
            size="small"
            danger
            onClick={async () => {
              await stopInstance(row.instance_id);
              onMutated();
            }}
          >
            Stop
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <Card className="panel-card" title="Agent Instances">
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
