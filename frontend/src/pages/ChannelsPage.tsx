import React from "react";
import { Button, Form, Input, Modal, Select, Space, Switch, Table, message } from "antd";
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

type FormValues = {
  name: string;
  type: string;
  agent_id: string;
  enabled: boolean;
  app_id?: string;
  domain?: string;
  app_secret?: string;
  encrypt_key?: string;
  verification_token?: string;
  thinking_card_id?: string;
  tool_call_card_id?: string;
  rich_text_card_id?: string;
};

function buildPayload(values: FormValues, originalSecrets: Record<string, string>): CreateAssistantChannelPayload {
  const secrets: Record<string, string> = {};
  for (const key of ["app_secret", "encrypt_key", "verification_token"] as const) {
    const value = values[key]?.trim();
    // Only include if non-empty AND different from original (user actually changed it)
    if (value && value !== originalSecrets[key]) {
      secrets[key] = value;
    }
  }
  return {
    name: values.name.trim(),
    type: values.type,
    enabled: Boolean(values.enabled),
    agent_id: values.agent_id,
    config: {
      app_id: values.app_id?.trim() || "",
      domain: values.domain || "feishu",
      thinking_card_id: values.thinking_card_id?.trim() || "",
      tool_call_card_id: values.tool_call_card_id?.trim() || "",
      rich_text_card_id: values.rich_text_card_id?.trim() || "",
    },
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
    form.setFieldsValue({
      type: "feishu",
      enabled: true,
      domain: "feishu",
      agent_id: agents[0]?.id,
    });
    setOpen(true);
  };

  const openEdit = async (channel: AssistantChannel) => {
    setEditing(channel);
    setOpen(true);
    form.setFieldsValue({
      name: channel.name,
      type: channel.type,
      enabled: channel.enabled,
      agent_id: channel.agent_id,
      app_id: String(channel.config.app_id ?? ""),
      domain: String(channel.config.domain ?? "feishu"),
      app_secret: "",
      encrypt_key: "",
      verification_token: "",
      thinking_card_id: String(channel.config.thinking_card_id ?? ""),
      tool_call_card_id: String(channel.config.tool_call_card_id ?? ""),
      rich_text_card_id: String(channel.config.rich_text_card_id ?? ""),
    });
    // Reset original secrets
    originalSecretsRef.current = {};
    // Fetch existing secrets so user can see them masked and click eye to reveal
    const secretKeys = ["app_secret", "encrypt_key", "verification_token"] as const;
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
        <Form form={form} layout="vertical" initialValues={{ type: "feishu", enabled: true, domain: "feishu" }}>
          <Form.Item label="Name" name="name" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item label="Type" name="type" rules={[{ required: true }]}>
            <Select options={[{ value: "feishu", label: "Feishu" }, { value: "websocket", label: "WebSocket" }, { value: "http", label: "HTTP" }]} />
          </Form.Item>
          <Form.Item label="Agent" name="agent_id" rules={[{ required: true }]}>
            <Select options={agents.map((agent) => ({ value: agent.id, label: agent.name }))} />
          </Form.Item>
          <Form.Item label="Enabled" name="enabled" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item label="App ID" name="app_id">
            <Input />
          </Form.Item>
          <Form.Item label="Domain" name="domain">
            <Select options={[{ value: "feishu", label: "feishu" }, { value: "lark", label: "lark" }]} />
          </Form.Item>
          <Form.Item label="Thinking Card ID" name="thinking_card_id">
            <Input placeholder="预注册的 CardKit Card ID for thinking 卡片" />
          </Form.Item>
          <Form.Item label="Tool Call Card ID" name="tool_call_card_id">
            <Input placeholder="预注册的 CardKit Card ID for 工具调用卡片" />
          </Form.Item>
          <Form.Item label="Rich Text Card ID" name="rich_text_card_id">
            <Input placeholder="预注册的 CardKit Card ID for 富文本卡片" />
          </Form.Item>
          <Form.Item label="App Secret" name="app_secret">
            <Input.Password placeholder={editing ? "Leave blank to keep unchanged" : ""} />
          </Form.Item>
          <Form.Item label="Encrypt Key" name="encrypt_key">
            <Input.Password placeholder={editing ? "Leave blank to keep unchanged" : ""} />
          </Form.Item>
          <Form.Item label="Verification Token" name="verification_token">
            <Input.Password placeholder={editing ? "Leave blank to keep unchanged" : ""} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
