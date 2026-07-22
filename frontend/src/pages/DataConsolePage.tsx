import React from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Input,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Segmented,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";

import {
  ConsoleApiError,
  createConsoleKey,
  fetchConsoleMe,
  fetchConsoleUsage,
  listConsoleKeys,
  revokeConsoleKey,
  type ConsoleApiKey,
  type ConsoleMe,
  type ConsoleUsage,
  type ConsoleUsagePeriod,
  type CreateConsoleKeyResult,
} from "../consoleApi";
import { usePageRefreshToken } from "../pageRefreshContext";

/**
 * 云端「数据接入」模块（cloud-only）：API Keys / 用量 / 接入教程 三个 Tab，
 * 功能自 doyoutrade-cloud console SPA 的 KeysPage / UsagePage / GuidePage 并入。
 * 数据面走同域 `/api/console/v1/*`（consoleApi.ts），401 会话失效由 consoleApi
 * 统一触发 beginAuthRedirect 回登录入口，页面自身只处理业务错误。
 */

/**
 * 数据面 Base URL 定案为硬编码：console 域名（window.location.origin）的
 * `/api/cloud/*` 不经 dytc、会打到用户自己的 copilot 进程，不能用作行情数据
 * 接入地址 —— 必须指向独立的数据 API 域名。
 */
const CLOUD_DATA_BASE_URL = "https://api.doyoutrade.cloud";

const KEY_PLACEHOLDER = "dytc_xxxxxxxxxxxxxxxx";

function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", { hour12: false });
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : "操作失败，请重试";
}

// ---------- API Keys ----------

