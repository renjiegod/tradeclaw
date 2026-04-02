import { Alert, Button, Col, ConfigProvider, Layout, Row, Space, Statistic, Typography, message } from "antd";
import { RadarChartOutlined, ReloadOutlined } from "@ant-design/icons";
import { useCallback, useEffect, useMemo, useState } from "react";

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

import "./styles.css";

const REFRESH_INTERVAL_MS = 8000;

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
      message.error(`Failed to load platform data: ${content}`);
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
      theme={{
        token: {
          colorPrimary: "#c98536",
          borderRadius: 14,
          fontFamily: "'IBM Plex Sans', sans-serif",
          colorBgLayout: "#f4efe6",
        },
      }}
    >
      <Layout className="app-root">
        <Layout.Header className="app-header">
          <Space align="center" size={10}>
            <RadarChartOutlined style={{ color: "#c98536", fontSize: 18 }} />
            <Typography.Title level={4} className="header-title">
              Tradeclaw Control Room
            </Typography.Title>
          </Space>
          <Space>
            <Button
              onClick={async () => {
                const state = await setKillSwitch(!killSwitchEnabled);
                setKillSwitchEnabled(state.kill_switch_enabled);
                await refresh();
              }}
              danger={!killSwitchEnabled}
              type={killSwitchEnabled ? "default" : "primary"}
            >
              {killSwitchEnabled ? "Disable Kill" : "Enable Kill"}
            </Button>
            <Button
              onClick={async () => {
                await tickOnce();
                await refresh();
              }}
            >
              Tick
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => void refresh()}>
              Refresh
            </Button>
          </Space>
        </Layout.Header>

        <Layout.Content className="app-content">
          <Row gutter={[16, 16]}>
            <Col xs={24} md={8}>
              <Statistic title="Platform Health" value={health} />
            </Col>
            <Col xs={24} md={8}>
              <Statistic title="Running Agents" value={runningCount} />
            </Col>
            <Col xs={24} md={8}>
              <Statistic title="Error Agents" value={errorCount} />
            </Col>
          </Row>

          {killSwitchEnabled && (
            <Alert
              className="status-alert"
              message="Kill switch is currently enabled"
              description="All start operations are blocked and running instances are stopped."
              type="error"
              showIcon
            />
          )}

          {health !== "ok" && (
            <Alert
              className="status-alert"
              message="Backend unreachable or unhealthy"
              description="Check Tradeclaw API service and API base URL"
              type="warning"
              showIcon
            />
          )}

          <Row gutter={[16, 16]}>
            <Col xs={24} xl={16}>
              <InstanceTableCard instances={instances} loading={loading} onMutated={() => void refresh()} />
            </Col>
            <Col xs={24} xl={8}>
              <Space direction="vertical" size={16} style={{ width: "100%" }}>
                <CreateAgentCard onCreated={() => void refresh()} />
                <ApprovalQueueCard items={approvals} loading={loading} onMutated={() => void refresh()} />
              </Space>
            </Col>
          </Row>
        </Layout.Content>
      </Layout>
    </ConfigProvider>
  );
}
