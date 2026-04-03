import { Card, Empty, Typography } from "antd";

type Props = {
  title: string;
  description: string;
};

export function EmptyStateCard({ title, description }: Props) {
  return (
    <Card className="!border !border-shell-line !bg-card-bg shadow-shell-card">
      <Empty description={title} />
      <Typography.Paragraph className="!mb-0 text-center text-shell-muted">
        {description}
      </Typography.Paragraph>
    </Card>
  );
}
