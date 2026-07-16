import React from "react";
import { Button, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, message } from "antd";
import {
  copyAssistantChannelSecret,
  createAssistantChannel,
  deleteAssistantChannel,
  listAssistantAgents,
  listAssistantChannels,
  startAssistantChannel,
  stopAssistantChannel,
  updateAssistantChannel,
} from "../api";
import type { Agent, AssistantChannel, CreateAssistantChannelPayload } from "../types";
import { usePageRefreshToken } from "../pageRefreshContext";

type FieldKind = "text" | "password" | "number" | "switch" | "select" | "list";

type FieldDef = {
  name: string;
  label: string;
  kind?: FieldKind;
  secret?: boolean;
  options?: { value: string; label: string }[];
  placeholder?: string;
  default?: string | number | boolean;
};

// Field definitions mirror the config models in doyoutrade/assistant/channels/config.py.
// `secret` fields are collected into payload.secrets, the rest into payload.config.
const CHANNEL_FIELDS: Record<string, FieldDef[]> = {
  feishu: [
    { name: "app_id", label: "App ID" },
    {
      name: "domain",
      label: "Domain",
      kind: "select",
      options: [{ value: "feishu", label: "feishu" }, { value: "lark", label: "lark" }],
      default: "feishu",
    },
    { name: "thinking_card_id", label: "Thinking Card ID", placeholder: "预注册的 CardKit Card ID for thinking 卡片" },
    { name: "tool_call_card_id", label: "Tool Call Card ID", placeholder: "预注册的 CardKit Card ID for 工具调用卡片" },
    { name: "rich_text_card_id", label: "Rich Text Card ID", placeholder: "预注册的 CardKit Card ID for 富文本卡片" },
    { name: "app_secret", label: "App Secret", kind: "password", secret: true },
    { name: "encrypt_key", label: "Encrypt Key", kind: "password", secret: true },
    { name: "verification_token", label: "Verification Token", kind: "password", secret: true },
  ],
  http: [],
  websocket: [],
  email: [
    { name: "smtp_host", label: "SMTP Host" },
    { name: "smtp_port", label: "SMTP Port", kind: "number", default: 465 },
    { name: "use_tls", label: "Use TLS (SMTPS, port 465)", kind: "switch", default: true },
    { name: "use_starttls", label: "Use STARTTLS (port 587)", kind: "switch", default: false },
    { name: "from_addr", label: "From Address" },
    { name: "to_addrs", label: "To Addresses", kind: "list", placeholder: "comma separated" },
    { name: "subject_prefix", label: "Subject Prefix", default: "[Doyoutrade]" },
    { name: "username", label: "SMTP Username", secret: true },
    { name: "password", label: "SMTP Password", kind: "password", secret: true },
  ],
  wecom: [
    {
      name: "msg_type",
      label: "Message Type",
      kind: "select",
      options: [{ value: "markdown", label: "markdown" }, { value: "text", label: "text" }],
      default: "markdown",
    },
    { name: "webhook_url", label: "Webhook URL", kind: "password", secret: true },
  ],
  dingtalk: [
    {
      name: "msg_type",
      label: "Message Type",
      kind: "select",
      options: [{ value: "markdown", label: "markdown" }, { value: "text", label: "text" }],
      default: "markdown",
    },
    { name: "webhook_url", label: "Webhook URL", kind: "password", secret: true },
    { name: "sign_secret", label: "Sign Secret", kind: "password", secret: true },
  ],
  telegram: [
    { name: "chat_id", label: "Chat ID" },
    { name: "message_thread_id", label: "Message Thread ID" },
    { name: "api_base", label: "API Base", default: "https://api.telegram.org" },
    { name: "bot_token", label: "Bot Token", kind: "password", secret: true },
  ],
  slack: [
    { name: "channel_id", label: "Channel ID" },
    { name: "api_base", label: "API Base", default: "https://slack.com/api" },
    { name: "webhook_url", label: "Webhook URL", kind: "password", secret: true },
    { name: "bot_token", label: "Bot Token", kind: "password", secret: true },
  ],
};

const CHANNEL_TYPE_OPTIONS = [
  { value: "feishu", label: "Feishu" },
  { value: "websocket", label: "WebSocket" },
  { value: "http", label: "HTTP" },
  { value: "email", label: "Email" },
  { value: "wecom", label: "WeCom (企业微信)" },
  { value: "dingtalk", label: "DingTalk (钉钉)" },
  { value: "telegram", label: "Telegram" },
  { value: "slack", label: "Slack" },
];

