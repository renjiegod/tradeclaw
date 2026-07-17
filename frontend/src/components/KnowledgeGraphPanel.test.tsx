import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, getKnowledgeGraph, syncKnowledgeGraph } from "../api";
import type { KnowledgeGraphNeighborhood } from "../types";
import { KnowledgeGraphPanel } from "./KnowledgeGraphPanel";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getKnowledgeGraph: vi.fn(),
    syncKnowledgeGraph: vi.fn(),
  };
});

const NEIGHBORHOOD: KnowledgeGraphNeighborhood = {
  center: {
    id: "kgn-center",
    node_type: "symbol",
    name: "300059",
    display_name: "东方财富",
    attrs: null,
  },
  candidates: [],
  nodes: [
    {
      id: "kgn-center",
      node_type: "symbol",
      name: "300059",
      display_name: "东方财富",
      attrs: null,
    },
    { id: "kgn-role", node_type: "role", name: "龙头", display_name: null, attrs: null },
    {
      id: "kgn-theme",
      node_type: "theme",
      name: "券商",
      display_name: null,
      attrs: null,
    },
  ],
  edges: [
    {
      id: "kge-1",
      src_id: "kgn-center",
      dst_id: "kgn-role",
      relation: "has_role",
      fact: "东方财富（300059）当前角色：龙头",
      attrs: null,
      provenance: "deterministic",
      confidence: null,
      source_ref: "kb:symbols/roles.jsonl",
      valid_at: "2026-03-10T09:00:00",
      invalid_at: null,
      created_at: "2026-07-17T10:00:00",
      expired_at: null,
    },
    {
      id: "kge-2",
      src_id: "kgn-center",
      dst_id: "kgn-theme",
      relation: "leads_theme",
      fact: "东方财富是本轮券商行情的龙头",
      attrs: null,
      provenance: "llm",
      confidence: 0.9,
      source_ref: "kb:journal/2026/2026-07-17.md",
      valid_at: "2026-07-17T00:00:00",
      invalid_at: null,
      created_at: "2026-07-17T16:00:00",
      expired_at: null,
    },
    {
      id: "kge-3",
      src_id: "kgn-center",
      dst_id: "kgn-role",
      relation: "has_role",
      fact: "东方财富（300059）当前角色：杂毛",
      attrs: null,
      provenance: "deterministic",
      confidence: null,
      source_ref: "kb:symbols/roles.jsonl",
      valid_at: "2026-02-01T09:00:00",
      invalid_at: null,
      created_at: "2026-06-01T10:00:00",
      expired_at: "2026-07-17T10:00:00",
    },
  ],
};

async function queryEntity(value: string) {
  const holder = screen.getByTestId("kg-entity-input");
  const input = (
    holder.tagName === "INPUT" ? holder : holder.querySelector("input")
  ) as HTMLInputElement;
  fireEvent.change(input, { target: { value } });
  fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
}

describe("KnowledgeGraphPanel", () => {
  beforeAll(() => {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(cleanup);

  it("renders the guide empty state before any query", () => {
    render(<KnowledgeGraphPanel />);
    expect(screen.getByTestId("kg-empty")).toBeInTheDocument();
    expect(vi.mocked(getKnowledgeGraph)).not.toHaveBeenCalled();
  });

  it("queries an entity and renders nodes, facts, legend and provenance tags", async () => {
    vi.mocked(getKnowledgeGraph).mockResolvedValue(NEIGHBORHOOD);
    render(<KnowledgeGraphPanel />);
    await queryEntity("东方财富");

    expect(await screen.findByTestId("kg-svg")).toBeInTheDocument();
    expect(vi.mocked(getKnowledgeGraph)).toHaveBeenCalledWith("东方财富", {
      hops: 1,
      includeExpired: false,
    });
    expect(screen.getByTestId("kg-node-kgn-center")).toBeInTheDocument();
    expect(screen.getByTestId("kg-node-kgn-role")).toBeInTheDocument();
    // 虚线 = LLM 观点边
    expect(screen.getByTestId("kg-edge-kge-2")).toHaveAttribute(
      "stroke-dasharray",
      "6 4",
    );
    // 事实列表按关系分组，带 provenance / confidence 标签
    const facts = screen.getAllByTestId("kg-fact-item");
    expect(facts.length).toBe(3);
    expect(screen.getByText("东方财富是本轮券商行情的龙头")).toBeInTheDocument();
    expect(screen.getByText("conf 0.90")).toBeInTheDocument();
    expect(screen.getAllByText("LLM 观点").length).toBeGreaterThanOrEqual(1);
    // 图例包含出现过的节点类型
    expect(screen.getByTestId("kg-legend")).toHaveTextContent("个股");
    expect(screen.getByTestId("kg-legend")).toHaveTextContent("角色");
  });

  it("shows the sync hint when the entity is not found (404)", async () => {
    vi.mocked(getKnowledgeGraph).mockRejectedValue(
      new ApiError("no graph node matches", 404),
    );
    render(<KnowledgeGraphPanel />);
    await queryEntity("陌生实体");

    const hint = await screen.findByTestId("kg-not-found");
    expect(hint).toHaveTextContent("陌生实体");
    expect(hint).toHaveTextContent("同步投影");
  });

  it("runs sync from the header button and reports the result", async () => {
    vi.mocked(syncKnowledgeGraph).mockResolvedValue({
      skipped: false,
      changed_sources: ["kb:symbols/roles.jsonl"],
      apply: {
        nodes_created: 2,
        nodes_updated: 0,
        edges_created: 3,
        edges_unchanged: 0,
        edges_expired: 1,
      },
      counts: { nodes: 10, active_edges: 12, expired_edges: 1 },
    });
    render(<KnowledgeGraphPanel />);
    fireEvent.click(screen.getByTestId("kg-sync"));

    expect(vi.mocked(syncKnowledgeGraph)).toHaveBeenCalledTimes(1);
    // 没有查询上下文时同步不应触发查询
    expect(vi.mocked(getKnowledgeGraph)).not.toHaveBeenCalled();
  });

  it("re-queries after sync when a not-found entity is pending", async () => {
    vi.mocked(getKnowledgeGraph)
      .mockRejectedValueOnce(new ApiError("no graph node matches", 404))
      .mockResolvedValueOnce(NEIGHBORHOOD);
    vi.mocked(syncKnowledgeGraph).mockResolvedValue({
      skipped: false,
      changed_sources: ["kb:symbols/roles.jsonl"],
      apply: {
        nodes_created: 1,
        nodes_updated: 0,
        edges_created: 1,
        edges_unchanged: 0,
        edges_expired: 0,
      },
      counts: { nodes: 3, active_edges: 2, expired_edges: 0 },
    });
    render(<KnowledgeGraphPanel />);
    await queryEntity("300059");
    const hint = await screen.findByTestId("kg-not-found");
    expect(hint).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("kg-sync"));
    expect(await screen.findByTestId("kg-svg")).toBeInTheDocument();
    expect(vi.mocked(getKnowledgeGraph)).toHaveBeenCalledTimes(2);
  });

  it("expired edges render struck-through in the fact list", async () => {
    vi.mocked(getKnowledgeGraph).mockResolvedValue(NEIGHBORHOOD);
    render(<KnowledgeGraphPanel />);
    await queryEntity("300059");
    await screen.findByTestId("kg-svg");

    expect(screen.getByText(/已失效 2026-07-17/)).toBeInTheDocument();
  });
});
