import { describe, expect, it } from "vitest";

import { parsePanelSpec, type ChartBlock, type KGraphBlock, type KlineBlock } from "../panelSpec";

describe("parsePanelSpec", () => {
  it("returns null for non-object / missing blocks", () => {
    expect(parsePanelSpec(null)).toBeNull();
    expect(parsePanelSpec(42)).toBeNull();
    expect(parsePanelSpec({})).toBeNull();
    expect(parsePanelSpec({ blocks: [] })).toBeNull();
    expect(parsePanelSpec({ blocks: "nope" })).toBeNull();
  });

  it("parses a JSON string defensively", () => {
    const spec = parsePanelSpec(JSON.stringify({ blocks: [{ type: "markdown", content: "hi" }] }));
    expect(spec?.blocks).toHaveLength(1);
    expect(spec?.blocks[0].type).toBe("markdown");
  });

  it("drops invalid blocks but keeps valid ones, assigning stable ids", () => {
    const spec = parsePanelSpec({
      blocks: [
        { type: "markdown", content: "keep" },
        { type: "heatmap" }, // unknown → dropped
        { type: "kline" }, // missing symbol → dropped
        { type: "markdown", content: "   " }, // blank → dropped
      ],
    });
    expect(spec?.blocks).toHaveLength(1);
    expect(spec?.blocks[0].id).toBe("b0");
  });

  it("returns null when every block is invalid", () => {
    expect(parsePanelSpec({ blocks: [{ type: "kline", symbol: "茅台" }] })).toBeNull();
  });

  it("normalizes a kline block with defaults + validates canonical symbol", () => {
    const spec = parsePanelSpec({ blocks: [{ type: "kline", symbol: "600519.SH" }] });
    const block = spec!.blocks[0] as KlineBlock;
    expect(block.type).toBe("kline");
    expect(block.interval).toBe("1d");
    expect(block.adjust).toBe("qfq");
    expect(block.provider).toBe("auto");
    expect(block.main_indicator).toBe("MA");
    expect(block.sub_indicator).toBe("MACD");
    expect(block.overlays).toEqual([]);
  });

  it("rejects a kline block with a non-canonical symbol", () => {
    expect(parsePanelSpec({ blocks: [{ type: "kline", symbol: "600519" }] })).toBeNull();
    expect(parsePanelSpec({ blocks: [{ type: "kline", symbol: "贵州茅台" }] })).toBeNull();
  });

  it("filters kline overlays to the known kinds", () => {
    const spec = parsePanelSpec({
      blocks: [{ type: "kline", symbol: "600519.SH", overlays: ["signals", "bogus", "task_fills"] }],
    });
    expect((spec!.blocks[0] as KlineBlock).overlays).toEqual(["signals", "task_fills"]);
  });

  it("requires x_field + y_fields for line/bar/area charts", () => {
    expect(
      parsePanelSpec({ blocks: [{ type: "chart", chart_type: "line", data: [{ a: 1 }] }] }),
    ).toBeNull();
    const ok = parsePanelSpec({
      blocks: [{ type: "chart", chart_type: "bar", data: [{ a: 1 }], x_field: "a", y_fields: ["a"] }],
    });
    expect((ok!.blocks[0] as ChartBlock).chart_type).toBe("bar");
  });

  it("requires category_field + value_field for pie charts", () => {
    expect(
      parsePanelSpec({ blocks: [{ type: "chart", chart_type: "pie", data: [{ a: 1 }], x_field: "a" }] }),
    ).toBeNull();
    const ok = parsePanelSpec({
      blocks: [
        { type: "chart", chart_type: "pie", data: [{ k: "白酒", v: 1 }], category_field: "k", value_field: "v" },
      ],
    });
    expect(ok!.blocks[0].type).toBe("chart");
  });

  it("accepts a kgraph reference block and clamps hops to 1..3", () => {
    const spec = parsePanelSpec({ blocks: [{ type: "kgraph", entity: "贵州茅台", hops: 9 }] });
    const block = spec!.blocks[0] as KGraphBlock;
    expect(block.entity).toBe("贵州茅台");
    expect(block.hops).toBe(3);
    expect(block.layout).toBe("radial");
    expect(block.color_mode).toBe("type");
  });

  it("accepts an inline kgraph block and drops dangling edges' missing endpoints defensively", () => {
    const spec = parsePanelSpec({
      blocks: [
        {
          type: "kgraph",
          nodes: [{ id: "n1", name: "A" }, { id: "n2", name: "B" }, { bad: true }],
          edges: [
            { id: "e1", src_id: "n1", dst_id: "n2" },
            { src_id: "n1" }, // missing dst → dropped
          ],
        },
      ],
    });
    const block = spec!.blocks[0] as KGraphBlock;
    expect(block.nodes).toHaveLength(2);
    expect(block.edges).toHaveLength(1);
    expect(block.edges![0].relation).toBe("related");
  });

  it("rejects a kgraph block with neither entity nor inline nodes", () => {
    expect(parsePanelSpec({ blocks: [{ type: "kgraph" }] })).toBeNull();
  });

  it("keeps table columns that have both title and data_index", () => {
    const spec = parsePanelSpec({
      blocks: [
        {
          type: "table",
          columns: [{ title: "代码", data_index: "code" }, { title: "缺列" }],
          rows: [{ code: "600519.SH" }],
        },
      ],
    });
    const block = spec!.blocks[0];
    expect(block.type).toBe("table");
    if (block.type === "table") expect(block.columns).toHaveLength(1);
  });

  it("assigns unique index-based block ids even when the model reuses one id", () => {
    const spec = parsePanelSpec({
      blocks: [
        { type: "markdown", content: "a", id: "dup" },
        { type: "markdown", content: "b", id: "dup" },
        { type: "markdown", content: "c" },
      ],
    });
    const ids = spec!.blocks.map((block) => block.id);
    expect(ids).toEqual(["b0", "b1", "b2"]);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("caps blocks at 12", () => {
    const blocks = Array.from({ length: 20 }, (_, i) => ({ type: "markdown", content: `b${i}` }));
    const spec = parsePanelSpec({ blocks });
    expect(spec!.blocks).toHaveLength(12);
  });
});
