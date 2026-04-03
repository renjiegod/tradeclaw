# Frontend Console Information Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the existing single-screen frontend into a lightweight admin console with first-level navigation and dedicated pages for dashboard, instances, creation, approvals, backtests, and system operations, while preserving the current backend-driven workflows.

**Architecture:** Keep the current React + Ant Design + Vite stack, but reorganize the app around a shell layout in `App.tsx` with sidebar navigation, a global header, and page components in `frontend/src/pages/`. Reuse the existing instance and approval cards inside dedicated pages, add a few shared primitives for headings and summary cards, and avoid adding routing or new backend endpoints in this iteration.

**Tech Stack:** React 18, TypeScript, Ant Design 5, Vite 7, Tailwind CSS 3

---

## File Structure

### Create

- `frontend/src/components/EmptyStateCard.tsx` - reusable empty-state panel for pages without backend-supported content yet
- `frontend/src/components/MetricSummaryCard.tsx` - reusable top summary card used by dashboard and system pages
- `frontend/src/components/PageIntro.tsx` - shared page title and subtitle block
- `frontend/src/pages/DashboardPage.tsx` - health and activity overview page
- `frontend/src/pages/InstancesPage.tsx` - page wrapper around the existing instance table
- `frontend/src/pages/CreateAgentPage.tsx` - page wrapper around the existing creation form
- `frontend/src/pages/ApprovalsPage.tsx` - page wrapper around the existing approval queue
- `frontend/src/pages/BacktestsPage.tsx` - explicit empty-state placeholder for future backtest flows
- `frontend/src/pages/SystemPage.tsx` - system operations and kill-switch status page

### Modify

- `frontend/src/App.tsx` - become the console shell, navigation controller, and shared data coordinator
- `frontend/src/api.ts` - reuse `SystemState` type from `types.ts` for system endpoints
- `frontend/src/types.ts` - add navigation and system-response types used by the shell and pages

### Keep As-Is and Reuse

- `frontend/src/components/ApprovalQueueCard.tsx`
- `frontend/src/components/CreateAgentCard.tsx`
- `frontend/src/components/InstanceTableCard.tsx`
- `frontend/src/styles.css`

### Verification

- `frontend/package.json` scripts: `npm run build`

### Session Git Constraint

- Do not create a git commit unless the user explicitly requests one in this session.
- Use the “checkpoint review” step in each task to stop for review. If the user later asks for a commit, use the suggested commit message shown in that task.

## Task 1: Add Shared Types and Page Primitives

**Files:**
- Create: `frontend/src/components/EmptyStateCard.tsx`
- Create: `frontend/src/components/MetricSummaryCard.tsx`
- Create: `frontend/src/components/PageIntro.tsx`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/types.ts`
- Test: `frontend/package.json`

- [ ] **Step 1: Write the failing type contract**

Update `frontend/src/api.ts` so the system endpoints depend on a `SystemState` type that does not exist yet.

```ts
import type {
  AgentTemplate,
  CreateInstancePayload,
  InstanceStatus,
  PendingApproval,
  SystemState,
} from "./types";

export async function getSystemState(): Promise<SystemState> {
  return request("/system/state");
}

export async function setKillSwitch(enabled: boolean): Promise<SystemState> {
  return request("/system/kill-switch", {
    method: "POST",
    body: JSON.stringify({ enabled }),
  });
}
```

- [ ] **Step 2: Run build to verify it fails**

Run: `npm run build`

Expected: FAIL with a TypeScript error saying `SystemState` is not exported from `./types`.

- [ ] **Step 3: Add the missing types and shared UI primitives**

Update `frontend/src/types.ts`:

```ts
export type ConsolePageKey =
  | "dashboard"
  | "instances"
  | "create-agent"
  | "approvals"
  | "backtests"
  | "system";

export type SystemState = {
  kill_switch_enabled: boolean;
  instance_count: number;
  running_count: number;
};
```

Create `frontend/src/components/PageIntro.tsx`:

```tsx
import { Space, Typography } from "antd";
import type { ReactNode } from "react";

type Props = {
  title: string;
  description: string;
  extra?: ReactNode;
};

