import { Button, Space, Spin, Tag, Typography, message } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  ApiError,
  addWatchlistEntry,
  deleteWatchlistEntry,
  getInstrumentCatalogItem,
  listWatchlist,
  syncInstrumentCatalog,
} from "../api";
import { LocalMarketKlinePanel } from "../components/LocalMarketKlinePanel";
import { usePageRefreshToken } from "../pageRefreshContext";
import type { InstrumentCatalogRow, WatchlistEntry } from "../types";

export function StockDetailPage() {
  const pageRefreshToken = usePageRefreshToken();
  const [params] = useSearchParams();
  const requestedSymbol = useMemo(() => params.get("symbol")?.trim() ?? "", [params]);
  const [row, setRow] = useState<InstrumentCatalogRow | null>(null);
  const [watchlistEntry, setWatchlistEntry] = useState<WatchlistEntry | null>(null);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [watchlistBusy, setWatchlistBusy] = useState(false);

  const resolvedSymbol = useMemo(
    () => row?.symbol?.trim() || requestedSymbol,
    [row?.symbol, requestedSymbol],
  );

  const loadWatchlistEntry = useCallback(async (targetSymbol: string) => {
    if (!targetSymbol) {
      setWatchlistEntry(null);
      return;
    }
    try {
      const list = await listWatchlist();
      setWatchlistEntry(list.items.find((item) => item.symbol === targetSymbol) ?? null);
    } catch {
      setWatchlistEntry(null);
    }
  }, []);

  const load = useCallback(async () => {
    if (!requestedSymbol) {
      setRow(null);
      setWatchlistEntry(null);
      return;
    }
    setLoading(true);
    try {
      const r = await getInstrumentCatalogItem(requestedSymbol);
      setRow(r);
      await loadWatchlistEntry(r.symbol?.trim() || requestedSymbol);
    } catch {
      setRow(null);
      message.error("未找到该标的或未入库");
    } finally {
      setLoading(false);
    }
  }, [requestedSymbol, loadWatchlistEntry]);

  useEffect(() => {
    void load();
  }, [load, pageRefreshToken]);

  const toggleWatchlist = async () => {
    if (!resolvedSymbol || !row) {
      return;
    }
    setWatchlistBusy(true);
    try {
      if (watchlistEntry) {
        await deleteWatchlistEntry(watchlistEntry.id);
        message.success("已移除自选");
        setWatchlistEntry(null);
        return;
      }
      const created = await addWatchlistEntry({
        symbol: resolvedSymbol,
        display_name: row.display_name ?? undefined,
      });
      message.success("已加入自选");
      setWatchlistEntry(created);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        message.error(err.message || "该股票已在自选股中");
        await loadWatchlistEntry();
        return;
      }
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setWatchlistBusy(false);
    }
  };

  const refreshOne = async (source: "akshare" | "qmt") => {
    if (!resolvedSymbol) return;
    setSyncing(true);
    try {
      await syncInstrumentCatalog({ source, mode: "symbols", symbols: [resolvedSymbol] });
      message.success("已刷新");
      await load();
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  };

  if (!requestedSymbol) {
    return <Typography.Text type="secondary">缺少 query 参数 symbol</Typography.Text>;
  }

  if (loading) {
    return <Spin />;
  }

  if (!row) {
    return <Typography.Text type="danger">无法加载 {requestedSymbol}</Typography.Text>;
  }

  return (
    <div className="flex flex-col gap-4">
      <section className="flex flex-wrap items-start justify-between gap-4 rounded border border-gray-200 px-4 py-3">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="flex items-center gap-3">
            <Typography.Title level={3} className="!mb-0 !leading-tight">
              {row.display_name ?? "—"}{" "}
              <Typography.Text type="secondary" className="text-base">
                {row.symbol}
              </Typography.Text>
            </Typography.Title>
            {watchlistEntry && watchlistEntry.tags.length > 0 ? (
              <Space size={[0, 4]} wrap>
                {watchlistEntry.tags.map((tag) => (
                  <Tag key={tag} color="blue" className="!m-0">
                    {tag}
                  </Tag>
                ))}
              </Space>
            ) : null}
          </div>
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-gray-500">
            <span>市场: {row.market ?? "—"}</span>
            <span>类型: {row.instrument_type ?? "—"}</span>
            <span>可交易: {row.is_tradable == null ? "—" : row.is_tradable ? "是" : "否"}</span>
            <span>同步来源: {row.last_sync_source}</span>
            <span>同步时间: {row.last_sync_at ?? "—"}</span>
          </div>
        </div>
        <Space size={8} wrap>
          <Button
            size="small"
            type={watchlistEntry ? "default" : "primary"}
            danger={watchlistEntry != null}
            loading={watchlistBusy}
            onClick={() => void toggleWatchlist()}
          >
            {watchlistEntry ? "移除自选" : "加入自选"}
          </Button>
          <Button size="small" loading={syncing} onClick={() => void refreshOne("akshare")}>
            从 akshare 刷新
          </Button>
          <Button size="small" loading={syncing} onClick={() => void refreshOne("qmt")}>
            从 QMT 刷新
          </Button>
        </Space>
      </section>

      <section className="flex flex-1 flex-col gap-2 min-h-0">
        <Typography.Title level={4} className="!mb-0 !leading-tight">
          本地 K 线
        </Typography.Title>
        <Typography.Paragraph type="secondary" className="!mb-0 text-xs">
          数据来自本地 <code>market_bars</code> 行情仓库（默认 SQLite，可选 TimescaleDB；后台同步任务写入）。若为空请先确认{" "}
          <code>market_data.database_url</code> 与同步状态。
        </Typography.Paragraph>
        <LocalMarketKlinePanel symbol={resolvedSymbol} />
      </section>
    </div>
  );
}
