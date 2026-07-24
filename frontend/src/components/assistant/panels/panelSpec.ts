// frontend/src/components/assistant/panels/panelSpec.ts
//
// Agent 动态面板（render_panel 工具）的前端契约 + 规范化器。
//
// 面板规范由 render_panel 的**调用参数**（tool.input / content_blocks 里的
// arguments）流到前端：MessageContentRenderer 识别 name === "render_panel"
// 的 tool_call，把参数交给 parsePanelSpec 规范化后由 <AssistantPanel> 渲染。
// 与后端 doyoutrade/tools/render_panel.py 的 JSON Schema 一一对应（字段名保持
// snake_case wire 形态）。
//
// 借鉴 modelgo-controller-web 的 fizz normalizePanel：宽进严出——非法块被丢弃
// （返回 null）而非抛错，半份/流式中的参数也能安全渲染已成形的部分。

export type PanelBlockType =
  | "kline"
  | "chart"
  | "kgraph"
  | "table"
  | "statcard"
  | "markdown";

export type KlineInterval = "1d" | "5m" | "60m";
export type KlineMainIndicator = "MA" | "BOLL" | "none";
export type KlineSubIndicator = "MACD" | "KDJ" | "RSI" | "WR" | "none";
export type KlineOverlayKind = "backtest_trades" | "task_fills" | "signals";
export type ChartType = "line" | "bar" | "area" | "pie";
export type KGraphLayout = "radial" | "force";
export type KGraphColorMode = "type" | "community";

export type KlineBlock = {
  id: string;
  type: "kline";
  title?: string;
  symbol: string;
  interval: KlineInterval;
  start?: string;
  end?: string;
  adjust: "qfq" | "hfq" | "none";
  provider: string;
  main_indicator: KlineMainIndicator;
  sub_indicator: KlineSubIndicator;
  overlays: KlineOverlayKind[];
  height: number;
};

export type ChartBlock = {
  id: string;
  type: "chart";
  title?: string;
  chart_type: ChartType;
  data: Array<Record<string, unknown>>;
  x_field?: string;
  y_fields: string[];
  series_names: Record<string, string>;
  category_field?: string;
  value_field?: string;
  unit?: string;
  stacked: boolean;
  height: number;
};

export type KGraphNodeLite = {
  id: string;
  name: string;
  node_type: string;
  display_name?: string | null;
};

export type KGraphEdgeLite = {
  id: string;
  src_id: string;
  dst_id: string;
  relation: string;
  fact?: string;
};

export type KGraphBlock = {
  id: string;
  type: "kgraph";
  title?: string;
  // 引用式：前端按 /knowledge/graph 拉取
  entity?: string;
  hops: number;
  include_expired: boolean;
  // 内联式：直接给节点/边
  nodes?: KGraphNodeLite[];
  edges?: KGraphEdgeLite[];
  center_id?: string;
  layout: KGraphLayout;
  color_mode: KGraphColorMode;
  height: number;
};

export type TableColumn = {
  title: string;
  data_index: string;
  align?: "left" | "right" | "center";
};

export type TableBlock = {
  id: string;
  type: "table";
  title?: string;
  columns: TableColumn[];
  rows: Array<Record<string, unknown>>;
};

export type StatMetric = {
  label: string;
  value: string | number;
  unit?: string;
  delta?: string | number;
  delta_dir?: "up" | "down" | "flat";
};

export type StatCardBlock = {
  id: string;
  type: "statcard";
  title?: string;
  metrics: StatMetric[];
};

export type MarkdownBlock = {
  id: string;
  type: "markdown";
  content: string;
};

export type PanelBlock =
  | KlineBlock
  | ChartBlock
  | KGraphBlock
  | TableBlock
  | StatCardBlock
  | MarkdownBlock;

export type PanelSpec = {
  v: number;
  title?: string;
  panel_id?: string;
  blocks: PanelBlock[];
};

const DEFAULT_KLINE_HEIGHT = 460;
const DEFAULT_CHART_HEIGHT = 300;
const DEFAULT_KGRAPH_HEIGHT = 460;
const MAX_BLOCKS = 12;