export function PageIntro({ title, description, extra }: Props) {
  return (
    <div className="mb-5 flex flex-col gap-3 border-b border-shell-line pb-4 md:flex-row md:items-end md:justify-between">
      <Space direction="vertical" size={2}>
        <Typography.Title level={3} className="!m-0 !font-display !text-shell-ink">
          {title}
        </Typography.Title>
        <Typography.Text className="text-sm text-shell-muted">{description}</Typography.Text>
      </Space>
      {extra ? <div className="shrink-0">{extra}</div> : null}
    </div>
  );
}
```

Create `frontend/src/components/MetricSummaryCard.tsx`:

```tsx
import { Card, Statistic } from "antd";

type Props = {
  title: string;
  value: string | number;
};

export function MetricSummaryCard({ title, value }: Props) {
  return (
    <Card className="!border !border-shell-line !bg-card-bg shadow-shell-card">
      <Statistic title={title} value={value} />
    </Card>
  );
}
```

Create `frontend/src/components/EmptyStateCard.tsx`:

```tsx
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
```

- [ ] **Step 4: Run build to verify it passes**

Run: `npm run build`

Expected: PASS with Vite build output ending in `built in` and generated files in `dist/`.

- [ ] **Step 5: Checkpoint review**

Run: `git diff -- frontend/src/api.ts frontend/src/types.ts frontend/src/components/`

Expected: only the new type definitions and shared page primitives appear in the diff.

If the user explicitly requests a commit later, use: `refactor: add frontend console page primitives`

## Task 2: Introduce the Console Shell and Page Scaffolds

**Files:**
- Create: `frontend/src/pages/DashboardPage.tsx`
- Create: `frontend/src/pages/InstancesPage.tsx`
- Create: `frontend/src/pages/CreateAgentPage.tsx`
- Create: `frontend/src/pages/ApprovalsPage.tsx`
- Create: `frontend/src/pages/BacktestsPage.tsx`
- Create: `frontend/src/pages/SystemPage.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/types.ts`
- Test: `frontend/package.json`

- [ ] **Step 1: Write the failing shell integration**

Refactor `frontend/src/App.tsx` to reference the new page components and navigation keys before those page files exist.

```tsx
import { Button, ConfigProvider, Layout, Menu, message } from "antd";
import { RadarChartOutlined, ReloadOutlined } from "@ant-design/icons";
import { useCallback, useEffect, useMemo, useState } from "react";

import { DashboardPage } from "./pages/DashboardPage";
import { InstancesPage } from "./pages/InstancesPage";
import { CreateAgentPage } from "./pages/CreateAgentPage";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { BacktestsPage } from "./pages/BacktestsPage";
import { SystemPage } from "./pages/SystemPage";
import type { ConsolePageKey } from "./types";

const NAV_ITEMS: Array<{ key: ConsolePageKey; label: string }> = [
  { key: "dashboard", label: "Dashboard" },
  { key: "instances", label: "Agent Instances" },
  { key: "create-agent", label: "Create Agent" },
  { key: "approvals", label: "Approvals" },
  { key: "backtests", label: "Backtests" },
  { key: "system", label: "System" },
];

const [activePage, setActivePage] = useState<ConsolePageKey>("dashboard");
```

- [ ] **Step 2: Run build to verify it fails**

Run: `npm run build`

Expected: FAIL with TypeScript module-resolution errors for one or more missing files under `src/pages/`.

- [ ] **Step 3: Create page stubs and complete the shell**

Create `frontend/src/pages/DashboardPage.tsx`:

```tsx
import { EmptyStateCard } from "../components/EmptyStateCard";
import { PageIntro } from "../components/PageIntro";

export function DashboardPage() {
  return (
    <>
      <PageIntro title="Dashboard" description="查看平台整体运行状态与核心摘要。" />
      <EmptyStateCard title="等待接入摘要数据" description="下一任务会把现有平台指标迁移到这里。" />
    </>
  );
}
```

Create `frontend/src/pages/InstancesPage.tsx`:

```tsx
import { EmptyStateCard } from "../components/EmptyStateCard";
import { PageIntro } from "../components/PageIntro";

