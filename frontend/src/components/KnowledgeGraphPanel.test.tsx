import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  applyKnowledgeGraphChange,
  approveKnowledgeGraphChange,
  deprecateKnowledgeGraphSchemaItem,
  getKnowledgeGraph,
  getKnowledgeGraphChangeSets,
  getKnowledgeGraphConflicts,
  getKnowledgeGraphLayout,
  getKnowledgeGraphSchema,
  redoKnowledgeGraphRevision,
  rejectKnowledgeGraphChange,
  syncKnowledgeGraph,
  undoKnowledgeGraphRevision,
  upsertKnowledgeGraphSchemaItem,
} from "../api";
import type { KnowledgeGraphNeighborhood } from "../types";
import { KnowledgeGraphPanel } from "./KnowledgeGraphPanel";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    applyKnowledgeGraphChange: vi.fn(),
    approveKnowledgeGraphChange: vi.fn(),
    deprecateKnowledgeGraphSchemaItem: vi.fn(),
    getKnowledgeGraph: vi.fn(),
    getKnowledgeGraphChangeSets: vi.fn(),
    getKnowledgeGraphConflicts: vi.fn(),
    getKnowledgeGraphLayout: vi.fn(),
    getKnowledgeGraphSchema: vi.fn(),
    redoKnowledgeGraphRevision: vi.fn(),
    rejectKnowledgeGraphChange: vi.fn(),
    syncKnowledgeGraph: vi.fn(),
    undoKnowledgeGraphRevision: vi.fn(),
    upsertKnowledgeGraphSchemaItem: vi.fn(),
  };
});

