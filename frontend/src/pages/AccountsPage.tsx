import React from "react";
import {
  Button,
  Checkbox,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  message,
} from "antd";

import {
  createAccount,
  deleteAccount,
  listAccounts,
  setDefaultAccount,
  updateAccount,
  type CreateAccountPayload,
} from "../api";
import { ApiError } from "../api";
import type { Account, AccountMockPosition } from "../types";
import { usePageRefreshToken } from "../pageRefreshContext";

type AccountFormValues = {
  name: string;
  mode: "live" | "mock";
  base_url?: string;
  token?: string;
  qmt_account_id?: string;
  qmt_terminal_id?: string;
  timeout_seconds?: number;
  is_default?: boolean;
  enabled?: boolean;
  mock_cash?: number;
  mock_equity?: number;
  /** JSON textarea for ``mock_positions``; parsed before submit. */
  mock_positions_text?: string;
};

function parseMockPositions(text: string | undefined): AccountMockPosition[] {
  const trimmed = (text ?? "").trim();
  if (!trimmed) {
    return [];
  }
  const parsed = JSON.parse(trimmed);
  if (!Array.isArray(parsed)) {
    throw new Error("mock_positions 必须是数组");
  }
  return parsed as AccountMockPosition[];
}

export function AccountsPage() {
  const pageRefreshToken = usePageRefreshToken();
  const [accounts, setAccounts] = React.useState<Account[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [showForm, setShowForm] = React.useState(false);
  const [editingAccount, setEditingAccount] = React.useState<Account | undefined>(undefined);
  const [submitting, setSubmitting] = React.useState(false);
  const [form] = Form.useForm<AccountFormValues>();
  const mode = Form.useWatch("mode", form);

  const loadAccounts = React.useCallback(async () => {
    setLoading(true);
    try {
      const result = await listAccounts();
      setAccounts(result.items ?? []);
    } catch (err) {
      message.error(`加载账户失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void loadAccounts();
  }, [loadAccounts, pageRefreshToken]);

  const openCreate = () => {
    setEditingAccount(undefined);
    form.resetFields();
    form.setFieldsValue({
      mode: "mock",
      timeout_seconds: 30,
      enabled: true,
      is_default: false,
    });
    setShowForm(true);
  };

  const openEdit = (account: Account) => {
    setEditingAccount(account);
    form.resetFields();
    form.setFieldsValue({
      name: account.name,
      mode: account.mode,
      base_url: account.base_url,
      token: account.token ?? "",
      qmt_account_id: account.qmt_account_id ?? "",
      qmt_terminal_id: account.qmt_terminal_id ?? "",
      timeout_seconds: account.timeout_seconds,
      is_default: account.is_default,
      enabled: account.enabled,
      mock_cash: account.mock_cash,
      mock_equity: account.mock_equity,
      mock_positions_text:
        account.mock_positions && account.mock_positions.length > 0
          ? JSON.stringify(account.mock_positions, null, 2)
          : "",
    });
    setShowForm(true);
  };

  const handleSubmit = async () => {
    let values: AccountFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }

    let mockPositions: AccountMockPosition[];
    try {
      mockPositions = parseMockPositions(values.mock_positions_text);
    } catch (err) {
      message.error(`持仓 JSON 解析失败：${err instanceof Error ? err.message : String(err)}`);
      return;
    }

    const payload: CreateAccountPayload = {
      name: values.name.trim(),
      mode: values.mode,
      base_url: values.base_url?.trim() || undefined,
      token: values.token?.trim() ? values.token.trim() : null,
      qmt_account_id: values.qmt_account_id?.trim() ? values.qmt_account_id.trim() : null,
      qmt_terminal_id: values.qmt_terminal_id?.trim() ? values.qmt_terminal_id.trim() : null,
      timeout_seconds: values.timeout_seconds,
      is_default: values.is_default ?? false,
      enabled: values.enabled ?? true,
    };
    if (values.mode === "mock") {
      payload.mock_cash = values.mock_cash;
      payload.mock_equity = values.mock_equity;
      payload.mock_positions = mockPositions;
    }

    setSubmitting(true);
    try {
      if (editingAccount) {
        await updateAccount(editingAccount.id, payload);
        message.success("已保存账户");
      } else {
        await createAccount(payload);
        message.success("已创建账户");
      }
      setShowForm(false);
      setEditingAccount(undefined);
      await loadAccounts();
    } catch (err) {
      message.error(
        `${editingAccount ? "保存" : "创建"}账户失败：${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async (account: Account) => {
    try {
      await deleteAccount(account.id);
      message.success("已删除账户");
      await loadAccounts();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Backend detail carries the "account_in_use" reason; surface it.
        message.error(err.message || "该账户被任务引用，无法删除");
        return;
      }
      message.error(`删除账户失败：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleSetDefault = async (account: Account) => {
    try {
      await setDefaultAccount(account.id);
      message.success("已设为默认账户");
      await loadAccounts();
    } catch (err) {
      message.error(`设为默认失败：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const columns = [
    {
      title: "名称",
      dataIndex: "name",
      key: "name",
      render: (name: string, record: Account) => (
        <Space>
          <span>{name}</span>
          {record.is_default ? <Tag color="gold">默认</Tag> : null}
        </Space>
      ),
    },
    {
      title: "模式",
      dataIndex: "mode",
      key: "mode",
      render: (m: Account["mode"]) =>
        m === "live" ? <Tag color="red">实盘 live</Tag> : <Tag color="blue">模拟 mock</Tag>,
    },
    {
      title: "Base URL",
      dataIndex: "base_url",
      key: "base_url",
      render: (v: string) => v || "—",
    },
    {
      title: "券商账号",
      dataIndex: "qmt_account_id",
      key: "qmt_account_id",
      render: (v: string | null) => v || "—",
    },
    {
      title: "启用",
      dataIndex: "enabled",
      key: "enabled",
      render: (enabled: boolean) =>
        enabled ? <Tag color="green">启用</Tag> : <Tag color="default">停用</Tag>,
    },
    {
      title: "操作",
      key: "actions",
      render: (_: unknown, record: Account) => (
        <Space>
          <Button size="small" onClick={() => openEdit(record)}>
            编辑
          </Button>
          {!record.is_default ? (
            <Button size="small" onClick={() => void handleSetDefault(record)}>
              设为默认
            </Button>
          ) : null}
          <Popconfirm title="删除该账户？" onConfirm={() => void handleDelete(record)}>
            <Button size="small" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          marginBottom: 16,
          gap: 16,
          alignItems: "center",
        }}
      >
        <h2 style={{ margin: 0 }}>账户管理</h2>
        <Button type="primary" onClick={openCreate}>
          新建账户
        </Button>
      </div>

      <Table
        dataSource={accounts}
        columns={columns}
        rowKey="id"
        loading={loading}
        pagination={false}
      />

      <Modal
        title={editingAccount ? "编辑账户" : "新建账户"}
        open={showForm}
        onCancel={() => {
          setShowForm(false);
          setEditingAccount(undefined);
        }}
        onOk={() => void handleSubmit()}
        confirmLoading={submitting}
        okText={editingAccount ? "保存" : "创建"}
        cancelText="取消"
        destroyOnHidden
      >
        <Form<AccountFormValues> layout="vertical" form={form}>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请输入账户名称" }]}>
            <Input placeholder="my-mock-account" />
          </Form.Item>
          <Form.Item name="mode" label="模式" rules={[{ required: true, message: "请选择模式" }]}>
            <Select
              options={[
                { label: "模拟 mock", value: "mock" },
                { label: "实盘 live", value: "live" },
              ]}
            />
          </Form.Item>
          <Form.Item name="base_url" label="Base URL">
            <Input placeholder="http://127.0.0.1:8000" />
          </Form.Item>
          <Form.Item name="token" label="Token">
            <Input placeholder="留空表示无 token" />
          </Form.Item>
          <Form.Item name="qmt_account_id" label="券商交易账号">
            <Input placeholder="留空表示未绑定" />
          </Form.Item>
          <Form.Item
            name="qmt_terminal_id"
            label="QMT 终端 (X-QMT-Terminal)"
            extra="多终端 qmt-proxy 部署时填该账户对应的 client_id；留空走代理默认终端"
          >
            <Input placeholder="留空表示使用默认终端" />
          </Form.Item>
          <Form.Item name="timeout_seconds" label="超时（秒）">
            <InputNumber min={1} className="w-full" style={{ width: "100%" }} />
          </Form.Item>
          {mode === "mock" ? (
            <>
              <Form.Item name="mock_cash" label="模拟现金 (mock_cash)">
                <InputNumber min={0} step={10000} style={{ width: "100%" }} />
              </Form.Item>
              <Form.Item name="mock_equity" label="模拟权益 (mock_equity)">
                <InputNumber min={0} step={10000} style={{ width: "100%" }} />
              </Form.Item>
              <Form.Item
                name="mock_positions_text"
                label="模拟持仓 (mock_positions, JSON)"
                extra='可选，留空即可。形如 [{"symbol":"600519","quantity":100,"cost_price":1700}]'
              >
                <Input.TextArea rows={4} placeholder='[{"symbol":"600519","quantity":100,"cost_price":1700}]' />
              </Form.Item>
            </>
          ) : null}
          <Form.Item name="is_default" label="设为默认账户" valuePropName="checked">
            <Checkbox />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch checkedChildren="启用" unCheckedChildren="停用" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
