import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { KnowledgeGraphSvg } from "./KnowledgeGraphSvg";
import type { KgEdge, KgNode } from "../types";

afterEach(cleanup);

function node(id: string, name: string, node_type = "symbol"): KgNode {
  return { id, node_type, name, display_name: null, attrs: null };
}

function edge(id: string, src: string, dst: string, relation = "linked_with"): KgEdge {
  return {
    id,
    src_id: src,
    dst_id: dst,
    relation,
    fact: "",
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

describe("KnowledgeGraphSvg", () => {
  const nodes = [node("n1", "茅台", "symbol"), node("n2", "白酒", "theme"), node("n3", "机构", "role")];
  const edges = [edge("e1", "n1", "n2"), edge("e2", "n1", "n3")];

  it("renders one circle + label per node and one path per edge", () => {
    render(<KnowledgeGraphSvg nodes={nodes} edges={edges} centerId="n1" />);
    const svg = screen.getByTestId("knowledge-graph-svg");
    expect(svg.querySelectorAll("[data-testid='kg-nodes'] circle")).toHaveLength(3);
    expect(svg.querySelectorAll("[data-testid='kg-edges'] path")).toHaveLength(2);
    expect(screen.getByText("茅台")).toBeInTheDocument();
  });

  it("shows a type legend in type color mode", () => {
    render(<KnowledgeGraphSvg nodes={nodes} edges={edges} centerId="n1" colorMode="type" />);
    const legend = screen.getByTestId("kg-legend");
    // symbol / theme / role → 个股 / 题材 / 角色
    expect(legend).toHaveTextContent("个股");
    expect(legend).toHaveTextContent("题材");
  });

  it("hides the type legend in community color mode", () => {
    render(<KnowledgeGraphSvg nodes={nodes} edges={edges} centerId="n1" colorMode="community" />);
    expect(screen.queryByTestId("kg-legend")).not.toBeInTheDocument();
  });

  it("drops edges whose endpoints are missing", () => {
    render(<KnowledgeGraphSvg nodes={nodes} edges={[...edges, edge("e3", "n1", "ghost")]} centerId="n1" />);
    const svg = screen.getByTestId("knowledge-graph-svg");
    expect(svg.querySelectorAll("[data-testid='kg-edges'] path")).toHaveLength(2);
  });

  it("renders an empty-state when there are no nodes", () => {
    render(<KnowledgeGraphSvg nodes={[]} edges={[]} />);
    expect(screen.queryByTestId("knowledge-graph-svg")).not.toBeInTheDocument();
    expect(screen.getByText("暂无图谱数据")).toBeInTheDocument();
  });

  it("is deterministic — identical input yields identical node positions", () => {
    const { container: c1 } = render(<KnowledgeGraphSvg nodes={nodes} edges={edges} centerId="n1" />);
    const first = c1.querySelector("[data-testid='kg-nodes']")?.innerHTML;
    cleanup();
    const { container: c2 } = render(<KnowledgeGraphSvg nodes={nodes} edges={edges} centerId="n1" />);
    const second = c2.querySelector("[data-testid='kg-nodes']")?.innerHTML;
    expect(first).toBe(second);
  });
});
