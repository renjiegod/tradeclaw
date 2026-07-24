// frontend/src/components/assistant/panels/AssistantKlineBlock.tsx
//
// Agent 面板的 K 线块：引用式数据 —— 只拿到 {symbol, interval, ...}，复用既有
// getLocalMarketBars / getLocalMarketOverlays API 拉本地行情，交给现成的
// LocalMarketKlineChart（klinecharts）渲染蜡烛图 + 指标 + 买卖点 overlay。

import { Alert, Spin } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { useCallback, useEffect, useMemo, useState } from "react";

import { getLocalMarketBars, getLocalMarketOverlays } from "../../../api";
import type {
  BacktestChartBar,
  LocalMarketBarsSnapshot,
  LocalMarketOverlayItem,
} from "../../../types";
import { LocalMarketKlineChart, parseBarTimestampMs } from "../../LocalMarketKlineChart";
import type { SubIndicator } from "../../LocalMarketKlineToolbar";
import type { KlineBlock, KlineOverlayKind } from "./panelSpec";

const INTRADAY_INTERVALS = new Set(["5m", "60m"]);
function isIntradayInterval(interval: string): boolean {
  return INTRADAY_INTERVALS.has(interval);
}

function initialLookbackDays(interval: string): number {
  if (interval === "5m") return 30;
  if (interval === "60m") return 180;
  return 730;
}

function lazyChunkDays(interval: string): number {
  if (interval === "5m") return 60;
  if (interval === "60m") return 120;
  return 730;
}

function formatRangeBound(value: Dayjs, interval: string, isEnd: boolean): string {
  if (isIntradayInterval(interval)) {
    return (isEnd ? value.endOf("day") : value.startOf("day")).toISOString();
  }
  return value.format("YYYY-MM-DD");
}

// 副图 klinecharts 恒需一个有效指标；spec 的 "none" 落到默认 MACD。
function resolveSubIndicator(value: KlineBlock["sub_indicator"]): SubIndicator {
  return value === "none" ? "MACD" : value;
}

export function AssistantKlineBlock({ block }: { block: KlineBlock }) {
  const { symbol, interval, adjust, provider, overlays } = block;
  const [snapshot, setSnapshot] = useState<LocalMarketBarsSnapshot | null>(null);
  const [overlayItems, setOverlayItems] = useState<LocalMarketOverlayItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState("");

  const startStr = useMemo(
    () =>
      block.start ?? formatRangeBound(dayjs().subtract(initialLookbackDays(interval), "day"), interval, false),
    [block.start, interval],
  );
  const endStr = useMemo(
    () => block.end ?? formatRangeBound(dayjs(), interval, true),
    [block.end, interval],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setErrorMessage("");
    setSnapshot(null);
    setOverlayItems([]);
    getLocalMarketBars({ symbol, interval, start: startStr, end: endStr, provider, adjust })
      .then((result) => {
        if (cancelled) return;
        setSnapshot(result);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setErrorMessage(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, interval, startStr, endStr, provider, adjust]);

  // Overlay：对每个请求的 kind 取本地可用的首个候选（与 LocalMarketKlinePanel
  // 的自动选中一致）；无候选则跳过，不报错。
  useEffect(() => {
    let cancelled = false;
    if (!snapshot || overlays.length === 0) {
      setOverlayItems([]);
      return;
    }
    const load = async () => {
      const collected: LocalMarketOverlayItem[] = [];
      for (const kind of overlays as KlineOverlayKind[]) {
        const candidate = snapshot.available_overlays?.[kind]?.[0];
        if (!candidate) continue;
        try {
          const overlaySnapshot = await getLocalMarketOverlays({
            symbol,
            interval,
            start: startStr,
            end: endStr,
            overlay_kind: kind,
            run_id: kind === "backtest_trades" ? candidate.run_id ?? candidate.id : undefined,
            task_id: kind === "task_fills" ? candidate.task_id ?? candidate.id : undefined,
            signal_source_id: kind === "signals" ? candidate.task_id ?? candidate.id : undefined,
          });
          collected.push(...overlaySnapshot.items);
        } catch {
          // Overlay 拉取失败不影响主图；静默跳过该 kind。
        }
      }
      if (!cancelled) {
        setOverlayItems(collected.sort((a, b) => a.timestamp.localeCompare(b.timestamp)));
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [snapshot, overlays, symbol, interval, startStr, endStr]);

  const loadOlderBars = useCallback(
    async (oldestTimestampMs: number): Promise<BacktestChartBar[]> => {
      const oldest = dayjs(oldestTimestampMs);
      const chunkEnd = oldest.subtract(1, "day");
      const chunkStart = chunkEnd.subtract(lazyChunkDays(interval), "day");
      try {
        const result = await getLocalMarketBars({
          symbol,
          interval,
          start: formatRangeBound(chunkStart, interval, false),
          end: formatRangeBound(chunkEnd, interval, true),
          provider,
          adjust,
        });
        return (result.bars ?? []).filter(
          (bar) => parseBarTimestampMs(bar.timestamp) < oldestTimestampMs,
        );
      } catch {
        return [];
      }
    },
    [symbol, interval, provider, adjust],
  );

  if (errorMessage) {
    return <Alert type="error" showIcon message={`K 线加载失败：${symbol}`} description={errorMessage} />;
  }

  return (
    <div className="relative" style={{ height: block.height }}>
      {loading ? (
        <div className="absolute inset-0 z-10 flex items-center justify-center rounded-xl bg-slate-950/40">
          <Spin />
        </div>
      ) : null}
      <LocalMarketKlineChart
        snapshot={snapshot}
        mainIndicator={block.main_indicator}
        subIndicator={resolveSubIndicator(block.sub_indicator)}
        overlayItems={overlayItems}
        loadOlderBars={loadOlderBars}
      />
    </div>
  );
}
