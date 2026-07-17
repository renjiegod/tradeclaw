import { describe, expect, it } from "vitest";

import type { KgEdge, KgNode } from "../types";
import { layoutNeighborhood } from "./knowledgeGraphLayout";

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
});
