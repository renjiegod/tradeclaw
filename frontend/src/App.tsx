import { Alert, Button, Col, ConfigProvider, Layout, Row, Statistic, Typography, message } from "antd";
import { RadarChartOutlined, ReloadOutlined } from "@ant-design/icons";
import { useCallback, useEffect, useMemo, useState } from "react";
import zhCN from "antd/locale/zh_CN";

import {
  getHealth,
  getSystemState,
  listInstances,
  listPendingApprovals,
  setKillSwitch,
  tickOnce,
} from "./api";
import { ApprovalQueueCard } from "./components/ApprovalQueueCard";
import { CreateAgentCard } from "./components/CreateAgentCard";
import { InstanceTableCard } from "./components/InstanceTableCard";
import type { InstanceStatus, PendingApproval } from "./types";

const REFRESH_INTERVAL_MS = 8000;

function formatHealth(status: string): string {
  if (status === "ok") {
    return "正常";
  }
  if (status === "unknown") {
    return "未知";
  }
  return "异常";
}

function usePlatformData() {
  const [instances, setInstances] = useState<InstanceStatus[]>([]);
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [health, setHealth] = useState<string>("unknown");
  const [killSwitchEnabled, setKillSwitchEnabled] = useState(false);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [healthResult, instancesResult, approvalsResult, systemState] = await Promise.all([
        getHealth(),
        listInstances(),
        listPendingApprovals(),
        getSystemState(),
      ]);
      setHealth(healthResult.status);
      setInstances(instancesResult);
      setApprovals(approvalsResult);
      setKillSwitchEnabled(systemState.kill_switch_enabled);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let alive = true;

    refresh().catch((error: unknown) => {
      if (!alive) {
        return;
      }
      const content = error instanceof Error ? error.message : String(error);
      message.error(`加载平台数据失败：${content}`);
    });

    const timer = window.setInterval(() => {
      refresh().catch(() => undefined);
    }, REFRESH_INTERVAL_MS);

    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [refresh]);

  return {
    instances,
    approvals,
    health,
    killSwitchEnabled,
    loading,
    refresh,
    setKillSwitchEnabled,
  };
}

export default function App() {
  const { instances, approvals, health, killSwitchEnabled, loading, refresh, setKillSwitchEnabled } =
    usePlatformData();

  const runningCount = useMemo(
    () => instances.filter((item) => item.status === "running").length,
    [instances],
  );

  const errorCount = useMemo(
    () => instances.filter((item) => item.status === "error").length,
    [instances],
  );

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#c98536",
          borderRadius: 14,
          fontFamily: "'IBM Plex Sans', sans-serif",
          colorBgLayout: "#f4efe6",
        },
      }}
    >
      <Layout className="min-h-screen bg-transparent">
        <Layout.Header className="!h-auto !bg-transparent !px-0 !py-0 !leading-normal">
          <div className="sticky top-0 z-40 flex flex-wrap items-center justify-between gap-3 border-b border-shell-line bg-[rgba(250,245,236,0.88)] px-3.5 py-3 backdrop-blur md:h-[68px] md:flex-nowrap md:px-5 md:py-0">
            <div className="flex items-center gap-2.5">
              <RadarChartOutlined className="text-lg text-shell-accent" />
              <Typography.Title level={4} className="!m-0 !font-display !tracking-[0.02em] !text-shell-ink">
                Tradeclaw 控制台
              </Typography.Title>
            </div>
            <div className="flex flex-wrap items-center justify-end gap-2">
              <Button
                className="rounded-xl"
                onClick={async () => {
                  const state = await setKillSwitch(!killSwitchEnabled);
                  setKillSwitchEnabled(state.kill_switch_enabled);
                  await refresh();
                }}
                danger={!killSwitchEnabled}
                type={killSwitchEnabled ? "default" : "primary"}
              >
                {killSwitchEnabled ? "关闭熔断开关" : "开启熔断开关"}
              </Button>
              <Button
                className="rounded-xl"
                onClick={async () => {
                  await tickOnce();
                  await refresh();
                }}
              >
                执行一轮
              </Button>
              <Button className="rounded-xl" icon={<ReloadOutlined />} onClick={() => void refresh()}>
                刷新
              </Button>
            </div>
          </div>
        </Layout.Header>

        <Layout.Content className="px-3.5 py-4 md:px-5 md:py-5">
          <Row gutter={[16, 16]}>
            <Col xs={24} md={8}>
              <div className="rounded-2xl border border-shell-line bg-card-bg px-5 py-4 shadow-shell-card">
                <Statistic title="平台健康状态" value={formatHealth(health)} />
              </div>
            </Col>
            <Col xs={24} md={8}>
              <div className="rounded-2xl border border-shell-line bg-card-bg px-5 py-4 shadow-shell-card">
                <Statistic title="运行中实例" value={runningCount} />
              </div>
            </Col>
            <Col xs={24} md={8}>
              <div className="rounded-2xl border border-shell-line bg-card-bg px-5 py-4 shadow-shell-card">
                <Statistic title="异常实例" value={errorCount} />
              </div>
            </Col>
          </Row>

          {killSwitchEnabled && (
            <Alert
              className="my-4 rounded-2xl border border-shell-line"
              message="熔断开关已开启"
              description="所有启动操作已被阻止，运行中的实例会被停止。"
              type="error"
              showIcon
            />
          )}

          {health !== "ok" && (
            <Alert
              className="my-4 rounded-2xl border border-shell-line"
              message="后端不可达或健康检查异常"
              description="请检查 Tradeclaw API 服务和 API Base URL 配置。"
              type="warning"
              showIcon
            />
          )}

          <Row gutter={[16, 16]}>
            <Col xs={24} xl={16}>
              <InstanceTableCard instances={instances} loading={loading} onMutated={() => void refresh()} />
            </Col>
            <Col xs={24} xl={8}>
              <div className="flex w-full flex-col gap-4">
                <CreateAgentCard onCreated={() => void refresh()} />
                <ApprovalQueueCard items={approvals} loading={loading} onMutated={() => void refresh()} />
              </div>
            </Col>
          </Row>
        </Layout.Content>
      </Layout>
    </ConfigProvider>
  );
}
