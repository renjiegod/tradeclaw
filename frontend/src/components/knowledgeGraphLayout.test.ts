import { describe, expect, it } from "vitest";

import type { KgEdge, KgNode } from "../types";
import {
  computeHopDepths,
  hopRingGeometry,
  layoutNeighborhood,
} from "./knowledgeGraphLayout";

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

const OPTS = { width: 760, height: 520, centerId: "center" };

describe("layoutNeighborhood", () => {
  it("pins the center node at the canvas middle", () => {
    const positions = layoutNeighborhood(
      [node("center"), node("a"), node("b")],
      [edge("e1", "center", "a"), edge("e2", "center", "b")],
      OPTS,
    );
    expect(positions.get("center")).toEqual({ x: 380, y: 260 });
  });

  it("is deterministic — same input, same layout", () => {
    const nodes = [node("center"), node("a"), node("b"), node("c")];
    const edges = [
      edge("e1", "center", "a"),
      edge("e2", "center", "b"),
      edge("e3", "b", "c"),
    ];
    const first = layoutNeighborhood(nodes, edges, OPTS);
    const second = layoutNeighborhood(nodes, edges, OPTS);
    for (const [id, p] of first) {
      expect(second.get(id)).toEqual(p);
    }
  });

  it("keeps every node inside the padded canvas and separates them", () => {
    const nodes = [node("center"), ...Array.from({ length: 24 }, (_, i) => node(`n${i}`))];
    const edges = nodes.slice(1).map((n, i) => edge(`e${i}`, "center", n.id));
    const positions = layoutNeighborhood(nodes, edges, { ...OPTS, padding: 48 });
    expect(positions.size).toBe(25);
    for (const p of positions.values()) {
      expect(p.x).toBeGreaterThanOrEqual(48);
      expect(p.x).toBeLessThanOrEqual(760 - 48);
      expect(p.y).toBeGreaterThanOrEqual(48);
      expect(p.y).toBeLessThanOrEqual(520 - 48);
    }
    // 相邻节点不该完全重叠（斥力生效）。
    const values = [...positions.values()];
    for (let i = 0; i < values.length; i += 1) {
      for (let j = i + 1; j < values.length; j += 1) {
        const dx = values[i].x - values[j].x;
        const dy = values[i].y - values[j].y;
        expect(Math.sqrt(dx * dx + dy * dy)).toBeGreaterThan(4);
      }
    }
  });

  it("tolerates edges pointing at unknown nodes and empty input", () => {
    expect(layoutNeighborhood([], [], OPTS).size).toBe(0);
    const positions = layoutNeighborhood(
      [node("center"), node("a")],
      [edge("e1", "center", "ghost")],
      OPTS,
    );
    expect(positions.size).toBe(2);
  });

  it("places deeper hops on larger rings（同心环 = 跳数）", () => {
    // center → a → b → c 的三跳链：到中心的距离应随跳数单调增大。
    const nodes = [node("center"), node("a"), node("b"), node("c")];
    const edges = [
      edge("e1", "center", "a"),
      edge("e2", "a", "b"),
      edge("e3", "b", "c"),
    ];
    const positions = layoutNeighborhood(nodes, edges, OPTS);
    const center = positions.get("center")!;
    const dist = (id: string) => {
      const p = positions.get(id)!;
      return Math.hypot(p.x - center.x, p.y - center.y);
    };
    expect(dist("a")).toBeLessThan(dist("b"));
    expect(dist("b")).toBeLessThan(dist("c"));
  });

  it("keeps seeded positions untouched（用户拖拽 / 已保存布局）", () => {
    const nodes = [node("center"), node("a"), node("b")];
    const edges = [edge("e1", "center", "a"), edge("e2", "center", "b")];
    const positions = layoutNeighborhood(nodes, edges, {
      ...OPTS,
      seedPositions: { a: { x: 100, y: 90 } },
    });
    expect(positions.get("a")).toEqual({ x: 100, y: 90 });
  });
});

describe("computeHopDepths", () => {
  it("returns BFS depth per node, orphans pushed past the last ring", () => {
    const nodes = [node("center"), node("a"), node("b"), node("lonely")];
    const edges = [edge("e1", "center", "a"), edge("e2", "a", "b")];
    const depths = computeHopDepths(nodes, edges, "center");
    expect(depths.get("center")).toBe(0);
    expect(depths.get("a")).toBe(1);
    expect(depths.get("b")).toBe(2);
    expect(depths.get("lonely")).toBe(3);
  });
});

describe("hopRingGeometry", () => {
  it("splits the padded canvas into evenly spaced rings", () => {
    const rings = hopRingGeometry(2, { width: 760, height: 520, padding: 48 });
    expect(rings).toHaveLength(2);
    expect(rings[0].rx).toBeCloseTo((760 / 2 - 48) / 2);
    expect(rings[0].ry).toBeCloseTo((520 / 2 - 48) / 2);
    expect(rings[1].rx).toBeCloseTo(760 / 2 - 48);
    expect(rings[1].ry).toBeCloseTo(520 / 2 - 48);
  });
});