function KeysTab() {
  const pageRefreshToken = usePageRefreshToken();
  const [keys, setKeys] = React.useState<ConsoleApiKey[] | null>(null);
  const [pageError, setPageError] = React.useState<string | null>(null);

  // 创建弹窗
  const [showCreate, setShowCreate] = React.useState(false);
  const [newName, setNewName] = React.useState("");
  const [creating, setCreating] = React.useState(false);
  const [createError, setCreateError] = React.useState<string | null>(null);

  // 创建成功：full_key 只显示这一次
  const [created, setCreated] = React.useState<CreateConsoleKeyResult | null>(null);

  const load = React.useCallback(async () => {
    try {
      const res = await listConsoleKeys();
      setKeys(res.keys);
      setPageError(null);
    } catch (err) {
      setPageError(errorMessage(err));
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load, pageRefreshToken]);

  const openCreate = () => {
    setNewName("");
    setCreateError(null);
    setShowCreate(true);
  };

  const submitCreate = async () => {
    setCreating(true);
    setCreateError(null);
    try {
      const name = newName.trim();
      const res = await createConsoleKey(name === "" ? null : name);
      setShowCreate(false);
      setCreated(res);
      void load();
    } catch (err) {
      if (err instanceof ConsoleApiError && err.errorCode === "key_limit_reached") {
        setCreateError(err.message || "已达到 key 数量上限");
      } else {
        setCreateError(errorMessage(err));
      }
    } finally {
      setCreating(false);
    }
  };

  const submitRevoke = async (key: ConsoleApiKey) => {
    try {
      await revokeConsoleKey(key.id);
      message.success(`已吊销 ${key.key_prefix}…`);
      await load();
    } catch (err) {
      message.error(`吊销失败：${errorMessage(err)}`);
    }
  };

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <Typography.Text type="secondary">
          用于访问 DoYouTrade Cloud 行情 API 的凭证（dytc_ 前缀）
        </Typography.Text>
        <Button type="primary" onClick={openCreate}>
          创建 Key
        </Button>
      </div>

      {pageError ? (
        <Alert
          className="mb-4"
          type="error"
          showIcon
          message="加载 API Keys 失败"
          description={pageError}
          action={
            <Button size="small" onClick={() => void load()}>
              重试
            </Button>
          }
        />
      ) : (
        <Table
          rowKey="id"
          loading={keys === null}
          dataSource={keys ?? []}
          pagination={false}
          locale={{ emptyText: "还没有 API key，点击右上角「创建 Key」开始接入" }}
          columns={[
            {
              title: "Key 前缀",
              dataIndex: "key_prefix",
              render: (prefix: string) => <Typography.Text code>{prefix}…</Typography.Text>,
            },
            {
              title: "名称",
              dataIndex: "name",
              render: (name: string | null) =>
                name || <Typography.Text type="secondary">未命名</Typography.Text>,
            },
            {
              title: "状态",
              dataIndex: "status",
              render: (status: ConsoleApiKey["status"]) =>
                status === "active" ? <Tag color="green">active</Tag> : <Tag>revoked</Tag>,
            },
            {
              title: "创建时间",
              dataIndex: "created_at",
              render: (iso: string) => formatDateTime(iso),
            },
            {
              title: "最后使用",
              dataIndex: "last_used_at",
              render: (iso: string | null) => formatDateTime(iso),
            },
            {
              title: "操作",
              render: (_: unknown, key: ConsoleApiKey) =>
                key.status === "active" ? (
                  <Popconfirm
                    title="吊销 API Key"
                    description="吊销后该 key 立即失效，使用它的程序将无法继续访问 API。此操作不可撤销。"
                    okText="确认吊销"
                    okButtonProps={{ danger: true }}
                    cancelText="取消"
                    onConfirm={() => void submitRevoke(key)}
                  >
                    <Button size="small" danger aria-label={`吊销 ${key.key_prefix}`}>
                      吊销
                    </Button>
                  </Popconfirm>
                ) : null,
            },
          ]}
        />
      )}

      {/* 创建 key 弹窗 */}
      <Modal
        title="创建 API Key"
        open={showCreate}
        confirmLoading={creating}
        okText="创建"
        cancelText="取消"
        onOk={() => void submitCreate()}
        onCancel={() => {
          if (!creating) setShowCreate(false);
        }}
      >
        {createError ? (
          <Alert className="mb-3" type="error" showIcon message={createError} />
        ) : null}
        <Typography.Paragraph className="!mb-2">名称（可选，便于区分用途）</Typography.Paragraph>
        <Input
          placeholder="例如：本地开发 / 生产策略机"
          value={newName}
          maxLength={64}
          onChange={(e) => setNewName(e.target.value)}
          onPressEnter={() => {
            if (!creating) void submitCreate();
          }}
          aria-label="Key 名称"
        />
      </Modal>

      {/* 创建成功：full_key 一次性展示，不可误关（无遮罩关闭） */}
      <Modal
        title="Key 创建成功"
        open={created !== null}
        maskClosable={false}
        closable={false}
        okText="我已保存，关闭"
        cancelButtonProps={{ style: { display: "none" } }}
        onOk={() => setCreated(null)}
        onCancel={() => setCreated(null)}
      >
        <Alert
          className="mb-3"
          type="warning"
          showIcon
          message="完整 key 只显示这一次，请立即保存。关闭后将无法再次查看。"
        />
        {created ? (
          <Typography.Text code copyable data-testid="full-key-value">
            {created.full_key}
          </Typography.Text>
        ) : null}
      </Modal>
    </div>
  );
}

// ---------- 用量 ----------

function OperationTable({ period }: { period: ConsoleUsagePeriod }) {
  const ops = Object.entries(period.by_operation)
    .sort((a, b) => b[1].requests - a[1].requests)
    .map(([op, s]) => ({ op, ...s }));
  return (
    <Table
      rowKey="op"
      size="small"
      dataSource={ops}
      pagination={false}
      locale={{ emptyText: "暂无数据" }}
      columns={[
        {
          title: "Operation",
          dataIndex: "op",
          render: (op: string) => <Typography.Text code>{op}</Typography.Text>,
        },
        { title: "请求数", dataIndex: "requests", align: "right" },
        { title: "缓存命中", dataIndex: "cache_hits", align: "right" },
        {
          title: "错误数",
          dataIndex: "errors",
          align: "right",
          render: (errors: number) =>
            errors > 0 ? <Typography.Text type="danger">{errors}</Typography.Text> : errors,
        },
      ]}
    />
  );
}

