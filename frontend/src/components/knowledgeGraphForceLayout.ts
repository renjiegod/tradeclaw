/**
 * 知识图谱的确定性力导向布局（Fruchterman-Reingold）——「探索模式」用。
 *
 * 与默认的同心环布局（{@link layoutNeighborhood}）并列：同心环让"第几跳"
 * 一眼可读，力导向让"谁和谁抱团"更自然。为了守住本项目的确定性纪律
 * （见 knowledgeGraphLayout.ts 顶部：无第三方图库、**无随机数**、无抖动），
 * 这里不用 d3-force，而是：
 *
 * - **初始坐标直接取同心环布局的结果做种子**（本身确定），而不是随机撒点；
 * - 固定迭代次数 + 线性降温，纯 FR 斥力/引力；
 * - 中心节点与用户拖拽/保存的 ``seedPositions`` 全程钉死（pinned），
 *   其余节点在其周围松弛。
 *
 * 因此**同样的输入永远得到同样的布局**——vitest 可断言，重渲染不抖动。
 */

import { layoutNeighborhood, type NodePosition } from "./knowledgeGraphLayout";
import type { KgEdge, KgNode } from "../types";

/** 固定迭代次数——邻域子图小，足够收敛且完全确定。 */
const ITERATIONS = 240;

export function layoutForce(
  nodes: KgNode[],
  edges: KgEdge[],
  options: {
    width: number;
    height: number;
    centerId: string;
    padding?: number;
    seedPositions?: Map<string, NodePosition> | Record<string, NodePosition>;
  },
): Map<string, NodePosition> {
  const { width, height, centerId } = options;
  const padding = options.padding ?? 48;
  if (nodes.length === 0) return new Map();

  const seed =
    options.seedPositions instanceof Map
      ? options.seedPositions
      : new Map(Object.entries(options.seedPositions ?? {}));

  // 用同心环布局做确定性初值（含 seed / center 的原样保留）。
  const positions = layoutNeighborhood(nodes, edges, options);

  const ids = nodes.map((n) => n.id);
  const known = new Set(ids);
  // 钉死集合：中心 + 用户显式放置的节点。
  const pinned = new Set<string>([centerId]);
  for (const id of seed.keys()) if (known.has(id)) pinned.add(id);

  // 单节点或全钉死时无需松弛。
  if (nodes.length <= 1 || pinned.size >= nodes.length) return positions;

  const area = Math.max((width - 2 * padding) * (height - 2 * padding), 1);
  // 理想边长 k = C·sqrt(area / n)。
  const k = 0.75 * Math.sqrt(area / nodes.length);
  const k2 = k * k;

  // 只对已有坐标的节点参与（防御：布局层保证齐全）。
  const live = ids.filter((id) => positions.has(id));

  const adjacency = new Map<string, Map<string, number>>();
  for (const id of live) adjacency.set(id, new Map());
  for (const edge of edges) {
    if (!known.has(edge.src_id) || !known.has(edge.dst_id)) continue;
    if (edge.src_id === edge.dst_id) continue;
    const a = adjacency.get(edge.src_id);
    const b = adjacency.get(edge.dst_id);
    if (!a || !b) continue;
    a.set(edge.dst_id, (a.get(edge.dst_id) ?? 0) + 1);
    b.set(edge.src_id, (b.get(edge.src_id) ?? 0) + 1);
  }

  const initialTemp = Math.max(width, height) / 10;

  for (let iter = 0; iter < ITERATIONS; iter += 1) {
    const temp = initialTemp * (1 - iter / ITERATIONS);
    const disp = new Map<string, { x: number; y: number }>();
    for (const id of live) disp.set(id, { x: 0, y: 0 });

    // 斥力：所有节点对（子图小，O(n^2) 可接受）。确定性遍历。
    for (let i = 0; i < live.length; i += 1) {
      const a = live[i];
      const pa = positions.get(a)!;
      for (let j = i + 1; j < live.length; j += 1) {
        const b = live[j];
        const pb = positions.get(b)!;
        let dx = pa.x - pb.x;
        let dy = pa.y - pb.y;
        let dist = Math.hypot(dx, dy);
        if (dist < 0.01) {
          // 完全重合：按 id 顺序确定性地掰开，不用随机。
          dx = a < b ? 0.01 : -0.01;
          dy = 0.01;
          dist = Math.hypot(dx, dy);
        }
        const force = k2 / dist;
        const ux = (dx / dist) * force;
        const uy = (dy / dist) * force;
        const da = disp.get(a)!;
        const db = disp.get(b)!;
        da.x += ux;
        da.y += uy;
        db.x -= ux;
        db.y -= uy;
      }
    }

    // 引力：沿边（平行边数作权重）。
    for (const a of live) {
      const pa = positions.get(a)!;
      for (const [b, weight] of adjacency.get(a) ?? []) {
        if (a >= b) continue; // 每条无序边只算一次
        const pb = positions.get(b)!;
        let dx = pa.x - pb.x;
        let dy = pa.y - pb.y;
        let dist = Math.hypot(dx, dy);
        if (dist < 0.01) dist = 0.01;
        const force = ((dist * dist) / k) * weight;
        const ux = (dx / dist) * force;
        const uy = (dy / dist) * force;
        const da = disp.get(a)!;
        const db = disp.get(b)!;
        da.x -= ux;
        da.y -= uy;
        db.x += ux;
        db.y += uy;
      }
    }

    // 位移（钉死节点不动），步长受温度限制，末尾钳制进画布。
    for (const id of live) {
      if (pinned.has(id)) continue;
      const d = disp.get(id)!;
      const len = Math.hypot(d.x, d.y);
      if (len < 1e-9) continue;
      const step = Math.min(len, temp);
      const p = positions.get(id)!;
      let x = p.x + (d.x / len) * step;
      let y = p.y + (d.y / len) * step;
      x = Math.min(Math.max(x, padding), width - padding);
      y = Math.min(Math.max(y, padding), height - padding);
      positions.set(id, { x, y });
    }
  }

  return positions;
}
