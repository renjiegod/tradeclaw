import {
  ApiOutlined,
  CheckCircleOutlined,
  LineChartOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MessageOutlined,
  ReloadOutlined,
  RobotOutlined,
  SearchOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { Alert, Button, ConfigProvider, Drawer, Layout, Menu, message, Spin } from "antd";
import zhCN from "antd/locale/zh_CN";
import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from "react";
import {
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";

import {
  getHealth,
  getRuntimeStatus,
  getSetupStatus,
  getSystemState,
  getVersion,
  listTasks,
  listPendingApprovals,
} from "./api";
import { CloudUserMenu } from "./components/CloudUserMenu";
import { SETUP_WIZARD_SKIPPED_KEY, SetupWizard } from "./components/SetupWizard";
import { UpdateBanner } from "./components/UpdateFlow";
import { type ConsoleOutletContext, useConsoleOutlet } from "./consoleOutletContext";
import { PageRefreshContext } from "./pageRefreshContext";
import type { ConsolePageKey, TaskStatus, PendingApproval, RuntimeStatus, SystemState, VersionInfo } from "./types";
import { menuKeyFromPathname } from "./utils/menuKeyFromPathname";

// Lazy-loaded route pages
const AgentsPage = lazy(() => import("./pages/AgentsPage").then((m) => ({ default: m.AgentsPage })));
const CronJobsPage = lazy(() => import("./pages/CronJobsPage").then((m) => ({ default: m.CronJobsPage })));
const ChannelsPage = lazy(() => import("./pages/ChannelsPage").then((m) => ({ default: m.ChannelsPage })));
const AssistantPage = lazy(() => import("./pages/AssistantPage").then((m) => ({ default: m.AssistantPage })));
const SwarmPage = lazy(() => import("./pages/SwarmPage").then((m) => ({ default: m.SwarmPage })));
const TasksPage = lazy(() => import("./pages/TasksPage").then((m) => ({ default: m.TasksPage })));
const TaskDetailPage = lazy(() => import("./pages/TaskDetailPage").then((m) => ({ default: m.TaskDetailPage })));
const AccountsPage = lazy(() => import("./pages/AccountsPage").then((m) => ({ default: m.AccountsPage })));
const StocksPage = lazy(() => import("./pages/StocksPage").then((m) => ({ default: m.StocksPage })));
const StockDetailPage = lazy(() => import("./pages/StockDetailPage").then((m) => ({ default: m.StockDetailPage })));
const WatchlistPage = lazy(() => import("./pages/WatchlistPage").then((m) => ({ default: m.WatchlistPage })));
const StockMonitorPage = lazy(() => import("./pages/StockMonitorPage").then((m) => ({ default: m.StockMonitorPage })));
const MarketReviewPage = lazy(() => import("./pages/MarketReviewPage").then((m) => ({ default: m.MarketReviewPage })));
const StrategiesPage = lazy(() => import("./pages/StrategiesPage").then((m) => ({ default: m.StrategiesPage })));
const ApprovalsPage = lazy(() => import("./pages/ApprovalsPage").then((m) => ({ default: m.ApprovalsPage })));
const ModelInvocationsPage = lazy(() => import("./pages/ModelInvocationsPage").then((m) => ({ default: m.ModelInvocationsPage })));
const ModelSettingsPage = lazy(() => import("./pages/ModelSettingsPage").then((m) => ({ default: m.ModelSettingsPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then((m) => ({ default: m.SettingsPage })));
const KnowledgePage = lazy(() => import("./pages/KnowledgePage").then((m) => ({ default: m.KnowledgePage })));
const DataConsolePage = lazy(() => import("./pages/DataConsolePage").then((m) => ({ default: m.DataConsolePage })));

function RoutePage({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<div className="flex h-64 items-center justify-center"><Spin /></div>}>{children}</Suspense>;
}

/** 后台仅轮询 /health，不刷新实例与审批等全量数据 */
const HEALTH_POLL_INTERVAL_MS = 8000;

/** 进入 Approvals 页时对审批列表的加速轮询频率。实盘审批要求及时可见，
 * 因此在该页停留期间静默重拉全量数据（含 /approvals/pending），离开后回落到
 * 默认的 8s 健康轮询。 */
const APPROVALS_POLL_INTERVAL_MS = 2500;

const EMPTY_SYSTEM_STATE: SystemState = {
  kill_switch_enabled: false,
  task_count: 0,
  running_count: 0,
};

type NavLeaf = {
  key: ConsolePageKey;
  label: string;
  icon?: React.ReactNode;
  /** 本地基础设施页（券商账户 / 模型 key / 全局设置），cloud 部署隐藏。 */
  hideInCloud?: boolean;
  /** 云端专属页（如数据接入：管理 dytc API key / 用量），local 部署隐藏。 */
  cloudOnly?: boolean;
};
type NavGroup = { key: string; label: string; icon: React.ReactNode; children: NavLeaf[] };
type NavEntry = NavLeaf | NavGroup;

const isNavGroup = (entry: NavEntry): entry is NavGroup => "children" in entry;

/**
 * 分组导航树：常用入口（对话 / 审批）保留在顶层，其余按「智能体 / 交易 /
 * 行情研究 / 系统」四组收进可折叠子菜单，降低侧边栏纵向长度。分组 key 用
 * `grp_` 前缀，避免与页面 key（ConsolePageKey）冲突。
 */
const NAV_TREE: NavEntry[] = [
  { key: "assistant", label: "对话", icon: <MessageOutlined /> },
  { key: "approvals", label: "审批", icon: <CheckCircleOutlined /> },
  {
    key: "grp_automation",
    label: "智能体",
    icon: <RobotOutlined />,
    children: [
      { key: "agents", label: "智能体列表" },
      { key: "swarm", label: "协作群" },
      { key: "cron_jobs", label: "提醒" },
      { key: "channels", label: "消息渠道" },
    ],
  },
  {
    key: "grp_trading",
    label: "交易",
    icon: <LineChartOutlined />,
    children: [
      { key: "tasks", label: "任务" },
      { key: "strategies", label: "策略库" },
      // 券商账户接入是本地客户端的事（交易留在本地）；云端隐藏。
      { key: "accounts", label: "账户", hideInCloud: true },
    ],
  },
  {
    key: "grp_market",
    label: "行情研究",
    icon: <SearchOutlined />,
    children: [
      { key: "stocks", label: "股票" },
      { key: "watchlist", label: "自选股" },
      { key: "stock_monitor", label: "盯盘" },
      { key: "market_review", label: "市场复盘" },
      { key: "knowledge", label: "知识库" },
    ],
  },
  {
    key: "grp_system",
    label: "系统",
    icon: <SettingOutlined />,
    children: [
      { key: "model_invocations", label: "模型调用记录" },
      // 云端由平台统一供模型（用户不配 key）；全局 YAML/qmt-proxy 等基础设施
      // 设置由平台托管，用户改了只会出错 —— 两项云端隐藏。
      { key: "settings_models", label: "模型配置", hideInCloud: true },
      { key: "settings", label: "设置", hideInCloud: true },
    ],
  },
  // 云端数据接入（dytc API Keys / 用量 / 接入教程）——管理云端行情 API 凭证，
  // 单机部署没有对应后端，local 隐藏（cloudOnly）。
  { key: "data_console", label: "数据接入", icon: <ApiOutlined />, cloudOnly: true },
];

/** 反查某个页面 key 所属的分组 key（顶层项无分组，返回 undefined）。 */
const GROUP_KEY_BY_PAGE: Partial<Record<ConsolePageKey, string>> = Object.fromEntries(
  NAV_TREE.filter(isNavGroup).flatMap((group) => group.children.map((leaf) => [leaf.key, group.key])),
);

const _leavesOf = (entry: NavEntry): NavLeaf[] => (isNavGroup(entry) ? entry.children : [entry]);

/** Page keys hidden in the cloud deployment (local-only infra: broker accounts,
 * model-key config, global YAML/qmt-proxy settings). Also used to guard routes
 * so a hidden page is unreachable by direct URL in cloud mode. */
export const CLOUD_HIDDEN_PAGES: ReadonlySet<ConsolePageKey> = new Set(
  NAV_TREE.flatMap(_leavesOf).filter((leaf) => leaf.hideInCloud).map((leaf) => leaf.key),
);

/** Page keys that only exist in the cloud deployment (dytc data-console:
 * API keys / usage / guide). Local mode hides them from the nav AND redirects
 * direct URLs away (no backing API on a single-machine install). */
export const CLOUD_ONLY_PAGES: ReadonlySet<ConsolePageKey> = new Set(
  NAV_TREE.flatMap(_leavesOf).filter((leaf) => leaf.cloudOnly).map((leaf) => leaf.key),
);

/** NAV_TREE filtered for the deployment mode — two-way: cloud drops
 * `hideInCloud` leaves, everything else (local, or mode not yet known/null)
 * drops `cloudOnly` leaves. Groups left empty by filtering are removed. */
export function visibleNavTree(mode: string | null): NavEntry[] {
  const keep = (leaf: NavLeaf) => (mode === "cloud" ? !leaf.hideInCloud : !leaf.cloudOnly);
  return NAV_TREE.flatMap((entry): NavEntry[] => {
    if (!isNavGroup(entry)) return keep(entry) ? [entry] : [];
    const children = entry.children.filter(keep);
    return children.length ? [{ ...entry, children }] : [];
  });
}

const PATHS: Record<ConsolePageKey, string> = {
  agents: "/agents",
  cron_jobs: "/cron_jobs",
  channels: "/channels",
  assistant: "/assistant",
  swarm: "/swarm",
  tasks: "/tasks",
  accounts: "/accounts",
  stocks: "/stocks",
  watchlist: "/watchlist",
  stock_monitor: "/stock_monitor",
  market_review: "/market_review",
  strategies: "/strategies",
  knowledge: "/knowledge",
  approvals: "/approvals",
  model_invocations: "/model_invocations",
  settings_models: "/settings/models",
  settings: "/settings",
  data_console: "/data_console",
};

function usePlatformData() {
  const [tasks, setTasks] = useState<TaskStatus[]>([]);
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [health, setHealth] = useState("unknown");
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus | null>(null);
  const [systemState, setSystemState] = useState<SystemState>(EMPTY_SYSTEM_STATE);
  const [loading, setLoading] = useState(true);
  const [dataRefreshFailed, setDataRefreshFailed] = useState(false);

  const refresh = useCallback(async (options?: { silent?: boolean }) => {
    const silent = options?.silent ?? false;
    if (!silent) {
      setLoading(true);
    }
    try {
      const [runtimeResult, tasksResult, approvalsResult, systemResult] = await Promise.all([
        getRuntimeStatus(),
        listTasks(),
        listPendingApprovals(),
        getSystemState(),
      ]);
      setHealth(runtimeResult.health);
      setRuntimeStatus(runtimeResult);
      setTasks(tasksResult);
      setApprovals(approvalsResult);
      setSystemState(systemResult);
      setDataRefreshFailed(false);
    } catch (error) {
      setHealth("unknown");
      setRuntimeStatus(null);
      setTasks([]);
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
    return () => {
      alive = false;
    };
  }, [refresh]);

  useEffect(() => {
    const tick = () => {
      void getHealth()
        .then((result) => {
          setHealth(result.status);
        })
        .catch(() => {
          setHealth("unknown");
        });
    };
    const timer = window.setInterval(tick, HEALTH_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, []);

  return {
    approvals,
    dataRefreshFailed,
    health,
    instances: tasks,
    loading,
    refresh,
    runtimeStatus,
    setSystemState,
    systemState,
  };
}

function ApiHealthHeaderStatus({ health }: { health: string }) {
  const label = `status: ${health}`;
  const dotClass =
    health === "ok" ? "bg-emerald-500" : health === "unknown" ? "bg-amber-500" : "bg-red-500";
  const title =
    health === "unknown"
      ? "尚未成功请求运行状态接口，或请求失败；以下为占位状态。"
      : "来自运行状态接口的健康状态";

  return (
    <div
      className="flex min-w-0 max-w-full flex-1 items-center"
      role="status"
      aria-live="polite"
      aria-label={`API 健康状态：${health}`}
      title={title}
    >
      <div className="flex min-w-0 max-w-[min(100%,28rem)] items-center gap-2 rounded-xl border border-shell-line bg-card-bg px-3 py-1.5 shadow-shell-card">
        <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${dotClass}`} aria-hidden />
        <span className="truncate font-mono text-sm text-shell-ink">{label}</span>
      </div>
    </div>
  );
}

function useVersionInfo() {
  const [version, setVersion] = useState<VersionInfo | null>(null);
  useEffect(() => {
    let alive = true;
    void getVersion()
      .then((result) => {
        if (alive) setVersion(result);
      })
      .catch(() => {
        if (alive) setVersion(null);
      });
    return () => {
      alive = false;
    };
  }, []);
  return version;
}

function SidebarVersionBadge() {
  const version = useVersionInfo();
  if (!version) return null;

  const display = version.git_tag ?? version.git_commit_short ?? version.package_version;
  const fullInfo = [
    `package: ${version.package_version}`,
    `engine: ${version.engine_version}`,
    version.git_tag ? `tag: ${version.git_tag}` : null,
    version.git_commit ? `commit: ${version.git_commit}` : null,
    version.git_dirty ? "工作区含未提交改动 (dirty)" : null,
  ]
    .filter(Boolean)
    .join("\n");

  return (
    <div
      className="mx-3 mt-1 flex items-center justify-between gap-2 rounded-xl border border-shell-line/60 bg-white/40 px-3 py-1.5 text-[11px] leading-snug text-shell-muted"
      title={fullInfo}
    >
      <span className="truncate font-mono">{display}</span>
      {version.git_dirty ? <span className="shrink-0 text-amber-600">dirty</span> : null}
    </div>
  );
}

function TasksOutlet() {
  const { refresh } = useConsoleOutlet();
  return <TasksPage onMutated={() => void refresh()} />;
}

function AgentsOutlet() {
  // deploymentMode 是 ConsoleShell 的 state，只能经 outlet context 取；
  // 直接在 App() 路由表里引用它会命中未定义变量（历史白屏事故）。
  const { deploymentMode } = useConsoleOutlet();
  return <AgentsPage deploymentMode={deploymentMode} />;
}

function ApprovalsOutlet() {
  const { refresh } = useConsoleOutlet();
  // ApprovalsPage 自取全量数据并自轮询；这里只保留对全局 pending（导航徽标 /
  // 对话页浮现卡）的加速静默刷新，停留该页期间生效。
  useEffect(() => {
    const timer = window.setInterval(() => {
      void refresh({ silent: true }).catch(() => {
        // 静默轮询失败由全量错误态在下次手动刷新时体现，这里不打断用户。
      });
    }, APPROVALS_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);
  return <ApprovalsPage onMutated={() => void refresh()} />;
}

/**
 * Mounts the web first-run SetupWizard overlay (SetupWizard.tsx) when the
 * default agent has no usable model route yet — the double-click-launch
 * replacement for the terminal onboarding wizard (doyoutrade/onboarding.py),
 * which is skipped in that launch mode (DOYOUTRADE_WEB_SETUP=1).
 *
 * "Configured?" always comes from GET /setup/status (never assumed); a
 * localStorage flag only suppresses the overlay for *this* browser after the
 * user explicitly clicks "跳过" — it never fakes "configured" for anyone else
 * hitting the same server, and a later status re-check still wins once real
 * configuration lands (e.g. someone finishes it from another tab/device).
 */
function SetupWizardGate() {
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [skipped, setSkipped] = useState(() => localStorage.getItem(SETUP_WIZARD_SKIPPED_KEY) === "1");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await getSetupStatus();
        if (!cancelled) setConfigured(status.configured);
      } catch {
        // /setup/status unavailable (older backend, or assistant/model-route
        // repositories not wired in this deployment) — never block the
        // console behind a wizard it cannot actually resolve.
        if (!cancelled) setConfigured(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (configured === null || configured === true || skipped) {
    return null;
  }

  return (
    <SetupWizard
      onCompleted={() => setConfigured(true)}
      onSkip={() => setSkipped(true)}
    />
  );
}

function ModelSettingsOutlet() {
  return <ModelSettingsPage />;
}

function ConsoleShell() {
  const { approvals, dataRefreshFailed, health, instances, loading, refresh, runtimeStatus, setSystemState, systemState } =
    usePlatformData();
  const location = useLocation();
  const navigate = useNavigate();

  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("sidebar_collapsed") === "true");
  // <lg 视口：Sider 常收起，导航改为 Drawer 浮层，避免挤压正文。
  // 窄屏下的 collapsed 状态不写回 localStorage，桌面偏好不受影响。
  const [isNarrow, setIsNarrow] = useState(false);
  const [navDrawerOpen, setNavDrawerOpen] = useState(false);
  const [pageRefreshToken, setPageRefreshToken] = useState(0);
  // Deployment mode drives cloud-only chrome (user menu, 数据接入 nav).
  // `null` = not yet determined: a direct URL to a cloud-only page must NOT be
  // redirected away before the mode is known (would bounce a legit cloud user
  // off /data_console on hard refresh), so we keep the "unknown" state
  // explicit and render a pending Spin for cloud-only pages until resolved.
  const [deploymentMode, setDeploymentMode] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await getSetupStatus();
        if (cancelled) return;
        const mode = status.deployment_mode ?? "local";
        setDeploymentMode(mode);
        if (mode === "cloud") document.title = "DoYouTrade Cloud";
      } catch {
        // /setup/status unavailable → treat as "local" (no cloud chrome).
        // MUST resolve to a concrete mode: staying null would leave cloud-only
        // pages spinning forever.
        if (!cancelled) setDeploymentMode("local");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Defense-in-depth: pages missing from the nav must also be unreachable by
  // direct URL. Boolean predicates (NOT JSX-returning helpers — that would
  // trip react-refresh's component hoisting); the Outlet below inlines
  // <Navigate>/<Spin> based on them.
  const isCloudHidden = (key: ConsolePageKey): boolean =>
    deploymentMode === "cloud" && CLOUD_HIDDEN_PAGES.has(key);
  // Cloud-only page in a resolved non-cloud deployment → redirect away.
  const isLocalBlocked = (key: ConsolePageKey): boolean =>
    deploymentMode !== null && deploymentMode !== "cloud" && CLOUD_ONLY_PAGES.has(key);
  // Cloud-only page while the mode is still unknown → wait, don't redirect.
  const isModePending = (key: ConsolePageKey): boolean =>
    deploymentMode === null && CLOUD_ONLY_PAGES.has(key);

  const selectedKey = menuKeyFromPathname(location.pathname);
  const currentGroupKey = GROUP_KEY_BY_PAGE[selectedKey];
  const [openKeys, setOpenKeys] = useState<string[]>(() => (currentGroupKey ? [currentGroupKey] : []));

  // 导航到某个分组内的页面时，确保其所在分组展开；不影响用户手动展开的其他分组。
  useEffect(() => {
    if (currentGroupKey) {
      setOpenKeys((prev) => (prev.includes(currentGroupKey) ? prev : [...prev, currentGroupKey]));
    }
  }, [currentGroupKey]);

  const outletContext = useMemo<ConsoleOutletContext>(
    () => ({
      approvals,
      dataRefreshFailed,
      deploymentMode,
      health,
      instances,
      loading,
      refresh,
      runtimeStatus,
      setSystemState,
      systemState,
    }),
    [approvals, dataRefreshFailed, deploymentMode, health, instances, loading, refresh, runtimeStatus, setSystemState, systemState],
  );

  const navBrand = (
    <div className="flex items-center gap-2 border-b border-shell-line px-5 py-5">
      <img src="/logo-nav.png" alt="DoYouTrade" className="h-8 w-8 shrink-0 object-contain" />
      <span className="font-display text-lg text-shell-ink">DoYouTrade</span>
      {deploymentMode === "cloud" ? (
        <span className="rounded-md border border-soft-tag-border bg-soft-tag-bg px-1.5 py-0.5 text-xs font-semibold text-soft-tag-text">
          Cloud
        </span>
      ) : null}
    </div>
  );
  const navMenuItems = visibleNavTree(deploymentMode).map((entry) =>
    isNavGroup(entry)
      ? {
          key: entry.key,
          icon: entry.icon,
          label: entry.label,
          children: entry.children.map((leaf) => ({ key: leaf.key, label: leaf.label })),
        }
      : { key: entry.key, icon: entry.icon, label: entry.label },
  );
  const handleNavClick = (key: string) => {
    if (key in PATHS) {
      navigate(PATHS[key as ConsolePageKey]);
    }
    setNavDrawerOpen(false);
  };
  const navFootnotes = (
    <>
      <div className="mx-3 mt-1 rounded-xl border border-shell-line/60 bg-white/40 px-3 py-2 text-[11px] leading-snug text-shell-muted">
        仅供研究 / 教育用途，不构成投资建议；不荐股、不预测涨跌，据此操作风险自负。
      </div>
      <SidebarVersionBadge />
    </>
  );

  return (
    <Layout className="h-[100dvh] overflow-hidden bg-[radial-gradient(circle_at_0%_0%,#f9f1e3_0%,transparent_38%),radial-gradient(circle_at_95%_20%,#f2e6d8_0%,transparent_36%),#f4efe6]">
      <Layout.Sider
        width={232}
        collapsedWidth={0}
        breakpoint="lg"
        onBreakpoint={(broken) => {
          setIsNarrow(broken);
          if (broken) {
            setCollapsed(true);
          } else {
            setNavDrawerOpen(false);
            setCollapsed(localStorage.getItem("sidebar_collapsed") === "true");
          }
        }}
        collapsed={isNarrow ? true : collapsed}
        onCollapse={(val) => {
          setCollapsed(val);
          localStorage.setItem("sidebar_collapsed", String(val));
        }}
        className="!bg-[rgba(255,253,249,0.72)] !backdrop-blur"
        trigger={null}
      >
        {navBrand}
        <Menu
          mode="inline"
          selectedKeys={[selectedKey]}
          openKeys={openKeys}
          onOpenChange={(keys) => setOpenKeys(keys as string[])}
          items={navMenuItems}
          onClick={({ key }) => handleNavClick(key)}
          className="border-e-0 bg-transparent px-3 py-4"
        />
        {navFootnotes}
      </Layout.Sider>
      <Drawer
        placement="left"
        open={navDrawerOpen}
        onClose={() => setNavDrawerOpen(false)}
        width="min(280px, 84vw)"
        rootClassName="lg:hidden"
        styles={{ body: { padding: 0 } }}
        title={null}
        closable={false}
      >
        {navBrand}
        <Menu
          mode="inline"
          selectedKeys={[selectedKey]}
          openKeys={openKeys}
          onOpenChange={(keys) => setOpenKeys(keys as string[])}
          items={navMenuItems}
          onClick={({ key }) => handleNavClick(key)}
          className="border-e-0 bg-transparent px-3 py-4"
        />
        {navFootnotes}
      </Drawer>
      <Layout className="min-h-0">
        <Layout.Header className="flex h-auto items-center justify-between gap-2 border-b border-shell-line bg-transparent px-3 py-3 lg:px-5">
          <div className="flex min-w-0 items-center gap-2">
            <Button
              type="text"
              icon={(isNarrow ? !navDrawerOpen : collapsed) ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
              onClick={() => {
                if (isNarrow) {
                  setNavDrawerOpen(true);
                  return;
                }
                setCollapsed(!collapsed);
                localStorage.setItem("sidebar_collapsed", String(!collapsed));
              }}
              className="!flex !items-center"
            />
            <ApiHealthHeaderStatus health={health} />
          </div>
          <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
          <CloudUserMenu mode={deploymentMode} />
          <Button
            className="rounded-xl"
            onClick={async () => {
              try {
                await refresh();
                setPageRefreshToken((n) => n + 1);
              } catch (error: unknown) {
                const content = error instanceof Error ? error.message : String(error);
                message.error(`刷新失败：${content}`);
              }
            }}
            loading={loading}
            icon={<ReloadOutlined />}
            title="刷新"
            aria-label="刷新"
          >
            <span className="hidden sm:inline">刷新</span>
          </Button>
          </div>
        </Layout.Header>
        {/* 内容区自身滚动（外层 Layout 固定 100dvh），使全高页面能 flex 精确填充、
            不再依赖 calc(100dvh-Nrem) 魔法数字；侧边栏 / 顶栏随之固定不随内容滚动。 */}
        <Layout.Content className="flex min-h-0 flex-1 flex-col overflow-y-auto px-3 py-4 lg:px-5 lg:py-5">
          <UpdateBanner />
          {dataRefreshFailed && !loading ? (
            <Alert
              className="mb-4 rounded-2xl border border-shell-line"
              message="数据刷新失败"
              description="当前展示的数据已降级或清空，可能不是最新状态，请检查 API 连接后点击「刷新」重试。"
              type="warning"
              showIcon
            />
          ) : null}
          <PageRefreshContext.Provider value={pageRefreshToken}>
            {/* Defense-in-depth: a mode-restricted page reached by direct URL
                redirects to the copilot (ConsoleShell is the layout for all
                child routes, so guarding the Outlet covers every path). While
                the mode is still unknown, cloud-only pages render a pending
                Spin instead of redirecting (avoid bouncing cloud users). */}
            {isCloudHidden(selectedKey) || isLocalBlocked(selectedKey) ? (
              <Navigate to="/assistant" replace />
            ) : isModePending(selectedKey) ? (
              <div className="flex h-64 items-center justify-center">
                <Spin />
              </div>
            ) : (
              <Outlet context={outletContext} />
            )}
          </PageRefreshContext.Provider>
        </Layout.Content>
      </Layout>
    </Layout>
  );
}

export default function App() {
  return (
    <ConfigProvider
      locale={zhCN}
      button={{ autoInsertSpace: false }}
      theme={{
        token: {
          colorPrimary: "#c98536",
          borderRadius: 14,
          fontFamily: "'IBM Plex Sans', sans-serif",
          colorBgLayout: "#f4efe6",
        },
      }}
    >
      <SetupWizardGate />
      <Routes>
        <Route path="/" element={<Navigate to="/assistant" replace />} />
        <Route element={<ConsoleShell />}>
          <Route path="/agents" element={<RoutePage><AgentsOutlet /></RoutePage>} />
          <Route path="/cron_jobs" element={<RoutePage><CronJobsPage /></RoutePage>} />
          <Route path="/channels" element={<RoutePage><ChannelsPage /></RoutePage>} />
          <Route path="/assistant" element={<RoutePage><AssistantPage /></RoutePage>} />
          <Route path="/swarm" element={<RoutePage><SwarmPage /></RoutePage>} />
          <Route path="/tasks" element={<RoutePage><TasksOutlet /></RoutePage>} />
          <Route path="/tasks/:taskId" element={<RoutePage><TaskDetailPage /></RoutePage>} />
          <Route path="/accounts" element={<RoutePage><AccountsPage /></RoutePage>} />
          <Route path="/stocks" element={<RoutePage><StocksPage /></RoutePage>} />
          <Route path="/stocks/detail" element={<RoutePage><StockDetailPage /></RoutePage>} />
          <Route path="/watchlist" element={<RoutePage><WatchlistPage /></RoutePage>} />
          <Route path="/stock_monitor" element={<RoutePage><StockMonitorPage /></RoutePage>} />
          <Route path="/market_review" element={<RoutePage><MarketReviewPage /></RoutePage>} />
          <Route path="/strategies" element={<RoutePage><StrategiesPage /></RoutePage>} />
          <Route path="/knowledge" element={<RoutePage><KnowledgePage /></RoutePage>} />
          <Route path="/approvals" element={<RoutePage><ApprovalsOutlet /></RoutePage>} />
          <Route path="/model_invocations" element={<RoutePage><ModelInvocationsPage /></RoutePage>} />
          <Route path="/settings/models" element={<RoutePage><ModelSettingsOutlet /></RoutePage>} />
          <Route path="/settings" element={<RoutePage><SettingsPage /></RoutePage>} />
          <Route path="/data_console" element={<RoutePage><DataConsolePage /></RoutePage>} />
        </Route>
        <Route path="*" element={<Navigate to="/assistant" replace />} />
      </Routes>
    </ConfigProvider>
  );
}
