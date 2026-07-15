import React from "react";
import { Button, Form, Input, message } from "antd";

import type { SkillFrontmatter } from "../types";

type Props = {
  value: SkillFrontmatter;
  onSave: (next: Partial<SkillFrontmatter>) => Promise<void>;
};

export default function SkillFrontmatterForm({ value, onSave }: Props) {
  const [form] = Form.useForm<SkillFrontmatter>();
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => {
    form.setFieldsValue(value);
  }, [value, form]);

  const handleSave = async () => {
    const fields = await form.validateFields();
    setSaving(true);
    try {
      await onSave(fields);
      message.success("Frontmatter 已保存");
    } catch (err) {
      message.error(String((err as Error).message ?? err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Form form={form} layout="vertical" initialValues={value}>
      <Form.Item label="name" name="name" rules={[{ required: true }]}>
        <Input />
      </Form.Item>
      <Form.Item label="description" name="description" rules={[{ required: true }]}>
        <Input.TextArea rows={2} />
      </Form.Item>
      <Form.Item label="license" name="license">
        <Input />
      </Form.Item>
      <Button type="primary" onClick={handleSave} loading={saving}>
        保存 frontmatter
      </Button>
    </Form>
  );
}