function PeriodStats({ title, period }: { title: string; period: ConsoleUsagePeriod }) {
  return (
    <Card size="small" title={title} className="h-full">
      <Row gutter={16}>
        <Col span={8}>
          <Statistic title="请求数" value={period.requests} />
        </Col>
        <Col span={8}>
          <Statistic title="缓存命中" value={period.cache_hits} />
        </Col>
        <Col span={8}>
          <Statistic
            title="错误数"
            value={period.errors}
            valueStyle={period.errors > 0 ? { color: "#cf1322" } : undefined}
          />
        </Col>
      </Row>
    </Card>
  );
}

function UsageTab() {
  const pageRefreshToken = usePageRefreshToken();
  const [usage, setUsage] = React.useState<ConsoleUsage | null>(null);
  const [me, setMe] = React.useState<ConsoleMe | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [opPeriod, setOpPeriod] = React.useState<"today" | "month">("today");

  const load = React.useCallback(async () => {
    try {
      // 并行拉 usage + me；me 只用于展示套餐名，失败可容忍（不阻塞用量渲染）。
      const [usageRes, meRes] = await Promise.all([
        fetchConsoleUsage(),
        fetchConsoleMe().catch(() => null),
      ]);
      setUsage(usageRes);
      setMe(meRes);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load, pageRefreshToken]);

  if (error) {
    return (
      <Alert
        type="error"
        showIcon
        message="加载用量失败"
        description={error}
        action={
          <Button size="small" onClick={() => void load()}>
            重试
          </Button>
        }
      />
    );
  }

  if (!usage) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Spin />
      </div>
    );
  }

  const { quota } = usage;
  const pct =
    quota.daily_requests > 0
      ? Math.min(100, (quota.used_today / quota.daily_requests) * 100)
      : 0;
  const strokeColor = pct >= 95 ? "#cf1322" : pct >= 80 ? "#d46b08" : undefined;

  return (
    <div className="flex flex-col gap-4">
      <Typography.Text type="secondary">
        统计日期 {usage.date}
        {me ? ` · 套餐 ${me.user.plan.plan_name}` : ""}
      </Typography.Text>

      <Card
        size="small"
        title="今日配额"
        extra={
          <Typography.Text type="secondary">
            {quota.used_today} / {quota.daily_requests}
          </Typography.Text>
        }
      >
        <Progress
          percent={Number(pct.toFixed(1))}
          strokeColor={strokeColor}
          status="normal"
        />
        <div className="flex justify-between">
          <Typography.Text type="secondary">已用 {quota.used_today}</Typography.Text>
          <Typography.Text type="secondary">剩余 {quota.remaining_today}</Typography.Text>
        </div>
      </Card>

      <Row gutter={16}>
        <Col xs={24} md={12}>
          <PeriodStats title="今日" period={usage.today} />
        </Col>
        <Col xs={24} md={12}>
          <PeriodStats title="本月" period={usage.month} />
        </Col>
      </Row>

      <Card
        size="small"
        title="按 Operation 统计"
        extra={
          <Segmented
            options={[
              { label: "今日", value: "today" },
              { label: "本月", value: "month" },
            ]}
            value={opPeriod}
            onChange={(value) => setOpPeriod(value as "today" | "month")}
          />
        }
      >
        <OperationTable period={opPeriod === "today" ? usage.today : usage.month} />
      </Card>

      <Typography.Text type="secondary">
        配额不够用？联系升级套餐（企业套餐支持更高频率、更大日配额与更多 WebSocket 连接）
      </Typography.Text>
    </div>
  );
}

// ---------- 接入教程 ----------

const CURL_CALENDAR = `curl -H "Authorization: Bearer ${KEY_PLACEHOLDER}" \\
  "${CLOUD_DATA_BASE_URL}/api/cloud/v1/trading-calendar"`;

