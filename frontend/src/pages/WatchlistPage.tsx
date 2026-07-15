import React from "react";
import { useNavigate } from "react-router-dom";
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  message,
} from "antd";

import {
  addWatchlistEntry,
  deleteWatchlistEntry,
  listWatchlist,
  listWatchlistTags,
  updateWatchlistEntry,
  type CreateWatchlistPayload,
} from "../api";
import { ApiError } from "../api";
import { SymbolSingleSelect } from "../components/SymbolSearchSelect";
import { ChangePctTag, formatAmount } from "../components/StockDetailModal";
import { useMarketQuoteStream } from "../hooks/useMarketQuoteStream";
import { usePageRefreshToken } from "../pageRefreshContext";
import type { QuoteSnapshot, WatchlistEntry, WatchlistTagCount } from "../types";

// ---- Intraday microstructure metrics for the 短线 watch view ----------------
// All derived purely from the existing realtime QuoteSnapshot (zero new data).
// Every helper returns null for a missing / suspended quote or absent inputs so
// the cell renders an em-dash and never a fabricated number.

/** 振幅%: (high - low) / prev_close × 100. */
function amplitudePct(q?: QuoteSnapshot): number | null {
  if (!q || q.status === "suspended") return null;
  const { high, low, prev_close } = q;
  if (high == null || low == null || prev_close == null || prev_close === 0) return null;
  return ((high - low) / prev_close) * 100;
}

/** 委比%: (bid_vol1 - ask_vol1) / (bid_vol1 + ask_vol1) × 100 — level-1 approximation. */
function orderImbalancePct(q?: QuoteSnapshot): number | null {
  if (!q || q.status === "suspended") return null;
  const { bid_vol1, ask_vol1 } = q;
  if (bid_vol1 == null || ask_vol1 == null) return null;
  const total = bid_vol1 + ask_vol1;
  if (total <= 0) return null;
  return ((bid_vol1 - ask_vol1) / total) * 100;
}

/** 距涨停%: (limit_up_price - price) / price × 100 (0 ≈ 已封板). */
function distanceToLimitUpPct(q?: QuoteSnapshot): number | null {
  if (!q || q.status === "suspended") return null;
  const { price, limit_up_price } = q;
  if (price == null || price === 0 || limit_up_price == null) return null;
  return ((limit_up_price - price) / price) * 100;
}

/**
 * 封单量: the level-1 seal queue, but only meaningful when the stock is actually
 * at a limit. Returns the 涨停买一封单 (side "up") when price is at/above
 * limit_up_price, the 跌停卖一封单 (side "down") at/below limit_down_price, else null.
 */
function sealVolume(q?: QuoteSnapshot): { vol: number; side: "up" | "down" } | null {
  if (!q || q.status === "suspended" || q.price == null) return null;
  const { price, limit_up_price, limit_down_price, bid_vol1, ask_vol1 } = q;
  const eps = 1e-6;
  if (limit_up_price != null && price >= limit_up_price - eps && bid_vol1 != null) {
    return { vol: bid_vol1, side: "up" };
  }
  if (limit_down_price != null && price <= limit_down_price + eps && ask_vol1 != null) {
    return { vol: ask_vol1, side: "down" };
  }
  return null;
}