const KLINE_INTERVALS = new Set<KlineInterval>(["1d", "5m", "60m"]);
const MAIN_INDICATORS = new Set<KlineMainIndicator>(["MA", "BOLL", "none"]);
const SUB_INDICATORS = new Set<KlineSubIndicator>(["MACD", "KDJ", "RSI", "WR", "none"]);
const OVERLAY_KINDS = new Set<KlineOverlayKind>(["backtest_trades", "task_fills", "signals"]);
const CHART_TYPES = new Set<ChartType>(["line", "bar", "area", "pie"]);
const ADJUSTS = new Set(["qfq", "hfq", "none"]);
const KGRAPH_LAYOUTS = new Set<KGraphLayout>(["radial", "force"]);
const KGRAPH_COLOR_MODES = new Set<KGraphColorMode>(["type", "community"]);
// 与后端 render_panel._SYMBOL_RE 保持一致（canonical CODE.EXCHANGE）。
const SYMBOL_RE = /^[A-Za-z0-9]{1,15}\.[A-Za-z]{1,6}$/;

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asPositiveInt(value: unknown, fallback: number): number {
  const num = typeof value === "number" ? value : Number(value);
  return Number.isFinite(num) && num > 0 ? Math.floor(num) : fallback;
}

function pickEnum<T extends string>(value: unknown, allowed: Set<T>, fallback: T): T {
  return typeof value === "string" && allowed.has(value as T) ? (value as T) : fallback;
}

function normalizeKline(raw: Record<string, unknown>, id: string): KlineBlock | null {
  const symbol = asString(raw.symbol);
  if (!symbol || !SYMBOL_RE.test(symbol)) return null;
  const overlaysRaw = Array.isArray(raw.overlays) ? raw.overlays : [];
  const overlays = overlaysRaw.filter(
    (item): item is KlineOverlayKind => typeof item === "string" && OVERLAY_KINDS.has(item as KlineOverlayKind),
  );
  return {
    id,
    type: "kline",
    title: asString(raw.title),
    symbol,
    interval: pickEnum(raw.interval, KLINE_INTERVALS, "1d"),
    start: asString(raw.start),
    end: asString(raw.end),
    adjust: pickEnum(raw.adjust, ADJUSTS, "qfq") as "qfq" | "hfq" | "none",
    provider: asString(raw.provider) ?? "auto",
    main_indicator: pickEnum(raw.main_indicator, MAIN_INDICATORS, "MA"),
    sub_indicator: pickEnum(raw.sub_indicator, SUB_INDICATORS, "MACD"),
    overlays,
    height: asPositiveInt(raw.height, DEFAULT_KLINE_HEIGHT),
  };
}

function normalizeChart(raw: Record<string, unknown>, id: string): ChartBlock | null {
  const chartType = pickEnum(raw.chart_type, CHART_TYPES, "line");
  const data = Array.isArray(raw.data)
    ? raw.data.filter((row): row is Record<string, unknown> => asRecord(row) !== null)
    : [];
  if (data.length === 0) return null;
  const yFieldsRaw = Array.isArray(raw.y_fields) ? raw.y_fields : [];
  const yFields = yFieldsRaw.filter((field): field is string => typeof field === "string" && field.length > 0);
  const xField = asString(raw.x_field);
  const categoryField = asString(raw.category_field);
  const valueField = asString(raw.value_field);
  if (chartType === "pie") {
    if (!categoryField || !valueField) return null;
  } else if (!xField || yFields.length === 0) {
    return null;
  }
  const seriesNamesRaw = asRecord(raw.series_names) ?? {};
  const seriesNames: Record<string, string> = {};
  for (const [key, value] of Object.entries(seriesNamesRaw)) {
    if (typeof value === "string") seriesNames[key] = value;
  }
  return {
    id,
    type: "chart",
    title: asString(raw.title),
    chart_type: chartType,
    data,
    x_field: xField,
    y_fields: yFields,
    series_names: seriesNames,
    category_field: categoryField,
    value_field: valueField,
    unit: asString(raw.unit),
    stacked: raw.stacked === true,
    height: asPositiveInt(raw.height, DEFAULT_CHART_HEIGHT),
  };
}

function normalizeKgraph(raw: Record<string, unknown>, id: string): KGraphBlock | null {
  const entity = asString(raw.entity);
  const nodesRaw = Array.isArray(raw.nodes) ? raw.nodes : null;
  const edgesRaw = Array.isArray(raw.edges) ? raw.edges : null;
  let nodes: KGraphNodeLite[] | undefined;
  let edges: KGraphEdgeLite[] | undefined;
  if (nodesRaw && nodesRaw.length > 0) {
    nodes = nodesRaw
      .map((node) => asRecord(node))
      .filter((node): node is Record<string, unknown> => node !== null && !!asString(node.id))
      .map((node) => ({
        id: asString(node.id)!,
        name: asString(node.name) ?? asString(node.id)!,
        node_type: asString(node.node_type) ?? "other",
        display_name: asString(node.display_name) ?? null,
      }));
    edges = (edgesRaw ?? [])
      .map((edge) => asRecord(edge))
      .filter(
        (edge): edge is Record<string, unknown> =>
          edge !== null && !!asString(edge.src_id) && !!asString(edge.dst_id),
      )
      .map((edge, index) => ({
        id: asString(edge.id) ?? `e${index}`,
        src_id: asString(edge.src_id)!,
        dst_id: asString(edge.dst_id)!,
        relation: asString(edge.relation) ?? "related",
        fact: asString(edge.fact),
      }));
  }
  if (!entity && !(nodes && nodes.length > 0)) return null;
  return {
    id,
    type: "kgraph",
    title: asString(raw.title),
    entity,
    hops: Math.min(3, Math.max(1, asPositiveInt(raw.hops, 1))),
    include_expired: raw.include_expired === true,
    nodes,
    edges,
    center_id: asString(raw.center_id),
    layout: pickEnum(raw.layout, KGRAPH_LAYOUTS, "radial"),
    color_mode: pickEnum(raw.color_mode, KGRAPH_COLOR_MODES, "type"),
    height: asPositiveInt(raw.height, DEFAULT_KGRAPH_HEIGHT),
  };
}

