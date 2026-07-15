import { Button, Input, Modal, Space, Table, Typography, message } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  clearInstrumentCatalog,
  deleteInstrumentCatalogSymbols,
  listInstrumentCatalog,
  searchInstrumentUniverse,
  syncInstrumentCatalog,
} from "../api";
import { usePageRefreshToken } from "../pageRefreshContext";
import type { InstrumentCatalogRow } from "../types";

import { DEFAULT_INSTRUMENT_SOURCE } from "../components/UniverseSymbolSelect";

export function StocksPage() {
  const pageRefreshToken = usePageRefreshToken();
  const navigate = useNavigate();
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [rows, setRows] = useState<InstrumentCatalogRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const pageSize = 30;
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listInstrumentCatalog({
        q: q.trim() || undefined,
        limit: pageSize,
        offset: (page - 1) * pageSize,
      });
      setRows(res.items);
      setTotal(res.total);
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [q, page]);

  useEffect(() => {
    void load();
  }, [load, pageRefreshToken]);

  const columns = useMemo(
    () => [
      { title: "代码", dataIndex: "symbol", key: "symbol" },
      { title: "名称", dataIndex: "display_name", key: "display_name" },
      { title: "市场", dataIndex: "market", key: "market" },
      {
        title: "同步来源",
        dataIndex: "last_sync_source",
        key: "last_sync_source",
      },
      {
        title: "同步时间",
        dataIndex: "last_sync_at",
        key: "last_sync_at",
        render: (t: string | null) => t ?? "—",
      },
    ],
    [],
  );

  const runFull = async (source: "akshare" | "qmt") => {
    setSyncing(true);
    try {
      const res = await syncInstrumentCatalog({ source, mode: "full" });
      message.success(`同步完成：新增 ${res.inserted}，更新 ${res.updated}（扫描 ${res.rows_seen} 行）`);
      await load();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  };

  const runSelected = async (source: "akshare" | "qmt") => {
    const syms = selectedKeys.map(String).filter(Boolean);
    if (!syms.length) {
      message.warning("请先在表格中勾选要刷新的标的");
      return;
    }
    setSyncing(true);
    try {
      const res = await syncInstrumentCatalog({ source, mode: "symbols", symbols: syms });
      message.success(`刷新：新增 ${res.inserted}，更新 ${res.updated}`);
      await load();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  };

  const importAkResultsToCatalog = async () => {
    const kw = q.trim();
    if (!kw) {
      message.warning("请在搜索框输入关键字，用 akshare 搜索后导入");
      return;
    }
    setSyncing(true);
    try {
      const found = await searchInstrumentUniverse({
        source: DEFAULT_INSTRUMENT_SOURCE,
        q: kw,
        limit: 50,
      });
      const symbols = found.items.map((i) => i.symbol);
      if (!symbols.length) {
        message.info("akshare 无匹配，未导入");
        return;
      }
      const res = await syncInstrumentCatalog({ source: "akshare", mode: "symbols", symbols });
      message.success(`已按 akshare 导入 ${symbols.length} 个代码（写入 ${res.rows_seen} 行）`);
      await load();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  };

  const deleteSelected = () => {
    const syms = selectedKeys.map(String).filter(Boolean);
    if (!syms.length) {
      message.warning("请先勾选要删除的标的");
      return;
    }
    const preview = syms.slice(0, 12).join(", ") + (syms.length > 12 ? " …" : "");
    Modal.confirm({
      title: "删除选中目录行",
      content: `将删除 ${syms.length} 条：${preview}`,
      okText: "删除",
      okButtonProps: { danger: true, loading: deleting },
      onOk: async () => {
        setDeleting(true);
        try {
          const res = await deleteInstrumentCatalogSymbols(syms);
          message.success(`已删除 ${res.deleted} 条`);
          setSelectedKeys([]);
          await load();
        } catch (e: unknown) {
          message.error(e instanceof Error ? e.message : String(e));
          throw e;
        } finally {
          setDeleting(false);
        }
      },
    });
  };

  const clearAll = () => {
    Modal.confirm({
      title: "清空全部门录",
      content:
        "将删除 instrument_catalog 表中的全部行。实例若仍引用已删代码，保存时会校验失败。此操作不可撤销。",
      okText: "清空全部",
      okButtonProps: { danger: true, loading: deleting },
      onOk: async () => {
        setDeleting(true);
        try {
          const res = await clearInstrumentCatalog("clear_all_instrument_catalog");
          message.success(`已清空，删除 ${res.deleted} 条`);
          setSelectedKeys([]);
          await load();
        } catch (e: unknown) {
          message.error(e instanceof Error ? e.message : String(e));
          throw e;
        } finally {
          setDeleting(false);
        }
      },
    });
  };

  return (
    <div className="flex flex-col gap-4">
      <Typography.Title level={3} className="!mb-0">
        股票目录
      </Typography.Title>
      <Typography.Paragraph type="secondary" className="!mb-0">
        手动从 akshare / QMT 同步到本地表；实例的观察标的与 universe 仅能从此目录选择。
      </Typography.Paragraph>
      <Space wrap className="flex-wrap">
        <Input.Search
          allowClear
          placeholder="过滤代码或名称"
          onSearch={(v) => {
            setPage(1);
            setQ(v);
          }}
          style={{ maxWidth: 320 }}
        />
        <Button loading={syncing} onClick={() => void runFull("akshare")}>
          全量同步 · akshare
        </Button>
        <Button loading={syncing} onClick={() => void runFull("qmt")}>
          全量同步 · QMT（板块合并）
        </Button>
        <Button loading={syncing} onClick={() => void runSelected("akshare")}>
          刷新选中 · akshare
        </Button>
        <Button loading={syncing} onClick={() => void runSelected("qmt")}>
          刷新选中 · QMT
        </Button>
        <Button loading={syncing} onClick={() => void importAkResultsToCatalog()}>
          akshare 搜索并导入（关键字=当前搜索框）
        </Button>
        <Button danger loading={deleting} disabled={syncing} onClick={() => deleteSelected()}>
          删除选中
        </Button>
        <Button danger type="primary" loading={deleting} disabled={syncing} onClick={() => clearAll()}>
          清空全部
        </Button>
      </Space>
      <Table<InstrumentCatalogRow>
        rowKey="symbol"
        loading={loading}
        dataSource={rows}
        columns={columns}
        rowSelection={{
          selectedRowKeys: selectedKeys,
          onChange: (keys) => setSelectedKeys(keys),
        }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: false,
          onChange: (p) => setPage(p),
        }}
        onRow={(record) => ({
          onClick: () => navigate(`/stocks/detail?symbol=${encodeURIComponent(record.symbol)}`),
        })}
      />
    </div>
  );
}
