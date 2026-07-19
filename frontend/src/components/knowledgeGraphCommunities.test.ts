import { describe, expect, it } from "vitest";

import type { KgEdge, KgNode } from "../types";
import {
  communityColor,
  communityCount,
  detectCommunities,
} from "./knowledgeGraphCommunities";

function node(id: string, nodeType = "symbol"): KgNode {
  return { id, node_type: nodeType, name: id, display_name: null, attrs: null };
}

function edge(id: string, src: string, dst: string): KgEdge {
  return {
    id,
    src_id: src,
    dst_id: dst,
    relation: "linked_with",
    fact: "x",
    attrs: null,
    provenance: "deterministic",
    confidence: null,
    source_ref: null,
    valid_at: null,
    invalid_at: null,
    created_at: null,
    expired_at: null,
  };
}

/** 两个三角团、互不相连 → 两个社区。 */
const NODES = ["a", "b", "c", "x", "y", "z"].map((id) => node(id));
const EDGES = [
  edge("e1", "a", "b"),
  edge("e2", "b", "c"),
  edge("e3", "c", "a"),
  edge("e4", "x", "y"),
  edge("e5", "y", "z"),
  edge("e6", "z", "x"),
];

describe("detectCommunities", () => {
  it("是确定性的：同一输入两次结果完全一致", () => {
    const first = detectCommunities(NODES, EDGES);
    const second = detectCommunities(NODES, EDGES);
    expect([...first.entries()].sort()).toEqual([...second.entries()].sort());
  });

  it("把相连的团聚成同一社区，不相连的团分开", () => {
    const communities = detectCommunities(NODES, EDGES);
    // 同一三角团内共享社区
    expect(communities.get("a")).toBe(communities.get("b"));
    expect(communities.get("b")).toBe(communities.get("c"));
    expect(communities.get("x")).toBe(communities.get("y"));
    expect(communities.get("y")).toBe(communities.get("z"));
    // 两团不同社区
    expect(communities.get("a")).not.toBe(communities.get("x"));
    expect(communityCount(communities)).toBe(2);
  });

  it("社区编号按社区内最小节点 id 稳定排序（a 团 = 0）", () => {
    const communities = detectCommunities(NODES, EDGES);
    expect(communities.get("a")).toBe(0);
    expect(communities.get("x")).toBe(1);
  });

  it("孤立节点自成一个社区", () => {
    const nodes = [node("a"), node("b"), node("lonely")];
    const edges = [edge("e1", "a", "b")];
    const communities = detectCommunities(nodes, edges);
    expect(communities.get("lonely")).not.toBe(communities.get("a"));
    expect(communityCount(communities)).toBe(2);
  });

  it("communityColor 稳定且超出板长时循环", () => {
    expect(communityColor(0)).toBe(communityColor(10));
    expect(communityColor(0)).not.toBe(communityColor(1));
  });
});
