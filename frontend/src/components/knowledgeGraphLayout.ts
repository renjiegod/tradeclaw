/**
 * 知识图谱的确定性同心环（radial）布局 — 无第三方图库、无随机数。
 *
 * 旧版是纯力导向（hash 撒点 + spring-electric），跳数一多节点就毫无
 * 层次、长边互相穿插。现在改为「按跳数分环」的放射布局：
 *
 * - 中心实体钉在画布中央，BFS 深度 = 第几跳 = 第几圈椭圆环，跳数结构
 *   一眼可读；
 * - 环内角度按 BFS 生成树的叶子数分配扇区——孩子聚在父节点的角度附近，
 *   边以径向为主、交叉最少；
 * - 同环节点再做一轮确定性的最小角距散开，避免节点 / 标签贴在一起。
 *
 * 初始角度与 tie-break 全部由节点 id 的稳定 hash / 字典序决定，因此
 * **同样的输入永远得到同样的布局** —— 组件重渲染不抖动，vitest 断言
 * 可复现。用户拖拽 / 保存的坐标通过 ``seedPositions`` 原样保留。
 */

import type { KgEdge, KgNode } from "../types";

export type NodePosition = { x: number; y: number };

/** 同环节点之间为标签预留的弧长（px），不足时退化为均匀分布。 */
const LABEL_ARC_PX = 96;

