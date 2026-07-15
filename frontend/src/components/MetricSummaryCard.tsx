import { Card, Statistic } from "antd";

type Props = {
  title: string;
  value: string | number;
  loading?: boolean;
};

export function MetricSummaryCard({ title, value, loading = false }: Props) {
  return (
    <Card className="!border !border-shell-line !bg-card-bg shadow-shell-card" loading={loading}>
      <Statistic title={title} value={value} />
    </Card>
  );
}
