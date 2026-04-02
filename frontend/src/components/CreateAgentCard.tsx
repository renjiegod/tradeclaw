import { Button, Card, Form, Input, Select, Space, Typography } from "antd";
import { useEffect, useMemo, useState } from "react";

import { createInstance, listTemplates } from "../api";
import type { AgentTemplate, CreateInstancePayload } from "../types";

type Props = {
  onCreated: () => void;
};

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
    () => templates.map((item) => ({ label: item.name, value: item.template_id })),
    [templates],
  );

  return (
    <Card className="panel-card" title="Create Agent" loading={fetchingTemplate}>
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
          label="Name"
          rules={[{ required: true, message: "Please input instance name" }]}
        >
          <Input placeholder="alpha-growth-paper" />
        </Form.Item>

        <Form.Item name="template_id" label="Template" rules={[{ required: true }]}>
          <Select options={templateOptions} />
        </Form.Item>

        <Space size={12} style={{ width: "100%" }}>
          <Form.Item name="mode" label="Run Mode" style={{ flex: 1 }}>
            <Select
              options={[
                { label: "Paper", value: "paper" },
                { label: "Live", value: "live" },
                { label: "Backtest", value: "backtest" },
              ]}
            />
          </Form.Item>
          <Form.Item name="orchestrator_mode" label="Orchestrator" style={{ flex: 1 }}>
            <Select
              options={[
                { label: "Single Agent", value: "single-agent" },
                { label: "Multi Role", value: "multi-role" },
              ]}
            />
          </Form.Item>
        </Space>

        <Form.Item name="description" label="Description">
          <Input.TextArea rows={3} placeholder="brief goal / risk preference" />
        </Form.Item>

        <Space style={{ width: "100%", justifyContent: "space-between" }}>
          <Typography.Text type="secondary">Template-driven creation with safe defaults.</Typography.Text>
          <Button type="primary" htmlType="submit" loading={loading}>
            Create
          </Button>
        </Space>
      </Form>
    </Card>
  );
}
