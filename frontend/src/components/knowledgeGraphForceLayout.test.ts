import { describe, expect, it } from "vitest";

import type { KgEdge, KgNode } from "../types";
import { layoutForce } from "./knowledgeGraphForceLayout";

function node(id: string, nodeType = "symbol"): KgNode {
  return { id, node_type: nodeType, name: id, display_name: null, attrs: null };
}

function edge(id: string, src: string, dst: string): KgEdge {
  return {
    id,
    src_id: src,
    dst_id: dst,
    relation: "has_role",
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

const NODES = ["center", "a", "b", "c", "d"].map((id) => node(id));
const EDGES = [
  edge("e1", "center", "a"),
  edge("e2", "center", "b"),
  edge("e3", "a", "c"),
  edge("e4", "b", "d"),
];
const OPTS = { width: 760, height: 520, centerId: "center" };

describe("layoutForce", () => {
  it("是确定性的：同一输入两次结果逐点一致（无随机数）", () => {
    const first = layoutForce(NODES, EDGES, OPTS);
    const second = layoutForce(NODES, EDGES, OPTS);
    expect(first.size).toBe(second.size);
    for (const [id, p] of first) {
      expect(second.get(id)).toEqual(p);
    }
  });

  it("中心节点钉死在画布中央", () => {
    const positions = layoutForce(NODES, EDGES, OPTS);
    expect(positions.get("center")).toEqual({ x: 380, y: 260 });
  });

  it("尊重用户拖拽的 seedPositions（原样保留）", () => {
    const seed = new Map([["a", { x: 123, y: 234 }]]);
    const positions = layoutForce(NODES, EDGES, { ...OPTS, seedPositions: seed });
    expect(positions.get("a")).toEqual({ x: 123, y: 234 });
  });

  it("所有坐标钳制在画布留白内", () => {
    const positions = layoutForce(NODES, EDGES, OPTS);
    for (const p of positions.values()) {
      expect(p.x).toBeGreaterThanOrEqual(48);
      expect(p.x).toBeLessThanOrEqual(760 - 48);
      expect(p.y).toBeGreaterThanOrEqual(48);
      expect(p.y).toBeLessThanOrEqual(520 - 48);
    }
  });

  it("空图返回空 Map", () => {
    expect(layoutForce([], [], OPTS).size).toBe(0);
  });
});
