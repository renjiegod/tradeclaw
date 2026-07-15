import { DeleteOutlined, EditOutlined, EyeOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useState } from "react";

import {
  createModelRoute,
  deleteModelRoute,
  listModelRoutes,
  patchModelRoute,
  revealModelRouteApiKey,
} from "../api";
import { PageIntro } from "../components/PageIntro";
import { usePageRefreshToken } from "../pageRefreshContext";
import type { ModelRouteRow } from "../types";

function parseJsonObjectOrNull(raw: string, fieldLabel: string): Record<string, unknown> | null {
  const t = raw.trim();
  if (!t) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(t);
  } catch {
    throw new Error(`${fieldLabel} 不是合法 JSON`);
  }
  if (parsed !== null && (typeof parsed !== "object" || Array.isArray(parsed))) {
    throw new Error(`${fieldLabel} 须为 JSON 对象`);
  }
  return parsed as Record<string, unknown>;
}

function isConfigUnavailableError(e: unknown): boolean {
  const msg = e instanceof Error ? e.message : String(e);
  return msg.includes("503") || msg.toLowerCase().includes("not configured");
}

const KIND_OPTIONS = [
  { value: "anthropic", label: "Anthropic（官方 Messages API）" },
  { value: "openai_compatible", label: "OpenAI 兼容接口" },
  { value: "lmstudio", label: "LM Studio（本地）" },
];

const KIND_LABELS: Record<string, string> = Object.fromEntries(
  KIND_OPTIONS.map((o) => [o.value, o.label]),
);

type RouteFormValues = {
  route_name: string;
  provider_kind: string;
  api_key: string;
  base_url: string;
  target_model: string;
  settings_json: string;
};