const NEIGHBORHOOD: KnowledgeGraphNeighborhood = {
  revision: 0,
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
    {
      id: "kge-manual",
      src_id: "kgn-center",
      dst_id: "kgn-theme",
      relation: "belongs_to_theme",
      fact: "东方财富属于券商题材。",
      attrs: null,
      provenance: "manual",
      confidence: 1,
      source_ref: "manual:change-set/kgcs-1",
      valid_at: null,
      invalid_at: null,
      created_at: "2026-07-18T01:00:00",
      expired_at: null,
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
    vi.mocked(getKnowledgeGraphSchema).mockResolvedValue({
      namespace: "system",
      version: 1,
      revision: 0,
      entity_types: [
        { key: "symbol", label: "股票", parent_key: null, protected: true },
        { key: "theme", label: "题材", parent_key: null, protected: true },
      ],
      relation_types: [
        {
          key: "belongs_to_theme",
          label: "属于题材",
          source_type: "symbol",
          target_type: "theme",
          symmetric: false,
          transitive: false,
          inverse_key: null,
          protected: true,
        },
      ],
      property_definitions: [],
    });
    vi.mocked(getKnowledgeGraphChangeSets).mockResolvedValue({ items: [] });
    vi.mocked(getKnowledgeGraphConflicts).mockResolvedValue({ items: [] });
    vi.mocked(getKnowledgeGraphLayout).mockResolvedValue({ layout: null });
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
    expect(facts.length).toBe(4);
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

  it("creates a local manual relation with the loaded graph revision", async () => {
    vi.mocked(getKnowledgeGraph).mockResolvedValue(NEIGHBORHOOD);
    vi.mocked(applyKnowledgeGraphChange).mockResolvedValue({
      id: "kgcs-local",
      status: "applied",
      actor_type: "local_user",
      actor_id: "local-user",
      base_revision: 0,
      revision: 1,
      proposal_hash: "hash",
      summary: "手工标记",
      created_at: "2026-07-18T01:00:00",
      applied_at: "2026-07-18T01:00:00",
      edge_ids: ["kge-manual"],
    });
    render(<KnowledgeGraphPanel />);
    await queryEntity("300059");
    await screen.findByTestId("kg-svg");

    fireEvent.click(screen.getByTestId("kg-manual-edit"));
    expect(await screen.findByTestId("kg-manual-edit-modal")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("kg-manual-target-name"), {
      target: { value: "金融科技" },
    });
    fireEvent.change(screen.getByTestId("kg-manual-fact"), {
      target: { value: "东方财富属于金融科技题材。" },
    });
    fireEvent.click(screen.getByTestId("kg-manual-submit"));

    await waitFor(() => {
      expect(vi.mocked(applyKnowledgeGraphChange)).toHaveBeenCalledWith(
        [
          expect.objectContaining({
            op: "create_relation",
            relation: "belongs_to_theme",
            source: expect.objectContaining({ type: "symbol", name: "300059" }),
            target: expect.objectContaining({ type: "theme", name: "金融科技" }),
            fact: "东方财富属于金融科技题材。",
          }),
        ],
        "手工新增关系：东方财富 → 金融科技",
        0,
      );
    });
  });

  it("shows Agent drafts and applies a one-time approval", async () => {
    vi.mocked(getKnowledgeGraphChangeSets).mockResolvedValue({
      items: [
        {
          id: "kgcs-agent",
          status: "pending",
          actor_type: "agent",
          actor_id: "agent-1",
          base_revision: 0,
          revision: null,
          proposal_hash: "proposal-hash",
          summary: "Agent 建议补充题材",
          created_at: "2026-07-18T01:00:00",
          applied_at: null,
          edge_ids: [],
          operations: [
            {
              op: "create_relation",
              source: { type: "symbol", name: "300059" },
              relation: "belongs_to_theme",
              target: { type: "theme", name: "券商" },
              fact: "东方财富属于券商题材。",
            },
          ],
        },
      ],
    });
    vi.mocked(approveKnowledgeGraphChange).mockResolvedValue({
      id: "kgcs-agent",
      status: "applied",
      actor_type: "agent",
      actor_id: "agent-1",
      base_revision: 0,
      revision: 1,
      proposal_hash: "proposal-hash",
      summary: "Agent 建议补充题材",
      created_at: "2026-07-18T01:00:00",
      applied_at: "2026-07-18T01:01:00",
      edge_ids: ["kge-manual"],
    });
    render(<KnowledgeGraphPanel />);

    fireEvent.click(screen.getByTestId("kg-change-inbox"));
    expect(await screen.findByText("Agent 建议补充题材")).toBeInTheDocument();
    expect(screen.getByText("东方财富属于券商题材。")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("kg-approve-kgcs-agent"));

    await waitFor(() => {
      expect(approveKnowledgeGraphChange).toHaveBeenCalledWith(
        "kgcs-agent",
        "proposal-hash",
      );
    });
    expect(rejectKnowledgeGraphChange).not.toHaveBeenCalled();
  });

  it("revises and retracts an active manual relation", async () => {
    vi.mocked(getKnowledgeGraph).mockResolvedValue(NEIGHBORHOOD);
    vi.mocked(applyKnowledgeGraphChange).mockResolvedValue({
      id: "kgcs-edit",
      status: "applied",
      actor_type: "local_user",
      actor_id: "local-user",
      base_revision: 0,
      revision: 1,
      proposal_hash: "hash",
      summary: "修订关系",
      created_at: "2026-07-18T01:00:00",
      applied_at: "2026-07-18T01:00:00",
      edge_ids: ["kge-manual-v2"],
    });
    render(<KnowledgeGraphPanel />);
    await queryEntity("300059");
    await screen.findByTestId("kg-svg");

    fireEvent.click(screen.getByTestId("kg-revise-kge-manual"));
    fireEvent.change(await screen.findByTestId("kg-revise-fact-kge-manual"), {
      target: { value: "东方财富明确属于金融科技题材。" },
    });
    fireEvent.click(screen.getByTestId("kg-revise-submit-kge-manual"));
    await waitFor(() => {
      expect(applyKnowledgeGraphChange).toHaveBeenCalledWith(
        [
          {
            op: "revise_relation",
            edge_id: "kge-manual",
            fact: "东方财富明确属于金融科技题材。",
          },
        ],
        "修订人工关系：kge-manual",
        0,
      );
    });

    vi.mocked(applyKnowledgeGraphChange).mockClear();
    fireEvent.click(screen.getByTestId("kg-retract-kge-manual"));
    fireEvent.click(await screen.findByText("确认失效"));
    await waitFor(() => {
      expect(applyKnowledgeGraphChange).toHaveBeenCalledWith(
        [
          {
            op: "retract_relation",
            edge_id: "kge-manual",
            reason: "本地用户手动失效",
          },
        ],
        "失效人工关系：kge-manual",
        0,
      );
    });
  });

  it("undoes an applied manual revision from history", async () => {
    const revisioned = { ...NEIGHBORHOOD, revision: 2 };
    vi.mocked(getKnowledgeGraph).mockResolvedValue(revisioned);
    vi.mocked(getKnowledgeGraphChangeSets).mockResolvedValue({
      items: [
        {
          id: "kgcs-revision-2",
          status: "applied",
          actor_type: "local_user",
          actor_id: "local-user",
          base_revision: 1,
          revision: 2,
          proposal_hash: "hash",
          summary: "修订人工关系",
          created_at: "2026-07-18T01:00:00",
          applied_at: "2026-07-18T01:00:00",
          edge_ids: ["kge-manual"],
        },
      ],
    });
    vi.mocked(undoKnowledgeGraphRevision).mockResolvedValue({
      id: "kgcs-undo",
      status: "applied",
      actor_type: "local_user",
      actor_id: "local-user",
      base_revision: 2,
      revision: 3,
      proposal_hash: "undo-hash",
      summary: "撤销 revision 2",
      created_at: "2026-07-18T01:01:00",
      applied_at: "2026-07-18T01:01:00",
      edge_ids: ["kge-restored"],
    });
    render(<KnowledgeGraphPanel />);
    await queryEntity("300059");
    await screen.findByTestId("kg-svg");

    fireEvent.click(screen.getByTestId("kg-change-history"));
    expect(await screen.findByText(/revision 2 · 修订人工关系/)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("kg-undo-2"));

    await waitFor(() => {
      expect(undoKnowledgeGraphRevision).toHaveBeenCalledWith(2, 2);
    });
    expect(redoKnowledgeGraphRevision).not.toHaveBeenCalled();
  });

  it("creates a custom entity type from the Schema manager", async () => {
    vi.mocked(upsertKnowledgeGraphSchemaItem).mockResolvedValue({
      id: "kgcs-schema",
      status: "applied",
      actor_type: "local_user",
      actor_id: "local-user",
      base_revision: 0,
      revision: 1,
      proposal_hash: "schema-hash",
      summary: "新增自定义 Schema",
      created_at: "2026-07-18T01:00:00",
      applied_at: "2026-07-18T01:00:00",
      edge_ids: [],
    });
    render(<KnowledgeGraphPanel />);

    fireEvent.click(screen.getByTestId("kg-schema-manager"));
    expect(await screen.findByTestId("kg-schema-form")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("kg-schema-key"), {
      target: { value: "custom.indicator" },
    });
    fireEvent.change(screen.getByTestId("kg-schema-label"), {
      target: { value: "技术指标" },
    });
    fireEvent.click(screen.getByTestId("kg-schema-submit"));

    await waitFor(() => {
      expect(upsertKnowledgeGraphSchemaItem).toHaveBeenCalledWith(
        "entity_type",
        "custom.indicator",
        { label: "技术指标", parent_key: null },
        0,
        0,
      );
    });
    expect(deprecateKnowledgeGraphSchemaItem).not.toHaveBeenCalled();
  });
});
