import React from "react";
import {
  Button,
  Divider,
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Select,
  Space,
  Typography,
  message,
} from "antd";
import { DeleteOutlined } from "@ant-design/icons";

import { createMonitor, listFeishuChats, updateMonitor } from "../api";
import { ApiError } from "../api";
import { SymbolTagsSelect } from "./SymbolSearchSelect";
import type {
  ConditionLeaf,
  ConditionNode,
  CreateMonitorPayload,
  FeishuChatOption,
  MonitorPreset,
  MonitorPredicateField,
  MonitorPredicateOp,
  MonitorRule,
  MonitorScopeKind,
} from "../types";

type Props = {
  /** Existing rule to edit; ``undefined`` opens a fresh create form. */
  rule?: MonitorRule;
  onSaved: (rule: MonitorRule) => void;
  onClose: () => void;
};

/** The 6 built-in presets with their 中文 labels. */
const PRESET_OPTIONS: { label: string; value: MonitorPreset }[] = [
  { label: "涨停", value: "limit_up" },
  { label: "跌停", value: "limit_down" },
  { label: "涨停大减", value: "limit_up_seal_shrink" },
  { label: "跌停大减", value: "limit_down_seal_shrink" },
  { label: "涨停打开", value: "limit_up_open" },
  { label: "跌停打开", value: "limit_down_open" },
];

/** Whitelisted predicate fields (mirror the backend whitelist exactly). */
const FIELD_OPTIONS: { label: string; value: MonitorPredicateField }[] = [
  { label: "现价 price", value: "price" },
  { label: "涨跌幅 change_pct", value: "change_pct" },
  { label: "买一量 bid_vol1", value: "bid_vol1" },
  { label: "卖一量 ask_vol1", value: "ask_vol1" },
  { label: "涨停价 limit_up_price", value: "limit_up_price" },
  { label: "跌停价 limit_down_price", value: "limit_down_price" },
  { label: "封单峰值买 seal_peak_bid", value: "seal_peak_bid" },
  { label: "封单峰值卖 seal_peak_ask", value: "seal_peak_ask" },
  { label: "成交量 volume", value: "volume" },
  { label: "成交额 amount", value: "amount" },
];

const OP_OPTIONS: { label: string; value: MonitorPredicateOp }[] = [
  { label: ">", value: ">" },
  { label: "≥", value: ">=" },
  { label: "<", value: "<" },
  { label: "≤", value: "<=" },
  { label: "=", value: "==" },
  { label: "≠", value: "!=" },
];

// --- Editor-local leaf model -----------------------------------------------
// A single-level AND/OR tree over leaves. Each leaf is either a preset or a
// field predicate. We keep a flat editable list and assemble a ConditionNode
// on submit.

type EditorPresetLeaf = {
  uid: number;
  kind: "preset";
  preset: MonitorPreset;
};

type EditorPredicateLeaf = {
  uid: number;
  kind: "predicate";
  field: MonitorPredicateField;
  op: MonitorPredicateOp;
  value: number;
};

type EditorLeaf = EditorPresetLeaf | EditorPredicateLeaf;

let uidCounter = 0;
function nextUid(): number {
  uidCounter += 1;
  return uidCounter;
}

function newPresetLeaf(): EditorPresetLeaf {
  return { uid: nextUid(), kind: "preset", preset: "limit_up" };
}

function newPredicateLeaf(): EditorPredicateLeaf {
  return { uid: nextUid(), kind: "predicate", field: "change_pct", op: ">=", value: 5 };
}

/** Decompose a persisted ConditionNode into the flat editor model. Unknown /
 * deeply-nested shapes degrade to a single default preset leaf so the editor
 * never renders blank for an existing rule. */
