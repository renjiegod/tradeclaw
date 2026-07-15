import { describe, expect, it } from "vitest";

import type { CycleRunRow } from "../types";
import {
  buildAccountReviewPoints,
  formatTradeOperationsFromDetails,
  summarizeAccountMetrics,
} from "./cycleRunListFormat";

/** Minimal cycle-run row carrying a post_cycle_account snapshot; only the fields
 * the account helpers read are populated, the rest is irrelevant to the math. */
function snapshotRow(runId: string, cycleTime: string | null, equity: string): CycleRunRow {
  return {
    run_id: runId,
    cycle_time: cycleTime,
    details: {
      post_cycle_account: {
        source: "ledger",
        captured_at: cycleTime ?? "",
        account: { cash: "0", equity },
        total_market_value: "0",
        positions: [],
      },
    },
  } as unknown as CycleRunRow;
}

describe("formatTradeOperationsFromDetails", () => {
  it("formats buy/sell from fills", () => {
    const out = formatTradeOperationsFromDetails({
      fills: [
        { symbol: "600519", side: "buy", quantity: 100, price: 1785 },
        { symbol: "300750", side: "sell", quantity: 200, price: 117 },
      ],
    });

    expect(out.lines).toEqual([
      "买 600519 100股 ¥178,500.00",
      "卖 300750 200股 ¥23,400.00",
    ]);
  });

  it("falls back to position_intents when fills missing", () => {
    const out = formatTradeOperationsFromDetails({
      position_intents: [
        // buy: amount = notional, shares = amount / price_reference
        { symbol: "600519", action: "buy", amount: 178500, price_reference: 1785 },
        // sell: amount = shares, notional = amount * price_reference
        { symbol: "300750", action: "sell", amount: 200, price_reference: 117 },
      ],
    });

    expect(out.lines).toEqual([
      "买 600519 100股 ¥178,500.00",
      "卖 300750 200股 ¥23,400.00",
    ]);
  });

  it("prefers fills over position_intents when both present", () => {
    const out = formatTradeOperationsFromDetails({
      position_intents: [
        { symbol: "600519", action: "buy", amount: 999999, price_reference: 1785 },
      ],
      fills: [{ symbol: "600519", side: "buy", quantity: 100, price: 1785 }],
    });

    expect(out.lines).toEqual(["买 600519 100股 ¥178,500.00"]);
  });

  it("returns empty when neither fills nor intents are present", () => {
    expect(formatTradeOperationsFromDetails({}).lines).toEqual([]);
    expect(formatTradeOperationsFromDetails(null).lines).toEqual([]);
  });

  it("falls back to legacy decisions and decision_execution payloads", () => {
    const out = formatTradeOperationsFromDetails({
      decisions: [
        { symbol: "600519", action: "buy" },
        { symbol: "300750", action: "sell" },
      ],
      decision_execution: [
        { quantity_shares: 100, total_notional: "178500" },
        { quantity_shares: 200, total_notional: 23400 },
      ],
    });

    expect(out.lines).toEqual([
      "买 600519 100股 ¥178,500.00",
      "卖 300750 200股 ¥23,400.00",
    ]);
  });

  it("skips fill rows with invalid side/quantity/price", () => {
    const out = formatTradeOperationsFromDetails({
      fills: [
        { symbol: "X", side: "hold", quantity: 100, price: 10 },
        { symbol: "Y", side: "buy", quantity: 0, price: 10 },
        { symbol: "Z", side: "buy", quantity: 50, price: 12.34 },
      ],
    });
    expect(out.lines).toEqual(["买 Z 50股 ¥617.00"]);
  });
});

describe("buildAccountReviewPoints", () => {
  it("sorts ascending by cycle time and keeps equity from the snapshot", () => {
    const points = buildAccountReviewPoints([
      snapshotRow("run-b", "2026-01-03T00:00:00Z", "110000"),
      snapshotRow("run-a", "2026-01-01T00:00:00Z", "100000"),
      snapshotRow("run-c", "2026-01-02T00:00:00Z", "105000"),
    ]);
    expect(points.map((p) => p.runId)).toEqual(["run-a", "run-c", "run-b"]);
    expect(points.map((p) => p.equity)).toEqual([100000, 105000, 110000]);
  });

  it("breaks ties on equal cycle time by run_id for determinism", () => {
    const points = buildAccountReviewPoints([
      snapshotRow("run-z", "2026-01-01T00:00:00Z", "100000"),
      snapshotRow("run-a", "2026-01-01T00:00:00Z", "100000"),
    ]);
    expect(points.map((p) => p.runId)).toEqual(["run-a", "run-z"]);
  });

  it("drops rows with no snapshot, non-numeric equity, or no cycle time", () => {
    const noSnapshot = { run_id: "x", cycle_time: "2026-01-01T00:00:00Z", details: {} } as unknown as CycleRunRow;
    const points = buildAccountReviewPoints([
      noSnapshot,
      snapshotRow("nan", "2026-01-01T00:00:00Z", "not-a-number"),
      snapshotRow("notime", null, "100000"),
      snapshotRow("ok", "2026-01-02T00:00:00Z", "100000"),
    ]);
    expect(points.map((p) => p.runId)).toEqual(["ok"]);
  });
});

describe("summarizeAccountMetrics", () => {
  it("returns null when no row carries a usable snapshot", () => {
    expect(summarizeAccountMetrics([])).toBeNull();
    const noSnapshot = { run_id: "x", cycle_time: "2026-01-01T00:00:00Z", details: {} } as unknown as CycleRunRow;
    expect(summarizeAccountMetrics([noSnapshot])).toBeNull();
  });

  it("computes start/end/change/percent from first vs latest snapshot", () => {
    const summary = summarizeAccountMetrics([
      snapshotRow("run-a", "2026-01-01T00:00:00Z", "100000"),
      snapshotRow("run-b", "2026-01-03T00:00:00Z", "110000"),
      snapshotRow("run-c", "2026-01-02T00:00:00Z", "105000"),
    ]);
    expect(summary).not.toBeNull();
    expect(summary?.startEquity).toBe(100000);
    expect(summary?.endEquity).toBe(110000);
    expect(summary?.change).toBe(10000);
    expect(summary?.changePct).toBeCloseTo(10, 6);
    expect(summary?.pointCount).toBe(3);
  });

  it("yields a flat zero P&L for a single snapshot", () => {
    const summary = summarizeAccountMetrics([snapshotRow("run-a", "2026-01-01T00:00:00Z", "100000")]);
    expect(summary?.change).toBe(0);
    expect(summary?.changePct).toBe(0);
  });

  it("returns null percent (not Infinity) when the starting equity is zero", () => {
    const summary = summarizeAccountMetrics([
      snapshotRow("run-a", "2026-01-01T00:00:00Z", "0"),
      snapshotRow("run-b", "2026-01-02T00:00:00Z", "5000"),
    ]);
    expect(summary?.change).toBe(5000);
    expect(summary?.changePct).toBeNull();
  });
});
