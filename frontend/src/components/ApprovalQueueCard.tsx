import { Button, Card, Empty, List, Space, Typography } from "antd";

import { approve, reject } from "../api";
import type { PendingApproval } from "../types";

type Props = {
  items: PendingApproval[];
  loading: boolean;
  onMutated: () => void;
};

const PANEL_CARD_CLASSNAME = "!overflow-hidden !border !border-shell-line !bg-card-bg shadow-shell-card";

function formatTs(raw: string): string {
  const date = new Date(raw);
  return Number.isNaN(date.getTime()) ? raw : date.toLocaleString("zh-CN");
}

export function ApprovalQueueCard({ items, loading, onMutated }: Props) {
  return (
    <Card className={PANEL_CARD_CLASSNAME} title="待处理审批" loading={loading}>
      {items.length === 0 ? (
        <Empty description="暂无待审批请求" />
      ) : (
        <List
          itemLayout="vertical"
          dataSource={items}
          renderItem={(item) => (
            <List.Item
              key={item.approval_id}
              actions={[
                <Button
                  key="approve"
                  className="rounded-xl"
                  type="primary"
                  size="small"
                  onClick={async () => {
                    await approve(item.approval_id);
                    onMutated();
                  }}
                >
                  同意
                </Button>,
                <Button
                  key="reject"
                  className="rounded-xl"
                  danger
                  size="small"
                  onClick={async () => {
                    await reject(item.approval_id);
                    onMutated();
                  }}
                >
                  拒绝
                </Button>,
              ]}
            >
              <Space direction="vertical" size={3}>
                <Typography.Text strong>意图: {item.intent_id}</Typography.Text>
                <Typography.Text className="text-xs" type="secondary">
                  请求ID: {item.approval_id}
                </Typography.Text>
                <Typography.Text className="text-xs" type="secondary">
                  创建时间: {formatTs(item.created_at)} | 过期时间: {formatTs(item.expires_at)}
                </Typography.Text>
              </Space>
            </List.Item>
          )}
        />
      )}
    </Card>
  );
}
