import { Modal, Space, Spin, Tag, Typography } from "antd";
import { useEffect, useState } from "react";

import { getInstrumentCatalogItem, getQuotesOnce } from "../api";
import type { InstrumentCatalogRow, QuoteSnapshot } from "../types";
import { instrumentTypeLabel } from "../utils/instrumentType";
import { LocalMarketKlinePanel } from "./LocalMarketKlinePanel";

type StockDetailModalProps = {
  /** Canonical symbol to inspect, or ``null`` to keep the modal closed. */
  symbol: string | null;
  onClose: () => void;
  /**
   * Optional live quote for ``symbol`` supplied by the parent (e.g. the
   * watchlist WebSocket stream). When absent, the modal fetches a one-shot
   * snapshot via ``getQuotesOnce`` so it still shows a price standalone.
   */
  quote?: QuoteSnapshot | null;
};

function formatNumber(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) {
    return "—";
  }
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

/** Human-readable 成交额: 亿 for ≥1e8, 万 for ≥1e4, else raw. */
export function formatAmount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) {
    return "—";
  }
  const abs = Math.abs(value);
  if (abs >= 1e8) {
    return `${(value / 1e8).toFixed(2)} 亿`;
  }
  if (abs >= 1e4) {
    return `${(value / 1e4).toFixed(2)} 万`;
  }
  return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

/** Red-up / green-down tag for a percentage change; ``—`` when null. */
export function ChangePctTag({ value }: { value: number | null | undefined }) {
  if (value == null || !Number.isFinite(value)) {
    return <span>—</span>;
  }
  const color = value > 0 ? "red" : value < 0 ? "green" : "default";
  const sign = value > 0 ? "+" : "";
  return <Tag color={color}>{`${sign}${value.toFixed(2)}%`}</Tag>;
}

export function StockDetailModal({ symbol, onClose, quote }: StockDetailModalProps) {
  const open = symbol !== null;
  const [row, setRow] = useState<InstrumentCatalogRow | null>(null);
  const [fetchedQuote, setFetchedQuote] = useState<QuoteSnapshot | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!symbol) {
      setRow(null);
      setFetchedQuote(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    // Catalog info + a one-shot quote in parallel. The parent-supplied
    // ``quote`` (if any) takes precedence over the fetched one.
    void Promise.allSettled([getInstrumentCatalogItem(symbol), getQuotesOnce([symbol])]).then(
      ([catalogResult, quoteResult]) => {
        if (cancelled) {
          return;
        }
        setRow(catalogResult.status === "fulfilled" ? catalogResult.value : null);
        setFetchedQuote(
          quoteResult.status === "fulfilled" ? quoteResult.value.items[0] ?? null : null,
        );
        setLoading(false);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [symbol]);

  const effectiveQuote = quote ?? fetchedQuote;
  const suspended = effectiveQuote?.status === "suspended";

  return (
    <Modal
      title={
        symbol ? (
          <Space>
            <span>{row?.display_name ?? symbol}</span>
            <Typography.Text type="secondary">{symbol}</Typography.Text>
          </Space>
        ) : (
          "标的详情"
        )
      }
      open={open}
      onCancel={onClose}
      footer={null}
      width={960}
      destroyOnHidden
    >
      {!symbol ? null : loading ? (
        <div style={{ display: "flex", justifyContent: "center", padding: 48 }}>
          <Spin />
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap gap-x-6 gap-y-2 text-sm text-gray-600 rounded border border-gray-200 px-4 py-3">
            <span>代码: {symbol}</span>
            <span>名称: {row?.display_name ?? "—"}</span>
            <span>市场: {row?.market ?? "—"}</span>
            <span>类型: {instrumentTypeLabel(row?.instrument_type)}</span>
            <span>现价: {suspended ? "停牌" : formatNumber(effectiveQuote?.price)}</span>
            <span className="flex items-center gap-1">涨跌幅: {suspended ? <Tag color="default">停牌</Tag> : <ChangePctTag value={effectiveQuote?.change_pct} />}</span>
            <span>成交额: {formatAmount(effectiveQuote?.amount)}</span>
            <span>行情时间: {effectiveQuote?.timestamp ?? "—"}</span>
          </div>

          <section className="flex flex-col gap-2">
            <Typography.Title level={5} className="!mb-0">
              本地 K 线
            </Typography.Title>
            <LocalMarketKlinePanel symbol={symbol} />
          </section>
        </div>
      )}
    </Modal>
  );
}