export function InstancesPage() {
  return (
    <>
      <PageIntro title="Agent Instances" description="管理已创建的实例和运行状态。" />
      <EmptyStateCard title="等待接入实例表格" description="下一任务会复用现有实例列表卡片。" />
    </>
  );
}
```

Create `frontend/src/pages/CreateAgentPage.tsx`:

```tsx
import { EmptyStateCard } from "../components/EmptyStateCard";
import { PageIntro } from "../components/PageIntro";

export function CreateAgentPage() {
  return (
    <>
      <PageIntro title="Create Agent" description="通过模板创建新的 Tradeclaw 实例。" />
      <EmptyStateCard title="等待接入创建表单" description="下一任务会复用现有创建卡片。" />
    </>
  );
}
```

Create `frontend/src/pages/ApprovalsPage.tsx`:

```tsx
import { EmptyStateCard } from "../components/EmptyStateCard";
import { PageIntro } from "../components/PageIntro";

export function ApprovalsPage() {
  return (
    <>
      <PageIntro title="Approvals" description="集中处理待审批订单与超时状态。" />
      <EmptyStateCard title="等待接入审批队列" description="下一任务会复用现有审批卡片。" />
    </>
  );
}
```

Create `frontend/src/pages/BacktestsPage.tsx`:

```tsx
import { EmptyStateCard } from "../components/EmptyStateCard";
import { PageIntro } from "../components/PageIntro";

export function BacktestsPage() {
  return (
    <>
      <PageIntro title="Backtests" description="为回测任务、对比与报告预留固定入口。" />
      <EmptyStateCard
        title="回测功能将在后端支持后接入"
        description="当前版本先保留信息架构位置，避免后续重新组织后台导航。"
      />
    </>
  );
}
```

Create `frontend/src/pages/SystemPage.tsx`:

```tsx
import { EmptyStateCard } from "../components/EmptyStateCard";
import { PageIntro } from "../components/PageIntro";

export function SystemPage() {
  return (
    <>
      <PageIntro title="System" description="查看系统级状态、熔断开关和运行说明。" />
      <EmptyStateCard title="等待接入系统摘要" description="下一任务会展示 health 与 kill switch 状态。" />
    </>
  );
}
```

Replace `frontend/src/App.tsx` with the shell layout:

```tsx
import { RadarChartOutlined, ReloadOutlined } from "@ant-design/icons";
import { Button, ConfigProvider, Layout, Menu, message } from "antd";
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
  const [systemState, setSystemState] = useState<SystemState>({
    kill_switch_enabled: false,
    instance_count: 0,
    running_count: 0,
  });
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
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
    } finally {
      setLoading(false);
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
      refresh().catch(() => undefined);
    }, REFRESH_INTERVAL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [refresh]);

  return { approvals, health, instances, loading, refresh, setSystemState, systemState };
}

export default function App() {
  const [activePage, setActivePage] = useState<ConsolePageKey>("dashboard");
  const { refresh, setSystemState, systemState } = usePlatformData();

  const page = useMemo(() => {
    switch (activePage) {
      case "instances":
        return <InstancesPage />;
      case "create-agent":
        return <CreateAgentPage />;
      case "approvals":
        return <ApprovalsPage />;
      case "backtests":
        return <BacktestsPage />;
      case "system":
        return <SystemPage />;
      default:
        return <DashboardPage />;
    }
  }, [activePage]);

  return (
    <ConfigProvider locale={zhCN}>
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
            <Button className="rounded-xl" onClick={() => void refresh()}>
              <ReloadOutlined /> 刷新
            </Button>
            <Button
              className="rounded-xl"
              onClick={async () => {
                const next = await setKillSwitch(!systemState.kill_switch_enabled);
                setSystemState(next);
                await refresh();
              }}
            >
              {systemState.kill_switch_enabled ? "关闭熔断开关" : "开启熔断开关"}
            </Button>
            <Button
              className="rounded-xl"
              type="primary"
              onClick={async () => {
                await tickOnce();
                await refresh();
              }}
            >
              执行一轮
            </Button>
          </Layout.Header>
          <Layout.Content className="px-5 py-5">{page}</Layout.Content>
        </Layout>
      </Layout>
    </ConfigProvider>
  );
}
```

- [ ] **Step 4: Run build to verify it passes**

Run: `npm run build`

Expected: PASS and the shell renders all six first-level pages as stub screens.

- [ ] **Step 5: Checkpoint review**

Run: `git diff -- frontend/src/App.tsx frontend/src/pages/ frontend/src/types.ts`

Expected: the diff shows the new shell layout and page scaffold files only, with no backend or shared-card regressions.

If the user explicitly requests a commit later, use: `refactor: add frontend console shell and page scaffolds`

## Task 3: Wire Real Data into Dashboard, Instance, Approval, Create, and System Pages

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/pages/DashboardPage.tsx`
- Modify: `frontend/src/pages/InstancesPage.tsx`
- Modify: `frontend/src/pages/CreateAgentPage.tsx`
- Modify: `frontend/src/pages/ApprovalsPage.tsx`
- Modify: `frontend/src/pages/SystemPage.tsx`
- Test: `frontend/package.json`