type FormValues = Record<string, unknown> & {
  name: string;
  type: string;
  agent_id: string;
  enabled: boolean;
};

function fieldsForType(type: string): FieldDef[] {
  return CHANNEL_FIELDS[type] ?? [];
}

function buildPayload(values: FormValues, originalSecrets: Record<string, string>): CreateAssistantChannelPayload {
  const config: Record<string, unknown> = {};
  const secrets: Record<string, string> = {};
  for (const field of fieldsForType(values.type)) {
    const raw = values[field.name];
    if (field.secret) {
      const value = typeof raw === "string" ? raw.trim() : raw;
      // Only include if non-empty AND different from original (user actually changed it)
      if (value && value !== originalSecrets[field.name]) {
        secrets[field.name] = value as string;
      }
      continue;
    }
    if (field.kind === "list") {
      config[field.name] = typeof raw === "string"
        ? raw.split(",").map((part) => part.trim()).filter(Boolean)
        : [];
    } else if (typeof raw === "string") {
      config[field.name] = raw.trim();
    } else {
      config[field.name] = raw ?? (field.kind === "switch" ? false : "");
    }
  }
  return {
    name: values.name.trim(),
    type: values.type,
    enabled: Boolean(values.enabled),
    agent_id: values.agent_id,
    config,
    secrets,
  };
}

