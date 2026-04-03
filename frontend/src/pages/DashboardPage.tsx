import { Alert, Col, Row } from "antd";

import { MetricSummaryCard } from "../components/MetricSummaryCard";
import { PageIntro } from "../components/PageIntro";

type Props = {
  health: string;
  killSwitchEnabled: boolean;
  runningCount: number;
  errorCount: number;
  pendingApprovalCount: number;
  loading: boolean;
  dataRefreshFailed: boolean;
};

function formatHealth(status: string): string {
  if (status === "ok") return "正常";
  if (status === "unknown") return "未知";
  return "异常";
}

export function DashboardPage({
  health,
  killSwitchEnabled,
  runningCount,
  errorCount,
  pendingApprovalCount,
  loading,
  dataRefreshFailed,
}: Props) {
  return (
    <>
      <PageIntro title="Dashboard" description="查看平台运行总览、审批压力和系统健康状态。" />
      {loading ? (
        <Alert
          className="mb-4 rounded-2xl border border-shell-line"
          message="正在加载平台数据"
          description="首次加载完成前，下方数字不代表真实平台状态。"
          type="info"
          showIcon
        />
      ) : null}
      {dataRefreshFailed && !loading ? (
        <Alert
          className="mb-4 rounded-2xl border border-shell-line"
          message="数据刷新失败"
          description="最近一轮数据获取失败，指标与列表已清空，请检查 API 或网络后重试刷新。"
          type="warning"
          showIcon
        />
      ) : null}
      <Row gutter={[16, 16]}>
        <Col xs={24} md={12} xl={6}>
          <MetricSummaryCard title="平台健康状态" value={formatHealth(health)} loading={loading} />
        </Col>
        <Col xs={24} md={12} xl={6}>
          <MetricSummaryCard title="运行中实例" value={runningCount} loading={loading} />
        </Col>
        <Col xs={24} md={12} xl={6}>
          <MetricSummaryCard title="异常实例" value={errorCount} loading={loading} />
        </Col>
        <Col xs={24} md={12} xl={6}>
          <MetricSummaryCard title="待审批订单" value={pendingApprovalCount} loading={loading} />
        </Col>
      </Row>
      {killSwitchEnabled ? (
        <Alert
          className="mt-4 rounded-2xl border border-shell-line"
          message="熔断开关已开启"
          description="所有启动操作会被阻止，运行中的实例会在系统层停止。"
          type="error"
          showIcon
        />
      ) : null}
      {health !== "ok" && health !== "unknown" ? (
        <Alert
          className="mt-4 rounded-2xl border border-shell-line"
          message="后端不可达或健康检查异常"
          description="请检查 Tradeclaw API 服务和 API Base URL 配置。"
          type="warning"
          showIcon
        />
      ) : null}
    </>
  );
}
