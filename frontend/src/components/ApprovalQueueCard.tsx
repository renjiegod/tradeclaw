import { Button, Card, Empty, List, Space, Typography } from "antd";

import { approve, reject } from "../api";
import type { PendingApproval } from "../types";

type Props = {
  items: PendingApproval[];
  loading: boolean;
  onMutated: () => void;
};

function formatTs(raw: string): string {
  const date = new Date(raw);
  return Number.isNaN(date.getTime()) ? raw : date.toLocaleString();
}

export function ApprovalQueueCard({ items, loading, onMutated }: Props) {
  return (
    <Card className="panel-card" title="Pending Approvals" loading={loading}>
      {items.length === 0 ? (
        <Empty description="No pending approvals" />
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
                  type="primary"
                  size="small"
                  onClick={async () => {
                    await approve(item.approval_id);
                    onMutated();
                  }}
                >
                  Approve
                </Button>,
                <Button
                  key="reject"
                  danger
                  size="small"
                  onClick={async () => {
                    await reject(item.approval_id);
                    onMutated();
                  }}
                >
                  Reject
                </Button>,
              ]}
            >
              <Space direction="vertical" size={3}>
                <Typography.Text strong>Intent: {item.intent_id}</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  Request: {item.approval_id}
                </Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  Created: {formatTs(item.created_at)} | Expires: {formatTs(item.expires_at)}
                </Typography.Text>
              </Space>
            </List.Item>
          )}
        />
      )}
    </Card>
  );
}