export function ChannelsPage() {
  const pageRefreshToken = usePageRefreshToken();
  const [channels, setChannels] = React.useState<AssistantChannel[]>([]);
  const [agents, setAgents] = React.useState<Agent[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [editing, setEditing] = React.useState<AssistantChannel | null>(null);
  const [open, setOpen] = React.useState(false);
  const [form] = Form.useForm<FormValues>();
  const currentType = Form.useWatch("type", form) ?? "feishu";
  // Stores fetched secrets when editing, so we only send changed values to backend
  const originalSecretsRef = React.useRef<Record<string, string>>({});

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const [channelResult, agentResult] = await Promise.all([
        listAssistantChannels(),
        listAssistantAgents({ include_inactive: true }),
      ]);
      setChannels(channelResult.items);
      setAgents(agentResult.items);
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load, pageRefreshToken]);

  const openCreate = () => {
    setEditing(null);
    originalSecretsRef.current = {};
    form.resetFields();
    const defaults: Record<string, unknown> = { type: "feishu", enabled: true, agent_id: agents[0]?.id };
    for (const field of fieldsForType("feishu")) {
      if (field.default !== undefined) defaults[field.name] = field.default;
    }
    form.setFieldsValue(defaults);
    setOpen(true);
  };

  const openEdit = async (channel: AssistantChannel) => {
    setEditing(channel);
    setOpen(true);
    form.resetFields();
    const fields = fieldsForType(channel.type);
    const configValues: Record<string, unknown> = {};
    for (const field of fields) {
      if (field.secret) continue;
      const raw = channel.config[field.name];
      if (field.kind === "list") {
        configValues[field.name] = Array.isArray(raw) ? raw.join(", ") : "";
      } else {
        configValues[field.name] = raw ?? (field.kind === "switch" ? false : "");
      }
    }
    form.setFieldsValue({
      name: channel.name,
      type: channel.type,
      enabled: channel.enabled,
      agent_id: channel.agent_id,
      ...configValues,
    });
    // Reset original secrets, then fetch existing ones so the user sees them masked
    originalSecretsRef.current = {};
    const secretKeys = fields.filter((field) => field.secret).map((field) => field.name);
    await Promise.all(
      secretKeys.map(async (key) => {
        try {
          const result = await copyAssistantChannelSecret(channel.id, key);
          originalSecretsRef.current[key] = result.value;
          // Set in form so it appears masked (••••) - user can click eye to reveal
          form.setFieldsValue({ [key]: result.value });
        } catch {
          // Secret doesn't exist, leave blank
        }
      }),
    );
  };

  const handleValuesChange = (changed: Partial<FormValues>) => {
    if (!editing && typeof changed.type === "string") {
      const defaults: Record<string, unknown> = {};
      for (const field of fieldsForType(changed.type)) {
        if (field.default !== undefined) defaults[field.name] = field.default;
      }
      form.setFieldsValue(defaults);
    }
  };

  const save = async () => {
    const values = await form.validateFields();
    const payload = buildPayload(values, originalSecretsRef.current);
    if (editing) {
      await updateAssistantChannel(editing.id, payload);
    } else {
      await createAssistantChannel(payload);
    }
    setOpen(false);
    await load();
  };

  const copySecret = async (channel: AssistantChannel, key: string) => {
    const result = await copyAssistantChannelSecret(channel.id, key);
    await navigator.clipboard.writeText(result.value);
    message.success("已复制到剪贴板");
  };

  const remove = async (channel: AssistantChannel) => {
    await deleteAssistantChannel(channel.id);
    await load();
  };

  const toggleChannel = async (channel: AssistantChannel) => {
    await updateAssistantChannel(channel.id, { enabled: !channel.enabled });
    await load();
  };

  const startChannel = async (channel: AssistantChannel) => {
    await startAssistantChannel(channel.id);
    await load();
  };

  const stopChannel = async (channel: AssistantChannel) => {
    await stopAssistantChannel(channel.id);
    await load();
  };

  const renderField = (field: FieldDef) => {
    if (field.kind === "switch") {
      return (
        <Form.Item key={field.name} label={field.label} name={field.name} valuePropName="checked">
          <Switch />
        </Form.Item>
      );
    }
    if (field.kind === "select") {
      return (
        <Form.Item key={field.name} label={field.label} name={field.name}>
          <Select options={field.options} />
        </Form.Item>
      );
    }
    if (field.kind === "number") {
      return (
        <Form.Item key={field.name} label={field.label} name={field.name}>
          <InputNumber style={{ width: "100%" }} />
        </Form.Item>
      );
    }
    if (field.kind === "password" || field.secret) {
      return (
        <Form.Item key={field.name} label={field.label} name={field.name}>
          <Input.Password placeholder={editing ? "Leave blank to keep unchanged" : field.placeholder} />
        </Form.Item>
      );
    }
    return (
      <Form.Item key={field.name} label={field.label} name={field.name}>
        <Input placeholder={field.placeholder} />
      </Form.Item>
    );
  };

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h2 className="m-0 text-xl font-semibold">Channels</h2>
        <Button type="primary" onClick={openCreate}>New Channel</Button>
      </div>
      <Table
        rowKey="id"
        loading={loading}
        dataSource={channels}
        pagination={false}
        columns={[
          { title: "Name", dataIndex: "name" },
          { title: "Type", dataIndex: "type" },
          {
            title: "Agent",
            dataIndex: "agent_id",
            render: (agentId: string) => agents.find((agent) => agent.id === agentId)?.name ?? agentId,
          },
          {
            title: "Status",
            dataIndex: "status",
          },
          {
            title: "Enabled",
            render: (_: unknown, channel: AssistantChannel) => (
              <Switch
                size="small"
                checked={channel.enabled}
                onChange={() => void toggleChannel(channel)}
              />
            ),
          },
          {
            title: "Secrets",
            dataIndex: "secret_keys",
            render: (_: unknown, channel: AssistantChannel) => (
              <Space wrap>
                {channel.secret_keys.map((key) => (
                  <Button
                    key={key}
                    size="small"
                    onClick={() => void copySecret(channel, key)}
                    aria-label={`Copy ${key} for ${channel.name}`}
                  >
                    Copy {key}
                  </Button>
                ))}
              </Space>
            ),
          },
          {
            title: "Actions",
            render: (_: unknown, channel: AssistantChannel) => (
              <Space>
                {channel.status !== "running" ? (
                  <Button size="small" onClick={() => void startChannel(channel)}>
                    启动
                  </Button>
                ) : (
                  <Button size="small" danger onClick={() => void stopChannel(channel)}>
                    停止
                  </Button>
                )}
                <Button size="small" aria-label={`Edit ${channel.name}`} onClick={() => openEdit(channel)}>
                  Edit
                </Button>
                <Button size="small" danger aria-label={`Delete ${channel.name}`} onClick={() => void remove(channel)}>
                  Delete
                </Button>
              </Space>
            ),
          },
        ]}
      />
      <Modal
        title={editing ? "Edit Channel" : "New Channel"}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={() => void save()}
        okText="Save"
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ type: "feishu", enabled: true, domain: "feishu" }}
          onValuesChange={handleValuesChange}
        >
          <Form.Item label="Name" name="name" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item label="Type" name="type" rules={[{ required: true }]}>
            <Select options={CHANNEL_TYPE_OPTIONS} disabled={Boolean(editing)} />
          </Form.Item>
          <Form.Item label="Agent" name="agent_id" rules={[{ required: true }]}>
            <Select options={agents.map((agent) => ({ value: agent.id, label: agent.name }))} />
          </Form.Item>
          <Form.Item label="Enabled" name="enabled" valuePropName="checked">
            <Switch />
          </Form.Item>
          {fieldsForType(currentType).map(renderField)}
        </Form>
      </Modal>
    </div>
  );
}
