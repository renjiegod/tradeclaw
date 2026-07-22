import {
  DeleteOutlined,
  PlusOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Collapse,
  Divider,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  ApiError,
  getConfig,
  getQmtProxyConfig,
  updateConfig,
  updateQmtProxyConfig,
} from "../api";
import { PageIntro } from "../components/PageIntro";
import { ToolbarButton } from "../components/ToolbarButton";
import { UpdateSection } from "../components/UpdateFlow";
import { usePageRefreshToken } from "../pageRefreshContext";
import type {
  ConfigUpdateResponse,
  QmtClientConfig,
  QmtProxyConfigResponse,
  DoyoutradeConfigResponse,
  DoyoutradeConfigValues,
} from "../types";

// --------------------------------------------------------------------------
// Shared helpers
// --------------------------------------------------------------------------

/** Leaf paths whose value is a masked secret on the doyoutrade config. */
const DOYOUTRADE_SECRET_PATHS = new Set([
  "data.tushare.token",
  "qmt_proxy.local_token",
  "feishu.app_secret",
  "feishu.encrypt_key",
  "feishu.verification_token",
]);

/** Leaf paths whose value is a masked secret on the qmt-proxy config. */
const QMT_SECRET_PATHS = new Set(["security.api_keys"]);

const MASK = "********";

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    return a.every((x, i) => deepEqual(x, b[i]));
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const ak = Object.keys(a);
    const bk = Object.keys(b);
    if (ak.length !== bk.length) return false;
    return ak.every((k) => deepEqual(a[k], b[k]));
  }
  return false;
}

/**
 * Build a minimal deep-merge patch of only the leaves that changed vs the
 * loaded baseline. Secret leaves are only emitted when the user explicitly
 * re-entered them (path in ``editingSecrets``) with a non-empty, non-masked
 * value — so leaving a secret as ``********`` never overwrites the stored one.
 */
function buildConfigPatch(
  current: Record<string, unknown>,
  original: Record<string, unknown> | undefined,
  secretPaths: Set<string>,
  editingSecrets: Set<string>,
  prefix = "",
): Record<string, unknown> {
  const patch: Record<string, unknown> = {};
  for (const key of Object.keys(current)) {
    const path = prefix ? `${prefix}.${key}` : key;
    const cur = current[key];
    const orig = original?.[key];

    if (secretPaths.has(path)) {
      if (editingSecrets.has(path)) {
        if (Array.isArray(cur)) {
          if (cur.length > 0) patch[key] = cur;
        } else {
          const v = typeof cur === "string" ? cur.trim() : cur;
          if (v != null && v !== "" && v !== MASK) patch[key] = v;
        }
      }
      continue;
    }

    if (isPlainObject(cur)) {
      const sub = buildConfigPatch(
        cur,
        isPlainObject(orig) ? orig : undefined,
        secretPaths,
        editingSecrets,
        path,
      );
      if (Object.keys(sub).length > 0) patch[key] = sub;
      continue;
    }

    if (cur === undefined) continue;
    if (!deepEqual(cur, orig)) patch[key] = cur;
  }
  return patch;
}

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// --------------------------------------------------------------------------
// Small presentational bits
// --------------------------------------------------------------------------

function FieldLabel({
  text,
  restart,
  help,
}: {
  text: string;
  restart?: boolean;
  help?: string;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      {text}
      {help ? (
        <Tooltip title={help}>
          <QuestionCircleOutlined className="text-shell-muted" />
        </Tooltip>
      ) : null}
      {restart ? (
        <Tag color="orange" className="!m-0 !text-[11px] !leading-4">
          需重启
        </Tag>
      ) : null}
    </span>
  );
}

function RestartBanner({
  fields,
  proxy,
  onClose,
}: {
  fields: string[];
  proxy?: boolean;
  onClose: () => void;
}) {
  return (
    <Alert
      className="mb-4 rounded-2xl border border-shell-line"
      type="warning"
      showIcon
      closable
      onClose={onClose}
      message={proxy ? "保存成功，需重启 qmt-proxy 后生效" : "保存成功，需重启后生效"}
      description={
        <div>
          <div className="mb-1 text-sm">以下字段的改动需要重启才会生效：</div>
          <Space size={[6, 6]} wrap>
            {fields.map((f) => (
              <Tag key={f} color="orange" className="font-mono text-xs">
                {f}
              </Tag>
            ))}
          </Space>
        </div>
      }
    />
  );
}

/** Form-bound secret input: masked until the user opts to re-enter it. */
function SecretInput({
  value,
  onChange,
  isSet,
  editing,
  onToggle,
}: {
  value?: string;
  onChange?: (v: string) => void;
  isSet: boolean;
  editing: boolean;
  onToggle: (next: boolean) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      {editing ? (
        <Input.Password
          className="flex-1"
          value={value ?? ""}
          onChange={(e) => onChange?.(e.target.value)}
          placeholder="输入新值（保存后覆盖旧值）"
          autoComplete="new-password"
        />
      ) : (
        <Input
          className="flex-1"
          value={isSet ? MASK : ""}
          disabled
          placeholder="未配置"
        />
      )}
      <Button
        size="small"
        onClick={() => {
          const next = !editing;
          onChange?.(next ? "" : isSet ? MASK : "");
          onToggle(next);
        }}
      >
        {editing ? "取消" : isSet ? "重新填写" : "填写"}
      </Button>
    </div>
  );
}

// --------------------------------------------------------------------------
// Options
// --------------------------------------------------------------------------

const LOG_LEVEL_OPTIONS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((v) => ({
  value: v,
  label: v,
}));
const XTQUANT_MODE_OPTIONS = ["mock", "dev", "prod"].map((v) => ({ value: v, label: v }));
const INTERVAL_OPTIONS = ["1m", "5m", "15m", "30m", "60m", "1d", "1w"].map((v) => ({
  value: v,
  label: v,
}));
const FEISHU_DOMAIN_OPTIONS = [
  { value: "feishu", label: "feishu（飞书）" },
  { value: "lark", label: "lark（国际版）" },
];

// --------------------------------------------------------------------------
// Collapse grouping
// --------------------------------------------------------------------------
//
// The ~114 flat Form.Items are grouped into Collapse panels per config
// section. Every panel uses ``forceRender: true`` so all fields stay
// registered on the Form (and remain reachable in tests) even while
// collapsed. Which top-level config sections live in which panel is recorded
// below so a validation failure inside a collapsed panel auto-expands it
// (mirrors CreateAgentCard's ADVANCED_PANEL_FIELDS + onFinishFailed).

