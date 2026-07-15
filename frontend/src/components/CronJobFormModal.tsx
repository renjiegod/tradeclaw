import React from "react";
import type { CronJob, CronJobFormValues, CronTask } from "../types";
import { createCronJob, updateCronJob } from "../api";
import { Button, Form, Input, InputNumber, message, Modal, Select, Space, Switch } from "antd";
import { parseCron, serializeCron, toFormPreset, type CronParts } from "./parseCron";

type Props = {
  agentId: string;
  job?: CronJob;
  onSaved: (job: CronJob) => void;
  onClose: () => void;
};

const TIMEZONES = [
  "UTC", "America/New_York", "America/Los_Angeles", "Europe/London",
  "Europe/Paris", "Asia/Shanghai", "Asia/Tokyo", "Asia/Singapore",
];

const PRESET_OPTIONS = [
  { label: "每小时", value: "hourly" },
  { label: "每天", value: "daily" },
  { label: "每周", value: "weekly" },
  { label: "自定义", value: "custom" },
];

// Reminders / replies are the only cron kind on this page now. Strategy signal
// push and the legacy strategy_cycle pre-action moved to per-Task triggers
// (see TaskTriggersPanel / TriggerFormModal), so this modal only ever emits an
// ``agent_chat_reply`` task.
const REMINDER_TASK_KIND = "agent_chat_reply";

