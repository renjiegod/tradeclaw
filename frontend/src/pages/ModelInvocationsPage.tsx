import { ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import { Button, Input, Modal, Space, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";

import { listModelInvocations } from "../api";
import { JsonCodeBlock } from "../components/JsonCodeBlock";
import { ModelInvocationRequestPanel } from "../components/ModelInvocationRequestPanel";
import { PageIntro } from "../components/PageIntro";
import { usePageRefreshToken } from "../pageRefreshContext";
import { TabbedJsonPanel } from "../components/TabbedJsonPanel";
import { FormattedRequestView } from "../components/FormattedRequestView";
import { FormattedResponseView } from "../components/FormattedResponseView";
import type { ModelInvocationRow } from "../types";
import { formatDateTimeUtc8 } from "../utils/datetime";

const DEFAULT_PAGE_SIZE = 10;

function idCellText(v: string | null | undefined) {
  return v ? (
    <Typography.Text
      className="font-mono text-xs !whitespace-normal !break-all"
      copyable={{ text: v }}
    >
      {v}
    </Typography.Text>
  ) : (
    "—"
  );
}

export function ModelInvocationsPage() {
  const pageRefreshToken = usePageRefreshToken();
  const [items, setItems] = useState<ModelInvocationRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<ModelInvocationRow | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [traceDraft, setTraceDraft] = useState("");
  const [spanDraft, setSpanDraft] = useState("");
  const [traceFilter, setTraceFilter] = useState("");
  const [spanFilter, setSpanFilter] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await listModelInvocations({
        limit: pageSize,
        offset: (page - 1) * pageSize,
        traceId: traceFilter || undefined,
        spanId: spanFilter || undefined,
      });
      setItems(res.items);
      setTotal(res.total);
    } catch (e: unknown) {
      setItems([]);
      setTotal(0);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, traceFilter, spanFilter]);

  useEffect(() => {
    void load();
  }, [load, pageRefreshToken]);

  const applyFilters = () => {
    setTraceFilter(traceDraft.trim());
    setSpanFilter(spanDraft.trim());
    setPage(1);
  };

  const resetFilters = () => {
    setTraceDraft("");
    setSpanDraft("");
    setTraceFilter("");
    setSpanFilter("");
    setPage(1);
  };

  const columns: ColumnsType<ModelInvocationRow> = useMemo(
    () => [
      {
        title: "时间 (UTC+8)",
        dataIndex: "created_at",
        key: "created_at",
        width: 200,
        render: (v: string) => (
          <Typography.Text className="text-xs !whitespace-normal !break-all">{formatDateTimeUtc8(v, v)}</Typography.Text>
        ),
      },
      {
        title: "模型 ID",
        dataIndex: "model_id",
        key: "model_id",
        width: 220,
      },
      {
        title: "接口类型",
        dataIndex: "provider_kind",
        key: "provider_kind",
        width: 130,
        render: (v: string) => <Tag className="rounded-lg">{v}</Tag>,
      },
      {
        title: "配置名称",
        dataIndex: "model_route_name",
        key: "model_route_name",
        width: 120,
        ellipsis: true,
        render: (v: string | null | undefined) => v ?? "—",
      },
      {
        title: "供应商标识",
        dataIndex: "provider_key",
        key: "provider_key",
        width: 120,
        ellipsis: true,
        render: (v: string | null | undefined) => v ?? "—",
      },
      {
        title: "trace_id",
        dataIndex: "trace_id",
        key: "trace_id",
        width: 280,
        render: (v: string | null) => idCellText(v),
      },
      {
        title: "span_id",
        dataIndex: "span_id",
        key: "span_id",
        width: 220,
        render: (v: string | null | undefined) => idCellText(v),
      },
      {
        title: "调用",
        dataIndex: "call_kind",
        key: "call_kind",
        width: 110,
        render: (v: string) => <Tag className="rounded-lg">{v}</Tag>,
      },
      {
        title: "首 token (ms)",
        dataIndex: "first_token_latency_ms",
        key: "ttft",
        width: 120,
        render: (v: number | null) => (v == null ? "—" : v),
      },
      {
        title: "总耗时 (ms)",
        dataIndex: "total_latency_ms",
        key: "total_ms",
        width: 110,
        render: (v: number | null) => (v == null ? "—" : v),
      },
      {
        title: "输入 tokens",
        dataIndex: "input_tokens",
        key: "input_tokens",
        width: 110,
        render: (v: number | null) => (v == null ? "—" : v),
      },
      {
        title: "输出 tokens",
        dataIndex: "output_tokens",
        key: "output_tokens",
        width: 110,
        render: (v: number | null) => (v == null ? "—" : v),
      },
      {
        title: "合计",
        dataIndex: "total_tokens",
        key: "total_tokens",
        width: 88,
        render: (v: number | null) => (v == null ? "—" : v),
      },
      {
        title: "Cache Read",
        dataIndex: "cache_read_tokens",
        key: "cache_read_tokens",
        width: 110,
        render: (v: number | null) => (v == null ? "—" : v),
      },
      {
        title: "Cache Write",
        dataIndex: "cache_write_tokens",
        key: "cache_write_tokens",
        width: 110,
        render: (v: number | null) => (v == null ? "—" : v),
      },
      {
        title: "状态",
        dataIndex: "ok",
        key: "ok",
        width: 88,
        render: (ok: boolean) => (
          <Tag color={ok ? "success" : "error"} className="rounded-lg">
            {ok ? "成功" : "失败"}
          </Tag>
        ),
      },
    ],
    [],
  );

  return (
    <>
      <PageIntro
        title="模型调用记录"
        description={`内部 AI 网关每次调用的原始请求与响应、耗时与输入/输出/合计 token（共 ${total} 条）。非流式调用时首 token 耗时可能为空。`}
        extra={
          <Button className="rounded-xl" icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
            刷新
          </Button>
        }
      />
      {error ? (
        <Typography.Paragraph type="danger" className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3">
          加载失败：{error}
        </Typography.Paragraph>
      ) : null}
      <div className="rounded-2xl border border-shell-line bg-[rgba(255,253,249,0.85)] p-4 backdrop-blur">
        <Space wrap className="mb-4 w-full" size="middle">
          <Input
            allowClear
            className="max-w-[min(100%,320px)] rounded-xl font-mono text-xs"
            placeholder="按 trace_id 精确筛选（须与库中一致）"
            value={traceDraft}
            onChange={(e) => setTraceDraft(e.target.value)}
            onPressEnter={() => applyFilters()}
          />
          <Input
            allowClear
            className="max-w-[min(100%,280px)] rounded-xl font-mono text-xs"
            placeholder="按 span_id 精确筛选（须与库中一致）"
            value={spanDraft}
            onChange={(e) => setSpanDraft(e.target.value)}
            onPressEnter={() => applyFilters()}
          />
          <Button type="primary" className="rounded-xl" icon={<SearchOutlined />} onClick={() => applyFilters()}>
            查询
          </Button>
          <Button className="rounded-xl" onClick={() => resetFilters()} disabled={!traceDraft && !spanDraft && !traceFilter && !spanFilter}>
            重置
          </Button>
        </Space>
        <Table<ModelInvocationRow>
          rowKey="id"
          loading={loading}
          columns={columns}
          dataSource={items}
          scroll={{ x: "max-content" }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: ["10", "20", "50", "100"],
            showTotal: (t) => `共 ${t} 条`,
            onChange: (p, ps) => {
              setPage(p);
              setPageSize(ps);
            },
          }}
          size="middle"
          className="doyoutrade-model-invocations-table"
          onRow={(record) => ({
            onClick: () => setDetail(record),
            className: "cursor-pointer",
          })}
        />
        <Typography.Text type="secondary" className="mt-2 block text-xs">
          点击行在中央弹窗中查看 JSON；长行自动换行（不显示行号以避免错位）。
        </Typography.Text>
      </div>

      <Modal
        title={`调用 #${detail?.id ?? ""}`}
        open={detail != null}
        onCancel={() => setDetail(null)}
        footer={null}
        centered
        width="min(1280px, 96vw)"
        destroyOnClose
        classNames={{ body: "!pt-1 !px-1" }}
        styles={{
          body: {
            maxHeight: "min(88vh, 900px)",
            overflowY: "auto",
          },
        }}
      >
        {detail ? (
          <div className="px-2 pb-1">
            <Typography.Paragraph className="mb-3 text-sm text-shell-muted">
              <span className="mr-3 inline-block">任务: {detail.task_id ?? "—"}</span>
              <span className="mr-3 inline-block">run: {detail.run_id ?? "—"}</span>
              <span className="mr-3 inline-block">
                trace_id:{" "}
                {detail.trace_id ? (
                  <Typography.Text className="font-mono text-xs" copyable={{ text: detail.trace_id }}>
                    {detail.trace_id}
                  </Typography.Text>
                ) : (
                  "—"
                )}
              </span>
              <span className="mr-3 inline-block">
                span_id:{" "}
                {detail.span_id ? (
                  <Typography.Text className="font-mono text-xs" copyable={{ text: detail.span_id }}>
                    {detail.span_id}
                  </Typography.Text>
                ) : (
                  "—"
                )}
              </span>
              <span className="mr-3 inline-block">model_id: {detail.model_id}</span>
              <span className="inline-block">model: {detail.model}</span>
            </Typography.Paragraph>
            {detail.error_message ? (
              <Typography.Paragraph type="danger">{detail.error_message}</Typography.Paragraph>
            ) : null}
            <TabbedJsonPanel
              title="请求 (request)"
              originContent={<ModelInvocationRequestPanel data={detail.request} />}
              formatContent={<FormattedRequestView data={detail.request} provider={detail.provider_kind} />}
            />
            <TabbedJsonPanel
              title="响应 (response)"
              originContent={<JsonCodeBlock value={detail.response} copyable />}
              formatContent={<FormattedResponseView data={detail.response} provider_kind={detail.provider_kind} />}
            />
          </div>
        ) : null}
      </Modal>
    </>
  );
}
