/**
 * 知识图谱的确定性力导向布局 — 无第三方图库、无随机数。
 *
 * 个人知识库的邻域子图规模很小（后端 edge_limit=200，实际通常几十），
 * 一个 O(n²·iterations) 的朴素 spring-electric 布局绰绰有余，不值得为此
 * 引入 G6/cytoscape 级别的依赖。初始位置由节点 id 的稳定 hash 决定（中心
 * 节点钉在画布中央），因此**同样的输入永远得到同样的布局** —— 组件重渲染
 * 不抖动，vitest 断言可复现。
 */

import type { KgEdge, KgNode } from "../types";

export type NodePosition = { x: number; y: number };

/** FNV-1a — 稳定的字符串 hash，用来给每个节点一个确定的初始角度/半径。 */
function hashString(text: string): number {
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

/**
 * Compute positions for a neighborhood subgraph.
 *
 * 中心节点固定在画布中央；其余节点从 hash 决定的圆环初始位置出发，经
 * ``iterations`` 轮「全对斥力 + 边弹簧 + 轻微向心力」迭代后收敛，最后
 * 统一钳制进画布留白内。返回 ``Map<nodeId, {x, y}>``。
 */
export function layoutNeighborhood(
  nodes: KgNode[],
  edges: KgEdge[],
  options: {
    width: number;
    height: number;
    centerId: string;
    iterations?: number;
    padding?: number;
  },
): Map<string, NodePosition> {
  const { width, height, centerId } = options;
  const iterations = options.iterations ?? 220;
  const padding = options.padding ?? 48;
  const cx = width / 2;
  const cy = height / 2;

  const positions = new Map<string, NodePosition>();
  if (nodes.length === 0) return positions;

  const ringRadius = Math.min(width, height) / 3;
  for (const node of nodes) {
    if (node.id === centerId) {
      positions.set(node.id, { x: cx, y: cy });
      continue;
    }
    const h = hashString(node.id);
    const angle = ((h % 3600) / 3600) * Math.PI * 2;
    const radius = ringRadius * (0.7 + ((h >>> 12) % 1000) / 3000);
    positions.set(node.id, {
      x: cx + Math.cos(angle) * radius,
      y: cy + Math.sin(angle) * radius,
    });
  }

  // 去掉指向未知节点的边（防御：后端保证端点齐全，但布局不该因此崩）。
  const knownEdges = edges.filter(
    (e) => positions.has(e.src_id) && positions.has(e.dst_id),
  );
  const ids = nodes.map((n) => n.id);
  // 理想边长随节点数微增，避免大邻域挤成一团。
  const springLength = Math.min(width, height) / 4 + Math.min(nodes.length * 2, 60);
  const repulsion = springLength * springLength * 0.55;

  for (let iter = 0; iter < iterations; iter += 1) {
    const cooling = 1 - iter / iterations;
    const forces = new Map<string, { fx: number; fy: number }>();
    for (const id of ids) forces.set(id, { fx: 0, fy: 0 });

    for (let i = 0; i < ids.length; i += 1) {
      for (let j = i + 1; j < ids.length; j += 1) {
        const a = positions.get(ids[i])!;
        const b = positions.get(ids[j])!;
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let distSq = dx * dx + dy * dy;
        if (distSq < 1) {
          // 完全重叠时用 hash 决定一个确定性的展开方向。
          const h = hashString(ids[i] + ids[j]);
          dx = Math.cos(h % 360) || 0.1;
          dy = Math.sin(h % 360) || 0.1;
          distSq = 1;
        }
        const dist = Math.sqrt(distSq);
        const push = repulsion / distSq;
        const fa = forces.get(ids[i])!;
        const fb = forces.get(ids[j])!;
        fa.fx += (dx / dist) * push;
        fa.fy += (dy / dist) * push;
        fb.fx -= (dx / dist) * push;
        fb.fy -= (dy / dist) * push;
      }
    }

    for (const edge of knownEdges) {
      const a = positions.get(edge.src_id)!;
      const b = positions.get(edge.dst_id)!;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const pull = ((dist - springLength) / dist) * 0.08;
      const fa = forces.get(edge.src_id)!;
      const fb = forces.get(edge.dst_id)!;
      fa.fx += dx * pull;
      fa.fy += dy * pull;
      fb.fx -= dx * pull;
      fb.fy -= dy * pull;
    }

    for (const id of ids) {
      if (id === centerId) continue; // 中心钉死
      const p = positions.get(id)!;
      const f = forces.get(id)!;
      // 轻微向心力，防孤立节点漂出画布。
      f.fx += (cx - p.x) * 0.015;
      f.fy += (cy - p.y) * 0.015;
      const maxStep = 18 * cooling + 2;
      const stepLen = Math.sqrt(f.fx * f.fx + f.fy * f.fy);
      const scale = stepLen > maxStep ? maxStep / stepLen : 1;
      p.x += f.fx * scale;
      p.y += f.fy * scale;
    }
  }

  for (const id of ids) {
    const p = positions.get(id)!;
    p.x = Math.min(Math.max(p.x, padding), width - padding);
    p.y = Math.min(Math.max(p.y, padding), height - padding);
  }
  return positions;
}
