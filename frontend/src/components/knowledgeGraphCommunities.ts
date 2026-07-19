/**
 * 知识图谱的确定性社区发现（label propagation）——无第三方图库、无随机数。
 *
 * 与放射布局同一套「确定性」纪律：初始 label = 节点自身 id；每轮按 id
 * 字典序遍历，节点采用**邻居中出现最多**的 label（平票时取字典序最小的
 * label 做 tie-break）；异步更新（当轮内立即生效，收敛更快）；无变化或
 * 到轮数上限即停。最后把社区重编号为稳定的 0..k-1（按社区内最小节点 id
 * 排序），因此**同样的输入永远得到同样的社区划分与编号**——组件重渲染
 * 不抖动、vitest 可复现。
 *
 * 用途：知识图谱面板「着色：社区」模式下，把同一题材簇的节点着同色，
 * 让"主线抱团"一眼可见（对照默认的「着色：类型」）。
 */

import type { KgEdge, KgNode } from "../types";

/** 迭代轮数上限——邻域子图很小（≤ 数百节点），远早于此收敛。 */
const MAX_ROUNDS = 30;

/**
 * dataviz 安全的分类色板（light surface #fffdf9）：足量不同色相，社区数
 * 超过板长时按 index 取模循环。身份始终有二级编码（图例「簇 N」+ 位置），
 * 不只靠颜色。
 */
export const COMMUNITY_PALETTE: readonly string[] = [
  "#b26a1f",
  "#3b6fd4",
  "#2f8f6b",
  "#b8508f",
  "#7b5fc0",
  "#6b7f2e",
  "#c0504d",
  "#1f8f8f",
  "#9b6a34",
  "#4a7c9b",
];

/** 社区编号 → 稳定颜色（超板长循环）。 */
export function communityColor(community: number): string {
  const size = COMMUNITY_PALETTE.length;
  return COMMUNITY_PALETTE[((community % size) + size) % size];
}

/**
 * 确定性 label-propagation 社区发现。
 *
 * 返回 ``Map<nodeId, community>``，``community`` 为稳定的 0..k-1 编号
 * （按社区内最小节点 id 排序）。孤立节点自成一社区。忽略指向未知节点
 * 的边（防御：布局层不该因端点缺失崩）。
 */
export function detectCommunities(
  nodes: KgNode[],
  edges: KgEdge[],
): Map<string, number> {
  const ids = nodes.map((n) => n.id);
  const known = new Set(ids);
  const adjacency = new Map<string, string[]>();
  for (const id of known) adjacency.set(id, []);
  for (const edge of edges) {
    if (!known.has(edge.src_id) || !known.has(edge.dst_id)) continue;
    if (edge.src_id === edge.dst_id) continue;
    adjacency.get(edge.src_id)!.push(edge.dst_id);
    adjacency.get(edge.dst_id)!.push(edge.src_id);
  }

  // 稳定遍历顺序（字典序），保证异步更新的确定性。
  const order = [...ids].sort();
  const labels = new Map<string, string>();
  for (const id of order) labels.set(id, id);

  for (let round = 0; round < MAX_ROUNDS; round += 1) {
    let changed = false;
    for (const id of order) {
      const neighbors = adjacency.get(id) ?? [];
      if (neighbors.length === 0) continue;
      // 邻居 label 计数 + 确定性 tie-break（先按计数降序，再按 label 升序）。
      const counts = new Map<string, number>();
      for (const neighbor of neighbors) {
        const label = labels.get(neighbor)!;
        counts.set(label, (counts.get(label) ?? 0) + 1);
      }
      let best = labels.get(id)!;
      let bestCount = counts.get(best) ?? 0;
      for (const [label, count] of counts) {
        if (count > bestCount || (count === bestCount && label < best)) {
          best = label;
          bestCount = count;
        }
      }
      if (best !== labels.get(id)) {
        labels.set(id, best);
        changed = true;
      }
    }
    if (!changed) break;
  }

  // 把内部 label 重映射为稳定的 0..k-1（按社区内最小节点 id 排序）。
  const membersByLabel = new Map<string, string[]>();
  for (const id of order) {
    const label = labels.get(id)!;
    const list = membersByLabel.get(label) ?? [];
    list.push(id);
    membersByLabel.set(label, list);
  }
  const communityOrder = [...membersByLabel.entries()]
    .map(([label, members]) => ({
      label,
      min: members.reduce((a, b) => (a < b ? a : b)),
    }))
    .sort((a, b) => (a.min < b.min ? -1 : a.min > b.min ? 1 : 0));

  const communityIndex = new Map<string, number>();
  communityOrder.forEach((entry, index) => {
    communityIndex.set(entry.label, index);
  });

  const result = new Map<string, number>();
  for (const id of ids) {
    result.set(id, communityIndex.get(labels.get(id)!) ?? 0);
  }
  return result;
}

/** 社区总数（编号 0..k-1 的 k）。 */
export function communityCount(communities: Map<string, number>): number {
  let max = -1;
  for (const value of communities.values()) max = Math.max(max, value);
  return max + 1;
}