function normalizeTable(raw: Record<string, unknown>, id: string): TableBlock | null {
  const columnsRaw = Array.isArray(raw.columns) ? raw.columns : [];
  const columns: TableColumn[] = columnsRaw
    .map((column) => asRecord(column))
    .filter((column): column is Record<string, unknown> => column !== null)
    .map((column) => ({
      title: asString(column.title) ?? "",
      data_index: asString(column.data_index) ?? "",
      align: pickEnum(column.align, new Set(["left", "right", "center"] as const), "left"),
    }))
    .filter((column) => column.title && column.data_index);
  if (columns.length === 0) return null;
  const rows = Array.isArray(raw.rows)
    ? raw.rows.filter((row): row is Record<string, unknown> => asRecord(row) !== null)
    : [];
  return { id, type: "table", title: asString(raw.title), columns, rows };
}

function normalizeStatCard(raw: Record<string, unknown>, id: string): StatCardBlock | null {
  const metricsRaw = Array.isArray(raw.metrics) ? raw.metrics : [];
  const metrics: StatMetric[] = metricsRaw
    .map((metric) => asRecord(metric))
    .filter((metric): metric is Record<string, unknown> => metric !== null && !!asString(metric.label))
    .map((metric) => ({
      label: asString(metric.label)!,
      value:
        typeof metric.value === "number" || typeof metric.value === "string"
          ? metric.value
          : String(metric.value ?? ""),
      unit: asString(metric.unit),
      delta:
        typeof metric.delta === "number" || typeof metric.delta === "string" ? metric.delta : undefined,
      delta_dir: pickEnum(metric.delta_dir, new Set(["up", "down", "flat"] as const), "flat"),
    }));
  if (metrics.length === 0) return null;
  return { id, type: "statcard", title: asString(raw.title), metrics };
}

function normalizeBlock(raw: unknown, index: number): PanelBlock | null {
  const record = asRecord(raw);
  if (!record) return null;
  // block.id 仅用作面板内的 React key；用位置索引而非 LLM 提供值，避免两个块
  // 撞同一个 id 导致 key 冲突（渲染错乱 / 状态串台）。索引在一次解析内唯一且
  // 跨渲染稳定。
  const id = `b${index}`;
  switch (record.type) {
    case "kline":
      return normalizeKline(record, id);
    case "chart":
      return normalizeChart(record, id);
    case "kgraph":
      return normalizeKgraph(record, id);
    case "table":
      return normalizeTable(record, id);
    case "statcard":
      return normalizeStatCard(record, id);
    case "markdown": {
      const content = asString(record.content);
      return content ? { id, type: "markdown", content } : null;
    }
    default:
      return null;
  }
}

/**
 * 把 render_panel 的原始调用参数规范化成一份可渲染的 PanelSpec。
 * 宽进严出：非法块被丢弃；无任何有效块时返回 null（调用方回退到普通工具卡）。
 * 接受已解析的对象，或（防御性地）JSON 字符串。
 */
export function parsePanelSpec(raw: unknown): PanelSpec | null {
  let source: unknown = raw;
  if (typeof source === "string") {
    try {
      source = JSON.parse(source);
    } catch {
      return null;
    }
  }
  const record = asRecord(source);
  if (!record) return null;
  const blocksRaw = Array.isArray(record.blocks) ? record.blocks.slice(0, MAX_BLOCKS) : [];
  const blocks = blocksRaw
    .map((block, index) => normalizeBlock(block, index))
    .filter((block): block is PanelBlock => block !== null);
  if (blocks.length === 0) return null;
  return {
    v: typeof record.v === "number" ? record.v : 1,
    title: asString(record.title),
    panel_id: asString(record.panel_id),
    blocks,
  };
}
