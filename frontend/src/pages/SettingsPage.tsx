import { DeleteOutlined, PlusOutlined, ReloadOutlined, SettingOutlined } from "@ant-design/icons";
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

function FieldLabel({ text, restart }: { text: string; restart?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      {text}
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
    assistant: { tool_result_max_chars: v.assistant.tool_result_max_chars },
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
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
          <Button type="primary" onClick={() => void onSave()} loading={saving}>
            保存系统配置
          </Button>
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
          <Form.Item name={["server", "host"]} label={<FieldLabel text="host" restart={isRestart("server.host")} />}>
            <Input data-testid="cfg-server-host" placeholder="0.0.0.0" />
          </Form.Item>
          <Form.Item name={["server", "port"]} label={<FieldLabel text="port" restart={isRestart("server.port")} />}>
            <InputNumber className="w-full" min={1} max={65535} />
          </Form.Item>
          <Form.Item
            name={["server", "tick_seconds"]}
            label={<FieldLabel text="tick_seconds" restart={isRestart("server.tick_seconds")} />}
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
            label={<FieldLabel text="default_provider" restart={isRestart("data.default_provider")} />}
          >
            <Input placeholder="auto / qmt / tushare / akshare …" />
          </Form.Item>
          <Form.Item
            name={["data", "tushare", "token"]}
            label={<FieldLabel text="tushare.token" restart={isRestart("data.tushare.token")} />}
          >
            {secretInput("data.tushare.token", data?.values.data.tushare.token_set ?? false)}
          </Form.Item>
          <Form.Item
            name={["data", "tushare", "timeout_seconds"]}
            label={
              <FieldLabel
                text="tushare.timeout_seconds"
                restart={isRestart("data.tushare.timeout_seconds")}
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
            label={<FieldLabel text="database_url" restart={isRestart("market_data.database_url")} />}
          >
            <Input placeholder="sqlite:///… / postgresql://…" />
          </Form.Item>
          <Form.Item
            name={["market_data", "enabled_intervals"]}
            label={<FieldLabel text="enabled_intervals" restart={isRestart("market_data.enabled_intervals")} />}
          >
            <Select mode="tags" options={INTERVAL_OPTIONS} placeholder="1d, 5m …" />
          </Form.Item>
          <Form.Item
            name={["market_data", "lookback_years"]}
            label={<FieldLabel text="lookback_years" restart={isRestart("market_data.lookback_years")} />}
          >
            <InputNumber className="w-full" min={0} />
          </Form.Item>
          <Form.Item
            name={["market_data", "default_provider"]}
            label={<FieldLabel text="default_provider" restart={isRestart("market_data.default_provider")} />}
          >
            <Input placeholder="auto / qmt / tushare …" />
          </Form.Item>
          <Form.Item
            name={["market_data", "sync_on_startup"]}
            label={<FieldLabel text="sync_on_startup" restart={isRestart("market_data.sync_on_startup")} />}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={["market_data", "sync_concurrency"]}
            label={<FieldLabel text="sync_concurrency" restart={isRestart("market_data.sync_concurrency")} />}
          >
            <InputNumber className="w-full" min={1} />
          </Form.Item>
          <Form.Item
            name={["market_data", "provider_rate_limit_per_second"]}
            label={
              <FieldLabel
                text="provider_rate_limit_per_second"
                restart={isRestart("market_data.provider_rate_limit_per_second")}
              />
            }
          >
            <InputNumber className="w-full" min={0} step={0.5} />
          </Form.Item>
          <Form.Item
            name={["market_data", "sync_full_market"]}
            label={<FieldLabel text="sync_full_market" restart={isRestart("market_data.sync_full_market")} />}
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
            label={<FieldLabel text="service_name" restart={isRestart("observability.service_name")} />}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name={["observability", "log_level"]}
            label={<FieldLabel text="log_level" restart={isRestart("observability.log_level")} />}
          >
            <Select options={LOG_LEVEL_OPTIONS} />
          </Form.Item>
          <Form.Item
            name={["observability", "console_enabled"]}
            label={<FieldLabel text="console_enabled" restart={isRestart("observability.console_enabled")} />}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={["observability", "tracing_enabled"]}
            label={<FieldLabel text="tracing_enabled" restart={isRestart("observability.tracing_enabled")} />}
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
            label={<FieldLabel text="symbol_scope_mode" restart={isRestart("review.symbol_scope_mode")} />}
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
            label={<FieldLabel text="enabled" restart={isRestart("retention.enabled")} />}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={["retention", "observability_ttl_days"]}
            label={<FieldLabel text="observability_ttl_days" restart={isRestart("retention.observability_ttl_days")} />}
          >
            <InputNumber className="w-full" min={0} />
          </Form.Item>
          <Form.Item
            name={["retention", "prune_interval_hours"]}
            label={<FieldLabel text="prune_interval_hours" restart={isRestart("retention.prune_interval_hours")} />}
          >
            <InputNumber className="w-full" min={0} />
          </Form.Item>
          <Form.Item
            name={["retention", "prune_on_startup"]}
            label={<FieldLabel text="prune_on_startup" restart={isRestart("retention.prune_on_startup")} />}
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
                <PanelLabel title="assistant · 助手" hint="助手工具结果截断长度，保持默认即可" />
              ),
              children: (
                <>
          <Form.Item
            name={["assistant", "tool_result_max_chars"]}
            label={<FieldLabel text="tool_result_max_chars" restart={isRestart("assistant.tool_result_max_chars")} />}
          >
            <InputNumber className="w-full" min={0} />
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
            label={<FieldLabel text="enabled（自动检查更新）" restart={isRestart("auto_update.enabled")} />}
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
              />
            }
          >
            <InputNumber className="w-full" min={0.25} step={1} />
          </Form.Item>
          <Form.Item
            name={["auto_update", "repo"]}
            label={<FieldLabel text="repo" restart={isRestart("auto_update.repo")} />}
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
            label={<FieldLabel text="url" restart={isRestart("database.url")} />}
          >
            <Input placeholder="sqlite:///… / postgresql://…" />
          </Form.Item>
          <Form.Item
            name={["database", "echo"]}
            label={<FieldLabel text="echo" restart={isRestart("database.echo")} />}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={["database", "pool_pre_ping"]}
            label={<FieldLabel text="pool_pre_ping" restart={isRestart("database.pool_pre_ping")} />}
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
            label={<FieldLabel text="host" restart={isRestart("qmt_proxy.host")} />}
          >
            <Input placeholder="127.0.0.1" />
          </Form.Item>
          <Form.Item
            name={["qmt_proxy", "port"]}
            label={<FieldLabel text="port" restart={isRestart("qmt_proxy.port")} />}
          >
            <InputNumber className="w-full" min={1} max={65535} />
          </Form.Item>
          <Form.Item
            name={["qmt_proxy", "mode"]}
            label={<FieldLabel text="mode" restart={isRestart("qmt_proxy.mode")} />}
          >
            <Select options={XTQUANT_MODE_OPTIONS} />
          </Form.Item>
          <Form.Item
            name={["qmt_proxy", "grpc_enabled"]}
            label={<FieldLabel text="grpc_enabled" restart={isRestart("qmt_proxy.grpc_enabled")} />}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={["qmt_proxy", "local_token"]}
            label={<FieldLabel text="local_token" restart={isRestart("qmt_proxy.local_token")} />}
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
            label={<FieldLabel text="enabled" restart={isRestart("feishu.enabled")} />}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name={["feishu", "app_id"]}
            label={<FieldLabel text="app_id" restart={isRestart("feishu.app_id")} />}
          >
            <Input placeholder="cli_…" />
          </Form.Item>
          <Form.Item
            name={["feishu", "app_secret"]}
            label={<FieldLabel text="app_secret" restart={isRestart("feishu.app_secret")} />}
          >
            {secretInput("feishu.app_secret", data?.values.feishu.app_secret_set ?? false)}
          </Form.Item>
          <Form.Item
            name={["feishu", "encrypt_key"]}
            label={<FieldLabel text="encrypt_key" restart={isRestart("feishu.encrypt_key")} />}
          >
            {secretInput("feishu.encrypt_key", data?.values.feishu.encrypt_key_set ?? false)}
          </Form.Item>
          <Form.Item
            name={["feishu", "verification_token"]}
            label={<FieldLabel text="verification_token" restart={isRestart("feishu.verification_token")} />}
          >
            {secretInput("feishu.verification_token", data?.values.feishu.verification_token_set ?? false)}
          </Form.Item>
          <Form.Item
            name={["feishu", "domain"]}
            label={<FieldLabel text="domain" restart={isRestart("feishu.domain")} />}
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
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
          <Button type="primary" onClick={() => void onSave()} loading={saving}>
            保存 qmt-proxy 配置
          </Button>
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
            label={<FieldLabel text="mode" restart={isRestart("xtquant.mode")} />}
          >
            <Select options={XTQUANT_MODE_OPTIONS} />
          </Form.Item>
          <Form.Item
            name={["xtquant", "data", "qmt_userdata_path"]}
            label={<FieldLabel text="qmt_userdata_path" restart={isRestart("xtquant.data.qmt_userdata_path")} />}
          >
            <Input placeholder="QMT userdata_mini 路径（留空使用默认）" allowClear />
          </Form.Item>
          <Form.Item
            name={["xtquant", "trading", "allow_real_trading"]}
            label={<FieldLabel text="allow_real_trading" restart={isRestart("xtquant.trading.allow_real_trading")} />}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>

          <Divider className="!my-2" orientation="left" plain>
            <FieldLabel text="clients（多终端）" restart={isRestart("xtquant.clients")} />
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
                      label="client_id"
                      rules={[{ required: true, message: "必填" }]}
                    >
                      <Input placeholder="终端唯一标识" />
                    </Form.Item>
                    <Form.Item {...rest} name={[name, "name"]} label="name">
                      <Input placeholder="显示名（可选）" />
                    </Form.Item>
                    <Form.Item {...rest} name={[name, "qmt_userdata_path"]} label="qmt_userdata_path">
                      <Input placeholder="该终端的 userdata 路径（可选）" allowClear />
                    </Form.Item>
                    <Form.Item {...rest} name={[name, "mode"]} label="mode">
                      <Select options={XTQUANT_MODE_OPTIONS} allowClear placeholder="继承全局" />
                    </Form.Item>
                    <Space size="large">
                      <Form.Item {...rest} name={[name, "allow_real_trading"]} label="allow_real_trading" valuePropName="checked">
                        <Switch />
                      </Form.Item>
                      <Form.Item {...rest} name={[name, "is_data_source"]} label="is_data_source" valuePropName="checked">
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
            label={<FieldLabel text="default_client_id" restart={isRestart("xtquant.default_client_id")} />}
          >
            <Select options={clientOptions} allowClear placeholder="选择一个终端作为默认" />
          </Form.Item>
          <Form.Item
            name={["xtquant", "data_source_client_id"]}
            label={<FieldLabel text="data_source_client_id" restart={isRestart("xtquant.data_source_client_id")} />}
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
          <Form.Item label={<FieldLabel text="api_keys" restart={isRestart("security.api_keys")} />}>
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
            label={<FieldLabel text="level" restart={isRestart("logging.level")} />}
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
            label={<FieldLabel text="enabled" restart={isRestart("grpc.enabled")} />}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item name={["grpc", "host"]} label={<FieldLabel text="host" restart={isRestart("grpc.host")} />}>
            <Input placeholder="0.0.0.0" />
          </Form.Item>
          <Form.Item name={["grpc", "port"]} label={<FieldLabel text="port" restart={isRestart("grpc.port")} />}>
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
          <Form.Item name={["app", "host"]} label={<FieldLabel text="host" restart={isRestart("app.host")} />}>
            <Input placeholder="0.0.0.0" />
          </Form.Item>
          <Form.Item name={["app", "port"]} label={<FieldLabel text="port" restart={isRestart("app.port")} />}>
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
