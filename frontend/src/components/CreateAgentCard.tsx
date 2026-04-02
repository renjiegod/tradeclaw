import { Button, Card, Form, Input, Select, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";

import { createInstance, listTemplates } from "../api";
import type { AgentTemplate, CreateInstancePayload } from "../types";

type Props = {
  onCreated: () => void;
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

export function CreateAgentCard({ onCreated }: Props) {
  const [form] = Form.useForm<CreateInstancePayload>();
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

  return (
    <Card className={PANEL_CARD_CLASSNAME} title="创建实例" loading={fetchingTemplate}>
      <Form
        layout="vertical"
        form={form}
        onFinish={async (values) => {
          setLoading(true);
          try {
            await createInstance(values);
            form.resetFields(["name", "description"]);
            onCreated();
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
          <Select options={templateOptions} />
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

        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <Typography.Text className="text-sm" type="secondary">
            基于模板快速创建，默认使用安全配置。
          </Typography.Text>
          <Button className="rounded-xl" type="primary" htmlType="submit" loading={loading}>
            创建
          </Button>
        </div>
      </Form>
    </Card>
  );
}
