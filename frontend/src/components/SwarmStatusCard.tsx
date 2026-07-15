import { Card, List, Space, Tag, Typography } from "antd";

import { PANEL_CARD_CLASSNAME } from "../styles/classNames";
import type { SwarmTaskView, SwarmWorkerStatus } from "../types";

const STATUS_META: Record<SwarmWorkerStatus, { label: string; color: string }> = {
  pending: { label: "等待中", color: "default" },
  blocked: { label: "阻塞", color: "warning" },
  in_progress: { label: "运行中", color: "processing" },
  completed: { label: "已完成", color: "success" },
  failed: { label: "失败", color: "error" },
  cancelled: { label: "已取消", color: "default" },
};

type Props = {
  /** 任务静态信息（来自 run 详情）。 */
  tasks: SwarmTaskView[];
  /** 任务 id → 实时状态（来自 SSE 流，覆盖 tasks 里的状态）。 */
  workerStatus?: Record<string, SwarmWorkerStatus>;
  loading?: boolean;
};

/** 实时展示一个 swarm run 中各 worker（任务）的状态卡。 */
export function SwarmStatusCard({ tasks, workerStatus = {}, loading = false }: Props) {
  return (
    <Card className={PANEL_CARD_CLASSNAME} title="Swarm 实时状态" loading={loading}>
      <List
        dataSource={tasks}
        locale={{ emptyText: "暂无任务" }}
        renderItem={(task) => {
          const status = workerStatus[task.task_id] ?? task.status;
          const meta = STATUS_META[status] ?? STATUS_META.pending;
          return (
            <List.Item>
              <Space direction="vertical" size={4} style={{ width: "100%" }}>
                <Space wrap>
                  <Typography.Text strong>{task.agent_id}</Typography.Text>
                  <Tag color={meta.color}>{meta.label}</Tag>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {task.task_id}
                  </Typography.Text>
                  {task.depends_on.length > 0 ? (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      依赖：{task.depends_on.join(", ")}
                    </Typography.Text>
                  ) : null}
                </Space>
                {task.error ? (
                  <Typography.Text type="danger" style={{ fontSize: 12 }}>
                    {task.error}
                  </Typography.Text>
                ) : task.summary ? (
                  <Typography.Paragraph
                    type="secondary"
                    ellipsis={{ rows: 2 }}
                    style={{ marginBottom: 0, fontSize: 12 }}
                  >
                    {task.summary}
                  </Typography.Paragraph>
                ) : null}
              </Space>
            </List.Item>
          );
        }}
      />
    </Card>
  );
}
