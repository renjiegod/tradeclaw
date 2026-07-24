// frontend/src/components/KnowledgeGraphSvg.tsx
//
// 纯展示型知识图谱 SVG 渲染器 —— 从 KnowledgeGraphPanel 的图形层抽出，剥离
// 拖拽 / 悬停 / 选择 / 6 个 API 调用等有状态逻辑，只接收 {nodes, edges} 就画图。
// 供 Agent 动态面板（kgraph 块）与其他只读展示场景复用。
//
// 复用 KnowledgeGraphPanel 同一套确定性布局纯函数（无第三方图库、无随机数）：
// 同心环 layoutNeighborhood / 力导向 layoutForce / 社区着色 detectCommunities，
// 因此同样的输入永远得到同样的图（vitest 可断言、重渲染不抖动）。

import { useMemo } from "react";

import type { KgEdge, KgNode } from "../types";
import { hashString, layoutNeighborhood, type NodePosition } from "./knowledgeGraphLayout";
import { layoutForce } from "./knowledgeGraphForceLayout";
import { communityColor, detectCommunities } from "./knowledgeGraphCommunities";

const SVG_WIDTH = 720;

// 节点类型 → 颜色 / 中文标签（与 KnowledgeGraphPanel 的 NODE_TYPE_STYLES 一致，
// dataviz 六项校验通过的 light-surface 分类色板）。
const NODE_TYPE_STYLES: Record<string, { color: string; label: string }> = {
  symbol: { color: "#b26a1f", label: "个股" },
  theme: { color: "#b8508f", label: "题材" },
  cycle: { color: "#3b6fd4", label: "周期月" },
  role: { color: "#2f8f6b", label: "角色" },
  playbook: { color: "#6b7f2e", label: "战法" },
  signal: { color: "#7b5fc0", label: "信号" },
};
const FALLBACK_NODE_STYLE = { color: "#8a7a63", label: "其他" };

const RELATION_LABELS: Record<string, string> = {
  has_role: "担任角色",
  traded_in: "交易于",
  signals: "决策信号",
  belongs_to_theme: "属于题材",
  leads_theme: "题材龙头",
  uses_playbook: "使用战法",
  linked_with: "个股联动",
  observed_in: "活跃于周期",
};

function nodeStyle(nodeType: string): { color: string; label: string } {
  return NODE_TYPE_STYLES[nodeType] ?? FALLBACK_NODE_STYLE;
}

function nodeLabel(node: KgNode): string {
  if (node.display_name && node.display_name !== node.name) return node.display_name;
  return node.name;
}

/** 边的二次贝塞尔路径，弯向 / 弧度由 edge id 的稳定 hash 决定（确定性、不重叠）。 */
function edgePath(a: NodePosition, b: NodePosition, edgeId: string): string {
  const h = hashString(edgeId);
  const bend = (h % 2 === 0 ? 1 : -1) * (0.06 + ((h >>> 3) % 5) * 0.015);
  const mx = (a.x + b.x) / 2 - (b.y - a.y) * bend;
  const my = (a.y + b.y) / 2 + (b.x - a.x) * bend;
  return `M ${a.x} ${a.y} Q ${mx} ${my} ${b.x} ${b.y}`;
}

/** 选一个中心：优先给定 centerId（须在节点集内），否则取度数最高的节点。 */
function resolveCenterId(nodes: KgNode[], edges: KgEdge[], hint?: string): string {
  const ids = new Set(nodes.map((node) => node.id));
  if (hint && ids.has(hint)) return hint;
  const degree = new Map<string, number>();
  for (const node of nodes) degree.set(node.id, 0);
  for (const edge of edges) {
    if (degree.has(edge.src_id)) degree.set(edge.src_id, (degree.get(edge.src_id) ?? 0) + 1);
    if (degree.has(edge.dst_id)) degree.set(edge.dst_id, (degree.get(edge.dst_id) ?? 0) + 1);
  }
  let best = nodes[0]?.id ?? "";
  let bestDeg = -1;
  for (const node of nodes) {
    const deg = degree.get(node.id) ?? 0;
    // 平票时按 id 字典序取最小，保持确定性。
    if (deg > bestDeg || (deg === bestDeg && node.id < best)) {
      best = node.id;
      bestDeg = deg;
    }
  }
  return best;
}

export type KnowledgeGraphSvgProps = {
  nodes: KgNode[];
  edges: KgEdge[];
  centerId?: string;
  layout?: "radial" | "force";
  colorMode?: "type" | "community";
  height?: number;
};