const CURL_HELLO = `curl -H "Authorization: Bearer ${KEY_PLACEHOLDER}" \\
  "${CLOUD_DATA_BASE_URL}/api/cloud/v1/hello"`;

function GuideStep({ no, title, children }: { no: number; title: string; children: React.ReactNode }) {
  return (
    <div className="mb-6 flex gap-3 last:mb-0">
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-soft-tag-bg font-semibold text-soft-tag-text">
        {no}
      </div>
      <div className="min-w-0 flex-1">
        <Typography.Title level={5} className="!mt-0.5">
          {title}
        </Typography.Title>
        {children}
      </div>
    </div>
  );
}

function GuideTab() {
  return (
    <Card size="small">
      <Typography.Paragraph type="secondary">
        四步完成 DoYouTrade 接入 DoYouTrade Cloud 行情源
      </Typography.Paragraph>

      <GuideStep no={1} title="创建 API Key">
        <Typography.Paragraph>
          前往「API Keys」标签页点击「创建 Key」。完整 key（
          <Typography.Text code>dytc_</Typography.Text> 前缀）只在创建成功时显示一次，
          请立即复制并妥善保存。
        </Typography.Paragraph>
      </GuideStep>

      <GuideStep no={2} title="在本地 DoYouTrade 客户端中新建账户">
        <Typography.Paragraph>
          打开本地 DoYouTrade 客户端的<strong>账户设置</strong>（Accounts 页），新建一个账户并按如下填写：
        </Typography.Paragraph>
        <ul className="list-disc pl-5">
          <li>
            Base URL：<Typography.Text code>{CLOUD_DATA_BASE_URL}</Typography.Text>
          </li>
          <li>
            Token：<Typography.Text code>{KEY_PLACEHOLDER}</Typography.Text>（替换为你的完整 key）
          </li>
          <li>
            模式：<Typography.Text code>mock</Typography.Text>
          </li>
        </ul>
      </GuideStep>

      <GuideStep no={3} title="用 curl 验证连通性">
        <Typography.Paragraph>把示例中的 token 替换为你的完整 key：</Typography.Paragraph>
        <pre className="overflow-x-auto rounded-lg bg-black/80 p-3 text-xs leading-relaxed text-white">
          {`# 查询交易日历\n${CURL_CALENDAR}`}
        </pre>
        <pre className="overflow-x-auto rounded-lg bg-black/80 p-3 text-xs leading-relaxed text-white">
          {`# 连通性 / 鉴权自检\n${CURL_HELLO}`}
        </pre>
        <Typography.Paragraph>两条请求均返回 200 即表示 key 有效、网关可达。</Typography.Paragraph>
      </GuideStep>

      <GuideStep no={4} title="注意事项">
        <ul className="list-disc pl-5">
          <li>
            DoYouTrade Cloud 只提供<strong>行情数据</strong>：交易类接口（下单、撤单、持仓等）不经过云端。
          </li>
          <li>
            需要实盘交易时，请在本地部署 <Typography.Text code>qmt-proxy</Typography.Text>
            ，由本地环境直连 QMT 完成交易。
          </li>
          <li>请勿将 API key 提交到代码仓库或分享给他人；泄露后可随时在「API Keys」页吊销。</li>
          <li>各套餐有频率与日配额限制，可在「用量」页查看当前消耗。</li>
        </ul>
      </GuideStep>
    </Card>
  );
}

// ---------- 页面 ----------

export function DataConsolePage() {
  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h2 className="m-0 text-xl font-semibold">数据接入</h2>
      </div>
      <Tabs
        defaultActiveKey="keys"
        items={[
          { key: "keys", label: "API Keys", children: <KeysTab /> },
          { key: "usage", label: "用量", children: <UsageTab /> },
          { key: "guide", label: "接入教程", children: <GuideTab /> },
        ]}
      />
    </div>
  );
}