/** 封单量 shorthand: 亿 / 万 / raw shares. */
function formatSeal(value: number): string {
  if (Math.abs(value) >= 1e8) return `${(value / 1e8).toFixed(2)} 亿`;
  if (Math.abs(value) >= 1e4) return `${(value / 1e4).toFixed(1)} 万`;
  return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

/** Parse a comma / 空白 separated tag string into a unique, trimmed list. */
function parseTags(raw: string | undefined): string[] {
  if (!raw) {
    return [];
  }
  const seen = new Set<string>();
  const out: string[] = [];
  for (const piece of raw.split(/[,，\s]+/)) {
    const tag = piece.trim();
    if (tag && !seen.has(tag)) {
      seen.add(tag);
      out.push(tag);
    }
  }
  return out;
}

export function WatchlistPage() {
  const navigate = useNavigate();
  const pageRefreshToken = usePageRefreshToken();
  const [entries, setEntries] = React.useState<WatchlistEntry[]>([]);
  const [tagCounts, setTagCounts] = React.useState<WatchlistTagCount[]>([]);
  const [activeTag, setActiveTag] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);

  const [showAdd, setShowAdd] = React.useState(false);
  const [addForm] = Form.useForm<{ symbol: string; tags?: string; note?: string }>();
  // Display name of the catalog row the user picked, so the new entry carries
  // the name from the stock database rather than being left blank.
  const [selectedName, setSelectedName] = React.useState<string | null>(null);

  const [editing, setEditing] = React.useState<WatchlistEntry | null>(null);
  const [editForm] = Form.useForm<{ tags?: string; note?: string }>();
  const [submitting, setSubmitting] = React.useState(false);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const [list, tags] = await Promise.all([listWatchlist(activeTag ?? undefined), listWatchlistTags()]);
      setEntries(list.items ?? []);
      setTagCounts(tags.items ?? []);
    } catch (err) {
      message.error(`加载自选股失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setLoading(false);
    }
  }, [activeTag]);

  React.useEffect(() => {
    void load();
  }, [load, pageRefreshToken]);

  // Drive the realtime columns from the symbols currently shown.
  const visibleSymbols = React.useMemo(() => entries.map((e) => e.symbol), [entries]);
  const { quotes, qmtDisconnected } = useMarketQuoteStream(visibleSymbols);

  const openAdd = () => {
    addForm.resetFields();
    setSelectedName(null);
    setShowAdd(true);
  };

  const handleAdd = async () => {
    let values: { symbol: string; tags?: string; note?: string };
    try {
      values = await addForm.validateFields();
    } catch {
      return;
    }
    const payload: CreateWatchlistPayload = {
      symbol: values.symbol.trim(),
      display_name: selectedName ?? undefined,
      tags: parseTags(values.tags),
      note: values.note?.trim() ?? "",
    };
    setSubmitting(true);
    try {
      await addWatchlistEntry(payload);
      message.success("已加入自选");
      setShowAdd(false);
      await load();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        message.error(err.message || "该股票已在自选股中");
        return;
      }
      message.error(`加入自选失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSubmitting(false);
    }
  };

  const openEdit = (entry: WatchlistEntry) => {
    setEditing(entry);
    editForm.resetFields();
    editForm.setFieldsValue({ tags: entry.tags.join(", "), note: entry.note });
  };

  const handleEdit = async () => {
    if (!editing) {
      return;
    }
    let values: { tags?: string; note?: string };
    try {
      values = await editForm.validateFields();
    } catch {
      return;
    }
    setSubmitting(true);
    try {
      await updateWatchlistEntry(editing.id, {
        tags: parseTags(values.tags),
        note: values.note?.trim() ?? "",
      });
      message.success("已保存");
      setEditing(null);
      await load();
    } catch (err) {
      message.error(`保存失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSubmitting(false);
    }
  };

  const handleRemove = async (entry: WatchlistEntry) => {
    try {
      await deleteWatchlistEntry(entry.id);
      message.success("已移除");
      await load();
    } catch (err) {
      message.error(`移除失败：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  // Effective quote for a row: dashed out entirely when the market feed is down.
  const quoteFor = (record: WatchlistEntry): QuoteSnapshot | undefined =>
    qmtDisconnected ? undefined : quotes[record.symbol];

  // Numeric column sorter that keeps null/absent values at the low end (so a
  // descending click surfaces the real numbers first).
  const numSorter =
    (get: (q?: QuoteSnapshot) => number | null) =>
    (a: WatchlistEntry, b: WatchlistEntry) => {
      const av = get(quoteFor(a));
      const bv = get(quoteFor(b));
      if (av == null && bv == null) return 0;
      if (av == null) return -1;
      if (bv == null) return 1;
      return av - bv;
    };

  const columns = [
    {
      title: "代码",
      dataIndex: "symbol",
      key: "symbol",
    },
    {
      title: "名称",
      dataIndex: "display_name",
      key: "display_name",
      render: (name: string | null) => name || "—",
    },
    {
      title: "股价",
      key: "price",
      sorter: numSorter((q) => q?.price ?? null),
      render: (_: unknown, record: WatchlistEntry) => {
        if (qmtDisconnected) {
          return "—";
        }
        if (quotes[record.symbol]?.status === "suspended") {
          return <Tag color="default">停牌</Tag>;
        }
        const price = quotes[record.symbol]?.price;
        return price == null || !Number.isFinite(price)
          ? "—"
          : price.toLocaleString(undefined, { maximumFractionDigits: 2 });
      },
    },
    {
      title: "涨跌幅",
      key: "change_pct",
      sorter: numSorter((q) => q?.change_pct ?? null),
      render: (_: unknown, record: WatchlistEntry) =>
        !qmtDisconnected && quotes[record.symbol]?.status === "suspended" ? (
          <Tag color="default">停牌</Tag>
        ) : (
          <ChangePctTag value={qmtDisconnected ? null : quotes[record.symbol]?.change_pct} />
        ),
    },
    {
      title: "成交额",
      key: "amount",
      sorter: numSorter((q) => q?.amount ?? null),
      render: (_: unknown, record: WatchlistEntry) =>
        qmtDisconnected ? "—" : formatAmount(quotes[record.symbol]?.amount),
    },
    {
      title: "振幅",
      key: "amplitude",
      sorter: numSorter(amplitudePct),
      render: (_: unknown, record: WatchlistEntry) => {
        const v = amplitudePct(quoteFor(record));
        return v == null ? "—" : `${v.toFixed(2)}%`;
      },
    },
    {
      title: (
        <Tooltip title="买一 / 卖一封单量对比（一档近似）：正=买盘强">
          <span>委比</span>
        </Tooltip>
      ),
      key: "order_imbalance",
      sorter: numSorter(orderImbalancePct),
      render: (_: unknown, record: WatchlistEntry) => {
        const v = orderImbalancePct(quoteFor(record));
        if (v == null) return "—";
        return (
          <span style={{ color: v >= 0 ? "#cf1322" : "#389e0d" }}>
            {`${v >= 0 ? "+" : ""}${v.toFixed(1)}%`}
          </span>
        );
      },
    },
    {
      title: (
        <Tooltip title="现价距涨停价的百分比，越小越贴板，涨停显示「涨停」">
          <span>距涨停</span>
        </Tooltip>
      ),
      key: "dist_limit_up",
      sorter: numSorter(distanceToLimitUpPct),
      render: (_: unknown, record: WatchlistEntry) => {
        const v = distanceToLimitUpPct(quoteFor(record));
        if (v == null) return "—";
        if (v <= 0.001) return <Tag color="red">涨停</Tag>;
        return `${v.toFixed(2)}%`;
      },
    },
    {
      title: (
        <Tooltip title="涨停买一封单 / 跌停卖一封单量（仅封板时有值）">
          <span>封单</span>
        </Tooltip>
      ),
      key: "seal_volume",
      sorter: numSorter((q) => sealVolume(q)?.vol ?? null),
      render: (_: unknown, record: WatchlistEntry) => {
        const s = sealVolume(quoteFor(record));
        if (s == null) return "—";
        return <Tag color={s.side === "up" ? "red" : "green"}>{formatSeal(s.vol)}</Tag>;
      },
    },
    {
      title: "标签",
      dataIndex: "tags",
      key: "tags",
      render: (tags: string[]) =>
        tags.length > 0 ? (
          <Space size={[0, 4]} wrap>
            {tags.map((tag) => (
              <Tag key={tag} color="blue">
                {tag}
              </Tag>
            ))}
          </Space>
        ) : (
          "—"
        ),
    },
    {
      title: "操作",
      key: "actions",
      render: (_: unknown, record: WatchlistEntry) => (
        <Space onClick={(e) => e.stopPropagation()}>
          <Button size="small" onClick={() => openEdit(record)}>
            编辑标签/备注
          </Button>
          <Popconfirm title="从自选股移除？" onConfirm={() => void handleRemove(record)}>
            <Button size="small" danger>
              移除
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
        <h2 style={{ margin: 0 }}>自选股</h2>
        <Button type="primary" onClick={openAdd}>
          添加股票
        </Button>
      </div>

      {qmtDisconnected ? (
        <Alert
          className="mb-4"
          style={{ marginBottom: 16 }}
          type="warning"
          showIcon
          message="行情未连接（需配置默认 QMT 账户）"
        />
      ) : null}

      <Space wrap style={{ marginBottom: 16 }}>
        <Tag.CheckableTag checked={activeTag === null} onChange={() => setActiveTag(null)}>
          全部
        </Tag.CheckableTag>
        {tagCounts.map((tc) => (
          <Tag.CheckableTag
            key={tc.tag}
            checked={activeTag === tc.tag}
            onChange={() => setActiveTag(activeTag === tc.tag ? null : tc.tag)}
          >
            {`${tc.tag} (${tc.count})`}
          </Tag.CheckableTag>
        ))}
      </Space>

      <Table
        dataSource={entries}
        columns={columns}
        rowKey="id"
        loading={loading}
        pagination={false}
        scroll={{ x: "max-content" }}
        onRow={(record) => ({
          onClick: () => navigate(`/stocks/detail?symbol=${encodeURIComponent(record.symbol)}`),
          style: { cursor: "pointer" },
        })}
      />

      <Modal
        title="添加股票"
        open={showAdd}
        onCancel={() => setShowAdd(false)}
        onOk={() => void handleAdd()}
        confirmLoading={submitting}
        okText="添加"
        cancelText="取消"
        destroyOnHidden
      >
        <Form layout="vertical" form={addForm}>
          <Form.Item name="symbol" label="股票" rules={[{ required: true, message: "请从股票库选择" }]}>
            <SymbolSingleSelect
              onSelectOption={(option) => setSelectedName(option.name)}
              placeholder="输入代码、名称、拼音或首字母，从股票库中选择"
              emptyHint="股票库中无匹配，请先在『股票』页同步"
            />
          </Form.Item>
          <Form.Item name="tags" label="标签" extra="逗号分隔，可填多个，如 龙头, 半导体">
            <Input placeholder="龙头, 半导体" />
          </Form.Item>
          <Form.Item name="note" label="备注">
            <Input.TextArea rows={2} placeholder="可选" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="编辑标签/备注"
        open={editing !== null}
        onCancel={() => setEditing(null)}
        onOk={() => void handleEdit()}
        confirmLoading={submitting}
        okText="保存"
        cancelText="取消"
        destroyOnHidden
      >
        <Form layout="vertical" form={editForm}>
          <Form.Item label="股票">
            <Select disabled value={editing?.symbol} options={editing ? [{ value: editing.symbol, label: editing.display_name ? `${editing.display_name} (${editing.symbol})` : editing.symbol }] : []} />
          </Form.Item>
          <Form.Item name="tags" label="标签" extra="逗号分隔，可填多个">
            <Input placeholder="龙头, 半导体" />
          </Form.Item>
          <Form.Item name="note" label="备注">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

    </div>
  );
}
