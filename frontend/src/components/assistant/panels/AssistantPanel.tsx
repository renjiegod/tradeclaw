// frontend/src/components/assistant/panels/AssistantPanel.tsx
//
// Agent 动态面板容器 + 块注册表。借鉴 modelgo-controller-web 的 fizz
// blockRegistry / FizzPanel：一份声明式 PanelSpec → 自上而下堆叠地渲染每个块，
// 每个块按 type 走注册表分发到对应组件。K线/图表/知识图谱是数据组件（各自文件），
// table/statcard/markdown 是轻量展示块（本文件内联）。
//
// 由 MessageContentRenderer 在识别到 render_panel 工具调用时挂载（见其路由逻辑）。

import { Table } from "antd";
import type { ColumnsType } from "antd/es/table";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { MODEL_INVOCATION_PROSE_CLASSNAME } from "../../../styles/classNames";
import { AssistantChartBlock } from "./AssistantChartBlock";
import { AssistantKGraphBlock } from "./AssistantKGraphBlock";
import { AssistantKlineBlock } from "./AssistantKlineBlock";
import type {
  MarkdownBlock,
  PanelBlock,
  PanelSpec,
  StatCardBlock,
  TableBlock,
} from "./panelSpec";

function cellToText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function AssistantTableBlock({ block }: { block: TableBlock }) {
  const columns: ColumnsType<Record<string, unknown>> = block.columns.map((column) => ({
    title: column.title,
    dataIndex: column.data_index,
    key: column.data_index,
    align: column.align,
    render: (value: unknown) => cellToText(value),
  }));
  const dataSource = block.rows.map((row, index) => ({ ...row, __row_key__: index }));
  return (
    <div className="w-full overflow-x-auto">
      <Table
        size="small"
        columns={columns}
        dataSource={dataSource}
        rowKey="__row_key__"
        pagination={block.rows.length > 20 ? { pageSize: 20, size: "small" } : false}
        scroll={{ x: "max-content" }}
        data-testid="assistant-table-block"
      />
    </div>
  );
}

function AssistantStatCardBlock({ block }: { block: StatCardBlock }) {
  const deltaColor = (dir: string | undefined): string => {
    if (dir === "up") return "#c0504d"; // 涨=红（A股约定，与 K 线一致）
    if (dir === "down") return "#2f8f6b"; // 跌=绿
    return "#6b7280";
  };
  const deltaArrow = (dir: string | undefined): string => {
    if (dir === "up") return "▲";
    if (dir === "down") return "▼";
    return "";
  };
  return (
    <div
      className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4"
      data-testid="assistant-statcard-block"
    >
      {block.metrics.map((metric, index) => (
        <div key={index} className="rounded-lg border border-shell-line bg-white px-3 py-2">
          <div className="truncate text-xs text-gray-500">{metric.label}</div>
          <div className="mt-1 flex items-baseline gap-1">
            <span className="text-lg font-semibold text-gray-800">{String(metric.value)}</span>
            {metric.unit ? <span className="text-xs text-gray-400">{metric.unit}</span> : null}
          </div>
          {metric.delta != null && String(metric.delta) !== "" ? (
            <div className="mt-0.5 text-xs" style={{ color: deltaColor(metric.delta_dir) }}>
              {deltaArrow(metric.delta_dir)} {String(metric.delta)}
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function AssistantMarkdownBlock({ block }: { block: MarkdownBlock }) {
  return (
    <div className={MODEL_INVOCATION_PROSE_CLASSNAME} data-testid="assistant-markdown-block">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{block.content}</ReactMarkdown>
    </div>
  );
}

// 块注册表：按 type 分发到对应渲染组件（类型安全的联合分派）。K线/图表/知识图谱
// 是数据组件，table/statcard/markdown 是本文件内联的轻量展示块。
function renderBlock(block: PanelBlock) {
  switch (block.type) {
    case "kline":
      return <AssistantKlineBlock block={block} />;
    case "chart":
      return <AssistantChartBlock block={block} />;
    case "kgraph":
      return <AssistantKGraphBlock block={block} />;
    case "table":
      return <AssistantTableBlock block={block} />;
    case "statcard":
      return <AssistantStatCardBlock block={block} />;
    case "markdown":
      return <AssistantMarkdownBlock block={block} />;
    default:
      return null;
  }
}

export const PANEL_BLOCK_TYPES: PanelBlock["type"][] = [
  "kline",
  "chart",
  "kgraph",
  "table",
  "statcard",
  "markdown",
];

function blockTitle(block: PanelBlock): string | undefined {
  return "title" in block ? block.title : undefined;
}

export function AssistantPanel({ spec }: { spec: PanelSpec }) {
  return (
    <div
      className="flex flex-col gap-3 rounded-xl border border-shell-line bg-white p-3 shadow-sm"
      data-testid="assistant-panel"
    >
      {spec.title ? (
        <div className="text-sm font-semibold text-gray-700">{spec.title}</div>
      ) : null}
      {spec.blocks.map((block) => {
        const title = blockTitle(block);
        return (
          <div key={block.id} className="flex flex-col gap-1.5">
            {title ? <div className="text-xs font-medium text-gray-500">{title}</div> : null}
            {renderBlock(block)}
          </div>
        );
      })}
    </div>
  );
}