export const CronJobFormModal: React.FC<Props> = ({ agentId, job, onSaved, onClose }) => {
  const [form] = Form.useForm();
  const [saving, setSaving] = React.useState(false);
  const [preset, setPreset] = React.useState<"hourly" | "daily" | "weekly" | "custom">(
    job ? toFormPreset(parseCron(job.cron_expression).type) : "daily"
  );

  const handleSubmit = async (values: Record<string, unknown>) => {
    setSaving(true);
    try {
      const cronParts = buildCronParts(preset, values);
      const task = buildTask(values, agentId);
      const payload: CronJobFormValues = {
        name: values.name as string,
        cron_expression: serializeCron(cronParts),
        timezone: values.timezone as string,
        max_concurrency: values.max_concurrency as number,
        timeout_seconds: values.timeout_seconds as number,
        enabled: values.enabled as boolean,
        // Always clear any legacy pre-action and send the reminder task so
        // editing an old row migrates it onto the task pipeline.
        pre_action: null,
        task,
      };
      const saved = job
        ? await updateCronJob(agentId, job.id, payload)
        : await createCronJob(agentId, payload);
      onSaved(saved);
    } catch (err) {
      const content = err instanceof Error ? err.message : String(err);
      message.error(`保存失败：${content}`);
    } finally {
      setSaving(false);
    }
  };

  const existingTaskParams = job?.task_params_json ?? undefined;

  const initialValues: Record<string, unknown> = job
    ? (() => {
        const parts = parseCron(job.cron_expression);
        const formPreset = toFormPreset(parts.type);
        return {
          name: job.name,
          cron_expression: job.cron_expression,
          timezone: job.timezone,
          max_concurrency: job.max_concurrency,
          timeout_seconds: job.timeout_seconds,
          enabled: job.enabled,
          preset: formPreset,
          minute: parts.type === "hourly" ? parts.minute : 0,
          hour: parts.type === "daily" || parts.type === "weekly" ? parts.hour : 9,
          daysOfWeek: parts.type === "weekly" ? parts.daysOfWeek : [1, 2, 3, 4, 5],
          task_user_request:
            typeof existingTaskParams?.user_request === "string"
              ? (existingTaskParams.user_request as string)
              : job.input_template ?? "",
          task_target_session_id:
            typeof existingTaskParams?.target_session_id === "string"
              ? (existingTaskParams.target_session_id as string)
              : "",
        };
      })()
    : {
        preset: "daily",
        minute: 0,
        hour: 9,
        daysOfWeek: [1, 2, 3, 4, 5],
        timezone: "UTC",
        max_concurrency: 1,
        timeout_seconds: 120,
        enabled: true,
        task_user_request: "",
        task_target_session_id: "",
      };

  return (
    <Modal
      title={job ? "编辑提醒" : "新建提醒"}
      open
      onCancel={onClose}
      destroyOnHidden
      width={600}
      footer={[
        <Button key="cancel" autoInsertSpace={false} onClick={onClose}>
          取消
        </Button>,
        <Button key="submit" type="primary" autoInsertSpace={false} loading={saving} onClick={() => form.submit()}>
          {saving ? "保存中…" : job ? "保存" : "创建提醒"}
        </Button>,
      ]}
    >
      <Form form={form} layout="vertical" initialValues={initialValues} onFinish={handleSubmit}>
        <Form.Item name="name" label="提醒名称" rules={[{ required: true, message: "请输入提醒名称" }]}>
          <Input placeholder="例如：每日市场复盘" />
        </Form.Item>

        <Form.Item label="调度类型">
          <Select
            value={preset}
            options={PRESET_OPTIONS}
            onChange={(v) => setPreset(v as typeof preset)}
          />
        </Form.Item>

        {preset === "hourly" && (
          <Form.Item name="minute" label="分钟">
            <InputNumber min={0} max={59} />
          </Form.Item>
        )}

        {preset === "daily" && (
          <Space>
            <Form.Item name="hour" label="小时" rules={[{ required: true }]}>
              <InputNumber min={0} max={23} />
            </Form.Item>
            <Form.Item name="minute" label="分钟" rules={[{ required: true }]}>
              <InputNumber min={0} max={59} />
            </Form.Item>
          </Space>
        )}

        {preset === "weekly" && (
          <Space direction="vertical">
            <Form.Item name="daysOfWeek" label="星期（0=周日）">
              <Select mode="multiple" options={[
                { label: "周日", value: 0 }, { label: "周一", value: 1 },
                { label: "周二", value: 2 }, { label: "周三", value: 3 },
                { label: "周四", value: 4 }, { label: "周五", value: 5 },
                { label: "周六", value: 6 },
              ]} />
            </Form.Item>
            <Space>
              <Form.Item name="hour" label="小时" rules={[{ required: true }]}>
                <InputNumber min={0} max={23} />
              </Form.Item>
              <Form.Item name="minute" label="分钟" rules={[{ required: true }]}>
                <InputNumber min={0} max={59} />
              </Form.Item>
            </Space>
          </Space>
        )}

        {preset === "custom" && (
          <Form.Item
            name="cron_expression"
            label="Cron 表达式（分 时 日 月 周）"
            rules={[{ required: true, message: "请输入 Cron 表达式" }]}
          >
            <Input placeholder="0 9 * * 1-5" />
          </Form.Item>
        )}

        <Form.Item name="timezone" label="时区" initialValue="UTC">
          <Select options={TIMEZONES.map((tz) => ({ label: tz, value: tz }))} />
        </Form.Item>

        <Form.Item
          name="task_user_request"
          label="请求内容"
          rules={[{ required: true, message: "请描述提醒要做什么" }]}
        >
          <Input.TextArea
            rows={4}
            placeholder="提醒我复盘今日市场，重点关注收盘后的策略信号。"
          />
        </Form.Item>
        <Form.Item
          name="task_target_session_id"
          label="目标 session_id（可选）"
          extra="除非该提醒要推送到指定 assistant 会话，否则留空。"
        >
          <Input placeholder="session id" />
        </Form.Item>

        <Space>
          <Form.Item name="max_concurrency" label="最大并发" initialValue={1}>
            <InputNumber min={1} max={10} />
          </Form.Item>
          <Form.Item name="timeout_seconds" label="超时（秒）" initialValue={120}>
            <InputNumber min={10} max={3600} />
          </Form.Item>
        </Space>

        <Form.Item name="enabled" label="启用" valuePropName="checked" initialValue={true}>
          <Switch />
        </Form.Item>
      </Form>
    </Modal>
  );
};

function buildCronParts(preset: string, values: Record<string, unknown>): CronParts {
  if (preset === "hourly") return { type: "hourly", minute: values.minute as number };
  if (preset === "daily") return { type: "daily", hour: values.hour as number, minute: values.minute as number };
  if (preset === "weekly") return { type: "weekly", daysOfWeek: values.daysOfWeek as number[], hour: values.hour as number, minute: values.minute as number };
  return { type: "custom", rawCron: values.cron_expression as string };
}

function buildTask(values: Record<string, unknown>, agentId: string): CronTask {
  const params: Record<string, unknown> = {
    user_request: String(values.task_user_request ?? "").trim(),
    agent_id: agentId,
  };
  const targetSessionId = String(values.task_target_session_id ?? "").trim();
  if (targetSessionId) {
    params.target_session_id = targetSessionId;
  }
  return { kind: REMINDER_TASK_KIND, params };
}