function nodeToEditor(node: ConditionNode | undefined | null): {
  op: "and" | "or";
  leaves: EditorLeaf[];
} {
  if (!node || typeof node !== "object") {
    return { op: "and", leaves: [newPresetLeaf()] };
  }
  // Logical node: take its direct children that are leaves.
  if ("op" in node && Array.isArray((node as { children?: unknown }).children)) {
    const logical = node as { op: "and" | "or"; children: ConditionNode[] };
    const leaves: EditorLeaf[] = [];
    for (const child of logical.children) {
      const leaf = leafToEditor(child as ConditionLeaf);
      if (leaf) leaves.push(leaf);
    }
    return {
      op: logical.op === "or" ? "or" : "and",
      leaves: leaves.length > 0 ? leaves : [newPresetLeaf()],
    };
  }
  // Bare leaf.
  const leaf = leafToEditor(node as ConditionLeaf);
  return { op: "and", leaves: leaf ? [leaf] : [newPresetLeaf()] };
}

function leafToEditor(node: ConditionLeaf): EditorLeaf | null {
  if (!node || typeof node !== "object") {
    return null;
  }
  if ("preset" in node && typeof node.preset === "string") {
    return { uid: nextUid(), kind: "preset", preset: node.preset };
  }
  if ("predicate" in node && node.predicate && typeof node.predicate === "object") {
    const p = node.predicate;
    return {
      uid: nextUid(),
      kind: "predicate",
      field: p.field as MonitorPredicateField,
      op: p.op,
      value: typeof p.value === "number" ? p.value : Number(p.value) || 0,
    };
  }
  return null;
}

/** Assemble the flat editor model into a ConditionNode. When exactly one preset
 * leaf and nothing else, emit the bare leaf; otherwise wrap in an and/or node. */
function editorToNode(op: "and" | "or", leaves: EditorLeaf[]): ConditionNode {
  const nodes: ConditionLeaf[] = leaves.map((leaf) =>
    leaf.kind === "preset"
      ? { preset: leaf.preset }
      : { predicate: { field: leaf.field, op: leaf.op, value: leaf.value } },
  );
  if (nodes.length === 1) {
    return nodes[0]!;
  }
  return { op, children: nodes };
}

type ScopeFields = {
  name: string;
  scope_kind: MonitorScopeKind;
  scope_tag: string;
  scope_symbols: string[];
  cooldown_seconds: number;
  target_kind: "none" | "channel";
  channel_chat_id: string;
};

/** Normalize the selected symbols into a unique, trimmed list. */
function normalizeSymbols(raw: string[] | undefined): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const piece of raw ?? []) {
    const sym = piece.trim();
    if (sym && !seen.has(sym)) {
      seen.add(sym);
      out.push(sym);
    }
  }
  return out;
}

