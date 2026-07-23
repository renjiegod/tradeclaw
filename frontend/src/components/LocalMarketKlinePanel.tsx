import { Alert, Button, Space, Spin } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { startTransition, useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  getLocalMarketBars,
  getLocalMarketOverlays,
  getLocalMarketSyncJob,
  syncLocalMarketBarsRange,
} from "../api";
import type {
  BacktestChartBar,
  LocalMarketBarsSnapshot,
  LocalMarketOverlayItem,
  LocalMarketOverlaySnapshot,
  LocalMarketSyncJob,
} from "../types";
import { LocalMarketKlineChart, parseBarTimestampMs } from "./LocalMarketKlineChart";
import { LocalMarketKlineSidebar } from "./LocalMarketKlineSidebar";
import { LocalMarketKlineToolbar, type MainIndicator, type SubIndicator } from "./LocalMarketKlineToolbar";

type LocalMarketKlinePanelProps = {
  symbol: string;
};

type OverlayKind = "backtest_trades" | "task_fills" | "signals";
const KLINE_DISPLAY_ADJUST = "qfq";

// Intraday intervals (denser bars, ISO tz-aware range bounds). Keep in sync with
// the backend's SUPPORTED_LOCAL_INTERVALS / is_intraday_interval.
const INTRADAY_INTERVALS = new Set(["5m", "60m"]);
function isIntradayInterval(interval: string): boolean {
  return INTRADAY_INTERVALS.has(interval);
}

// Initial window anchored to today, sized so a multi-month data lag still shows
// the latest bars on first paint. Older history is pulled lazily on scroll.
// Denser intervals load a shorter window to avoid pulling too many bars at once.
function initialLookbackDays(interval: string): number {
  if (interval === "5m") return 30;
  if (interval === "60m") return 180;
  return 730;
}

// Chunk pulled per lazy-load step when the user scrolls past the left edge.
function lazyChunkDays(interval: string): number {
  if (interval === "5m") return 60;
  if (interval === "60m") return 120;
  return 730;
}

function initialStart(interval: string): Dayjs {
  return dayjs().subtract(initialLookbackDays(interval), "day");
}

function formatRangeBound(value: Dayjs, interval: string, isEnd: boolean): string {
  if (isIntradayInterval(interval)) {
    return (isEnd ? value.endOf("day") : value.startOf("day")).toISOString();
  }
  return value.format("YYYY-MM-DD");
}

function formatSyncTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  return value.replace("T", " ").replace(/Z$/, "");
}

