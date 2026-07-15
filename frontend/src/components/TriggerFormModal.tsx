import React from "react";
import {
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Segmented,
  Select,
  Space,
  Typography,
  message,
} from "antd";

import { createTaskTrigger, listAssistantAgents, listFeishuChats, updateTaskTrigger } from "../api";
import type { CreateTaskTriggerPayload } from "../api";
import type {
  DeliveryMode,
  DeliveryTarget,
  ExecutionIntent,
  FeishuChatOption,
  NoSignalMode,
  TaskTrigger,
  TriggerScheduleKind,
} from "../types";

type Props = {
  taskId: string;
  trigger?: TaskTrigger;
  onSaved: (t: TaskTrigger) => void;
  onClose: () => void;
};

// Reuse the timezone idiom from CronJobFormModal; A股 triggers default to
// Asia/Shanghai but we still expose the same set so non-A股 schedules work.
const TIMEZONES = [
  "Asia/Shanghai",
  "UTC",
  "America/New_York",
  "America/Los_Angeles",
  "Europe/London",
  "Europe/Paris",
  "Asia/Tokyo",
  "Asia/Singapore",
];

// Schedule kinds the form natively edits. backtest_range is intentionally
// omitted from the UI (Phase 2 scope note); existing backtest_range triggers
// still render read-only in the panel.
type FormScheduleKind = Extract<TriggerScheduleKind, "cron" | "interval" | "at">;

const SCHEDULE_KIND_OPTIONS: { label: string; value: FormScheduleKind }[] = [
  { label: "Cron", value: "cron" },
  { label: "周期 interval", value: "interval" },
  { label: "单次 at", value: "at" },
];

type DeliveryTargetKind = "session" | "channel";

type FormValues = {
  name: string;
  schedule_kind: FormScheduleKind;
  cron_expression: string;
  timezone: string;
  interval_seconds: number;
  at_iso: string;
  trading_session: "" | "ashare";
  execution_intent: ExecutionIntent;
  delivery_mode: DeliveryMode;
  target_kind: DeliveryTargetKind;
  /** Selected Feishu group chat id (``oc_…``); the bot (channel_id) + display
   * name are resolved from the loaded chat options at submit time. */
  channel_chat_id: string;
  no_signal_mode: NoSignalMode;
  /** Which assistant agent composes the prose-mode card text. Only meaningful
   * when ``delivery_mode === "prose"``; empty → backend picks the first active
   * agent. */
  composer_agent_id?: string;
};

type TemplateKey = "intraday_trade" | "close_signal" | "one_shot" | "custom";

type TemplatePatch = Partial<FormValues>;

const TEMPLATES: { key: TemplateKey; label: string; patch: TemplatePatch }[] = [
  {
    key: "intraday_trade",
    label: "盘中自动交易",
    patch: {
      schedule_kind: "cron",
      cron_expression: "*/5 9-11,13-15 * * mon-fri",
      timezone: "Asia/Shanghai",
      trading_session: "ashare",
      execution_intent: "trade",
      delivery_mode: "none",
    },
  },
  {
    key: "close_signal",
    label: "收盘推送信号",
    patch: {
      schedule_kind: "cron",
      cron_expression: "50 14 * * mon-fri",
      timezone: "Asia/Shanghai",
      trading_session: "ashare",
      execution_intent: "signal_only",
      delivery_mode: "card",
      target_kind: "session",
      no_signal_mode: "brief",
    },
  },
  {
    key: "one_shot",
    label: "一次性提醒",
    patch: {
      schedule_kind: "at",
      execution_intent: "signal_only",
      delivery_mode: "card",
      target_kind: "session",
    },
  },
  {
    key: "custom",
    label: "自定义",
    patch: {},
  },
];

const DEFAULT_VALUES: FormValues = {
  name: "",
  schedule_kind: "cron",
  cron_expression: "0 9 * * mon-fri",
  timezone: "Asia/Shanghai",
  interval_seconds: 300,
  at_iso: "",
  trading_session: "",
  execution_intent: "signal_only",
  delivery_mode: "card",
  target_kind: "session",
  channel_chat_id: "",
  no_signal_mode: "brief",
  composer_agent_id: undefined,
};

