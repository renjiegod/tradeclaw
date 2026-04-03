import { RadarChartOutlined, ReloadOutlined } from "@ant-design/icons";
import { Alert, Button, ConfigProvider, Layout, Menu, message } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";
import zhCN from "antd/locale/zh_CN";

import { getHealth, getSystemState, listInstances, listPendingApprovals, setKillSwitch, tickOnce } from "./api";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { BacktestsPage } from "./pages/BacktestsPage";
import { CreateAgentPage } from "./pages/CreateAgentPage";
import { DashboardPage } from "./pages/DashboardPage";
import { InstancesPage } from "./pages/InstancesPage";
import { SystemPage } from "./pages/SystemPage";
import type { ConsolePageKey, InstanceStatus, PendingApproval, SystemState } from "./types";

const REFRESH_INTERVAL_MS = 8000;

const EMPTY_SYSTEM_STATE: SystemState = {
  kill_switch_enabled: false,
  instance_count: 0,
  running_count: 0,
};

const NAV_ITEMS: Array<{ key: ConsolePageKey; label: string }> = [
  { key: "dashboard", label: "Dashboard" },
  { key: "instances", label: "Agent Instances" },
  { key: "create-agent", label: "Create Agent" },
  { key: "approvals", label: "Approvals" },
  { key: "backtests", label: "Backtests" },
  { key: "system", label: "System" },
];

function usePlatformData() {
  const [instances, setInstances] = useState<InstanceStatus[]>([]);
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [health, setHealth] = useState("unknown");
  const [systemState, setSystemState] = useState<SystemState>(EMPTY_SYSTEM_STATE);
  const [loading, setLoading] = useState(true);
  const [dataRefreshFailed, setDataRefreshFailed] = useState(false);

  const refresh = useCallback(async (options?: { silent?: boolean }) => {
    const silent = options?.silent ?? false;
    if (!silent) {
      setLoading(true);
    }
    try {
      const [healthResult, instancesResult, approvalsResult, systemResult] = await Promise.all([
        getHealth(),
        listInstances(),
        listPendingApprovals(),
        getSystemState(),
      ]);
      setHealth(healthResult.status);
      setInstances(instancesResult);
      setApprovals(approvalsResult);
      setSystemState(systemResult);
      setDataRefreshFailed(false);
    } catch (error) {
      setHealth("unknown");
      setInstances([]);
      setApprovals([]);
      setSystemState({ ...EMPTY_SYSTEM_STATE });
      setDataRefreshFailed(true);
      throw error;
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    let alive = true;
    refresh().catch((error: unknown) => {
      if (!alive) return;
      const content = error instanceof Error ? error.message : String(error);
      message.error(`加载平台数据失败：${content}`);
    });
    const timer = window.setInterval(() => {
      refresh({ silent: true }).catch(() => undefined);
    }, REFRESH_INTERVAL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [refresh]);

  return { approvals, dataRefreshFailed, health, instances, loading, refresh, setSystemState, systemState };
}

export default function App() {
  const [activePage, setActivePage] = useState<ConsolePageKey>("dashboard");
  const { approvals, dataRefreshFailed, health, instances, loading, refresh, setSystemState, systemState } =
    usePlatformData();

  const runningCount = useMemo(
    () => instances.filter((item) => item.status === "running").length,
    [instances],
  );
  const errorCount = useMemo(
    () => instances.filter((item) => item.status === "error").length,
    [instances],
  );

  const page = useMemo(() => {
    switch (activePage) {
      case "instances":
        return <InstancesPage instances={instances} loading={loading} onMutated={() => void refresh()} />;
      case "create-agent":
        return <CreateAgentPage onCreated={() => void refresh()} />;
      case "approvals":
        return <ApprovalsPage items={approvals} loading={loading} onMutated={() => void refresh()} />;
      case "backtests":
        return <BacktestsPage />;
      case "system":
        return (
          <SystemPage
            health={health}
            systemState={systemState}
            loading={loading}
            dataRefreshFailed={dataRefreshFailed}
          />
        );
      default:
        return (
          <DashboardPage
            health={health}
            killSwitchEnabled={systemState.kill_switch_enabled}
            runningCount={runningCount}
            errorCount={errorCount}
            pendingApprovalCount={approvals.length}
            loading={loading}
            dataRefreshFailed={dataRefreshFailed}
          />
        );
    }
  }, [
    activePage,
    approvals,
    dataRefreshFailed,
    health,
    instances,
    loading,
    refresh,
    systemState,
    runningCount,
    errorCount,
  ]);

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
        <Layout.Sider width={232} className="!bg-[rgba(255,253,249,0.72)] !backdrop-blur">
          <div className="flex items-center gap-2 border-b border-shell-line px-5 py-5">
            <RadarChartOutlined className="text-shell-accent" />
            <span className="font-display text-lg text-shell-ink">Tradeclaw</span>
          </div>
          <Menu
            mode="inline"
            selectedKeys={[activePage]}
            items={NAV_ITEMS.map((item) => ({ key: item.key, label: item.label }))}
            onClick={({ key }) => setActivePage(key as ConsolePageKey)}
            className="border-e-0 bg-transparent px-3 py-4"
          />
        </Layout.Sider>
        <Layout>
          <Layout.Header className="flex h-auto items-center justify-end gap-2 border-b border-shell-line bg-transparent px-5 py-4">
            <Button
              className="rounded-xl"
              onClick={async () => {
                try {
                  await refresh();
                } catch (error: unknown) {
                  const content = error instanceof Error ? error.message : String(error);
                  message.error(`刷新失败：${content}`);
                }
              }}
            >
              <ReloadOutlined /> 刷新
            </Button>
            <Button
              className="rounded-xl"
              onClick={async () => {
                try {
                  const next = await setKillSwitch(!systemState.kill_switch_enabled);
                  setSystemState(next);
                  await refresh();
                } catch (error: unknown) {
                  const content = error instanceof Error ? error.message : String(error);
                  message.error(`熔断开关操作失败：${content}`);
                }
              }}
            >
              {systemState.kill_switch_enabled ? "关闭熔断开关" : "开启熔断开关"}
            </Button>
            <Button
              className="rounded-xl"
              type="primary"
              onClick={async () => {
                try {
                  await tickOnce();
                  await refresh();
                } catch (error: unknown) {
                  const content = error instanceof Error ? error.message : String(error);
                  message.error(`执行一轮失败：${content}`);
                }
              }}
            >
              执行一轮
            </Button>
          </Layout.Header>
          <Layout.Content className="px-5 py-5">
            {dataRefreshFailed && !loading ? (
              <Alert
                className="mb-4 rounded-2xl border border-shell-line"
                message="数据刷新失败"
                description="当前展示的数据已降级或清空，可能不是最新状态，请检查 API 连接后点击「刷新」重试。"
                type="warning"
                showIcon
              />
            ) : null}
            {page}
          </Layout.Content>
        </Layout>
      </Layout>
    </ConfigProvider>
  );
}
