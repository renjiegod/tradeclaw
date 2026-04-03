import { Alert, Col, Row, Typography } from "antd";

import { MetricSummaryCard } from "../components/MetricSummaryCard";
import { PageIntro } from "../components/PageIntro";
import type { SystemState } from "../types";

type Props = {
  health: string;
  systemState: SystemState;
  loading: boolean;
  dataRefreshFailed: boolean;
};

function formatHealth(status: string): string {
  if (status === "ok") return "正常";
  if (status === "unknown") return "未知";
  return "异常";
}

export function SystemPage({ health, systemState, loading, dataRefreshFailed }: Props) {
  return (
    <>
      <PageIntro title="System" description="查看系统级运行状态、熔断开关和操作影响范围。" />
      {dataRefreshFailed && !loading ? (
        <Alert
          className="mb-4 rounded-2xl border border-shell-line"
          message="数据刷新失败"
          description="系统状态可能已过期或已清空，请检查 API 后重试刷新。"
          type="warning"
          showIcon
        />
      ) : null}
      <Row gutter={[16, 16]}>
        <Col xs={24} md={8}>
          <MetricSummaryCard title="后端健康状态" value={formatHealth(health)} loading={loading} />
        </Col>
        <Col xs={24} md={8}>
          <MetricSummaryCard title="实例总数" value={systemState.instance_count} loading={loading} />
        </Col>
        <Col xs={24} md={8}>
          <MetricSummaryCard title="运行中实例" value={systemState.running_count} loading={loading} />
        </Col>
      </Row>
      {!loading ? (
        <Alert
          className="mt-4 rounded-2xl border border-shell-line"
          message={systemState.kill_switch_enabled ? "熔断开关已开启" : "熔断开关未开启"}
          description={
            systemState.kill_switch_enabled
              ? "系统会阻止新的启动操作，并将平台切换到更保守的运行状态。"
              : "当前允许实例正常启动和单轮执行，请在高风险状态下谨慎开启熔断。"
          }
          type={systemState.kill_switch_enabled ? "error" : "info"}
          showIcon
        />
      ) : null}
      <Typography.Paragraph className="mt-4 text-shell-muted">
        System 页面只展示现有后端已支持的状态，不在本次迭代中引入新的系统 API。
      </Typography.Paragraph>
    </>
  );
}