/** Panel key → top-level config sections it contains (doyoutrade tab). */
const SYSTEM_PANEL_ROOTS: Record<string, string[]> = {
  data: ["data"],
  market_data: ["market_data"],
  auto_update: ["auto_update"],
  feishu: ["feishu"],
  qmt_proxy: ["qmt_proxy"],
  server: ["server"],
  database: ["database"],
  observability: ["observability"],
  review: ["review"],
  retention: ["retention"],
  assistant: ["assistant"],
};
/** Common panels users actually edit — expanded by default. */
const SYSTEM_DEFAULT_OPEN_PANELS = ["data", "market_data", "auto_update"];

/** Panel key → top-level config sections it contains (qmt-proxy tab). */
const QMT_PANEL_ROOTS: Record<string, string[]> = {
  xtquant: ["xtquant"],
  security: ["security"],
  logging: ["logging"],
  grpc: ["grpc"],
  app: ["app"],
};
const QMT_DEFAULT_OPEN_PANELS = ["xtquant", "security"];

type ValidateErrorField = { name: Array<string | number> };

/** Narrow the rejection of ``form.validateFields()`` to its errorFields shape. */
function isValidateError(e: unknown): e is { errorFields: ValidateErrorField[] } {
  return isPlainObject(e) && Array.isArray((e as { errorFields?: unknown }).errorFields);
}

/** Map failed field name-paths back to the Collapse panels that contain them. */
function panelsContainingErrors(
  errorFields: ValidateErrorField[],
  panelRoots: Record<string, string[]>,
): string[] {
  const roots = new Set(errorFields.map((f) => String(f.name[0] ?? "")));
  return Object.entries(panelRoots)
    .filter(([, sections]) => sections.some((s) => roots.has(s)))
    .map(([key]) => key);
}

/** Collapse panel header: section title + a short "what's inside / can I skip it" hint. */
function PanelLabel({ title, hint }: { title: string; hint?: string }) {
  return (
    <span className="inline-flex flex-wrap items-baseline gap-x-2">
      <span className="font-medium">{title}</span>
      {hint ? (
        <Typography.Text type="secondary" className="text-xs">
          {hint}
        </Typography.Text>
      ) : null}
    </span>
  );
}

// --------------------------------------------------------------------------
// Form value projections
// --------------------------------------------------------------------------

function toSystemFormValues(v: DoyoutradeConfigValues): Record<string, unknown> {
  return {
    server: { host: v.server.host, port: v.server.port, tick_seconds: v.server.tick_seconds },
    data: {
      default_provider: v.data.default_provider,
      tushare: {
        token: v.data.tushare.token_set ? MASK : "",
        timeout_seconds: v.data.tushare.timeout_seconds,
      },
    },
    market_data: {
      database_url: v.market_data.database_url,
      enabled_intervals: [...(v.market_data.enabled_intervals ?? [])],
      lookback_years: v.market_data.lookback_years,
      default_provider: v.market_data.default_provider,
      sync_on_startup: v.market_data.sync_on_startup,
      sync_concurrency: v.market_data.sync_concurrency,
      provider_rate_limit_per_second: v.market_data.provider_rate_limit_per_second,
      sync_full_market: v.market_data.sync_full_market,
    },
    observability: {
      service_name: v.observability.service_name,
      log_level: v.observability.log_level,
      console_enabled: v.observability.console_enabled,
      tracing_enabled: v.observability.tracing_enabled,
    },
    review: { symbol_scope_mode: v.review.symbol_scope_mode },
    retention: {
      enabled: v.retention.enabled,
      observability_ttl_days: v.retention.observability_ttl_days,
      prune_interval_hours: v.retention.prune_interval_hours,
      prune_on_startup: v.retention.prune_on_startup,
    },
    assistant: {
      tool_result_max_chars: v.assistant.tool_result_max_chars,
      approval_allowlist: {
        rule_keys: [...(v.assistant.approval_allowlist?.rule_keys ?? [])],
        command_prefixes: [...(v.assistant.approval_allowlist?.command_prefixes ?? [])],
      },
    },
    auto_update: {
      enabled: v.auto_update.enabled,
      check_interval_hours: v.auto_update.check_interval_hours,
      repo: v.auto_update.repo,
    },
    database: { url: v.database.url, echo: v.database.echo, pool_pre_ping: v.database.pool_pre_ping },
    qmt_proxy: {
      host: v.qmt_proxy.host,
      port: v.qmt_proxy.port,
      mode: v.qmt_proxy.mode,
      grpc_enabled: v.qmt_proxy.grpc_enabled,
      local_token: v.qmt_proxy.local_token_set ? MASK : "",
    },
    feishu: {
      enabled: v.feishu.enabled,
      app_id: v.feishu.app_id,
      domain: v.feishu.domain,
      app_secret: v.feishu.app_secret_set ? MASK : "",
      encrypt_key: v.feishu.encrypt_key_set ? MASK : "",
      verification_token: v.feishu.verification_token_set ? MASK : "",
    },
  };
}

function normClient(c: Partial<QmtClientConfig> | undefined): Record<string, unknown> {
  return {
    client_id: (c?.client_id ?? "").toString().trim(),
    name: c?.name ?? null,
    qmt_userdata_path: c?.qmt_userdata_path ?? null,
    mode: c?.mode ?? null,
    allow_real_trading: !!c?.allow_real_trading,
    is_data_source: !!c?.is_data_source,
  };
}

function toQmtFormValues(v: QmtProxyConfigResponse["values"]): Record<string, unknown> {
  return {
    xtquant: {
      mode: v.xtquant.mode,
      data: { qmt_userdata_path: v.xtquant.data?.qmt_userdata_path ?? "" },
      trading: { allow_real_trading: v.xtquant.trading?.allow_real_trading ?? false },
      clients: (v.xtquant.clients ?? []).map((c) => ({
        client_id: c.client_id ?? "",
        name: c.name ?? "",
        qmt_userdata_path: c.qmt_userdata_path ?? "",
        mode: c.mode ?? undefined,
        allow_real_trading: !!c.allow_real_trading,
        is_data_source: !!c.is_data_source,
      })),
      default_client_id: v.xtquant.default_client_id ?? undefined,
      data_source_client_id: v.xtquant.data_source_client_id ?? undefined,
    },
    security: { api_keys: v.security.api_keys_set ? [MASK] : [] },
    logging: { level: v.logging.level },
    grpc: { enabled: v.grpc.enabled, host: v.grpc.host, port: v.grpc.port },
    app: { host: v.app.host, port: v.app.port },
  };
}

// --------------------------------------------------------------------------
// System config tab (doyoutrade global)
// --------------------------------------------------------------------------

