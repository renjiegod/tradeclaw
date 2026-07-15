import {
  CaretDownOutlined,
  CaretRightOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  CopyOutlined,
} from "@ant-design/icons";
import { Alert, Button, Collapse, Descriptions, Modal, Space, Spin, Tabs, Tree, Typography, message } from "antd";
import type { DataNode } from "antd/es/tree";
import { useCallback, useMemo, useState, type ReactNode } from "react";

import type { ModelInvocationRow, Span } from "../types";
import { JsonCodeBlock } from "./JsonCodeBlock";
import { JsonPanel } from "./JsonPanel";
import { ModelInvocationRequestPanel } from "./ModelInvocationRequestPanel";
import { getModelInvocationBySpan } from "../api";
import { EventCard } from "./EventCard";

type Props = {
  spans: Span[];
  loading?: boolean;
};

function formatDuration(ms: number | null): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1) return `${(ms * 1000).toFixed(0)}μs`;
  if (ms < 1000) return `${ms.toFixed(1)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

function StatusIcon({ status }: { status: string }) {
  if (status === "error") return <CloseCircleOutlined className="text-red-500" />;
  return <ClockCircleOutlined className="text-green-500" />;
}

interface TreeSpan {
  key: string;
  span: Span;
  children: TreeSpan[];
  duration_ms: number | null;
}

function buildSpanTree(spans: Span[]): TreeSpan[] {
  const spanMap = new Map<string, TreeSpan>();
  const rootSpans: TreeSpan[] = [];

  for (const span of spans) {
    spanMap.set(span.span_id, {
      key: span.span_id,
      span,
      children: [],
      duration_ms: span.duration_ms,
    });
  }

  for (const span of spans) {
    const node = spanMap.get(span.span_id)!;
    if (span.parent_span_id && spanMap.has(span.parent_span_id)) {
      spanMap.get(span.parent_span_id)!.children.push(node);
    } else {
      rootSpans.push(node);
    }
  }

  return rootSpans;
}

function SpanTitle({ span, durationMs }: { span: Span; durationMs: number | null }) {
  return (
    <div className="flex min-w-0 max-w-full flex-nowrap items-center gap-1.5 overflow-hidden py-0.5">
      <span className="shrink-0">
        <StatusIcon status={span.status} />
      </span>
      <div className="min-w-0 flex-1 overflow-hidden">
        <Typography.Text
          strong
          className="font-mono text-sm"
          style={{ marginBottom: 0, width: "100%" }}
          ellipsis={{ tooltip: span.name }}
        >
          {span.name}
        </Typography.Text>
      </div>
      {durationMs !== null && (
        <Typography.Text type="secondary" className="shrink-0 whitespace-nowrap text-xs">
          {formatDuration(durationMs)}
        </Typography.Text>
      )}
    </div>
  );
}

function JsonPreview({
  value,
  maxHeight,
  modalTitle,
}: {
  value: unknown;
  maxHeight: number | string;
  modalTitle?: string;
}) {
  const [open, setOpen] = useState(false);
  const jsonText = useMemo(() => (value == null ? "" : JSON.stringify(value, null, 2)), [value]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(jsonText);
      message.success("已复制到剪贴板");
    } catch {
      message.error("复制失败");
    }
  };

  return (
    <div className="min-w-0">
      <div className="mb-1 flex flex-wrap items-center justify-end gap-x-2 gap-y-1">
        <Button type="text" size="small" icon={<CopyOutlined />} title="复制 JSON" onClick={() => void copy()} />
        <Typography.Link className="text-xs" onClick={() => setOpen(true)}>
          放大查看
        </Typography.Link>
      </div>
      <JsonCodeBlock value={value} maxHeight={maxHeight} />
      <Modal
        title={modalTitle ?? "JSON"}
        open={open}
        onCancel={() => setOpen(false)}
        footer={null}
        width="min(920px, 92vw)"
        destroyOnClose
        styles={{ body: { maxHeight: "min(85vh, 900px)", overflow: "auto", paddingTop: 8 } }}
      >
        <div className="mb-2 flex justify-end">
          <Button type="default" size="small" icon={<CopyOutlined />} onClick={() => void copy()}>
            复制
          </Button>
        </div>
        <JsonCodeBlock value={value} maxHeight="min(78vh, 800px)" />
      </Modal>
    </div>
  );
}

/** Mirrored from OTel span status description in ``debug_span_export`` (error spans only). */
const SPAN_STATUS_MESSAGE_KEY = "span_status_message";

function spanMetaForDisplay(span: Span): Record<string, unknown> {
  return {
    trace_id: span.trace_id,
    span_id: span.span_id,
    parent_span_id: span.parent_span_id,
    span_source: span.span_source,
    span_status: span.status,
  };
}

function getStructuredSpanError(span: Span): Record<string, unknown> | null {
  const raw = span.attributes.error;
  if (raw != null && typeof raw === "object" && !Array.isArray(raw)) {
    return raw as Record<string, unknown>;
  }
  if (typeof raw === "string") {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      return { message: raw };
    }
  }
  return null;
}

function getSpanStatusMessage(span: Span): string | null {
  const v = span.attributes[SPAN_STATUS_MESSAGE_KEY];
  if (typeof v === "string" && v.trim()) return v.trim();
  return null;
}

function getDisplayAttributes(span: Span): Record<string, unknown> {
  const attrs = { ...span.attributes };
  delete attrs._events;
  delete attrs.error;
  delete attrs[SPAN_STATUS_MESSAGE_KEY];
  return attrs;
}

function strField(obj: Record<string, unknown>, key: string): string | undefined {
  const v = obj[key];
  return typeof v === "string" && v.trim() ? v : undefined;
}

function SpanErrorPanel({ span }: { span: Span }) {
  const structured = getStructuredSpanError(span);
  const statusMsg = getSpanStatusMessage(span);
  const traceback = structured && typeof structured.traceback === "string" ? structured.traceback : null;

  if (!structured && !statusMsg) {
    return (
      <Typography.Text type="secondary">
        该 span 标记为 error，但未找到结构化 error 属性或 OTel status 描述。
      </Typography.Text>
    );
  }

  const descItems: { key: string; label: string; children: ReactNode }[] = [];
  if (structured) {
    const code = strField(structured, "code");
    const typ = strField(structured, "type");
    const msg = strField(structured, "message");
    if (code) descItems.push({ key: "code", label: "code", children: code });
    if (typ) descItems.push({ key: "type", label: "type", children: typ });
    if (msg) descItems.push({ key: "message", label: "message", children: <span className="whitespace-pre-wrap break-words">{msg}</span> });
  }

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {statusMsg ? (
        <Alert
          type="info"
          showIcon
          message="OTel span status"
          description={<pre className="mb-0 max-h-[220px] overflow-auto whitespace-pre-wrap break-words font-mono text-xs">{statusMsg}</pre>}
        />
      ) : null}
      {descItems.length > 0 ? (
        <Descriptions size="small" bordered column={1} items={descItems} />
      ) : null}
      {traceback ? (
        <Collapse
          bordered={false}
          items={[
            {
              key: "traceback",
              label: "Traceback（可折叠）",
              children: (
                <pre className="mb-0 max-h-[min(55vh,520px)] overflow-auto whitespace-pre-wrap break-words rounded border border-shell-line bg-[#fafafa] p-3 font-mono text-xs leading-relaxed">
                  {traceback}
                </pre>
              ),
            },
          ]}
        />
      ) : null}
      {structured ? (
        <Collapse
          bordered={false}
          items={[
            {
              key: "full",
              label: "完整 error 载荷（JSON，默认折叠）",
              children: <JsonPreview value={structured} maxHeight="min(50vh, 480px)" modalTitle={`${span.name} · error`} />,
            },
          ]}
        />
      ) : null}
    </Space>
  );
}

export function TraceViewer({ spans, loading }: Props) {
  const [modelInvocationDetail, setModelInvocationDetail] = useState<ModelInvocationRow | null>(null);
  const [showModelInvocationModal, setShowModelInvocationModal] = useState(false);

  // Which span is selected in the tree
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null);

  // Which event is selected in the Events tab
  const [selectedEvent, setSelectedEvent] = useState<{ event_type: string; payload: Record<string, unknown> } | null>(null);
  const [showEventModal, setShowEventModal] = useState(false);

  const openModelInvocationForSpan = useCallback(async (spanId: string) => {
    try {
      const invocation = await getModelInvocationBySpan(spanId);
      setModelInvocationDetail(invocation);
      setShowModelInvocationModal(true);
    } catch (err) {
      message.error(`加载模型调用详情失败：${err instanceof Error ? err.message : String(err)}`);
    }
  }, []);

  const spanById = useMemo(() => new Map(spans.map((s) => [s.span_id, s])), [spans]);

  /** 默认只展开根节点，使「第二级」（根的子 span）可见；更深层由用户手动展开。 */
  const { treeData, defaultExpandedKeys } = useMemo(() => {
    const roots = buildSpanTree(spans);
    const expandedKeys = roots.filter((r) => r.children.length > 0).map((r) => r.key);

    const convertToDataNode = (node: TreeSpan): DataNode => {
      const span = node.span;
      const hasChildren = node.children.length > 0;

      return {
        key: node.key,
        title: (
          <div
            onClick={() => setSelectedSpanId(span.span_id)}
            style={{ cursor: "pointer", width: "100%", minWidth: 0, maxWidth: "100%" }}
          >
            <SpanTitle span={span} durationMs={node.duration_ms} />
          </div>
        ),
        children: node.children.map(convertToDataNode),
        isLeaf: !hasChildren,
      };
    };

    return { treeData: roots.map(convertToDataNode), defaultExpandedKeys: expandedKeys };
  }, [spans]);

  const selectedSpan = selectedSpanId ? spanById.get(selectedSpanId) : null;
  const selectedSpanEvents = selectedSpan
    ? ((selectedSpan.attributes._events as Array<{ event_type: string; payload: Record<string, unknown> }>) ?? [])
    : [];

  if (loading) {
    return (
      <div className="flex min-h-[200px] items-center justify-center">
        <Spin />
      </div>
    );
  }

  if (spans.length === 0) {
    return <Typography.Text type="secondary">暂无 trace 数据</Typography.Text>;
  }

  return (
    <>
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-4">
        <Typography.Text type="secondary" className="text-xs">
          {spans.length} spans
        </Typography.Text>
        <div className="flex items-center gap-1">
          <ClockCircleOutlined className="text-green-500" />
          <Typography.Text type="secondary" className="text-xs">
            ok
          </Typography.Text>
        </div>
        <div className="flex items-center gap-1">
          <CloseCircleOutlined className="text-red-500" />
          <Typography.Text type="secondary" className="text-xs">
            error
          </Typography.Text>
        </div>
      </div>
      <div className="flex min-h-[280px] flex-col gap-4 lg:flex-row lg:items-start" style={{ gap: 12 }}>
        <div className="min-w-0 rounded border border-shell-line bg-card-bg p-2 lg:flex-none lg:basis-[55%]" style={{ flex: "0 0 55%", minWidth: 0 }}>
          <Tree
            className="trace-viewer-span-tree [&_.ant-tree-node-content-wrapper]:min-w-0 [&_.ant-tree-node-content-wrapper]:overflow-hidden [&_.ant-tree-title]:!inline-block [&_.ant-tree-title]:min-w-0 [&_.ant-tree-title]:max-w-full"
            defaultExpandedKeys={defaultExpandedKeys}
            showLine
            switcherIcon={({ expanded }) =>
              expanded ? <CaretDownOutlined /> : <CaretRightOutlined />
            }
            treeData={treeData}
            selectedKeys={selectedSpanId ? [selectedSpanId] : []}
            onSelect={(keys) => {
              const next = keys[0] as string | undefined;
              setSelectedSpanId(next ?? null);
            }}
            blockNode
          />
        </div>
        <div
          style={{
            flex: 1,
            display: "flex",
            flexDirection: "column",
            border: "1px solid #d9d9d9",
            borderRadius: 8,
            overflow: "hidden",
            background: "#fafafa",
          }}
        >
          {selectedSpan ? (
            <>
              {/* Header */}
              <div style={{ padding: "10px 14px", borderBottom: "1px solid #d9d9d9", background: "#fff" }}>
                <Typography.Text strong style={{ fontSize: 13 }}>
                  {selectedSpan.name}
                </Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 11, marginLeft: 8 }}>
                  {selectedSpan.span_type} · {selectedSpan.span_source}
                </Typography.Text>
              </div>

              {/* Tabs: all span info */}
              <div style={{ flex: 1, overflow: "auto" }}>
                <Tabs
                  style={{ height: "100%" }}
                  tabBarGutter={40}
                  tabBarStyle={{ paddingInline: 14, marginBottom: 0 }}
                  items={[
                    {
                      key: "events",
                      label: `Events (${selectedSpanEvents.length})`,
                      children: (
                        <div style={{ padding: 12, overflow: "auto" }}>
                          {selectedSpanEvents.length > 0 ? (
                            <Space direction="vertical" size={8} style={{ width: "100%" }}>
                              {selectedSpanEvents.map((ev, idx) => (
                                <EventCard
                                  key={`${selectedSpanId}:${idx}`}
                                  event={ev}
                                  isSelected={selectedEvent?.event_type === ev.event_type && selectedEvent?.payload === ev.payload}
                                  onClick={() => {
                                    setSelectedEvent(ev);
                                    setShowEventModal(true);
                                  }}
                                />
                              ))}
                            </Space>
                          ) : (
                            <Typography.Text type="secondary">该 span 暂无事件</Typography.Text>
                          )}
                        </div>
                      ),
                    },
                    ...(selectedSpan.status === "error"
                      ? [
                          {
                            key: "error",
                            label: "错误",
                            children: (
                              <div style={{ padding: 14, overflow: "auto" }}>
                                <SpanErrorPanel span={selectedSpan} />
                              </div>
                            ),
                          },
                        ]
                      : []),
                    {
                      key: "span-meta",
                      label: "Span 元信息",
                      children: (
                        <div style={{ padding: 14, overflow: "auto" }}>
                          <JsonPreview value={spanMetaForDisplay(selectedSpan)} maxHeight={360} modalTitle={`${selectedSpan.name} · meta`} />
                        </div>
                      ),
                    },
                    {
                      key: "attributes",
                      label: "Attributes",
                      children: (
                        <div style={{ padding: 14, overflow: "auto" }}>
                          <JsonPreview value={getDisplayAttributes(selectedSpan)} maxHeight={360} modalTitle={`${selectedSpan.name} · attributes`} />
                        </div>
                      ),
                    },
                  ]}
                />
              </div>
            </>
          ) : (
            <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
              <Typography.Text type="secondary">点击左侧 Span 节点查看其事件流</Typography.Text>
            </div>
          )}
        </div>
      </div>
    </div>
    <Modal
      title="Model Invocation Detail"
      open={showModelInvocationModal}
      onCancel={() => setShowModelInvocationModal(false)}
      footer={null}
      width="min(920px, 92vw)"
      destroyOnClose
    >
      {modelInvocationDetail && (
        <Space direction="vertical" size={12} className="w-full">
          <Descriptions size="small" bordered column={2} items={[
            { key: "model", label: "Model", children: modelInvocationDetail.model },
            { key: "model_id", label: "Model ID", children: modelInvocationDetail.model_id },
            { key: "provider_kind", label: "Kind", children: modelInvocationDetail.provider_kind },
            { key: "call_kind", label: "Call Kind", children: modelInvocationDetail.call_kind },
            { key: "ok", label: "Status", children: modelInvocationDetail.ok ? "OK" : "Error" },
            { key: "total_latency_ms", label: "Latency (ms)", children: modelInvocationDetail.total_latency_ms },
            { key: "total_tokens", label: "Total Tokens", children: modelInvocationDetail.total_tokens },
            { key: "cache_read_tokens", label: "Cache Read", children: modelInvocationDetail.cache_read_tokens ?? "—" },
            { key: "cache_write_tokens", label: "Cache Write", children: modelInvocationDetail.cache_write_tokens ?? "—" },
          ]} />
          {modelInvocationDetail.error_message && (
            <Alert type="error" message={modelInvocationDetail.error_message} />
          )}
          <ModelInvocationRequestPanel data={modelInvocationDetail.request} maxHeight={360} />
          <JsonPanel title="Response" data={modelInvocationDetail.response} maxHeight={360} />
        </Space>
      )}
    </Modal>
    <Modal
      title={selectedEvent ? `事件详情 · ${selectedEvent.event_type}` : "事件详情"}
      open={showEventModal}
      onCancel={() => setShowEventModal(false)}
      footer={null}
      width="min(820px, 92vw)"
      destroyOnClose
    >
      {selectedEvent && (
        <JsonCodeBlock value={selectedEvent.payload} maxHeight="min(70vh, 640px)" copyable />
      )}
    </Modal>
    </>
  );
}
