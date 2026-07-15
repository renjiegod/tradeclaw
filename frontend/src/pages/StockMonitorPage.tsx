import React from "react";
import {
  Alert,
  Button,
  Empty,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";

import {
  deleteMonitor,
  disableMonitor,
  enableMonitor,
  listMonitorAlerts,
  listMonitors,
  runMonitorOnce,
} from "../api";
import { MonitorFormModal } from "../components/MonitorFormModal";
import { usePageRefreshToken } from "../pageRefreshContext";
import type {
  ConditionLeaf,
  ConditionNode,
  MonitorAlert,
  MonitorPreset,
  MonitorRule,
  MonitorStatus,
} from "../types";

/** 中文 labels for the 6 presets (kept local; mirrors MonitorFormModal). */
const PRESET_LABELS: Record<MonitorPreset, string> = {
  limit_up: "涨停",
  limit_down: "跌停",
  limit_up_seal_shrink: "涨停大减",
  limit_down_seal_shrink: "跌停大减",
  limit_up_open: "涨停打开",
  limit_down_open: "跌停打开",
};

const STATUS_META: Record<MonitorStatus, { label: string; color: string }> = {
  active: { label: "运行中", color: "green" },
  paused: { label: "已暂停", color: "default" },
  error: { label: "错误", color: "red" },
};

/** Render a single leaf as a short human-readable string. */
function leafSummary(leaf: ConditionLeaf): string {
  if ("preset" in leaf && typeof leaf.preset === "string") {
    return PRESET_LABELS[leaf.preset as MonitorPreset] ?? leaf.preset;
  }
  if ("predicate" in leaf && leaf.predicate) {
    const p = leaf.predicate;
    return `${p.field} ${p.op} ${p.value}`;
  }
  return "?";
}

/** Build a compact one-line summary of a condition tree. */
function conditionSummary(node: ConditionNode | undefined | null): string {
  if (!node || typeof node !== "object") {
    return "—";
  }
  if ("op" in node && Array.isArray((node as { children?: unknown }).children)) {
    const logical = node as { op: "and" | "or"; children: ConditionNode[] };
    const joiner = logical.op === "or" ? " 或 " : " 且 ";
    const parts = logical.children.map((child) =>
      "op" in child && Array.isArray((child as { children?: unknown }).children)
        ? `(${conditionSummary(child)})`
        : leafSummary(child as ConditionLeaf),
    );
    return parts.join(joiner) || "—";
  }
  return leafSummary(node as ConditionLeaf);
}

/** Describe a rule's scope for the table column. */
function scopeSummary(rule: MonitorRule): string {
  if (rule.scope_kind === "watchlist_tag") {
    return `标签：${rule.scope_json.tag ?? "—"}`;
  }
  const symbols = rule.scope_json.symbols ?? [];
  if (symbols.length === 0) {
    return "指定股票：—";
  }
  if (symbols.length <= 3) {
    return `指定股票：${symbols.join(", ")}`;
  }
  return `指定股票：${symbols.slice(0, 3).join(", ")} 等 ${symbols.length} 只`;
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) {
    return "—";
  }
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function formatPrice(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) {
    return "—";
  }
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

export function StockMonitorPage() {
  const pageRefreshToken = usePageRefreshToken();
  const [rules, setRules] = React.useState<MonitorRule[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [actingId, setActingId] = React.useState<string | null>(null);

  const [editing, setEditing] = React.useState<MonitorRule | null>(null);
  const [creating, setCreating] = React.useState(false);

  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [alerts, setAlerts] = React.useState<MonitorAlert[]>([]);
  const [alertsLoading, setAlertsLoading] = React.useState(false);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const resp = await listMonitors();
      const items = resp.items ?? [];
      setRules(items);
      // Keep a selection so the alerts panel always has a rule to show.
      setSelectedId((prev) => {
        if (prev && items.some((r) => r.id === prev)) {
          return prev;
        }
        return items.length > 0 ? items[0]!.id : null;
      });
    } catch (err) {
      message.error(`加载盯盘规则失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load, pageRefreshToken]);

  const loadAlerts = React.useCallback(async (ruleId: string) => {
    setAlertsLoading(true);
    try {
      const resp = await listMonitorAlerts(ruleId, { limit: 50 });
      setAlerts(resp.items ?? []);
    } catch (err) {
      message.error(`加载触发记录失败：${err instanceof Error ? err.message : String(err)}`);
      setAlerts([]);
    } finally {
      setAlertsLoading(false);
    }
  }, []);

  React.useEffect(() => {
    if (selectedId) {
      void loadAlerts(selectedId);
    } else {
      setAlerts([]);
    }
  }, [selectedId, loadAlerts, pageRefreshToken]);

  const handleToggle = async (rule: MonitorRule) => {
    setActingId(rule.id);
    try {
      if (rule.enabled) {
        await disableMonitor(rule.id);
        message.success("已暂停");
      } else {
        await enableMonitor(rule.id);
        message.success("已启用");
      }
      await load();
    } catch (err) {
      message.error(`操作失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setActingId(null);
    }
  };

  const handleDelete = async (rule: MonitorRule) => {
    setActingId(rule.id);
    try {
      await deleteMonitor(rule.id);
      message.success("已删除");
      if (selectedId === rule.id) {
        setSelectedId(null);
      }
      await load();
    } catch (err) {
      message.error(`删除失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setActingId(null);
    }
  };

  const handleRunOnce = async (rule: MonitorRule) => {
    setActingId(rule.id);
    try {
      const result = await runMonitorOnce(rule.id);
      message.success(`已试跑：命中 ${result.matched_count} / ${result.symbols.length} 只`);
      // Surface any freshly-recorded alerts in the panel for this rule.
      setSelectedId(rule.id);
      await loadAlerts(rule.id);
    } catch (err) {
      message.error(`试跑失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setActingId(null);
    }
  };

  const ruleColumns = [
    {
      title: "名称",
      dataIndex: "name",
      key: "name",
      render: (name: string) => <Typography.Text strong>{name}</Typography.Text>,
    },
    {
      title: "范围",
      key: "scope",
      render: (_: unknown, record: MonitorRule) => scopeSummary(record),
    },
    {
      title: "条件",
      key: "condition",
      render: (_: unknown, record: MonitorRule) => (
        <Tooltip title={JSON.stringify(record.condition_json)}>
          <span>{conditionSummary(record.condition_json)}</span>
        </Tooltip>
      ),
    },
    {
      title: "状态",
      key: "status",
      render: (_: unknown, record: MonitorRule) => {
        const meta = STATUS_META[record.status] ?? { label: record.status, color: "default" };
        return (
          <Space direction="vertical" size={0}>
            <Tag color={meta.color}>{meta.label}</Tag>
            {record.status === "error" && record.last_error ? (
              <Typography.Text type="danger" style={{ fontSize: 12 }}>
                {record.last_error}
              </Typography.Text>
            ) : null}
          </Space>
        );
      },
    },
    {
      title: "冷却",
      dataIndex: "cooldown_seconds",
      key: "cooldown_seconds",
      render: (value: number) => `${value}s`,
    },
    {
      title: "操作",
      key: "actions",
      render: (_: unknown, record: MonitorRule) => (
        <Space onClick={(e) => e.stopPropagation()} wrap>
          <Button
            size="small"
            loading={actingId === record.id}
            onClick={() => void handleToggle(record)}
          >
            {record.enabled ? "暂停" : "启用"}
          </Button>
          <Button
            size="small"
            loading={actingId === record.id}
            onClick={() => void handleRunOnce(record)}
          >
            立即试跑
          </Button>
          <Button size="small" onClick={() => setEditing(record)}>
            编辑
          </Button>
          <Popconfirm title="删除该盯盘规则？" onConfirm={() => void handleDelete(record)}>
            <Button size="small" danger loading={actingId === record.id}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const alertColumns = [
    {
      title: "时间",
      dataIndex: "triggered_at",
      key: "triggered_at",
      render: (iso: string) => formatTime(iso),
    },
    {
      title: "代码",
      dataIndex: "symbol",
      key: "symbol",
    },
    {
      title: "条件",
      dataIndex: "condition_name",
      key: "condition_name",
    },
    {
      title: "触发价",
      dataIndex: "last_price",
      key: "last_price",
      render: (value: number | null) => formatPrice(value),
    },
    {
      title: "限价",
      dataIndex: "limit_price",
      key: "limit_price",
      render: (value: number | null) => formatPrice(value),
    },
    {
      title: "推送",
      dataIndex: "delivery_status",
      key: "delivery_status",
      render: (status: string) => status || "—",
    },
  ];

  const selectedRule = rules.find((r) => r.id === selectedId) ?? null;

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
        <h2 style={{ margin: 0 }}>股票智能盯盘</h2>
        <Button type="primary" onClick={() => setCreating(true)}>
          新建盯盘
        </Button>
      </div>

      <Table<MonitorRule>
        dataSource={rules}
        columns={ruleColumns}
        rowKey="id"
        loading={loading}
        pagination={false}
        rowClassName={(record) => (record.id === selectedId ? "ant-table-row-selected" : "")}
        onRow={(record) => ({
          onClick: () => setSelectedId(record.id),
          style: { cursor: "pointer" },
        })}
        locale={{ emptyText: <Empty description="还没有盯盘规则，点击右上角新建。" /> }}
      />

      <div style={{ marginTop: 24 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>触发记录</h3>
          {selectedRule ? (
            <Typography.Text type="secondary">（{selectedRule.name}）</Typography.Text>
          ) : null}
        </div>
        {selectedRule ? (
          <Table<MonitorAlert>
            dataSource={alerts}
            columns={alertColumns}
            rowKey="id"
            loading={alertsLoading}
            pagination={false}
            size="small"
            locale={{ emptyText: <Empty description="暂无触发记录" /> }}
          />
        ) : (
          <Alert type="info" showIcon message="选择上方一条规则以查看其触发记录。" />
        )}
      </div>

      {creating ? (
        <MonitorFormModal
          onClose={() => setCreating(false)}
          onSaved={() => {
            setCreating(false);
            void load();
          }}
        />
      ) : null}

      {editing ? (
        <MonitorFormModal
          rule={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void load();
          }}
        />
      ) : null}
    </div>
  );
}
