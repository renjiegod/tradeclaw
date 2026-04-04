import { Button, Card, Form, Input, message, Select, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";

import { createInstance, listTemplates } from "../api";
import type { AgentTemplate, CreateInstancePayload } from "../types";

const FALLBACK_TEMPLATES: AgentTemplate[] = [
  {
    template_id: "single-agent-trend",
    name: "单智能体 / 趋势跟踪",
    default_mode: "paper",
    default_orchestrator_mode: "single-agent",
  },
  {
    template_id: "single-agent-event",
    name: "单智能体 / 事件驱动",
    default_mode: "paper",
    default_orchestrator_mode: "single-agent",
  },
  {
    template_id: "multi-role-rtr",
    name: "多角色 / 研究 + 交易 + 风控",
    default_mode: "paper",
    default_orchestrator_mode: "multi-role",
  },
];

type Props = {
  onCreated: () => void;
};

type CreateAgentFormValues = Omit<CreateInstancePayload, "watch_symbols" | "settings"> & {
  watch_symbols_text?: string;
  settings_text?: string;
};

const TEMPLATE_NAME_MAP: Record<string, string> = {
  "single-agent-trend": "单智能体 / 趋势跟踪",
  "single-agent-event": "单智能体 / 事件驱动",
  "multi-role-rtr": "多角色 / 研究 + 交易 + 风控",
};

const PANEL_CARD_CLASSNAME = "!overflow-hidden !border !border-shell-line !bg-card-bg shadow-shell-card";

function formatTemplateName(template: AgentTemplate): string {
  return TEMPLATE_NAME_MAP[template.template_id] ?? template.name;
}

function normalizeOptionalText(value?: string): string | undefined {
  const normalized = value?.trim();
  return normalized ? normalized : undefined;
}

function parseWatchSymbols(value?: string): string[] | undefined {
  const normalized = value
    ?.split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
  return normalized?.length ? normalized : undefined;
}

function parseSettings(value?: string): Record<string, unknown> | null | undefined {
  const normalized = value?.trim();
  if (!normalized) {
    return undefined;
  }

  const parsed = JSON.parse(normalized) as unknown;
  if (parsed === null) {
    return null;
  }
  if (typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Settings 必须是 JSON 对象或 null。");
  }
  return parsed as Record<string, unknown>;
}

export function CreateAgentCard({ onCreated }: Props) {
  const [form] = Form.useForm<CreateAgentFormValues>();
  const [templates, setTemplates] = useState<AgentTemplate[]>([]);
  const [loading, setLoading] = useState(false);
  const [fetchingTemplate, setFetchingTemplate] = useState(false);

  useEffect(() => {
    let active = true;
    setFetchingTemplate(true);
    listTemplates()
      .then((result) => {
        if (!active) {
          return;
        }
        setTemplates(result);
        if (result[0]) {
          form.setFieldsValue({
            template_id: result[0].template_id,
            mode: result[0].default_mode,
            orchestrator_mode: result[0].default_orchestrator_mode,
          });
        }
      })
      .catch((error: unknown) => {
        if (!active) {
          return;
        }
        const detail = error instanceof Error ? error.message : String(error);
        message.warning(`模板列表加载失败，已使用内置默认模板。${detail}`);
        setTemplates(FALLBACK_TEMPLATES);
        const first = FALLBACK_TEMPLATES[0];
        form.setFieldsValue({
          template_id: first.template_id,
          mode: first.default_mode,
          orchestrator_mode: first.default_orchestrator_mode,
        });
      })
      .finally(() => {
        if (active) {
          setFetchingTemplate(false);
        }
      });

    return () => {
      active = false;
    };
  }, [form]);

  const templateOptions = useMemo(
    () => templates.map((item) => ({ label: formatTemplateName(item), value: item.template_id })),
    [templates],
  );

  const applyTemplateDefaults = (templateId: string) => {
    const selected = templates.find((item) => item.template_id === templateId);
    if (!selected) {
      return;
    }

    form.setFieldsValue({
      mode: selected.default_mode,
      orchestrator_mode: selected.default_orchestrator_mode,
    });
  };

  return (
    <Card className={PANEL_CARD_CLASSNAME} title="创建实例" loading={fetchingTemplate}>
      <Form
        layout="vertical"
        form={form}
        onFinish={async (values) => {
          let payload: CreateInstancePayload;
          try {
            payload = {
              name: values.name.trim(),
              template_id: values.template_id,
              mode: values.mode,
              orchestrator_mode: values.orchestrator_mode,
              description: normalizeOptionalText(values.description),
              data_provider: normalizeOptionalText(values.data_provider),
              watch_symbols: parseWatchSymbols(values.watch_symbols_text),
              execution_strategy: normalizeOptionalText(values.execution_strategy),
              account_id: normalizeOptionalText(values.account_id),
              model_id: normalizeOptionalText(values.model_id),
              settings: parseSettings(values.settings_text),
            };
            form.setFields([{ name: "settings_text", errors: [] }]);
          } catch (error: unknown) {
            const content = error instanceof Error ? error.message : String(error);
            form.setFields([{ name: "settings_text", errors: [content] }]);
            message.error(content);
            return;
          }

          setLoading(true);
          try {
            await createInstance(payload);
            form.resetFields([
              "name",
              "description",
              "data_provider",
              "watch_symbols_text",
              "execution_strategy",
              "account_id",
              "model_id",
              "settings_text",
            ]);
            onCreated();
          } catch (error: unknown) {
            const content = error instanceof Error ? error.message : String(error);
            message.error(`创建实例失败：${content}`);
          } finally {
            setLoading(false);
          }
        }}
      >
        <Form.Item
          name="name"
          label="名称"
          rules={[{ required: true, message: "请输入实例名称" }]}
        >
          <Input placeholder="alpha-growth-paper" />
        </Form.Item>

        <Form.Item name="template_id" label="模板" rules={[{ required: true }]}>
          <Select options={templateOptions} onChange={applyTemplateDefaults} />
        </Form.Item>

        <div className="grid gap-1 md:grid-cols-2 md:gap-3">
          <Form.Item name="mode" label="运行模式">
            <Select
              options={[
                { label: "模拟盘", value: "paper" },
                { label: "实盘", value: "live" },
                { label: "回测", value: "backtest" },
              ]}
            />
          </Form.Item>
          <Form.Item name="orchestrator_mode" label="编排模式">
            <Select
              options={[
                { label: "单智能体", value: "single-agent" },
                { label: "多角色", value: "multi-role" },
              ]}
            />
          </Form.Item>
        </div>

        <Form.Item name="description" label="描述">
          <Input.TextArea rows={3} placeholder="填写策略目标、风控偏好等信息" />
        </Form.Item>

        <div className="grid gap-1 md:grid-cols-2 md:gap-3">
          <Form.Item name="data_provider" label="数据提供器">
            <Input placeholder="auto / mock / qmt / custom-provider" />
          </Form.Item>
          <Form.Item name="execution_strategy" label="执行策略">
            <Input placeholder="langchain" />
          </Form.Item>
        </div>

        <div className="grid gap-1 md:grid-cols-2 md:gap-3">
          <Form.Item name="account_id" label="账户 ID">
            <Input placeholder="acct-001" />
          </Form.Item>
          <Form.Item name="model_id" label="模型 ID">
            <Input placeholder="gpt-4.1" />
          </Form.Item>
        </div>

        <Form.Item name="watch_symbols_text" label="观察标的">
          <Input.TextArea rows={2} placeholder="AAPL, MSFT, NVDA" />
        </Form.Item>

        <Form.Item
          name="settings_text"
          label="Settings JSON"
          extra="留空表示不传；如需清空可填写 null。"
        >
          <Input.TextArea
            rows={8}
            placeholder={'{\n  "risk": "medium",\n  "max_position_pct": 0.15\n}'}
          />
        </Form.Item>

        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <Typography.Text className="text-sm" type="secondary">
            业务字段会在提交前转换为结构化 payload，系统字段仍由后端自动生成。
          </Typography.Text>
          <Button className="rounded-xl" type="primary" htmlType="submit" loading={loading}>
            创建
          </Button>
        </div>
      </Form>
    </Card>
  );
}