export const MonitorFormModal: React.FC<Props> = ({ rule, onSaved, onClose }) => {
  const isEditing = Boolean(rule);
  const [form] = Form.useForm<ScopeFields>();
  const [saving, setSaving] = React.useState(false);

  // Condition tree editor state (lives outside antd Form so we control the
  // ConditionNode assembly explicitly).
  const initialTree = React.useMemo(() => nodeToEditor(rule?.condition_json), [rule]);
  const [rootOp, setRootOp] = React.useState<"and" | "or">(initialTree.op);
  const [leaves, setLeaves] = React.useState<EditorLeaf[]>(initialTree.leaves);

  const initialValues = React.useMemo<ScopeFields>(() => {
    const target = rule?.delivery_json?.target;
    return {
      name: rule?.name ?? "",
      scope_kind: rule?.scope_kind ?? "watchlist_tag",
      scope_tag: rule?.scope_json?.tag ?? "",
      scope_symbols: [...(rule?.scope_json?.symbols ?? [])],
      cooldown_seconds: rule?.cooldown_seconds ?? 300,
      target_kind: target?.kind === "channel" ? "channel" : "none",
      channel_chat_id: target?.kind === "channel" ? target.chat_id ?? "" : "",
    };
  }, [rule]);

  const scopeKind = Form.useWatch("scope_kind", form) ?? initialValues.scope_kind;
  const targetKind = Form.useWatch("target_kind", form) ?? initialValues.target_kind;

  // Live Feishu group list for the channel picker (same idiom as TriggerFormModal).
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

  // Only rows with a real chat_id are selectable. When editing a rule whose
  // saved group isn't in the live list, inject a synthetic option so the Select
  // still shows the saved target.
  const selectableChats = chatOptions.filter((item) => item.chat_id && !item.error);
  const editingTarget = rule?.delivery_json?.target;
  const chatOptionsForSelect =
    editingTarget?.kind === "channel" &&
    editingTarget.chat_id &&
    !selectableChats.some((item) => item.chat_id === editingTarget.chat_id)
      ? [
          {
            channel_id: editingTarget.channel_id ?? "",
            channel_name: "已保存",
            chat_id: editingTarget.chat_id,
            name: editingTarget.chat_id,
          },
          ...selectableChats,
        ]
      : selectableChats;
  const multipleBots = new Set(chatOptionsForSelect.map((item) => item.channel_id)).size > 1;
  const chatById = new Map(chatOptionsForSelect.map((item) => [item.chat_id, item] as const));

  // --- Leaf mutation helpers ----
  const addPreset = () => setLeaves((prev) => [...prev, newPresetLeaf()]);
  const addPredicate = () => setLeaves((prev) => [...prev, newPredicateLeaf()]);
  const removeLeaf = (uid: number) =>
    setLeaves((prev) => (prev.length <= 1 ? prev : prev.filter((leaf) => leaf.uid !== uid)));
  const patchLeaf = (uid: number, patch: Partial<EditorLeaf>) =>
    setLeaves((prev) =>
      prev.map((leaf) => (leaf.uid === uid ? ({ ...leaf, ...patch } as EditorLeaf) : leaf)),
    );

  const handleSubmit = async (values: ScopeFields) => {
    if (leaves.length === 0) {
      message.error("请至少添加一个条件");
      return;
    }
    const condition = editorToNode(rootOp, leaves);
    const scope =
      values.scope_kind === "watchlist_tag"
        ? { tag: values.scope_tag.trim() }
        : { symbols: normalizeSymbols(values.scope_symbols) };

    const payload: CreateMonitorPayload = {
      name: values.name.trim(),
      scope_kind: values.scope_kind,
      scope,
      condition_json: condition,
      cooldown_seconds: values.cooldown_seconds,
    };

    if (values.target_kind === "channel") {
      const chatId = values.channel_chat_id.trim();
      const opt = chatById.get(chatId);
      const channelId =
        opt?.channel_id ??
        (editingTarget?.kind === "channel" ? editingTarget.channel_id ?? "" : "");
      payload.channel_id = channelId;
      payload.chat_id = chatId;
    }

    setSaving(true);
    try {
      const saved = rule
        ? await updateMonitor(rule.id, payload)
        : await createMonitor(payload);
      message.success(isEditing ? "盯盘规则已更新" : "盯盘规则已创建");
      onSaved(saved);
    } catch (err) {
      // Bad condition / scope → 400 with {detail:{error_code,message,field?}};
      // request() surfaces detail.message as ApiError.message.
      if (err instanceof ApiError) {
        message.error(err.message || "保存盯盘规则失败");
      } else {
        message.error(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={isEditing ? "编辑盯盘规则" : "新建盯盘"}
      open
      onCancel={onClose}
      destroyOnHidden
      width={640}
      footer={[
        <Button key="cancel" autoInsertSpace={false} onClick={onClose}>
          取消
        </Button>,
        <Button
          key="submit"
          type="primary"
          autoInsertSpace={false}
          loading={saving}
          onClick={() => form.submit()}
        >
          {isEditing ? "保存规则" : "创建规则"}
        </Button>,
      ]}
    >
      <Form<ScopeFields>
        form={form}
        layout="vertical"
        initialValues={initialValues}
        onFinish={handleSubmit}
      >
        <Form.Item name="name" label="名称" rules={[{ required: true, message: "请填写规则名称" }]}>
          <Input placeholder="例如：白酒板块涨停盯盘" />
        </Form.Item>

        <Form.Item name="scope_kind" label="监控范围">
          <Radio.Group>
            <Radio value="watchlist_tag">自选股标签</Radio>
            <Radio value="symbols">指定股票</Radio>
          </Radio.Group>
        </Form.Item>

        {scopeKind === "watchlist_tag" ? (
          <Form.Item
            name="scope_tag"
            label="标签"
            rules={[{ required: true, message: "请填写自选股标签" }]}
            extra="按自选股标签动态解析监控池，例如 白酒 / 龙头。"
          >
            <Input placeholder="白酒" />
          </Form.Item>
        ) : (
          <Form.Item
            name="scope_symbols"
            label="股票代码"
            rules={[{ required: true, message: "请选择至少一个股票代码" }]}
            extra="输入代码 / 名称 / 拼音搜索后选中添加；粘贴逗号分隔的代码也会自动拆分。"
          >
            <SymbolTagsSelect placeholder="搜索添加，例如 600519 或 贵州茅台" />
          </Form.Item>
        )}

        <Divider style={{ margin: "8px 0 16px" }} orientation="left" orientationMargin={0}>
          触发条件
        </Divider>

        <div className="mb-4 rounded-lg border border-shell-line bg-shell-bg p-3">
          <Form.Item label="条件组合" style={{ marginBottom: 12 }}>
            <Radio.Group value={rootOp} onChange={(e) => setRootOp(e.target.value as "and" | "or")}>
              <Radio value="and">满足全部 (AND)</Radio>
              <Radio value="or">满足任一 (OR)</Radio>
            </Radio.Group>
          </Form.Item>

          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            {leaves.map((leaf) => (
              <div
                key={leaf.uid}
                style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}
              >
                {leaf.kind === "preset" ? (
                  <>
                    <Typography.Text type="secondary" style={{ width: 56 }}>
                      预设
                    </Typography.Text>
                    <Select
                      value={leaf.preset}
                      style={{ minWidth: 160 }}
                      options={PRESET_OPTIONS}
                      onChange={(value) => patchLeaf(leaf.uid, { preset: value })}
                    />
                  </>
                ) : (
                  <>
                    <Typography.Text type="secondary" style={{ width: 56 }}>
                      字段
                    </Typography.Text>
                    <Select
                      value={leaf.field}
                      style={{ minWidth: 200 }}
                      options={FIELD_OPTIONS}
                      onChange={(value) => patchLeaf(leaf.uid, { field: value })}
                    />
                    <Select
                      value={leaf.op}
                      style={{ width: 76 }}
                      options={OP_OPTIONS}
                      onChange={(value) => patchLeaf(leaf.uid, { op: value })}
                    />
                    <InputNumber
                      value={leaf.value}
                      style={{ width: 120 }}
                      onChange={(value) =>
                        patchLeaf(leaf.uid, { value: typeof value === "number" ? value : 0 })
                      }
                    />
                  </>
                )}
                <Button
                  size="small"
                  type="text"
                  danger
                  icon={<DeleteOutlined />}
                  aria-label="移除条件"
                  disabled={leaves.length <= 1}
                  onClick={() => removeLeaf(leaf.uid)}
                />
              </div>
            ))}
          </Space>

          <Space style={{ marginTop: 12 }}>
            <Button size="small" onClick={addPreset}>
              添加预设
            </Button>
            <Button size="small" onClick={addPredicate}>
              添加字段条件
            </Button>
          </Space>
        </div>

        <Form.Item name="cooldown_seconds" label="冷却时间（秒）" extra="同一标的再次触发的最短间隔。">
          <InputNumber min={0} max={86400} style={{ width: 200 }} addonAfter="秒" />
        </Form.Item>

        <Divider style={{ margin: "8px 0 16px" }} orientation="left" orientationMargin={0}>
          推送
        </Divider>

        <Form.Item name="target_kind" label="推送目标">
          <Radio.Group>
            <Radio value="none">不推送</Radio>
            <Radio value="channel">飞书频道</Radio>
          </Radio.Group>
        </Form.Item>

        {targetKind === "channel" && (
          <Form.Item
            name="channel_chat_id"
            label="飞书群"
            rules={[{ required: true, message: "请选择要推送的飞书群" }]}
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
      </Form>
    </Modal>
  );
};
