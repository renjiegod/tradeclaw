import { Space, Tag, Typography } from "antd";
import type { CollapseProps } from "antd";
import type { ReactNode } from "react";

import { JsonCodeBlock } from "../components/JsonCodeBlock";
import { FormattedRequestView } from "../components/FormattedRequestView";
import { FormattedResponseView } from "../components/FormattedResponseView";
import { ModelInvocationRequestPanel } from "../components/ModelInvocationRequestPanel";
import { TabbedJsonPanel } from "../components/TabbedJsonPanel";
import type { ModelInvocationRow } from "../types";
import { formatDateTimeUtc8 } from "../utils/datetime";

export function modelInvocationTokenSummary(row: ModelInvocationRow): string | null {
  const hasIn = row.input_tokens != null;
  const hasOut = row.output_tokens != null;
  const hasTotal = row.total_tokens != null;
  const hasCacheRead = row.cache_read_tokens != null;
  const hasCacheWrite = row.cache_write_tokens != null;
  if (!hasIn && !hasOut && !hasTotal && !hasCacheRead && !hasCacheWrite) return null;
  const parts: string[] = [];
  if (hasIn || hasOut) {
    parts.push(`in ${row.input_tokens ?? "—"} / out ${row.output_tokens ?? "—"}`);
  }
  if (hasTotal) {
    parts.push(`合计 ${row.total_tokens}`);
  }
  if (hasCacheRead || hasCacheWrite) {
    parts.push(`cache read ${row.cache_read_tokens ?? "—"} / write ${row.cache_write_tokens ?? "—"}`);
  }
  return parts.join(" · ");
}

function modelInvocationTokenSummaryParts(row: ModelInvocationRow): {
  main: string | null;
  cache: string | null;
} {
  const mainParts: string[] = [];
  const cacheParts: string[] = [];

  if (row.input_tokens != null || row.output_tokens != null) {
    mainParts.push(`in ${row.input_tokens ?? "—"} / out ${row.output_tokens ?? "—"}`);
  }
  if (row.total_tokens != null) {
    mainParts.push(`合计 ${row.total_tokens}`);
  }
  if (row.cache_read_tokens != null || row.cache_write_tokens != null) {
    cacheParts.push(`cache read ${row.cache_read_tokens ?? "—"} / write ${row.cache_write_tokens ?? "—"}`);
  }

  return {
    main: mainParts.length ? mainParts.join(" · ") : null,
    cache: cacheParts.length ? cacheParts.join(" · ") : null,
  };
}

export function renderModelInvocationTokenSummary(row: ModelInvocationRow): ReactNode {
  const { main, cache } = modelInvocationTokenSummaryParts(row);
  if (!main && !cache) return "—";

  return (
    <>
      {main ? <span>{main}</span> : null}
      {main && cache ? <span> · </span> : null}
      {cache ? <span className="text-orange-500">{cache}</span> : null}
    </>
  );
}

export function buildModelInvocationCollapseItems(
  invocations: ModelInvocationRow[],
): NonNullable<CollapseProps["items"]> {
  return invocations.map((item) => ({
    key: `mi-${item.id}`,
    label: (
      <Space size={8} wrap>
        <Tag>{item.call_kind}</Tag>
        <Typography.Text>{item.model}</Typography.Text>
        <Tag color={item.ok ? "success" : "error"} className="rounded-lg">
          {item.ok ? "成功" : "失败"}
        </Tag>
        {item.total_latency_ms != null ? (
          <Typography.Text type="secondary" className="text-xs">
            耗时 {item.total_latency_ms} ms
          </Typography.Text>
        ) : (
          <Typography.Text type="secondary" className="text-xs">
            耗时 —
          </Typography.Text>
        )}
        {item.first_token_latency_ms != null ? (
          <Typography.Text type="secondary" className="text-xs">
            首 token {item.first_token_latency_ms} ms
          </Typography.Text>
        ) : null}
        {modelInvocationTokenSummary(item) ? (
          <Typography.Text type="secondary" className="text-xs">
            tokens {renderModelInvocationTokenSummary(item)}
          </Typography.Text>
        ) : (
          <Typography.Text type="secondary" className="text-xs">
            tokens —
          </Typography.Text>
        )}
        {item.trace_id ? (
          <Typography.Text
            className="max-w-[220px] font-mono text-xs text-shell-muted"
            ellipsis={{ tooltip: item.trace_id }}
            copyable={{ text: item.trace_id }}
          >
            {item.trace_id}
          </Typography.Text>
        ) : null}
        {item.span_id ? (
          <Typography.Text
            className="max-w-[160px] font-mono text-xs text-shell-muted"
            ellipsis={{ tooltip: item.span_id }}
            copyable={{ text: item.span_id }}
          >
            {item.span_id}
          </Typography.Text>
        ) : null}
        <Typography.Text className="text-xs text-shell-muted">
          {formatDateTimeUtc8(item.created_at, item.created_at)}
        </Typography.Text>
      </Space>
    ),
    children: (
      <Space direction="vertical" size={12} className="w-full">
        {item.error_message ? (
          <Typography.Paragraph type="danger" className="!mb-0">
            {item.error_message}
          </Typography.Paragraph>
        ) : null}
        <TabbedJsonPanel
          title="请求 (request)"
          originContent={<ModelInvocationRequestPanel data={item.request} showMarkdown={false} />}
          formatContent={<FormattedRequestView data={item.request} provider={item.provider_kind} />}
        />
        <TabbedJsonPanel
          title="响应 (response)"
          originContent={<JsonCodeBlock value={item.response} copyable />}
          formatContent={<FormattedResponseView data={item.response} provider_kind={item.provider_kind} />}
        />
      </Space>
    ),
  }));
}