/** Map an existing trigger into editable form values. */
function triggerToFormValues(trigger: TaskTrigger): FormValues {
  const delivery = trigger.delivery_json;
  const mode: DeliveryMode = delivery?.mode ?? "none";
  const target = delivery?.target;
  // A channel target keeps channel; anything else (session/origin) edits as a
  // session target so the radio reflects "当前会话".
  const targetKind: DeliveryTargetKind = target?.kind === "channel" ? "channel" : "session";
  // Only cron/interval/at are editable; coerce others to cron so the form
  // always has a concrete kind to render (the original is preserved on the
  // server for unsupported kinds since we never resubmit them here).
  const scheduleKind: FormScheduleKind =
    trigger.schedule_kind === "interval" || trigger.schedule_kind === "at"
      ? trigger.schedule_kind
      : "cron";
  return {
    name: trigger.name,
    schedule_kind: scheduleKind,
    cron_expression: trigger.cron_expression ?? DEFAULT_VALUES.cron_expression,
    timezone: trigger.timezone || DEFAULT_VALUES.timezone,
    interval_seconds: trigger.interval_seconds ?? DEFAULT_VALUES.interval_seconds,
    at_iso: trigger.at_iso ?? "",
    trading_session: trigger.trading_session === "ashare" ? "ashare" : "",
    execution_intent: trigger.execution_intent,
    delivery_mode: mode,
    target_kind: targetKind,
    channel_chat_id: target?.chat_id ?? "",
    no_signal_mode: delivery?.no_signal_mode ?? "brief",
    composer_agent_id: delivery?.composer_agent_id ?? undefined,
  };
}

/** Build the snake_case create/update body the backend expects.
 *
 * For a channel target the caller passes the resolved ``{channel_id, chat_id,
 * chat_name}`` (the picker only stores the chat_id; the bot + display name are
 * resolved from the loaded chat options). */
function buildPayload(
  values: FormValues,
  channelTarget: DeliveryTarget | null,
): CreateTaskTriggerPayload {
  const payload: CreateTaskTriggerPayload = {
    name: values.name.trim(),
    schedule_kind: values.schedule_kind,
    execution_intent: values.execution_intent,
    trading_session: values.trading_session === "ashare" ? "ashare" : null,
    delivery_json: null,
  };

  switch (values.schedule_kind) {
    case "cron":
      payload.cron_expression = values.cron_expression.trim();
      payload.timezone = values.timezone;
      break;
    case "interval":
      payload.interval_seconds = values.interval_seconds;
      break;
    case "at":
      payload.at_iso = values.at_iso.trim();
      break;
  }

  if (values.delivery_mode !== "none") {
    payload.delivery_json = {
      mode: values.delivery_mode,
      target:
        values.target_kind === "channel"
          ? channelTarget ?? { kind: "channel", chat_id: values.channel_chat_id.trim() }
          : // Server auto-fills session_id from the calling session header.
            { kind: "session", origin: true },
      no_signal_mode: values.no_signal_mode,
    };
    // Only prose mode runs a composer agent; card renders deterministically and
    // none doesn't push at all, so neither ever carries composer_agent_id.
    const composerId = values.composer_agent_id?.trim();
    if (values.delivery_mode === "prose" && composerId) {
      payload.delivery_json.composer_agent_id = composerId;
    }
  }

  return payload;
}