export function ModelSettingsPage() {
  const pageRefreshToken = usePageRefreshToken();
  const [routes, setRoutes] = useState<ModelRouteRow[]>([]);
  const [loadingRoutes, setLoadingRoutes] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);

  const [routeModalOpen, setRouteModalOpen] = useState(false);
  const [routeEditId, setRouteEditId] = useState<string | null>(null);
  const [routeSubmitting, setRouteSubmitting] = useState(false);
  const [routeForm] = Form.useForm<RouteFormValues>();

  const [revealOpen, setRevealOpen] = useState(false);
  const [revealLoading, setRevealLoading] = useState(false);
  const [revealedKey, setRevealedKey] = useState("");
  const [revealTitle, setRevealTitle] = useState("");

  const loadRoutes = useCallback(async () => {
    setLoadingRoutes(true);
    try {
      const res = await listModelRoutes();
      setRoutes(res.items ?? []);
      setConfigError(null);
    } catch (e: unknown) {
      setRoutes([]);
      if (isConfigUnavailableError(e)) {
        setConfigError("当前运行环境未配置模型仓库（常见于未走完整 bootstrap 的桩服务）。完整后端应暴露 GET /model-routes。");
      } else {
        const detail = e instanceof Error ? e.message : String(e);
        message.error(`加载模型列表失败：${detail}`);
      }
    } finally {
      setLoadingRoutes(false);
    }
  }, []);

  useEffect(() => {
    void loadRoutes();
  }, [loadRoutes, pageRefreshToken]);

  const openCreateRoute = () => {
    setRouteEditId(null);
    routeForm.resetFields();
    routeForm.setFieldsValue({
      route_name: "",
      provider_kind: "anthropic",
      api_key: "",
      base_url: "",
      target_model: "",
      settings_json: "",
    });
    setRouteModalOpen(true);
  };

  const openEditRoute = (row: ModelRouteRow) => {
    setRouteEditId(row.id);
    routeForm.setFieldsValue({
      route_name: row.route_name,
      provider_kind: row.provider_kind,
      api_key: "",
      base_url: row.base_url ?? "",
      target_model: row.target_model ?? "",
      settings_json: row.settings ? JSON.stringify(row.settings, null, 2) : "",
    });
    setRouteModalOpen(true);
  };

  const submitRoute = async () => {
    try {
      const v = await routeForm.validateFields();
      const settings = parseJsonObjectOrNull(v.settings_json, "高级参数");
      setRouteSubmitting(true);
      if (routeEditId) {
        const patch: Parameters<typeof patchModelRoute>[1] = {
          route_name: v.route_name.trim(),
          provider_kind: v.provider_kind,
          base_url: v.base_url.trim() || null,
          target_model: v.target_model.trim() || null,
          settings,
        };
        if (v.api_key.trim()) {
          patch.api_key = v.api_key.trim();
        }
        await patchModelRoute(routeEditId, patch);
        message.success("已更新模型");
      } else {
        await createModelRoute({
          route_name: v.route_name.trim(),
          provider_kind: v.provider_kind,
          api_key: v.api_key.trim(),
          base_url: v.base_url.trim() || null,
          target_model: v.target_model.trim() || null,
          settings,
        });
        message.success("已创建模型");
      }
      setRouteModalOpen(false);
      await loadRoutes();
    } catch (e: unknown) {
      if (e && typeof e === "object" && "errorFields" in e) {
        return;
      }
      const detail = e instanceof Error ? e.message : String(e);
      message.error(detail);
    } finally {
      setRouteSubmitting(false);
    }
  };

  const openRevealKey = async (row: ModelRouteRow) => {
    setRevealTitle(row.route_name);
    setRevealedKey("");
    setRevealOpen(true);
    setRevealLoading(true);
    try {
      const res = await revealModelRouteApiKey(row.id);
      setRevealedKey(res.api_key ?? "");
    } catch (e: unknown) {
      const detail = e instanceof Error ? e.message : String(e);
      message.error(`读取 API Key 失败：${detail}`);
      setRevealOpen(false);
    } finally {
      setRevealLoading(false);
    }
  };

  const routeColumns: ColumnsType<ModelRouteRow> = [
    {
      title: "配置名称",
      dataIndex: "route_name",
      key: "route_name",
      ellipsis: true,
      render: (t: string) => (
        <Typography.Text className="font-mono text-xs" copyable={{ text: t }}>
          {t}
        </Typography.Text>
      ),
    },
    {
      title: "接口类型",
      dataIndex: "provider_kind",
      key: "provider_kind",
      width: 180,
      render: (k: string) => KIND_LABELS[k] ?? k,
    },
    {
      title: "接口地址",
      dataIndex: "base_url",
      key: "base_url",
      ellipsis: true,
      render: (u: string | null) => u ?? "—",
    },
    {
      title: "API Key",
      dataIndex: "api_key_masked",
      key: "api_key_masked",
      width: 120,
      render: (m: string) => <Typography.Text className="font-mono text-xs">{m || "—"}</Typography.Text>,
    },
    {
      title: "模型 ID",
      dataIndex: "target_model",
      key: "target_model",
      ellipsis: true,
      render: (m: string | null) => m ?? "—",
    },
    {
      title: "高级参数",
      key: "settings",
      width: 160,
      ellipsis: true,
      render: (_: unknown, row) => (
        <Typography.Text className="font-mono text-xs" ellipsis={{ tooltip: JSON.stringify(row.settings) }}>
          {row.settings ? JSON.stringify(row.settings) : "—"}
        </Typography.Text>
      ),
    },
    {
      title: "操作",
      key: "actions",
      width: 200,
      fixed: "right",
      render: (_: unknown, row) => (
        <Space size="small" wrap>
          <Button type="link" size="small" icon={<EditOutlined />} onClick={() => openEditRoute(row)}>
            编辑
          </Button>
          <Button type="link" size="small" icon={<EyeOutlined />} onClick={() => void openRevealKey(row)}>
            查看 Key
          </Button>
          <Popconfirm
            title="删除此模型？"
            description="若实例或回测任务仍引用该配置名称，将无法删除（返回 409）。"
            onConfirm={async () => {
              try {
                await deleteModelRoute(row.id);
                message.success("已删除");
                await loadRoutes();
              } catch (e: unknown) {
                message.error(e instanceof Error ? e.message : String(e));
              }
            }}
          >
            <Button type="link" size="small" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <>
      <PageIntro
        title="模型配置"
        description="管理可用模型：每条配置包含接口类型、接口地址、API Key 与模型 ID，并用「配置名称」供实例、回测与智能体引用。创建智能体时的「使用的模型」下拉与此共用同一数据源。"
        extra={
          <Button className="rounded-xl" icon={<ReloadOutlined />} onClick={() => void loadRoutes()}>
            刷新
          </Button>
        }
      />

      {configError ? (
        <Alert className="mb-4 rounded-2xl border border-shell-line" type="warning" showIcon message={configError} />
      ) : null}

      <div className="rounded-2xl border border-shell-line bg-card-bg p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <Typography.Text type="secondary" className="text-sm">
            「配置名称」会在实例与智能体中被引用；列表中的 API Key 已脱敏，完整密钥通过「查看 Key」拉取。
          </Typography.Text>
          <Button type="primary" className="rounded-xl" icon={<PlusOutlined />} onClick={openCreateRoute}>
            新建模型
          </Button>
        </div>
        <Table<ModelRouteRow>
          rowKey="id"
          loading={loadingRoutes}
          columns={routeColumns}
          dataSource={routes}
          scroll={{ x: 980 }}
          pagination={false}
          size="small"
        />
      </div>

      <Modal
        title={routeEditId ? "编辑模型" : "新建模型"}
        open={routeModalOpen}
        onCancel={() => setRouteModalOpen(false)}
        onOk={() => void submitRoute()}
        confirmLoading={routeSubmitting}
        width={560}
        destroyOnClose
      >
        <Form form={routeForm} layout="vertical" className="mt-2">
          <Form.Item name="route_name" label="配置名称" rules={[{ required: true, message: "必填" }]}>
            <Input placeholder="例如 default；实例与智能体会按此名称引用" />
          </Form.Item>
          <Form.Item
            name="provider_kind"
            label="接口类型"
            rules={[{ required: true }]}
            extra="OpenAI 兼容接口须填写接口地址；LM Studio 可省略（使用本地默认）；Anthropic 可留空走官方端点。"
          >
            <Select options={KIND_OPTIONS} />
          </Form.Item>
          <Form.Item name="api_key" label={routeEditId ? "API Key（留空则不修改）" : "API Key"}>
            <Input.Password placeholder={routeEditId ? "不修改请留空" : "可留空；生产环境请填写"} autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="base_url"
            label="接口地址"
            extra="OpenAI 兼容接口要求填写；Anthropic 可留空使用官方默认。"
          >
            <Input placeholder="https://…" allowClear />
          </Form.Item>
          <Form.Item name="target_model" label="模型 ID">
            <Input placeholder="例如 claude-sonnet-4-5 / deepseek-chat" allowClear />
          </Form.Item>
          <Form.Item
            name="settings_json"
            label="高级参数（JSON）"
            extra="留空表示不传；可填 temperature、max_tokens 等。"
          >
            <Input.TextArea rows={5} placeholder='例如 {"temperature": 0.2}' className="font-mono text-xs" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`API Key：${revealTitle}`}
        open={revealOpen}
        onCancel={() => setRevealOpen(false)}
        footer={[
          <Button key="close" onClick={() => setRevealOpen(false)}>
            关闭
          </Button>,
        ]}
        width={520}
      >
        {revealLoading ? (
          <Typography.Text type="secondary">加载中…</Typography.Text>
        ) : (
          <Input.TextArea
            readOnly
            value={revealedKey}
            autoSize={{ minRows: 3, maxRows: 8 }}
            className="font-mono text-xs"
          />
        )}
      </Modal>
    </>
  );
}