function SystemConfigTab() {
  const pageRefreshToken = usePageRefreshToken();
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [data, setData] = useState<DoyoutradeConfigResponse | null>(null);
  const [baseline, setBaseline] = useState<Record<string, unknown> | null>(null);
  const [editingSecrets, setEditingSecrets] = useState<Set<string>>(new Set());
  const [restartFields, setRestartFields] = useState<string[] | null>(null);
  const [openPanels, setOpenPanels] = useState<string[]>(() => [...SYSTEM_DEFAULT_OPEN_PANELS]);

  const restartSet = useMemo(
    () => new Set(data?.restart_required_fields ?? []),
    [data],
  );
  const isRestart = useCallback((path: string) => restartSet.has(path), [restartSet]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getConfig();
      setData(res);
      const projected = toSystemFormValues(res.values);
      setBaseline(projected);
      form.setFieldsValue(projected);
      setEditingSecrets(new Set());
      setLoadError(null);
    } catch (e) {
      setLoadError(errorText(e));
    } finally {
      setLoading(false);
    }
  }, [form]);

  useEffect(() => {
    void load();
  }, [load, pageRefreshToken]);

  const toggleSecret = (path: string, next: boolean) => {
    setEditingSecrets((prev) => {
      const copy = new Set(prev);
      if (next) copy.add(path);
      else copy.delete(path);
      return copy;
    });
  };

  const onSave = async () => {
    let values: Record<string, unknown>;
    try {
      values = await form.validateFields();
    } catch (e) {
      // antd renders the inline field errors; here we only make sure they are
      // not hidden inside a collapsed panel, then scroll to the first one.
      if (isValidateError(e) && e.errorFields.length > 0) {
        const panels = panelsContainingErrors(e.errorFields, SYSTEM_PANEL_ROOTS);
        if (panels.length > 0) {
          setOpenPanels((prev) => Array.from(new Set([...prev, ...panels])));
        }
        form.scrollToField(e.errorFields[0]!.name, { behavior: "smooth", block: "center" });
      }
      return;
    }
    if (!baseline) return;
    const patch = buildConfigPatch(values, baseline, DOYOUTRADE_SECRET_PATHS, editingSecrets);
    if (Object.keys(patch).length === 0) {
      message.info("没有需要保存的变更");
      return;
    }
    setSaving(true);
    try {
      const res: ConfigUpdateResponse = await updateConfig(patch);
      message.success("已保存系统配置");
      setRestartFields(res.restart_required ? res.restart_fields : null);
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.errorCode === "invalid_config") {
        const field = (e.detail as { field?: string } | null)?.field;
        message.error(`配置校验失败${field ? `（${field}）` : ""}：${e.message}`);
      } else {
        message.error(`保存失败：${errorText(e)}`);
      }
    } finally {
      setSaving(false);
    }
  };

  const secretInput = (path: string, isSet: boolean) => (
    <SecretInput
      isSet={isSet}
      editing={editingSecrets.has(path)}
      onToggle={(next) => toggleSecret(path, next)}
    />
  );

  if (loadError) {
    return (
      <Alert
        className="rounded-2xl border border-shell-line"
        type="error"
        showIcon
        message="加载系统配置失败"
        description={loadError}
        action={
          <Button size="small" onClick={() => void load()}>
            重试
          </Button>
        }
      />
    );
  }

  return (
    <div>
      {restartFields ? (
        <RestartBanner fields={restartFields} onClose={() => setRestartFields(null)} />
      ) : null}
      <div className="mb-3 flex items-center justify-between gap-2">
        <Typography.Text type="secondary" className="text-sm">
          配置写入 <Typography.Text code>{data?.path ?? "~/.doyoutrade/config.yaml"}</Typography.Text>
          （YAML 保留注释）。标「需重启」的字段保存后需重启进程才生效；密钥字段保存时留空则不改动。
        </Typography.Text>
        <Space>
          <ToolbarButton icon={<ReloadOutlined />} onClick={() => void load()} loading={loading} label="刷新" />
          <ToolbarButton type="primary" icon={<SaveOutlined />} onClick={() => void onSave()} loading={saving} label="保存系统配置" />
        </Space>
      </div>

      <Form form={form} layout="vertical" disabled={loading}>
        {/* 分组折叠降低填写负担：常用面板默认展开，冷门面板默认收起。
            所有面板 forceRender，字段始终注册；校验失败会自动展开所在面板。 */}
        <Collapse
          className="rounded-2xl"
          activeKey={openPanels}
          onChange={(keys) => setOpenPanels((Array.isArray(keys) ? keys : [keys]).map(String))}
          items={[
            {
              key: "server",
              forceRender: true,
              label: (
                <PanelLabel
                  title="server · 服务端口"
                  hint="API 监听地址与轮询间隔，保持默认即可；改动需重启"
                />
              ),
              children: (
                <>
          <Form.Item
            name={["server", "host"]}
            label={
              <FieldLabel
                text="host"
                restart={isRestart("server.host")}
                help="API 服务监听的网卡地址；0.0.0.0 表示监听所有网卡，仅本机访问可填 127.0.0.1"
              />
            }
          >
            <Input data-testid="cfg-server-host" placeholder="0.0.0.0" />
          </Form.Item>
          <Form.Item
            name={["server", "port"]}
            label={
              <FieldLabel
                text="port"
                restart={isRestart("server.port")}
                help="API 服务监听端口"
              />
            }
          >
            <InputNumber className="w-full" min={1} max={65535} />
          </Form.Item>
          <Form.Item
            name={["server", "tick_seconds"]}
            label={
              <FieldLabel
                text="tick_seconds"
                restart={isRestart("server.tick_seconds")}
                help="worker 主循环轮询间隔（秒）；越小响应越快但空转开销越高"
              />
            }
          >
            <InputNumber className="w-full" min={0} step={0.5} />
          </Form.Item>
                </>
              ),
            },
            {
              key: "data",
              forceRender: true,
              label: (
                <PanelLabel
                  title="data · 数据源（TuShare）"
                  hint="常用：默认数据 provider 与 TuShare token"
                />
              ),
              children: (
                <>
          <Form.Item
            name={["data", "default_provider"]}
            label={
              <FieldLabel
                text="default_provider"
                restart={isRestart("data.default_provider")}
                help="实时/历史取数的默认数据源；auto 会按可用性自动选择，也可指定 qmt / tushare / akshare 等固定源"
              />
            }
          >
            <Input placeholder="auto / qmt / tushare / akshare …" />
          </Form.Item>
          <Form.Item
            name={["data", "tushare", "token"]}
            label={
              <FieldLabel
                text="tushare.token"
                restart={isRestart("data.tushare.token")}
                help="TuShare Pro 接口 token，使用 TuShare 作为数据源时必填；已配置则脱敏显示，留空不改动"
              />
            }
          >
            {secretInput("data.tushare.token", data?.values.data.tushare.token_set ?? false)}
          </Form.Item>
          <Form.Item
            name={["data", "tushare", "timeout_seconds"]}
            label={
              <FieldLabel
                text="tushare.timeout_seconds"
                restart={isRestart("data.tushare.timeout_seconds")}
                help="调用 TuShare 接口的超时时间（秒），超时会触发失败重试或切换数据源"
              />
            }
          >
            <InputNumber className="w-full" min={1} />
          </Form.Item>
                </>
              ),
            },
            {
              key: "market_data",
              forceRender: true,
              label: (
                <PanelLabel
                  title="market_data · 行情缓存与同步"
                  hint="常用：K 线周期、回看年限与同步行为"
                />
              ),
              children: (
                <>
          <Form.Item
            name={["market_data", "database_url"]}
            label={
              <FieldLabel
                text="database_url"
                restart={isRestart("market_data.database_url")}
                help="行情缓存库连接串，独立于主库；本地默认用 SQLite 文件，多进程/大数据量可换 PostgreSQL"
              />
            }
          >
            <Input placeholder="sqlite:///… / postgresql://…" />
          </Form.Item>
          <Form.Item
            name={["market_data", "enabled_intervals"]}
            label={
              <FieldLabel
                text="enabled_intervals"
                restart={isRestart("market_data.enabled_intervals")}
                help="需要同步与缓存的 K 线周期列表（如 1d、5m）；未启用的周期策略无法读取"
              />
            }
          >
            <Select mode="tags" options={INTERVAL_OPTIONS} placeholder="1d, 5m …" />
          </Form.Item>
          <Form.Item
            name={["market_data", "lookback_years"]}
            label={
              <FieldLabel
                text="lookback_years"
                restart={isRestart("market_data.lookback_years")}
                help="首次同步/回测拉取历史行情回看的年数，越大初次同步耗时越长"
              />
            }
          >
            <InputNumber className="w-full" min={0} />
          </Form.Item>
          <Form.Item
            name={["market_data", "default_provider"]}
            label={
              <FieldLabel
                text="default_provider"
                restart={isRestart("market_data.default_provider")}
                help="行情同步任务使用的默认数据源；auto 自动选择，也可固定为 qmt / tushare 等"
              />
            }
          >
            <Input placeholder="auto / qmt / tushare …" />
          </Form.Item>
          <Form.Item
            name={["market_data", "sync_on_startup"]}
            label={
              <FieldLabel
                text="sync_on_startup"
                restart={isRestart("market_data.sync_on_startup")}
                help="进程启动时是否自动触发一次行情同步"
              />
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={["market_data", "sync_concurrency"]}
            label={
              <FieldLabel
                text="sync_concurrency"
                restart={isRestart("market_data.sync_concurrency")}
                help="行情同步的并发任务数；调大提速但可能触发数据源限流"
              />
            }
          >
            <InputNumber className="w-full" min={1} />
          </Form.Item>
          <Form.Item
            name={["market_data", "provider_rate_limit_per_second"]}
            label={
              <FieldLabel
                text="provider_rate_limit_per_second"
                restart={isRestart("market_data.provider_rate_limit_per_second")}
                help="对数据源发起请求的每秒限速，避免触发数据源的调用频率限制"
              />
            }
          >
            <InputNumber className="w-full" min={0} step={0.5} />
          </Form.Item>
          <Form.Item
            name={["market_data", "sync_full_market"]}
            label={
              <FieldLabel
                text="sync_full_market"
                restart={isRestart("market_data.sync_full_market")}
                help="开启后同步全市场标的，而非仅同步当前已订阅/持仓涉及的标的"
              />
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
                </>
              ),
            },
            {
              key: "observability",
              forceRender: true,
              label: (
                <PanelLabel
                  title="observability · 日志与追踪"
                  hint="日志级别与追踪开关，保持默认即可"
                />
              ),
              children: (
                <>
          <Form.Item
            name={["observability", "service_name"]}
            label={
              <FieldLabel
                text="service_name"
                restart={isRestart("observability.service_name")}
                help="上报给 OpenTelemetry 的服务名，用于在追踪后端区分不同实例/环境"
              />
            }
          >
            <Input />
          </Form.Item>
          <Form.Item
            name={["observability", "log_level"]}
            label={
              <FieldLabel
                text="log_level"
                restart={isRestart("observability.log_level")}
                help="全局日志输出级别；DEBUG 最详细，排查问题临时调低，日常建议 INFO"
              />
            }
          >
            <Select options={LOG_LEVEL_OPTIONS} />
          </Form.Item>
          <Form.Item
            name={["observability", "console_enabled"]}
            label={
              <FieldLabel
                text="console_enabled"
                restart={isRestart("observability.console_enabled")}
                help="是否把日志同时输出到控制台（stdout），关闭后只写入日志文件"
              />
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={["observability", "tracing_enabled"]}
            label={
              <FieldLabel
                text="tracing_enabled"
                restart={isRestart("observability.tracing_enabled")}
                help="是否开启 OTel 分布式追踪（span 导出）；关闭后调试页看不到 trace/span 详情"
              />
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
                </>
              ),
            },
            {
              key: "review",
              forceRender: true,
              label: (
                <PanelLabel title="review · 复盘" hint="复盘 symbol 范围，保持默认即可" />
              ),
              children: (
                <>
          <Form.Item
            name={["review", "symbol_scope_mode"]}
            label={
              <FieldLabel
                text="symbol_scope_mode"
                restart={isRestart("review.symbol_scope_mode")}
                help="复盘功能统计标的范围的模式，一般保持默认即可"
              />
            }
          >
            <Input placeholder="default …" />
          </Form.Item>
                </>
              ),
            },
            {
              key: "retention",
              forceRender: true,
              label: (
                <PanelLabel
                  title="retention · 可观测性数据保留"
                  hint="观测数据自动清理，保持默认即可"
                />
              ),
              children: (
                <>
          <Form.Item
            name={["retention", "enabled"]}
            label={
              <FieldLabel
                text="enabled"
                restart={isRestart("retention.enabled")}
                help="是否开启可观测性数据（span/调试事件等）的定时自动清理"
              />
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={["retention", "observability_ttl_days"]}
            label={
              <FieldLabel
                text="observability_ttl_days"
                restart={isRestart("retention.observability_ttl_days")}
                help="可观测性数据（trace/span/调试会话等）保留天数，超期数据会被清理任务删除"
              />
            }
          >
            <InputNumber className="w-full" min={0} />
          </Form.Item>
          <Form.Item
            name={["retention", "prune_interval_hours"]}
            label={
              <FieldLabel
                text="prune_interval_hours"
                restart={isRestart("retention.prune_interval_hours")}
                help="清理任务的运行间隔（小时）"
              />
            }
          >
            <InputNumber className="w-full" min={0} />
          </Form.Item>
          <Form.Item
            name={["retention", "prune_on_startup"]}
            label={
              <FieldLabel
                text="prune_on_startup"
                restart={isRestart("retention.prune_on_startup")}
                help="进程启动时是否立即执行一次清理，而不是等到下一个间隔"
              />
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
                </>
              ),
            },
            {
              key: "assistant",
              forceRender: true,
              label: (
                <PanelLabel
                  title="assistant · 助手"
                  hint="工具结果截断与高危操作审批白名单"
                />
              ),
              children: (
                <>
          <Form.Item
            name={["assistant", "tool_result_max_chars"]}
            label={
              <FieldLabel
                text="tool_result_max_chars"
                restart={isRestart("assistant.tool_result_max_chars")}
                help="assistant 工具调用返回结果注入对话上下文前的最大字符数，超出会被截断，避免撑爆上下文"
              />
            }
          >
            <InputNumber className="w-full" min={0} />
          </Form.Item>
          <Form.Item
            name={["assistant", "approval_allowlist", "rule_keys"]}
            label={
              <FieldLabel
                text="approval_allowlist.rule_keys"
                restart={false}
                help="持久记住的审批规则 key（如 task_start）。对话里点「写入 settings」可自动追加。热生效，无需重启。"
              />
            }
          >
            <Select
              mode="tags"
              className="w-full"
              tokenSeparators={[","]}
              placeholder="例如 task_start"
              allowClear
            />
          </Form.Item>
          <Form.Item
            name={["assistant", "approval_allowlist", "command_prefixes"]}
            label={
              <FieldLabel
                text="approval_allowlist.command_prefixes"
                restart={false}
                help="持久记住的命令前缀（ClaudeCode 风格，如 doyoutrade-cli task start:*）。匹配到的高危命令将自动放行。"
              />
            }
          >
            <Select
              mode="tags"
              className="w-full"
              tokenSeparators={[","]}
              placeholder="例如 doyoutrade-cli task start:*"
              allowClear
            />
          </Form.Item>
                </>
              ),
            },
            {
              key: "auto_update",
              forceRender: true,
              label: (
                <PanelLabel
                  title="auto_update · 自动更新"
                  hint="常用：自动检查新版本，并可在此手动更新"
                />
              ),
              children: (
                <>
          <Typography.Text type="secondary" className="mb-2 block text-xs">
            开启后后台会按间隔检查 GitHub Release；发现新版本只在界面上提示，
            由你点击「立即更新」才会安装并重启。开关保存后立即生效（无需重启）。
          </Typography.Text>
          <Form.Item
            name={["auto_update", "enabled"]}
            label={
              <FieldLabel
                text="enabled（自动检查更新）"
                restart={isRestart("auto_update.enabled")}
                help="是否后台按间隔自动检查 GitHub Release 新版本；发现新版本仅提示，需手动点击「立即更新」才会安装重启"
              />
            }
            valuePropName="checked"
          >
            <Switch data-testid="cfg-auto-update-enabled" />
          </Form.Item>
          <Form.Item
            name={["auto_update", "check_interval_hours"]}
            label={
              <FieldLabel
                text="check_interval_hours"
                restart={isRestart("auto_update.check_interval_hours")}
                help="检查新版本的时间间隔（小时）"
              />
            }
          >
            <InputNumber className="w-full" min={0.25} step={1} />
          </Form.Item>
          <Form.Item
            name={["auto_update", "repo"]}
            label={
              <FieldLabel
                text="repo"
                restart={isRestart("auto_update.repo")}
                help="检查更新所用的 GitHub 仓库，格式 owner/name"
              />
            }
          >
            <Input placeholder="owner/name（GitHub 仓库）" />
          </Form.Item>
          <Divider className="!my-3" />
          <UpdateSection />
                </>
              ),
            },
            {
              key: "database",
              forceRender: true,
              label: (
                <PanelLabel
                  title="database · 主库"
                  hint="主库连接，默认 SQLite 即可，一般无需改动"
                />
              ),
              children: (
                <>
          <Form.Item
            name={["database", "url"]}
            label={
              <FieldLabel
                text="url"
                restart={isRestart("database.url")}
                help="主库（存账户/任务/策略/cycle 等业务数据）连接串，默认 SQLite 文件，多实例部署建议换 PostgreSQL"
              />
            }
          >
            <Input placeholder="sqlite:///… / postgresql://…" />
          </Form.Item>
          <Form.Item
            name={["database", "echo"]}
            label={
              <FieldLabel
                text="echo"
                restart={isRestart("database.echo")}
                help="是否打印 SQLAlchemy 执行的 SQL 语句到日志，仅调试用，生产环境建议关闭"
              />
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={["database", "pool_pre_ping"]}
            label={
              <FieldLabel
                text="pool_pre_ping"
                restart={isRestart("database.pool_pre_ping")}
                help="从连接池取连接前先探活，避免使用已失效的数据库连接（如长时间空闲后断开）"
              />
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
                </>
              ),
            },
            {
              key: "qmt_proxy",
              forceRender: true,
              label: (
                <PanelLabel
                  title="qmt_proxy · 内嵌启动器"
                  hint="内嵌启动 qmt-proxy 的参数，不用内嵌模式保持默认即可"
                />
              ),
              children: (
                <>
          <Typography.Text type="secondary" className="mb-2 block text-xs">
            这些是 doyoutrade 侧内嵌启动 qmt-proxy 的参数；qmt-proxy 服务端自身的配置请在「QMT 服务端」页编辑。
          </Typography.Text>
          <Form.Item
            name={["qmt_proxy", "host"]}
            label={
              <FieldLabel
                text="host"
                restart={isRestart("qmt_proxy.host")}
                help="内嵌启动 qmt-proxy 时监听的网卡地址，仅内嵌（both）模式下生效"
              />
            }
          >
            <Input placeholder="127.0.0.1" />
          </Form.Item>
          <Form.Item
            name={["qmt_proxy", "port"]}
            label={
              <FieldLabel
                text="port"
                restart={isRestart("qmt_proxy.port")}
                help="内嵌启动 qmt-proxy 时监听的端口，仅内嵌（both）模式下生效"
              />
            }
          >
            <InputNumber className="w-full" min={1} max={65535} />
          </Form.Item>
          <Form.Item
            name={["qmt_proxy", "mode"]}
            label={
              <FieldLabel
                text="mode"
                restart={isRestart("qmt_proxy.mode")}
                help="内嵌 qmt-proxy 的 xtquant 运行模式：mock 无需真实终端、dev/prod 需连接真实 QMT 客户端"
              />
            }
          >
            <Select options={XTQUANT_MODE_OPTIONS} />
          </Form.Item>
          <Form.Item
            name={["qmt_proxy", "grpc_enabled"]}
            label={
              <FieldLabel
                text="grpc_enabled"
                restart={isRestart("qmt_proxy.grpc_enabled")}
                help="内嵌 qmt-proxy 是否同时开启 gRPC 服务端口"
              />
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={["qmt_proxy", "local_token"]}
            label={
              <FieldLabel
                text="local_token"
                restart={isRestart("qmt_proxy.local_token")}
                help="doyoutrade 访问内嵌 qmt-proxy 使用的鉴权 token；已配置则脱敏显示，留空不改动"
              />
            }
          >
            {secretInput("qmt_proxy.local_token", data?.values.qmt_proxy.local_token_set ?? false)}
          </Form.Item>
                </>
              ),
            },
            {
              key: "feishu",
              forceRender: true,
              label: (
                <PanelLabel
                  title="feishu · 飞书默认渠道"
                  hint="飞书机器人通知渠道，未接入飞书保持默认即可"
                />
              ),
              children: (
                <>
          <Form.Item
            name={["feishu", "enabled"]}
            label={
              <FieldLabel
                text="enabled"
                restart={isRestart("feishu.enabled")}
                help="是否启用默认飞书渠道，用于通知推送与飞书机器人事件回调"
              />
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={["feishu", "app_id"]}
            label={
              <FieldLabel
                text="app_id"
                restart={isRestart("feishu.app_id")}
                help="飞书自建应用的 App ID（cli_ 开头），在飞书开放平台应用详情页获取"
              />
            }
          >
            <Input placeholder="cli_…" />
          </Form.Item>
          <Form.Item
            name={["feishu", "app_secret"]}
            label={
              <FieldLabel
                text="app_secret"
                restart={isRestart("feishu.app_secret")}
                help="飞书自建应用的 App Secret；已配置则脱敏显示，留空不改动"
              />
            }
          >
            {secretInput("feishu.app_secret", data?.values.feishu.app_secret_set ?? false)}
          </Form.Item>
          <Form.Item
            name={["feishu", "encrypt_key"]}
            label={
              <FieldLabel
                text="encrypt_key"
                restart={isRestart("feishu.encrypt_key")}
                help="飞书事件订阅的 Encrypt Key，用于解密回调事件；未开启加密可留空"
              />
            }
          >
            {secretInput("feishu.encrypt_key", data?.values.feishu.encrypt_key_set ?? false)}
          </Form.Item>
          <Form.Item
            name={["feishu", "verification_token"]}
            label={
              <FieldLabel
                text="verification_token"
                restart={isRestart("feishu.verification_token")}
                help="飞书事件订阅的 Verification Token，用于校验回调请求确实来自飞书"
              />
            }
          >
            {secretInput("feishu.verification_token", data?.values.feishu.verification_token_set ?? false)}
          </Form.Item>
          <Form.Item
            name={["feishu", "domain"]}
            label={
              <FieldLabel
                text="domain"
                restart={isRestart("feishu.domain")}
                help="飞书 API 域名：国内版选 feishu，国际版（Lark）选 lark"
              />
            }
          >
            <Select options={FEISHU_DOMAIN_OPTIONS} />
          </Form.Item>
                </>
              ),
            },
          ]}
        />
      </Form>
    </div>
  );
}

// --------------------------------------------------------------------------
// QMT proxy config tab
// --------------------------------------------------------------------------

function QmtProxyConfigTab({ active }: { active: boolean }) {
  const pageRefreshToken = usePageRefreshToken();
  const [form] = Form.useForm();
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [data, setData] = useState<QmtProxyConfigResponse | null>(null);
  const [baseline, setBaseline] = useState<Record<string, unknown> | null>(null);
  const [unreachable, setUnreachable] = useState<{ code: string | null; message: string } | null>(null);
  const [editingApiKeys, setEditingApiKeys] = useState(false);
  const [restartFields, setRestartFields] = useState<string[] | null>(null);
  const [openPanels, setOpenPanels] = useState<string[]>(() => [...QMT_DEFAULT_OPEN_PANELS]);

  const clients = Form.useWatch(["xtquant", "clients"], form) as
    | Array<{ client_id?: string }>
    | undefined;
  const clientOptions = useMemo(
    () =>
      (clients ?? [])
        .map((c) => (c?.client_id ?? "").toString().trim())
        .filter((id) => id !== "")
        .map((id) => ({ value: id, label: id })),
    [clients],
  );

  const restartSet = useMemo(() => new Set(data?.restart_required_fields ?? []), [data]);
  const isRestart = useCallback((path: string) => restartSet.has(path), [restartSet]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getQmtProxyConfig();
      setData(res);
      const projected = toQmtFormValues(res.values);
      setBaseline(projected);
      form.setFieldsValue(projected);
      setEditingApiKeys(false);
      setUnreachable(null);
    } catch (e) {
      if (e instanceof ApiError && (e.status === 400 || e.status === 502)) {
        setUnreachable({ code: e.errorCode, message: e.message });
      } else {
        setUnreachable({ code: null, message: errorText(e) });
      }
      setData(null);
    } finally {
      setLoaded(true);
      setLoading(false);
    }
  }, [form]);

  // Lazy-load: only fetch (fires the server-side forward to the proxy) once the
  // tab is actually opened, then follow global refresh while it stays mounted.
  useEffect(() => {
    if (active && !loaded) void load();
  }, [active, loaded, load]);
  useEffect(() => {
    if (loaded && pageRefreshToken > 0) void load();
  }, [pageRefreshToken, loaded, load]);

  const onSave = async () => {
    let values: Record<string, unknown>;
    try {
      values = await form.validateFields();
    } catch (e) {
      // antd renders the inline field errors; here we only make sure they are
      // not hidden inside a collapsed panel, then scroll to the first one.
      if (isValidateError(e) && e.errorFields.length > 0) {
        const panels = panelsContainingErrors(e.errorFields, QMT_PANEL_ROOTS);
        if (panels.length > 0) {
          setOpenPanels((prev) => Array.from(new Set([...prev, ...panels])));
        }
        form.scrollToField(e.errorFields[0]!.name, { behavior: "smooth", block: "center" });
      }
      return;
    }
    if (!baseline) return;

    // Client-side uniqueness guard (mirrors resolve_clients() de-dup on the backend).
    const rawClients = ((values.xtquant as Record<string, unknown> | undefined)?.clients ??
      []) as Array<Partial<QmtClientConfig>>;
    const ids = rawClients.map((c) => (c?.client_id ?? "").toString().trim());
    if (ids.some((id) => id === "")) {
      message.error("每个终端的 client_id 必填");
      return;
    }
    const dupes = ids.filter((id, i) => ids.indexOf(id) !== i);
    if (dupes.length > 0) {
      message.error(`client_id 重复：${Array.from(new Set(dupes)).join(", ")}`);
      return;
    }

    // Diff everything except clients generically; clients get canonicalized so
    // absent-vs-default keys don't produce a false "changed" positive.
    const xt = { ...((values.xtquant as Record<string, unknown>) ?? {}) };
    delete xt.clients;
    const forDiff = { ...values, xtquant: xt };
    const editing = editingApiKeys ? new Set(["security.api_keys"]) : new Set<string>();
    const patch = buildConfigPatch(forDiff, baseline, QMT_SECRET_PATHS, editing);

    const currentClients = rawClients.map(normClient);
    const originalClients = ((data?.values.xtquant.clients ?? []) as QmtClientConfig[]).map(normClient);
    if (!deepEqual(currentClients, originalClients)) {
      patch.xtquant = { ...((patch.xtquant as Record<string, unknown>) ?? {}), clients: currentClients };
    }

    if (Object.keys(patch).length === 0) {
      message.info("没有需要保存的变更");
      return;
    }

    setSaving(true);
    try {
      const res: ConfigUpdateResponse = await updateQmtProxyConfig(patch);
      message.success("已保存 qmt-proxy 配置");
      setRestartFields(res.restart_required ? res.restart_fields : null);
      await load();
    } catch (e) {
      if (e instanceof ApiError && (e.status === 400 || e.status === 502)) {
        setUnreachable({ code: e.errorCode, message: e.message });
      }
      message.error(`保存失败：${errorText(e)}`);
    } finally {
      setSaving(false);
    }
  };

  if (unreachable) {
    const isUnreachable = unreachable.code === "qmt_proxy_unreachable";
    return (
      <Alert
        className="rounded-2xl border border-shell-line"
        type={isUnreachable ? "warning" : "error"}
        showIcon
        message={isUnreachable ? "无法连接 qmt-proxy" : "qmt-proxy 返回错误"}
        description={
          <div className="space-y-2">
            <div className="text-sm">{unreachable.message}</div>
            {isUnreachable ? (
              <div className="text-sm text-shell-muted">
                管理 qmt-proxy 配置要求代理可达：请到「账户」页为默认账户配置正确的 base_url 与
                token（内嵌 both 模式下代理天然可达；远程 Windows 代理需填写其地址与令牌）。
              </div>
            ) : null}
            <Space>
              <Button size="small" onClick={() => void load()}>
                重试
              </Button>
              <AccountsLinkButton />
            </Space>
          </div>
        }
      />
    );
  }

  return (
    <div>
      {restartFields ? (
        <RestartBanner proxy fields={restartFields} onClose={() => setRestartFields(null)} />
      ) : null}
      <div className="mb-3 flex items-center justify-between gap-2">
        <Typography.Text type="secondary" className="text-sm">
          写入 <Typography.Text code>{data?.path ?? "~/.doyoutrade/qmt-proxy.yml"}</Typography.Text>
          （app_mode=<Typography.Text code>{data?.app_mode ?? "?"}</Typography.Text>）。
          <Tag color="orange" className="ml-2">
            所有字段改动均需重启 qmt-proxy
          </Tag>
        </Typography.Text>
        <Space>
          <ToolbarButton icon={<ReloadOutlined />} onClick={() => void load()} loading={loading} label="刷新" />
          <ToolbarButton type="primary" icon={<SaveOutlined />} onClick={() => void onSave()} loading={saving} label="保存 qmt-proxy 配置" />
        </Space>
      </div>

      <Form form={form} layout="vertical" disabled={loading}>
        {/* 分组折叠降低填写负担：常用面板默认展开，冷门面板默认收起。
            所有面板 forceRender，字段始终注册；校验失败会自动展开所在面板。 */}
        <Collapse
          className="rounded-2xl"
          activeKey={openPanels}
          onChange={(keys) => setOpenPanels((Array.isArray(keys) ? keys : [keys]).map(String))}
          items={[
            {
              key: "xtquant",
              forceRender: true,
              label: (
                <PanelLabel
                  title="xtquant · 运行模式 / 数据 / 交易"
                  hint="常用：运行模式、多终端与默认/取数终端"
                />
              ),
              children: (
                <>
          <Form.Item
            name={["xtquant", "mode"]}
            label={
              <FieldLabel
                text="mode"
                restart={isRestart("xtquant.mode")}
                help="全局默认运行模式：mock 用模拟数据无需真实终端、dev/prod 需连接真实 QMT 客户端下单/取数"
              />
            }
          >
            <Select options={XTQUANT_MODE_OPTIONS} />
          </Form.Item>
          <Form.Item
            name={["xtquant", "data", "qmt_userdata_path"]}
            label={
              <FieldLabel
                text="qmt_userdata_path"
                restart={isRestart("xtquant.data.qmt_userdata_path")}
                help="QMT 客户端 userdata_mini 目录路径，留空使用 QMT 默认安装路径"
              />
            }
          >
            <Input placeholder="QMT userdata_mini 路径（留空使用默认）" allowClear />
          </Form.Item>
          <Form.Item
            name={["xtquant", "trading", "allow_real_trading"]}
            label={
              <FieldLabel
                text="allow_real_trading"
                restart={isRestart("xtquant.trading.allow_real_trading")}
                help="全局是否允许真实下单；关闭时下单请求会被拒绝，仅用于取数或模拟"
              />
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>

          <Divider className="!my-2" orientation="left" plain>
            <FieldLabel
              text="clients（多终端）"
              restart={isRestart("xtquant.clients")}
              help="多个 QMT 客户端终端的配置列表，每个终端可独立设置路径/模式/是否允许真实交易/是否作为数据源"
            />
          </Divider>
          <Form.List name={["xtquant", "clients"]}>
            {(fields, { add, remove }) => (
              <div className="space-y-2">
                {fields.map(({ key, name, ...rest }) => (
                  <Card
                    key={key}
                    size="small"
                    className="rounded-xl bg-white/40"
                    extra={
                      <Button
                        type="text"
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={() => remove(name)}
                      >
                        删除
                      </Button>
                    }
                  >
                    <Form.Item
                      {...rest}
                      name={[name, "client_id"]}
                      label={<FieldLabel text="client_id" help="终端唯一标识，供 default_client_id / data_source_client_id 引用" />}
                      rules={[{ required: true, message: "必填" }]}
                    >
                      <Input placeholder="终端唯一标识" />
                    </Form.Item>
                    <Form.Item
                      {...rest}
                      name={[name, "name"]}
                      label={<FieldLabel text="name" help="终端显示名，仅用于界面展示，可选" />}
                    >
                      <Input placeholder="显示名（可选）" />
                    </Form.Item>
                    <Form.Item
                      {...rest}
                      name={[name, "qmt_userdata_path"]}
                      label={<FieldLabel text="qmt_userdata_path" help="该终端专用的 userdata_mini 路径，留空继承全局 xtquant.data.qmt_userdata_path" />}
                    >
                      <Input placeholder="该终端的 userdata 路径（可选）" allowClear />
                    </Form.Item>
                    <Form.Item
                      {...rest}
                      name={[name, "mode"]}
                      label={<FieldLabel text="mode" help="该终端专用的运行模式，留空继承全局 xtquant.mode" />}
                    >
                      <Select options={XTQUANT_MODE_OPTIONS} allowClear placeholder="继承全局" />
                    </Form.Item>
                    <Space size="large">
                      <Form.Item
                        {...rest}
                        name={[name, "allow_real_trading"]}
                        label={<FieldLabel text="allow_real_trading" help="该终端是否允许真实下单" />}
                        valuePropName="checked"
                      >
                        <Switch />
                      </Form.Item>
                      <Form.Item
                        {...rest}
                        name={[name, "is_data_source"]}
                        label={<FieldLabel text="is_data_source" help="该终端是否可作为取数终端（供 data_source_client_id 选用）" />}
                        valuePropName="checked"
                      >
                        <Switch />
                      </Form.Item>
                    </Space>
                  </Card>
                ))}
                <Button
                  type="dashed"
                  icon={<PlusOutlined />}
                  onClick={() => add({ client_id: "", allow_real_trading: false, is_data_source: false })}
                  block
                >
                  新增终端
                </Button>
              </div>
            )}
          </Form.List>

          <Form.Item
            className="mt-3"
            name={["xtquant", "default_client_id"]}
            label={
              <FieldLabel
                text="default_client_id"
                restart={isRestart("xtquant.default_client_id")}
                help="未指定终端时默认使用的终端 client_id；留空则用全局 xtquant 配置"
              />
            }
          >
            <Select options={clientOptions} allowClear placeholder="选择一个终端作为默认" />
          </Form.Item>
          <Form.Item
            name={["xtquant", "data_source_client_id"]}
            label={
              <FieldLabel
                text="data_source_client_id"
                restart={isRestart("xtquant.data_source_client_id")}
                help="用于取数（行情拉取）的终端 client_id；留空则用默认终端"
              />
            }
          >
            <Select options={clientOptions} allowClear placeholder="选择取数终端" />
          </Form.Item>
                </>
              ),
            },
            {
              key: "security",
              forceRender: true,
              label: (
                <PanelLabel
                  title="security · API Key 鉴权"
                  hint="常用：代理访问密钥（已脱敏，重新填写才会覆盖）"
                />
              ),
              children: (
                <>
          <Form.Item
            label={
              <FieldLabel
                text="api_keys"
                restart={isRestart("security.api_keys")}
                help="访问 qmt-proxy HTTP/gRPC 接口所需的 Bearer API Key 列表，可配置多个；已配置则脱敏显示，需重新填写才会覆盖"
              />
            }
          >
            {editingApiKeys ? (
              <div className="space-y-1">
                <Form.Item name={["security", "api_keys"]} noStyle>
                  <Select mode="tags" placeholder="输入一个或多个 API Key，回车分隔" tokenSeparators={[",", " "]} />
                </Form.Item>
                <Button size="small" onClick={() => {
                  setEditingApiKeys(false);
                  form.setFieldValue(["security", "api_keys"], (baseline?.security as { api_keys?: string[] } | undefined)?.api_keys ?? []);
                }}>
                  取消
                </Button>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <Input
                  className="flex-1"
                  disabled
                  value={
                    data?.values.security.api_keys_set
                      ? `已配置 ${data.values.security.api_keys_count} 个（已脱敏）`
                      : "未配置"
                  }
                />
                <Button size="small" onClick={() => {
                  setEditingApiKeys(true);
                  form.setFieldValue(["security", "api_keys"], []);
                }}>
                  重新填写
                </Button>
              </div>
            )}
          </Form.Item>
                </>
              ),
            },
            {
              key: "logging",
              forceRender: true,
              label: (
                <PanelLabel title="logging · 日志" hint="日志级别，保持默认 INFO 即可" />
              ),
              children: (
                <>
          <Form.Item
            name={["logging", "level"]}
            label={
              <FieldLabel
                text="level"
                restart={isRestart("logging.level")}
                help="qmt-proxy 日志输出级别；排查问题临时调低，日常建议 INFO"
              />
            }
          >
            <Select options={LOG_LEVEL_OPTIONS} />
          </Form.Item>
                </>
              ),
            },
            {
              key: "grpc",
              forceRender: true,
              label: (
                <PanelLabel title="grpc · gRPC 服务" hint="gRPC 开关与监听端口，保持默认即可" />
              ),
              children: (
                <>
          <Form.Item
            name={["grpc", "enabled"]}
            label={
              <FieldLabel
                text="enabled"
                restart={isRestart("grpc.enabled")}
                help="是否开启 qmt-proxy 的 gRPC 服务端口（HTTP 接口不受此开关影响）"
              />
            }
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={["grpc", "host"]}
            label={
              <FieldLabel
                text="host"
                restart={isRestart("grpc.host")}
                help="gRPC 服务监听的网卡地址"
              />
            }
          >
            <Input placeholder="0.0.0.0" />
          </Form.Item>
          <Form.Item
            name={["grpc", "port"]}
            label={
              <FieldLabel
                text="port"
                restart={isRestart("grpc.port")}
                help="gRPC 服务监听端口"
              />
            }
          >
            <InputNumber className="w-full" min={1} max={65535} />
          </Form.Item>
                </>
              ),
            },
            {
              key: "app",
              forceRender: true,
              label: (
                <PanelLabel title="app · HTTP 服务" hint="HTTP 监听地址与端口，保持默认即可" />
              ),
              children: (
                <>
          <Form.Item
            name={["app", "host"]}
            label={
              <FieldLabel
                text="host"
                restart={isRestart("app.host")}
                help="qmt-proxy HTTP 服务监听的网卡地址"
              />
            }
          >
            <Input placeholder="0.0.0.0" />
          </Form.Item>
          <Form.Item
            name={["app", "port"]}
            label={
              <FieldLabel
                text="port"
                restart={isRestart("app.port")}
                help="qmt-proxy HTTP 服务监听端口"
              />
            }
          >
            <InputNumber className="w-full" min={1} max={65535} />
          </Form.Item>
                </>
              ),
            },
          ]}
        />
      </Form>
    </div>
  );
}

function AccountsLinkButton() {
  const navigate = useNavigate();
  return (
    <Button size="small" type="primary" onClick={() => navigate("/accounts")}>
      去账户页配置
    </Button>
  );
}

// --------------------------------------------------------------------------
// Page
// --------------------------------------------------------------------------

export function SettingsPage() {
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState("system");

  return (
    <div>
      <PageIntro
        title="设置"
        description="集中管理落在 ~/.doyoutrade 的静态 / 低频 YAML 配置：doyoutrade 全局项与 qmt-proxy 服务端。改动不会自动重启进程，标「需重启」的字段保存后需手动重启才生效。"
        extra={
          <Button icon={<SettingOutlined />} className="rounded-xl" onClick={() => navigate("/settings/models")}>
            模型与路由
          </Button>
        }
      />
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          { key: "system", label: "系统配置", children: <SystemConfigTab /> },
          {
            key: "qmt",
            label: "QMT 服务端",
            children: <QmtProxyConfigTab active={activeTab === "qmt"} />,
          },
        ]}
      />
    </div>
  );
}
