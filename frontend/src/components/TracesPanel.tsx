import { useEffect, useMemo, useRef, useCallback, useState } from "react";
import { Badge, Collapse, Grid, List, Space, Tag, Typography, Spin, Empty } from "antd";
import { WarningOutlined, CheckCircleOutlined } from "@ant-design/icons";

import { TraceViewer } from "./TraceViewer";
import { listAssistantTraces, getAssistantTraceDetail } from "../api";
import type { TraceSummary, TraceDetail } from "../types";
import { modelInvocationTokenSummary, buildModelInvocationCollapseItems } from "../hooks/modelInvocation";

interface TracesPanelProps {
  sessionId: string;
  newTraceId?: string | null;
  onNewTraceIdConsumed?: () => void;
}

interface TraceEntryProps {
  trace: TraceSummary;
  isSelected: boolean;
  onClick: () => void;
}

function TraceEntry({ trace, isSelected, onClick }: TraceEntryProps) {
  const statusIcon = trace.status === "ok" ? (
    <CheckCircleOutlined style={{ color: "#52c41a" }} />
  ) : (
    <WarningOutlined style={{ color: "#ff4d4f" }} />
  );

  return (
    <List.Item
      onClick={onClick}
      style={{
        cursor: "pointer",
        padding: "8px 12px",
        background: isSelected ? "#f0f5ff" : undefined,
        borderLeft: isSelected ? "2px solid #1890ff" : "2px solid transparent",
      }}
    >
      <div className="flex flex-col gap-1 overflow-hidden">
        <div className="flex items-center gap-2">
          {statusIcon}
          <Typography.Text strong style={{ fontSize: 13 }} ellipsis={{ tooltip: trace.span_name }}>
            {trace.span_name}
          </Typography.Text>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-500">
          <span>{new Date(trace.created_at).toLocaleTimeString()}</span>
          {trace.duration_ms != null && (
            <span>{(trace.duration_ms / 1000).toFixed(1)}s</span>
          )}
          {trace.model && <Tag className="m-0">{trace.model}</Tag>}
          {trace.input_tokens != null && trace.output_tokens != null && (
            <span>tokens in {trace.input_tokens} / out {trace.output_tokens}</span>
          )}
          {(trace.cache_read_tokens != null || trace.cache_write_tokens != null) && (
            <span className="text-orange-500"> · cache R:{trace.cache_read_tokens ?? 0} W:{trace.cache_write_tokens ?? 0}</span>
          )}
          <span>{trace.span_count} spans</span>
        </div>
      </div>
    </List.Item>
  );
}