- [ ] **Step 1: Write the failing page-prop integration**

Update `frontend/src/App.tsx` to pass real props into the page components before those components accept them.

```tsx
const runningCount = useMemo(
  () => instances.filter((item) => item.status === "running").length,
  [instances],
);

const errorCount = useMemo(
  () => instances.filter((item) => item.status === "error").length,
  [instances],
);

case "instances":
  return <InstancesPage instances={instances} loading={loading} onMutated={() => void refresh()} />;
case "create-agent":
  return <CreateAgentPage onCreated={() => void refresh()} />;
case "approvals":
  return <ApprovalsPage items={approvals} loading={loading} onMutated={() => void refresh()} />;
case "system":
  return <SystemPage health={health} systemState={systemState} />;
default:
  return (
    <DashboardPage
      health={health}
      killSwitchEnabled={systemState.kill_switch_enabled}
      runningCount={runningCount}
      errorCount={errorCount}
      pendingApprovalCount={approvals.length}
    />
  );
```

- [ ] **Step 2: Run build to verify it fails**

Run: `npm run build`

Expected: FAIL with TypeScript prop-type errors because the page components still have empty signatures.

- [ ] **Step 3: Implement the page components with real content**

Update `frontend/src/pages/DashboardPage.tsx`:

```tsx
import { Alert, Col, Row } from "antd";

import { MetricSummaryCard } from "../components/MetricSummaryCard";
import { PageIntro } from "../components/PageIntro";

type Props = {
  health: string;
  killSwitchEnabled: boolean;
  runningCount: number;
  errorCount: number;
  pendingApprovalCount: number;
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
}: Props) {
  return (
    <>
      <PageIntro title="Dashboard" description="查看平台运行总览、审批压力和系统健康状态。" />
      <Row gutter={[16, 16]}>
        <Col xs={24} md={12} xl={6}>
          <MetricSummaryCard title="平台健康状态" value={formatHealth(health)} />
        </Col>
        <Col xs={24} md={12} xl={6}>
          <MetricSummaryCard title="运行中实例" value={runningCount} />
        </Col>
        <Col xs={24} md={12} xl={6}>
          <MetricSummaryCard title="异常实例" value={errorCount} />
        </Col>
        <Col xs={24} md={12} xl={6}>
          <MetricSummaryCard title="待审批订单" value={pendingApprovalCount} />
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
      {health !== "ok" ? (
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
```

Update `frontend/src/pages/InstancesPage.tsx`:

```tsx
import { InstanceTableCard } from "../components/InstanceTableCard";
import { PageIntro } from "../components/PageIntro";
import type { InstanceStatus } from "../types";

type Props = {
  instances: InstanceStatus[];
  loading: boolean;
  onMutated: () => void;
};

export function InstancesPage({ instances, loading, onMutated }: Props) {
  return (
    <>
      <PageIntro title="Agent Instances" description="查看实例状态，并执行启动、暂停和停止操作。" />
      <InstanceTableCard instances={instances} loading={loading} onMutated={onMutated} />
    </>
  );
}
```

Update `frontend/src/pages/CreateAgentPage.tsx`:

```tsx
import { CreateAgentCard } from "../components/CreateAgentCard";
import { PageIntro } from "../components/PageIntro";

type Props = {
  onCreated: () => void;
};

export function CreateAgentPage({ onCreated }: Props) {
  return (
    <>
      <PageIntro title="Create Agent" description="基于模板创建新实例，并预填安全默认配置。" />
      <div className="max-w-3xl">
        <CreateAgentCard onCreated={onCreated} />
      </div>
    </>
  );
}
```