export function KnowledgeGraphSvg({
  nodes,
  edges,
  centerId,
  layout = "radial",
  colorMode = "type",
  height = 460,
}: KnowledgeGraphSvgProps) {
  const centerResolved = useMemo(
    () => resolveCenterId(nodes, edges, centerId),
    [nodes, edges, centerId],
  );

  const positions = useMemo<Map<string, NodePosition>>(() => {
    if (nodes.length === 0) return new Map();
    const options = { width: SVG_WIDTH, height, centerId: centerResolved };
    return layout === "force"
      ? layoutForce(nodes, edges, options)
      : layoutNeighborhood(nodes, edges, options);
  }, [nodes, edges, centerResolved, layout, height]);

  const communities = useMemo(
    () => (colorMode === "community" ? detectCommunities(nodes, edges) : null),
    [colorMode, nodes, edges],
  );

  const denseGraph = nodes.length > 26;
  const nodeRadius = denseGraph ? 6 : 8;
  const fontSize = denseGraph ? 10 : 11;

  const colorFor = (node: KgNode): string => {
    if (communities) return communityColor(communities.get(node.id) ?? 0);
    return nodeStyle(node.node_type).color;
  };

  // 图例：着色=类型时列出现的类型；着色=社区时略（簇号由位置区分）。
  const typeLegend = useMemo(() => {
    if (colorMode === "community") return [];
    const seen = new Map<string, { color: string; label: string }>();
    for (const node of nodes) {
      if (!seen.has(node.node_type)) seen.set(node.node_type, nodeStyle(node.node_type));
    }
    return Array.from(seen.entries()).map(([type, style]) => ({ type, ...style }));
  }, [colorMode, nodes]);

  if (nodes.length === 0) {
    return (
      <div className="flex min-h-[160px] items-center justify-center rounded-lg border border-shell-line bg-gray-50 text-sm text-gray-400">
        暂无图谱数据
      </div>
    );
  }

  const drawableEdges = edges.filter(
    (edge) => positions.has(edge.src_id) && positions.has(edge.dst_id),
  );

  return (
    <div className="w-full overflow-x-auto">
      <svg
        viewBox={`0 0 ${SVG_WIDTH} ${height}`}
        className="h-auto w-full"
        style={{ background: "#fffdf9", borderRadius: 12 }}
        role="img"
        aria-label="知识图谱"
        data-testid="knowledge-graph-svg"
      >
        <g data-testid="kg-edges">
          {drawableEdges.map((edge) => {
            const a = positions.get(edge.src_id)!;
            const b = positions.get(edge.dst_id)!;
            const expired = Boolean(edge.expired_at);
            const isLlm = edge.provenance === "llm";
            const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
            const label = RELATION_LABELS[edge.relation] ?? edge.relation;
            return (
              <g key={edge.id}>
                <path
                  d={edgePath(a, b, edge.id)}
                  fill="none"
                  stroke={expired ? "#d6ccbe" : "#b8ac97"}
                  strokeWidth={expired ? 1 : 1.5}
                  strokeDasharray={isLlm ? "4 3" : undefined}
                  opacity={expired ? 0.5 : 0.8}
                />
                {!denseGraph && label ? (
                  <text
                    x={mid.x}
                    y={mid.y - 2}
                    textAnchor="middle"
                    fontSize={9}
                    fill="#9a8f7a"
                  >
                    {label}
                  </text>
                ) : null}
              </g>
            );
          })}
        </g>
        <g data-testid="kg-nodes">
          {nodes.map((node) => {
            const pos = positions.get(node.id);
            if (!pos) return null;
            const isCenter = node.id === centerResolved;
            const color = colorFor(node);
            const r = isCenter ? nodeRadius + 3 : nodeRadius;
            return (
              <g key={node.id} transform={`translate(${pos.x} ${pos.y})`}>
                <circle
                  r={r}
                  fill={color}
                  stroke={isCenter ? "#1f2937" : "#ffffff"}
                  strokeWidth={isCenter ? 2 : 1.5}
                />
                <text
                  x={0}
                  y={r + fontSize + 1}
                  textAnchor="middle"
                  fontSize={fontSize}
                  fontWeight={isCenter ? 600 : 400}
                  fill="#3f3a30"
                >
                  {nodeLabel(node)}
                </text>
              </g>
            );
          })}
        </g>
      </svg>
      {typeLegend.length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1" data-testid="kg-legend">
          {typeLegend.map((item) => (
            <span key={item.type} className="flex items-center gap-1 text-xs text-gray-500">
              <span
                className="inline-block h-2.5 w-2.5 rounded-full"
                style={{ background: item.color }}
              />
              {item.label}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