export function LocalMarketKlinePanel({ symbol }: LocalMarketKlinePanelProps) {
  const mountedRef = useRef(true);
  const [interval, setInterval] = useState("1d");
  const [provider, setProvider] = useState("auto");
  const [snapshot, setSnapshot] = useState<LocalMarketBarsSnapshot | null>(null);
  // Earliest date currently loaded into the chart; moves backward as the user
  // scrolls and lazy-loads history. Drives overlay coverage + sync range.
  const [earliestLoaded, setEarliestLoaded] = useState<Dayjs>(() => initialStart("1d"));
  const [overlaySnapshots, setOverlaySnapshots] = useState<Partial<Record<OverlayKind, LocalMarketOverlaySnapshot>>>({});
  const [selectedOverlays, setSelectedOverlays] = useState<Partial<Record<OverlayKind, string>>>({});
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [refreshTick, setRefreshTick] = useState(0);
  const [mainIndicator, setMainIndicator] = useState<MainIndicator>("MA");
  const [subIndicator, setSubIndicator] = useState<SubIndicator>("MACD");
  const [syncingMode, setSyncingMode] = useState<string | null>(null);
  const [syncJob, setSyncJob] = useState<LocalMarketSyncJob | null>(null);
  const [syncMessage, setSyncMessage] = useState("");

  const endStr = useMemo(
    () => formatRangeBound(dayjs(), interval, true),
    [interval, provider, refreshTick, symbol],
  );
  const initialStartStr = useMemo(
    () => formatRangeBound(initialStart(interval), interval, false),
    [interval, refreshTick, symbol],
  );
  const earliestStr = useMemo(
    () => formatRangeBound(earliestLoaded, interval, false),
    [earliestLoaded, interval],
  );
  // Only hand the chart a snapshot that matches the currently selected series, so
  // switching interval never flashes (or re-inits the chart with) stale bars from
  // the previous interval before the new fetch resolves.
  const activeSnapshot = useMemo(
    () => (snapshot && snapshot.symbol === symbol && snapshot.interval === interval ? snapshot : null),
    [snapshot, symbol, interval],
  );
  const latestSyncFailure = useMemo(() => {
    if (syncJob?.status === "failed") {
      return {
        source: "manual",
        code: syncJob.error_code,
        type: syncJob.error_type,
        message: syncJob.error_message ?? "同步失败",
        hint: syncJob.hint,
        attemptedAt: syncJob.finished_at ?? syncJob.started_at,
        retryCount: null,
      };
    }
    const syncState = activeSnapshot?.sync_state;
    if (!syncState || syncState.status !== "failed") {
      return null;
    }
    return {
      source: "auto",
      code: syncState.last_error_code,
      type: syncState.last_error_type,
      message: syncState.last_error_message ?? "自动同步失败",
      hint: null,
      attemptedAt: syncState.last_attempt_at,
      retryCount: syncState.retry_count,
    };
  }, [activeSnapshot?.sync_state, syncJob]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Series / window reset: clearing the snapshot forces the remounted chart to
  // wait for the freshly-fetched data (e.g. after a sync bumps refreshTick).
  useEffect(() => {
    setSnapshot(null);
    setEarliestLoaded(initialStart(interval));
    setOverlaySnapshots({});
    setErrorMessage("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, interval, provider, refreshTick]);

  // Identity-level reset: only when the symbol itself changes. Kept out of the
  // refreshTick path so a successful sync's status message survives the reload.
  useEffect(() => {
    setSelectedOverlays({});
    setSyncJob(null);
    setSyncMessage("");
  }, [symbol]);

  useEffect(() => {
    let cancelled = false;
    if (!symbol) return;
    setLoading(true);
    setErrorMessage("");
    getLocalMarketBars({
      symbol,
      interval,
      start: initialStartStr,
      end: endStr,
      provider,
      adjust: KLINE_DISPLAY_ADJUST,
    })
      .then((result) => {
        if (cancelled) return;
        startTransition(() => {
          setSnapshot(result);
        });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setSnapshot(null);
        setErrorMessage(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [endStr, initialStartStr, interval, provider, refreshTick, symbol]);

  useEffect(() => {
    const nextSelections: Partial<Record<OverlayKind, string>> = {};
    const kinds: OverlayKind[] = ["backtest_trades", "task_fills", "signals"];
    for (const kind of kinds) {
      const options = snapshot?.available_overlays?.[kind] ?? [];
      const current = selectedOverlays[kind];
      if (current && options.some((item) => item.id === current)) {
        nextSelections[kind] = current;
      } else if (options[0]?.id) {
        nextSelections[kind] = options[0].id;
      }
    }
    setSelectedOverlays((prev) => {
      const changed = kinds.some((kind) => prev[kind] !== nextSelections[kind]);
      return changed ? nextSelections : prev;
    });
  }, [snapshot?.available_overlays]);

  useEffect(() => {
    let cancelled = false;
    const kinds: OverlayKind[] = ["backtest_trades", "task_fills", "signals"];
    const load = async () => {
      const next: Partial<Record<OverlayKind, LocalMarketOverlaySnapshot>> = {};
      for (const kind of kinds) {
        const selectedId = selectedOverlays[kind];
        if (!selectedId) continue;
        const candidate = snapshot?.available_overlays?.[kind]?.find((item) => item.id === selectedId);
        if (!candidate) continue;
        next[kind] = await getLocalMarketOverlays({
          symbol,
          interval,
          start: earliestStr,
          end: endStr,
          overlay_kind: kind,
          run_id: kind === "backtest_trades" ? candidate.run_id ?? candidate.id : undefined,
          task_id: kind === "task_fills" ? candidate.task_id ?? candidate.id : undefined,
          signal_source_id: kind === "signals" ? candidate.task_id ?? candidate.id : undefined,
        });
      }
      if (!cancelled) setOverlaySnapshots(next);
    };
    if (snapshot) {
      void load();
    } else {
      setOverlaySnapshots({});
    }
    return () => {
      cancelled = true;
    };
  }, [earliestStr, endStr, interval, selectedOverlays, snapshot, symbol]);

  useEffect(() => {
    if (!syncJob?.job_id || !["pending", "running"].includes(syncJob.status)) return;
    const timer = window.setInterval(() => {
      void getLocalMarketSyncJob(syncJob.job_id)
        .then((job) => {
          if (!mountedRef.current) return;
          setSyncJob(job);
          if (job.status === "ok") {
            setSyncMessage(
              job.adjust_drift_refreshed
                ? `检测到除权，已自动全量重刷，写入 ${job.upserted_count} 条`
                : `同步完成，写入 ${job.upserted_count} 条`,
            );
            setRefreshTick((tick) => tick + 1);
          } else if (job.status === "failed") {
            setSyncMessage(job.error_message ?? "同步失败");
          }
        })
        .catch((err: unknown) => {
          if (!mountedRef.current) return;
          setSyncMessage(err instanceof Error ? err.message : String(err));
        });
    }, 1500);
    return () => window.clearInterval(timer);
  }, [syncJob?.job_id, syncJob?.status]);

  const overlayItems = useMemo<LocalMarketOverlayItem[]>(
    () =>
      Object.values(overlaySnapshots)
        .flatMap((snapshotItem) => snapshotItem?.items ?? [])
        .sort((left, right) => left.timestamp.localeCompare(right.timestamp)),
    [overlaySnapshots],
  );

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
          adjust: KLINE_DISPLAY_ADJUST,
        });
        // Guard against date-boundary overlap regardless of inclusive bounds.
        return (result.bars ?? []).filter((bar) => parseBarTimestampMs(bar.timestamp) < oldestTimestampMs);
      } catch {
        return [];
      }
    },
    [interval, provider, symbol],
  );

  const handleEarliestLoaded = useCallback((earliestTimestampMs: number) => {
    setEarliestLoaded((prev) => {
      const next = dayjs(earliestTimestampMs);
      return next.isBefore(prev) ? next : prev;
    });
  }, []);

  const runSync = async (mode: "fill_gap" | "force_refresh") => {
    setSyncingMode(mode);
    setSyncMessage("");
    try {
      const response = await syncLocalMarketBarsRange({
        symbol,
        interval,
        start: earliestStr,
        end: endStr,
        provider,
        adjust: KLINE_DISPLAY_ADJUST,
        mode,
      });
      if (!mountedRef.current) return;
      if (response.execution_mode === "sync") {
        setSyncJob(null);
        const modeLabel = mode === "fill_gap" ? "补缺口" : "强制重刷";
        if (response.adjust_drift_refreshed) {
          setSyncMessage(
            response.upserted_count != null
              ? `检测到除权，已自动全量重刷，写入 ${response.upserted_count} 条`
              : "检测到除权，已自动全量重刷",
          );
        } else {
          setSyncMessage(
            response.upserted_count != null
              ? `${modeLabel}完成，写入 ${response.upserted_count} 条`
              : `${modeLabel}完成`,
          );
        }
        setRefreshTick((tick) => tick + 1);
      } else if (response.job_id) {
        setSyncMessage("同步任务已提交，正在轮询状态");
        const job = await getLocalMarketSyncJob(response.job_id);
        if (!mountedRef.current) return;
        setSyncJob(job);
      }
    } catch (err: unknown) {
      if (!mountedRef.current) return;
      setSyncMessage(err instanceof Error ? err.message : String(err));
    } finally {
      if (mountedRef.current) {
        setSyncingMode(null);
      }
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <LocalMarketKlineToolbar
        interval={interval}
        provider={provider}
        mainIndicator={mainIndicator}
        subIndicator={subIndicator}
        loading={loading}
        onIntervalChange={setInterval}
        onProviderChange={setProvider}
        onMainIndicatorChange={setMainIndicator}
        onSubIndicatorChange={setSubIndicator}
        onRefresh={() => setRefreshTick((tick) => tick + 1)}
      />

      {errorMessage ? <Alert type="error" showIcon message={errorMessage} /> : null}
      {latestSyncFailure ? (
        <Alert
          type="error"
          showIcon
          message={latestSyncFailure.source === "auto" ? "本地 K 线自动同步失败" : "手动同步失败"}
          description={
            <Space direction="vertical" size={8} className="w-full">
              <div className="text-sm">
                <div>{latestSyncFailure.message}</div>
                <div className="text-xs text-slate-500">
                  错误码: {latestSyncFailure.code ?? "—"}
                  {latestSyncFailure.type ? ` · ${latestSyncFailure.type}` : ""}
                  {latestSyncFailure.attemptedAt
                    ? ` · 最近尝试 ${formatSyncTimestamp(latestSyncFailure.attemptedAt)}`
                    : ""}
                  {latestSyncFailure.retryCount != null ? ` · 已重试 ${latestSyncFailure.retryCount} 次` : ""}
                </div>
                {latestSyncFailure.hint ? (
                  <div className="text-xs text-slate-500">{latestSyncFailure.hint}</div>
                ) : null}
              </div>
              <Space wrap>
                <Button type="primary" loading={syncingMode === "fill_gap"} onClick={() => void runSync("fill_gap")}>
                  立即补缺口
                </Button>
                <Button danger loading={syncingMode === "force_refresh"} onClick={() => void runSync("force_refresh")}>
                  手动强制重刷
                </Button>
              </Space>
            </Space>
          }
        />
      ) : null}

      <div className="grid h-[60vh] min-h-[420px] flex-1 gap-3 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="relative h-full">
          {loading ? (
            <div className="absolute inset-0 z-10 flex items-center justify-center rounded-xl bg-slate-950/50">
              <Spin />
            </div>
          ) : null}
          <LocalMarketKlineChart
            snapshot={activeSnapshot}
            mainIndicator={mainIndicator}
            subIndicator={subIndicator}
            overlayItems={overlayItems}
            loadOlderBars={loadOlderBars}
            onEarliestLoaded={handleEarliestLoaded}
          />
        </div>
        <div className="h-full overflow-y-auto">
          <LocalMarketKlineSidebar
            snapshot={snapshot}
            syncJob={syncJob}
            syncMessage={syncMessage}
            syncingMode={syncingMode}
            selectedOverlays={selectedOverlays}
            onOverlayChange={(kind, value) =>
              setSelectedOverlays((prev) => ({ ...prev, [kind]: value }))
            }
            onFillGap={() => void runSync("fill_gap")}
            onForceRefresh={() => void runSync("force_refresh")}
          />
        </div>
      </div>
    </div>
  );
}