export const TriggerFormModal: React.FC<Props> = ({ taskId, trigger, onSaved, onClose }) => {
  const [form] = Form.useForm<FormValues>();
  const [saving, setSaving] = React.useState(false);

  const isEditing = Boolean(trigger);
  const initialValues = React.useMemo<FormValues>(
    () => (trigger ? triggerToFormValues(trigger) : DEFAULT_VALUES),
    [trigger],
  );

  // Watch the fields that gate conditional sub-sections so the modal re-renders
  // when they change.
  const scheduleKind = Form.useWatch("schedule_kind", form) ?? initialValues.schedule_kind;
  const deliveryMode = Form.useWatch("delivery_mode", form) ?? initialValues.delivery_mode;
  const targetKind = Form.useWatch("target_kind", form) ?? initialValues.target_kind;

  // Live Feishu group list for the channel picker. Fetched once on mount; each
  // option = (bot × a group it belongs to), so selecting a group also pins the bot.
  const [chatOptions, setChatOptions] = React.useState<FeishuChatOption[]>([]);
  const [chatsLoading, setChatsLoading] = React.useState(false);
  const [chatsError, setChatsError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    setChatsLoading(true);
    setChatsError(null);
    void listFeishuChats()
      .then((items) => {
        if (cancelled) return;
        setChatOptions(items);
        const failed = items.filter((item) => item.error);
        if (items.length === 0) {
          setChatsError("未获取到飞书群：确认机器人已加入群、频道已启动，且具备 im:chat 读权限。");
        } else if (failed.length > 0) {
          setChatsError(`部分机器人拉取失败：${failed[0]!.error}`);
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setChatOptions([]);
        setChatsError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setChatsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Active assistant agents that can compose prose-mode push text. Fetched once
  // on mount; failure degrades to an empty list (the field then just shows the
  // "默认（首个可用 Agent）" placeholder) rather than silently swallowing the error.
  const [agentOptions, setAgentOptions] = React.useState<{ label: string; value: string }[]>([]);

  React.useEffect(() => {
    let cancelled = false;
    void listAssistantAgents({})
      .then((res) => {
        if (cancelled) return;
        setAgentOptions(
          res.items
            .filter((a) => a.status === "active")
            .map((a) => ({
              label: a.is_default ? `${a.name}（默认）` : a.name,
              value: a.id,
            })),
        );
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setAgentOptions([]);
        // Visible, not swallowed: the field still renders (empty options →
        // backend default) but the operator can see why the picker is empty.
        console.error("listAssistantAgents failed for trigger composer picker", err);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Only rows with a real chat_id are selectable. When editing a trigger whose
  // saved group isn't in the live list (bot offline / left the group), inject a
  // synthetic option so the Select still shows the saved target.
  const selectableChats = chatOptions.filter((item) => item.chat_id && !item.error);
  const editingTarget = trigger?.delivery_json?.target;
  const chatOptionsForSelect =
    editingTarget?.kind === "channel" &&
    editingTarget.chat_id &&
    !selectableChats.some((item) => item.chat_id === editingTarget.chat_id)
      ? [
          {
            channel_id: editingTarget.channel_id ?? "",
            channel_name: "已保存",
            chat_id: editingTarget.chat_id,
            name: editingTarget.chat_name || editingTarget.chat_id,
          },
          ...selectableChats,
        ]
      : selectableChats;
  // >1 distinct bot → disambiguate each group with its bot name in the label.
  const multipleBots = new Set(chatOptionsForSelect.map((item) => item.channel_id)).size > 1;
  const chatById = new Map(chatOptionsForSelect.map((item) => [item.chat_id, item] as const));

  const applyTemplate = (key: TemplateKey) => {
    const template = TEMPLATES.find((item) => item.key === key);
    if (!template) return;
    // Reset to defaults first so re-selecting a template gives a clean slate,
    // then layer the template patch and keep whatever name the user typed.
    const currentName = (form.getFieldValue("name") as string | undefined) ?? "";
    form.setFieldsValue({ ...DEFAULT_VALUES, name: currentName, ...template.patch });
  };

  const handleSubmit = async (values: FormValues) => {
    setSaving(true);
    try {
      let channelTarget: DeliveryTarget | null = null;
      if (values.delivery_mode !== "none" && values.target_kind === "channel") {
        const chatId = values.channel_chat_id.trim();
        const opt = chatById.get(chatId);
        channelTarget = {
          kind: "channel",
          // Resolve the bot from the picked group; fall back to the saved target
          // when editing offline. Empty channel_id → server rejects (visible).
          channel_id: opt?.channel_id ?? (editingTarget?.kind === "channel" ? editingTarget.channel_id ?? "" : ""),
          chat_id: chatId,
          chat_name: opt?.name ?? (editingTarget?.kind === "channel" ? editingTarget.chat_name : undefined),
          channel_type: "feishu",
        };
      }
      const payload = buildPayload(values, channelTarget);
      const saved = trigger
        ? await updateTaskTrigger(taskId, trigger.id, payload)
        : await createTaskTrigger(taskId, payload);
      message.success(isEditing ? "触发器已更新" : "触发器已创建");
      onSaved(saved);
    } catch (err) {
      // TriggerValidationError → 400 with {detail:{error_code,message,field}};
      // request() surfaces detail.message as ApiError.message.
      const detail = err instanceof Error ? err.message : String(err);
      message.error(detail || "保存触发器失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={isEditing ? "编辑触发器" : "新建触发器"}
      open
      onCancel={onClose}
      destroyOnHidden
      width={620}
      footer={[
        <Button key="cancel" autoInsertSpace={false} onClick={onClose}>
          取消
        </Button>,
        <Button key="submit" type="primary" autoInsertSpace={false} loading={saving} onClick={() => form.submit()}>
          {isEditing ? "保存触发器" : "创建触发器"}
        </Button>,
      ]}
    >
      <Form<FormValues>
        form={form}
        layout="vertical"
        initialValues={initialValues}
        onFinish={handleSubmit}
      >
        {!isEditing && (
          <Form.Item label="模板">
            <Space wrap size={8}>
              {TEMPLATES.map((template) => (
                <Button key={template.key} size="small" onClick={() => applyTemplate(template.key)}>
                  {template.label}
                </Button>
              ))}
            </Space>
          </Form.Item>
        )}

        <Form.Item name="name" label="名称" rules={[{ required: true, message: "请填写触发器名称" }]}>
          <Input placeholder="例如：收盘信号推送" />
        </Form.Item>

        <Form.Item name="schedule_kind" label="调度类型">
          <Segmented options={SCHEDULE_KIND_OPTIONS} />
        </Form.Item>

        {scheduleKind === "cron" && (
          <>
            <Form.Item
              name="cron_expression"
              label="Cron 表达式"
              rules={[{ required: true, message: "请填写 cron 表达式" }]}
            >
              <Input placeholder="*/5 9-11,13-15 * * mon-fri" />
            </Form.Item>
            <Form.Item name="timezone" label="时区">
              <Select options={TIMEZONES.map((tz) => ({ label: tz, value: tz }))} />
            </Form.Item>
            <Typography.Paragraph type="secondary" style={{ marginTop: -8, fontSize: 12 }}>
              五段式 <code>分 时 日 月 周</code>。A股 盘中可用 <code>*/5 9-11,13-15 * * mon-fri</code>，
              收盘后用 <code>50 14 * * mon-fri</code>。
            </Typography.Paragraph>
          </>
        )}

        {scheduleKind === "interval" && (
          <Form.Item
            name="interval_seconds"
            label="间隔（秒）"
            rules={[{ required: true, message: "请填写间隔秒数" }]}
          >
            <InputNumber min={1} max={86400} style={{ width: 200 }} addonAfter="秒" />
          </Form.Item>
        )}

        {scheduleKind === "at" && (
          <Form.Item
            name="at_iso"
            label="触发时刻（ISO-8601 带时区）"
            rules={[{ required: true, message: "请填写 ISO-8601 时刻" }]}
          >
            <Input placeholder="2026-06-12T09:25:00+08:00" />
          </Form.Item>
        )}

        <Form.Item name="trading_session" label="交易时段">
          <Select
            options={[
              { label: "无", value: "" },
              { label: "A股 ashare", value: "ashare" },
            ]}
          />
        </Form.Item>

        <Form.Item name="execution_intent" label="执行意图">
          <Radio.Group>
            <Radio value="signal_only">只出信号</Radio>
            <Radio value="trade">真实交易</Radio>
          </Radio.Group>
        </Form.Item>

        <Form.Item
          name="delivery_mode"
          label="推送方式"
          extra="卡片：确定性快讯（固定模板渲染）。文字：Agent 解读（读本轮行情与信号诊断后组织文案）。不推送：仅运行不发送。"
        >
          <Radio.Group>
            <Radio value="card">卡片</Radio>
            <Radio value="prose">文字</Radio>
            <Radio value="none">不推送</Radio>
          </Radio.Group>
        </Form.Item>

        {deliveryMode === "prose" && (
          <Form.Item
            name="composer_agent_id"
            label="解析 Agent"
            extra="留空则用首个可用 Agent；该 Agent 会读取本轮行情与信号诊断，组织成卡片文案。"
          >
            <Select
              allowClear
              placeholder="默认（首个可用 Agent）"
              options={agentOptions}
            />
          </Form.Item>
        )}

        {deliveryMode !== "none" && (
          <div className="mb-6 rounded-lg border border-shell-line bg-shell-bg p-3">
            <Form.Item name="target_kind" label="推送目标" style={{ marginBottom: 12 }}>
              <Radio.Group>
                <Radio value="session">当前会话</Radio>
                <Radio value="channel">飞书频道</Radio>
              </Radio.Group>
            </Form.Item>

            {targetKind === "channel" && (
              <Form.Item
                name="channel_chat_id"
                label="飞书群"
                rules={[{ required: true, message: "请选择要推送的飞书群" }]}
                style={{ marginBottom: 12 }}
                extra={chatsError ?? undefined}
                validateStatus={chatsError && chatOptionsForSelect.length === 0 ? "warning" : undefined}
              >
                <Select
                  showSearch
                  loading={chatsLoading}
                  placeholder="选择机器人所在的飞书群"
                  optionFilterProp="label"
                  notFoundContent={chatsLoading ? "加载中…" : "无可选飞书群"}
                  options={chatOptionsForSelect.map((item) => ({
                    label: multipleBots ? `${item.name}（${item.channel_name}）` : item.name,
                    value: item.chat_id,
                  }))}
                />
              </Form.Item>
            )}

            <Form.Item name="no_signal_mode" label="无信号时" style={{ marginBottom: 0 }}>
              <Select
                options={[
                  { label: "静默", value: "silent" },
                  { label: "简要", value: "brief" },
                  { label: "完整", value: "full" },
                ]}
              />
            </Form.Item>
          </div>
        )}

      </Form>
    </Modal>
  );
};