/** FNV-1a — 稳定的字符串 hash，用于孤立节点的确定性初始角度与边的弯曲方向。 */
export function hashString(text: string): number {
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

function hashAngle(id: string): number {
  return ((hashString(id) % 3600) / 3600) * Math.PI * 2 - Math.PI / 2;
}

type RadialModel = {
  /** 节点 id → BFS 跳数；与中心不连通的节点排在最外环 +1。 */
  depths: Map<string, number>;
  /** BFS 生成树：父 → 子（按 id 字典序，确定性）。 */
  children: Map<string, string[]>;
  /** BFS 访问顺序（含中心）。 */
  order: string[];
};

function buildRadialModel(
  nodes: KgNode[],
  edges: KgEdge[],
  centerId: string,
): RadialModel {
  const known = new Set(nodes.map((n) => n.id));
  const adjacency = new Map<string, string[]>();
  for (const id of known) adjacency.set(id, []);
  // 去掉指向未知节点的边（防御：后端保证端点齐全，但布局不该因此崩）。
  for (const edge of edges) {
    if (!known.has(edge.src_id) || !known.has(edge.dst_id)) continue;
    adjacency.get(edge.src_id)!.push(edge.dst_id);
    adjacency.get(edge.dst_id)!.push(edge.src_id);
  }
  for (const list of adjacency.values()) list.sort();

  const depths = new Map<string, number>();
  const children = new Map<string, string[]>();
  const order: string[] = [];
  if (!known.has(centerId)) {
    // 防御：中心不在节点表时全部按 1 跳处理（面板保证不会发生）。
    for (const id of [...known].sort()) {
      depths.set(id, 1);
      order.push(id);
    }
    return { depths, children, order };
  }

  depths.set(centerId, 0);
  const queue = [centerId];
  let maxReached = 0;
  while (queue.length > 0) {
    const current = queue.shift()!;
    order.push(current);
    const depth = depths.get(current)!;
    for (const neighbor of adjacency.get(current) ?? []) {
      if (depths.has(neighbor)) continue;
      depths.set(neighbor, depth + 1);
      maxReached = Math.max(maxReached, depth + 1);
      const kids = children.get(current) ?? [];
      kids.push(neighbor);
      children.set(current, kids);
      queue.push(neighbor);
    }
  }
  // 不可达节点（理论上不该出现）确定性地排到最外环 +1。
  const orphanDepth = maxReached + 1;
  for (const id of [...known].sort()) {
    if (!depths.has(id)) {
      depths.set(id, orphanDepth);
      order.push(id);
    }
  }
  return { depths, children, order };
}

/**
 * 每个节点到中心的跳数（BFS 深度）。中心为 0；与中心不连通的节点
 * 排到最外环 +1。供面板绘制「N 跳」参考环使用。
 */
export function computeHopDepths(
  nodes: KgNode[],
  edges: KgEdge[],
  centerId: string,
): Map<string, number> {
  return buildRadialModel(nodes, edges, centerId).depths;
}

/**
 * 第 1..maxDepth 跳对应的椭圆环半径（与 {@link layoutNeighborhood} 的
 * 放置公式一致），供面板绘制参考环。
 */
export function hopRingGeometry(
  maxDepth: number,
  options: { width: number; height: number; padding?: number },
): Array<{ depth: number; rx: number; ry: number }> {
  const padding = options.padding ?? 48;
  const rxMax = Math.max(options.width / 2 - padding, 40);
  const ryMax = Math.max(options.height / 2 - padding, 40);
  const rings: Array<{ depth: number; rx: number; ry: number }> = [];
  for (let depth = 1; depth <= maxDepth; depth += 1) {
    rings.push({
      depth,
      rx: (rxMax * depth) / maxDepth,
      ry: (ryMax * depth) / maxDepth,
    });
  }
  return rings;
}

/**
 * Compute positions for a neighborhood subgraph.
 *
 * 中心节点固定在画布中央；其余节点按 BFS 跳数落在对应的椭圆环上，环内
 * 角度继承 BFS 生成树的扇区（叶子数加权），同环节点再按最小角距散开。
 * ``seedPositions``（用户拖拽 / 已保存布局）原样保留，最后统一钳制进
 * 画布留白内。返回 ``Map<nodeId, {x, y}>``。
 */
export function layoutNeighborhood(
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
  const cx = width / 2;
  const cy = height / 2;

  const seed =
    options.seedPositions instanceof Map
      ? options.seedPositions
      : new Map(Object.entries(options.seedPositions ?? {}));

  const positions = new Map<string, NodePosition>();
  if (nodes.length === 0) return positions;

  const { depths, children, order } = buildRadialModel(nodes, edges, centerId);
  let maxDepth = 1;
  for (const depth of depths.values()) maxDepth = Math.max(maxDepth, depth);
  const rings = hopRingGeometry(maxDepth, { width, height, padding });

  // BFS 树扇区分配：根占整圈（从正上方起），孩子按叶子数瓜分父扇区。
  const leafCount = new Map<string, number>();
  for (let i = order.length - 1; i >= 0; i -= 1) {
    const id = order[i];
    const kids = children.get(id) ?? [];
    if (kids.length === 0) {
      leafCount.set(id, 1);
      continue;
    }
    let sum = 0;
    for (const kid of kids) sum += leafCount.get(kid) ?? 1;
    leafCount.set(id, sum);
  }
  const treeAngle = new Map<string, number>();
  const sectors = new Map<string, [number, number]>();
  sectors.set(centerId, [-Math.PI / 2, Math.PI * 1.5]);
  for (const id of order) {
    const sector = sectors.get(id);
    if (!sector) continue; // 不可达节点没有树扇区，走 hash 角度兜底。
    const [a0, a1] = sector;
    if (id !== centerId) treeAngle.set(id, (a0 + a1) / 2);
    const kids = children.get(id) ?? [];
    if (kids.length === 0) continue;
    let total = 0;
    for (const kid of kids) total += leafCount.get(kid) ?? 1;
    let cursor = a0;
    for (const kid of kids) {
      const span = ((a1 - a0) * (leafCount.get(kid) ?? 1)) / total;
      sectors.set(kid, [cursor, cursor + span]);
      cursor += span;
    }
  }

  // 逐环放置（种子节点与中心除外），同环做确定性的最小角距散开。
  const byRing = new Map<number, string[]>();
  for (const node of nodes) {
    if (node.id === centerId || seed.has(node.id)) continue;
    const depth = depths.get(node.id) ?? maxDepth;
    const list = byRing.get(depth) ?? [];
    list.push(node.id);
    byRing.set(depth, list);
  }
  for (const [depth, ids] of byRing) {
    const ring = rings[Math.min(Math.max(depth, 1), maxDepth) - 1];
    const effectiveRadius = (ring.rx + ring.ry) / 2;
    const items = ids
      .map((id) => ({ id, angle: treeAngle.get(id) ?? hashAngle(id) }))
      .sort((a, b) => a.angle - b.angle || (a.id < b.id ? -1 : 1));
    const count = items.length;
    const evenGap = (Math.PI * 2) / count;
    const minGap = Math.min(LABEL_ARC_PX / effectiveRadius, evenGap);
    let prev = Number.NEGATIVE_INFINITY;
    let placed = items.map((item) => {
      const angle = Math.max(item.angle, prev + minGap);
      prev = angle;
      return { id: item.id, angle };
    });
    // 前推散开后若绕圈越界（首尾重叠），退化为从首角起的均匀分布。
    if (
      count > 1 &&
      placed[count - 1].angle - placed[0].angle > Math.PI * 2 - minGap
    ) {
      placed = items.map((item, index) => ({
        id: item.id,
        angle: items[0].angle + index * evenGap,
      }));
    }
    for (const { id, angle } of placed) {
      positions.set(id, {
        x: cx + ring.rx * Math.cos(angle),
        y: cy + ring.ry * Math.sin(angle),
      });
    }
  }

  for (const node of nodes) {
    const seeded = seed.get(node.id);
    if (seeded) {
      positions.set(node.id, { x: seeded.x, y: seeded.y });
    } else if (node.id === centerId) {
      positions.set(node.id, { x: cx, y: cy });
    }
  }

  for (const p of positions.values()) {
    p.x = Math.min(Math.max(p.x, padding), width - padding);
    p.y = Math.min(Math.max(p.y, padding), height - padding);
  }
  return positions;
}