Update `frontend/src/pages/ApprovalsPage.tsx`:

```tsx
import { ApprovalQueueCard } from "../components/ApprovalQueueCard";
import { PageIntro } from "../components/PageIntro";
import type { PendingApproval } from "../types";

type Props = {
  items: PendingApproval[];
  loading: boolean;
  onMutated: () => void;
};

export function ApprovalsPage({ items, loading, onMutated }: Props) {
  return (
    <>
      <PageIntro title="Approvals" description="集中查看待审批请求，快速批准或拒绝风险订单。" />
      <ApprovalQueueCard items={items} loading={loading} onMutated={onMutated} />
    </>
  );
}
```

Update `frontend/src/pages/SystemPage.tsx`:

```tsx
import { Alert, Col, Row, Typography } from "antd";

import { MetricSummaryCard } from "../components/MetricSummaryCard";
import { PageIntro } from "../components/PageIntro";
import type { SystemState } from "../types";

type Props = {
  health: string;
  systemState: SystemState;
};

export function SystemPage({ health, systemState }: Props) {
  return (
    <>
      <PageIntro title="System" description="查看系统级运行状态、熔断开关和操作影响范围。" />
      <Row gutter={[16, 16]}>
        <Col xs={24} md={8}>
          <MetricSummaryCard title="后端健康状态" value={health === "ok" ? "正常" : "异常"} />
        </Col>
        <Col xs={24} md={8}>
          <MetricSummaryCard title="实例总数" value={systemState.instance_count} />
        </Col>
        <Col xs={24} md={8}>
          <MetricSummaryCard title="运行中实例" value={systemState.running_count} />
        </Col>
      </Row>
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
      <Typography.Paragraph className="mt-4 text-shell-muted">
        System 页面只展示现有后端已支持的状态，不在本次迭代中引入新的系统 API。
      </Typography.Paragraph>
    </>
  );
}
```

Update the page selection in `frontend/src/App.tsx` so the real props are passed through:

```tsx
const { approvals, health, instances, loading, refresh, setSystemState, systemState } = usePlatformData();

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
      return <SystemPage health={health} systemState={systemState} />;
    default:
      return (
        <DashboardPage
          health={health}
          killSwitchEnabled={systemState.kill_switch_enabled}
          runningCount={runningCount}
          errorCount={errorCount}
          pendingApprovalCount={approvals.length}
        />
      );
  }
}, [activePage, approvals, errorCount, health, instances, loading, refresh, runningCount, systemState]);
```

- [ ] **Step 4: Run build to verify it passes**

Run: `npm run build`

Expected: PASS and the dashboard, instances, create, approvals, backtests, and system pages all render within the shared shell without TypeScript errors.

- [ ] **Step 5: Checkpoint review**

Run: `git diff -- frontend/src/App.tsx frontend/src/pages/`

Expected: the diff shows data-driven page implementations that reuse the existing cards instead of duplicating business logic.

If the user explicitly requests a commit later, use: `refactor: reorganize frontend into admin console pages`

## Self-Review

### 1. Spec coverage

- Shell layout with sidebar, header, and page content: covered by Task 2
- Top-level entries for dashboard, instances, create, approvals, backtests, and system: covered by Task 2
- Functional dashboard, instances, create, approvals, and system pages based on existing APIs: covered by Task 3
- Explicit placeholder strategy for unsupported backtest depth: covered by Task 2 and retained in Task 3
- Reuse of existing cards instead of rewriting operational logic: covered by Task 3

No approved spec requirement is left without a task.

### 2. Placeholder scan

- No `TODO`, `TBD`, or “implement later” instructions appear in the task steps.
- The only intentionally incomplete area is the `BacktestsPage`, which is defined explicitly as an empty-state page backed by no new API.

### 3. Type consistency

- Navigation keys use `ConsolePageKey` consistently across `types.ts` and `App.tsx`.
- System endpoint responses use `SystemState` consistently across `types.ts`, `api.ts`, `App.tsx`, and `SystemPage.tsx`.
- Reused operational props align with existing component contracts for instance, approval, and creation cards.