export function TracesPanel({ sessionId, newTraceId, onNewTraceIdConsumed }: TracesPanelProps) {
  const screens = Grid.useBreakpoint();
  const isMobile = !screens.lg;
  const [traces, setTraces] = useState<TraceSummary[]>([]);
  const [selectedTrace, setSelectedTrace] = useState<TraceSummary | null>(null);
  const [traceDetail, setTraceDetail] = useState<TraceDetail | null>(null);
  const [loadingTraces, setLoadingTraces] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const previousNewTraceIdRef = useRef<string | null>(null);

  // Load traces on mount or when sessionId changes
  const loadTraces = useCallback(async () => {
    if (!sessionId) return;
    setLoadingTraces(true);
    try {
      const result = await listAssistantTraces(sessionId, { limit: 50 });
      setTraces(result.items);
    } catch (e) {
      console.error("Failed to load traces:", e);
    } finally {
      setLoadingTraces(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void loadTraces();
  }, [loadTraces]);

  // Load trace detail when selectedTrace changes
  useEffect(() => {
    if (!selectedTrace || !sessionId) {
      setTraceDetail(null);
      return;
    }
    setLoadingDetail(true);
    setTraceDetail(null);
    getAssistantTraceDetail(sessionId, selectedTrace.trace_id)
      .then(setTraceDetail)
      .catch((e) => console.error("Failed to load trace detail:", e))
      .finally(() => setLoadingDetail(false));
  }, [selectedTrace, sessionId]);

  // Handle newTraceId prop changes — poll with retry if trace not yet persisted
  useEffect(() => {
    if (!newTraceId || newTraceId === previousNewTraceIdRef.current) return;
    previousNewTraceIdRef.current = newTraceId;

    const traceIdRef = { current: newTraceId };

    const delay = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

    const poll = async () => {
      // Poll intervals: 500ms, 1s, 2s
      const intervals = [500, 1000, 2000];

      for (let i = 0; i <= intervals.length; i++) {
        if (!traceIdRef.current) return;

        // Fetch fresh trace list directly from API (not via React state)
        let found: ReturnType<typeof traces.find> | undefined;
        try {
          const result = await listAssistantTraces(sessionId, { limit: 50 });
          found = result.items.find((t) => t.trace_id === traceIdRef.current);
          if (found) {
            setTraces(result.items);
            setSelectedTrace(found);
            onNewTraceIdConsumed?.();
            traceIdRef.current = null;
            return;
          }
          // Update traces list even if ours wasn't found
          setTraces(result.items);
        } catch (e) {
          console.error("[TracesPanel] poll load error:", e);
        }

        if (!traceIdRef.current) return;
        if (i < intervals.length) {
          await delay(intervals[i]);
        }
      }

      // Exhausted retries
      if (traceIdRef.current) {
        traceIdRef.current = null;
        onNewTraceIdConsumed?.();
      }
    };

    poll();
  }, [newTraceId, sessionId, onNewTraceIdConsumed]);

  // Auto-select first trace on mount
  useEffect(() => {
    if (traces.length > 0 && !selectedTrace) {
      setSelectedTrace(traces[0]);
    }
  }, [traces, selectedTrace]);

  const invocationItems = useMemo(
    () => (traceDetail?.model_invocations?.length ? buildModelInvocationCollapseItems(traceDetail.model_invocations) : []),
    [traceDetail?.model_invocations],
  );

  if (!sessionId) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <Empty description="No session ID" />
      </div>
    );
  }

  return (
    <div className={`flex h-full min-h-0 ${isMobile ? "flex-col gap-3 p-3" : "gap-4 p-4"}`}>
      <div
        className={`${isMobile ? "min-h-[220px] w-full" : "lg:w-[280px] lg:min-w-[280px] lg:max-w-[280px]"} flex shrink-0 flex-col overflow-hidden rounded-2xl border border-shell-line bg-card-bg/70`}
      >
        <div className="border-b border-shell-line bg-white/70 px-4 py-3">
          <Typography.Text type="secondary" className="text-xs uppercase tracking-[0.18em]">
            Traces ({traces.length})
          </Typography.Text>
        </div>
        <div className="flex-1 overflow-auto">
          {loadingTraces ? (
            <div className="flex h-[200px] items-center justify-center">
              <Spin />
            </div>
          ) : traces.length === 0 ? (
            <Empty description="No traces" className="mt-8" />
          ) : (
            <List
              dataSource={traces}
              renderItem={(trace) => (
                <TraceEntry
                  trace={trace}
                  isSelected={selectedTrace?.trace_id === trace.trace_id}
                  onClick={() => setSelectedTrace(trace)}
                />
              )}
            />
          )}
        </div>
      </div>
      <div className="flex min-h-[360px] min-w-0 flex-1 flex-col overflow-hidden rounded-2xl border border-shell-line bg-white">
        {selectedTrace ? (
          <>
            <div className="border-b border-shell-line bg-card-bg/60 px-4 py-3 lg:px-5">
              <Space direction="vertical" size={6} className="w-full">
                <Space wrap>
                  <Badge status={selectedTrace.status === "ok" ? "success" : "error"} />
                  <Typography.Text strong className="text-sm lg:text-base">
                    {selectedTrace.span_name}
                  </Typography.Text>
                  {selectedTrace.model ? <Tag className="mr-0">{selectedTrace.model}</Tag> : null}
                </Space>
                <Typography.Text type="secondary" className="text-xs">
                  {new Date(selectedTrace.created_at).toLocaleString()}
                  {selectedTrace.duration_ms != null && ` · ${(selectedTrace.duration_ms / 1000).toFixed(2)}s`}
                  {selectedTrace.input_tokens != null && selectedTrace.output_tokens != null
                    ? ` · tokens ${selectedTrace.input_tokens}/${selectedTrace.output_tokens}`
                    : ""}
                </Typography.Text>
              </Space>
            </div>
            <div className="flex-1 overflow-auto px-4 py-4 lg:px-5">
              <Space direction="vertical" size={16} className="w-full">
                <TraceViewer spans={traceDetail?.spans ?? []} loading={loadingDetail} />
                <div>
                  <Typography.Title level={5} style={{ margin: "8px 0 12px" }}>
                    模型调用
                  </Typography.Title>
                  {loadingDetail ? (
                    <div className="flex h-24 items-center justify-center">
                      <Spin size="small" />
                    </div>
                  ) : invocationItems.length > 0 ? (
                    <Collapse items={invocationItems} size="small" />
                  ) : (
                    <Typography.Text type="secondary">暂无模型调用</Typography.Text>
                  )}
                </div>
              </Space>
            </div>
          </>
        ) : (
          <div className="flex h-full items-center justify-center p-6">
            <Empty description={traces.length === 0 ? "暂无 trace" : "选择一条 trace 查看详情"} />
          </div>
        )}
      </div>
    </div>
  );
}
