import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { TaskReviewPanel } from "./TaskReviewPanel";
import type { CycleRunRow, PostCycleAccount } from "../types";

afterEach(() => {
  cleanup();
});

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

function postCycleAccount(overrides: Partial<PostCycleAccount> = {}): PostCycleAccount {
  return {
    source: "ledger",
    captured_at: "2026-01-05T07:00:00Z",
    account: { cash: "50000.00", equity: "100000.00" },
    total_market_value: "50000.00",
    positions: [],
    ...overrides,
  };
}

function cycleRunRow(overrides: Partial<CycleRunRow> = {}): CycleRunRow {
  return {
    run_id: "run-1",
    task_id: "task-1",
    agent_name: "agent",
    session_id: null,
    trace_id: null,
    run_mode: "paper",
    run_kind: "scheduled",
    clock_mode: "wall",
    cycle_time: "2026-01-05T07:00:00Z",
    cycle_time_utc: "2026-01-05T07:00:00Z",
    wall_started_at: "2026-01-05T07:00:00Z",
    wall_finished_at: "2026-01-05T07:01:00Z",
    runtime_params: null,
    status: "completed",
    details: { post_cycle_account: postCycleAccount() },
    cycle_failed: false,
    failure_message: null,
    completed_phases: null,
    submitted_count: null,
    vetoed_count: null,
    pending_approval_count: null,
    code_version: null,
    code_hash: null,
    ...overrides,
  };
}

describe("TaskReviewPanel", () => {
  it("renders the equity trend + period summary from cycle runs with post_cycle_account", () => {
    const rows: CycleRunRow[] = [
      // Intentionally out of order to exercise the ascending sort.
      cycleRunRow({
        run_id: "run-2",
        cycle_time: "2026-01-06T07:00:00Z",
        cycle_time_utc: "2026-01-06T07:00:00Z",
        details: {
          post_cycle_account: postCycleAccount({
            captured_at: "2026-01-06T07:00:00Z",
            account: { cash: "60000.00", equity: "110000.00" },
          }),
        },
      }),
      cycleRunRow({
        run_id: "run-1",
        cycle_time: "2026-01-05T07:00:00Z",
        cycle_time_utc: "2026-01-05T07:00:00Z",
        details: {
          post_cycle_account: postCycleAccount({
            account: { cash: "50000.00", equity: "100000.00" },
          }),
        },
      }),
    ];

    render(<TaskReviewPanel rows={rows} />);

    expect(screen.getByTestId("task-review-summary")).toBeTruthy();
    expect(screen.getByText("起始权益")).toBeTruthy();
    expect(screen.getByText("期末权益")).toBeTruthy();
    expect(screen.getByText("区间盈亏")).toBeTruthy();
    expect(screen.getByText("区间收益率")).toBeTruthy();
    expect(screen.getByText("覆盖周期数")).toBeTruthy();

    // Start = earliest cycle (100,000), end = latest cycle (110,000).
    expect(screen.getByText("100,000.00")).toBeTruthy();
    expect(screen.getByText("110,000.00")).toBeTruthy();
    // Absolute change +10,000 and % change +10.00%.
    expect(screen.getByText("+10,000.00")).toBeTruthy();
    expect(screen.getByText("+10.00%")).toBeTruthy();
    // Two qualifying cycle runs.
    expect(screen.getByText("2")).toBeTruthy();

    expect(screen.getByTestId("task-review-equity-chart")).toBeTruthy();
  });

  it("renders the empty state when no rows carry a usable post_cycle_account", () => {
    const rows: CycleRunRow[] = [
      // No post_cycle_account at all.
      cycleRunRow({ run_id: "run-a", details: { universe: ["600000.SH"] } }),
      // post_cycle_account present but equity is not parseable.
      cycleRunRow({
        run_id: "run-b",
        details: {
          post_cycle_account: postCycleAccount({ account: { cash: "0", equity: "not-a-number" } }),
        },
      }),
      // No details at all.
      cycleRunRow({ run_id: "run-c", details: null }),
    ];

    render(<TaskReviewPanel rows={rows} />);

    expect(screen.getByText("暂无复盘数据")).toBeTruthy();
    expect(screen.getByText(/还没有带账户快照的 cycle run/)).toBeTruthy();
    expect(screen.queryByTestId("task-review-equity-chart")).toBeNull();
    expect(screen.queryByTestId("task-review-summary")).toBeNull();
  });

  it("renders the latest positions from the most recent cycle run", () => {
    const rows: CycleRunRow[] = [
      cycleRunRow({
        run_id: "run-old",
        cycle_time: "2026-01-05T07:00:00Z",
        cycle_time_utc: "2026-01-05T07:00:00Z",
        details: {
          post_cycle_account: postCycleAccount({
            account: { cash: "50000.00", equity: "100000.00" },
            positions: [
              {
                symbol: "STALE.SH",
                name: "旧持仓",
                quantity: 100,
                available: 100,
                cost_price: "10.00",
                last_price: "10.00",
                market_value: "1000.00",
              },
            ],
          }),
        },
      }),
      cycleRunRow({
        run_id: "run-new",
        cycle_time: "2026-01-06T07:00:00Z",
        cycle_time_utc: "2026-01-06T07:00:00Z",
        details: {
          post_cycle_account: postCycleAccount({
            captured_at: "2026-01-06T07:00:00Z",
            account: { cash: "40000.00", equity: "110000.00" },
            positions: [
              {
                symbol: "600000.SH",
                name: "浦发银行",
                quantity: 200,
                available: 200,
                cost_price: "12.00",
                last_price: "13.00",
                market_value: "2600.00",
              },
              {
                symbol: "000001.SZ",
                name: "平安银行",
                quantity: 100,
                available: 100,
                cost_price: "11.00",
                last_price: "12.00",
                market_value: "1200.00",
              },
            ],
          }),
        },
      }),
    ];

    const { container } = render(<TaskReviewPanel rows={rows} />);

    expect(screen.getAllByText("最新持仓").length).toBeGreaterThan(0);
    // Latest run's positions are shown.
    expect(screen.getByText("600000.SH")).toBeTruthy();
    expect(screen.getByText("000001.SZ")).toBeTruthy();
    expect(screen.getByText("浦发银行")).toBeTruthy();
    // The stale (older cycle) position is NOT shown.
    expect(screen.queryByText("STALE.SH")).toBeNull();

    // Positions are sorted by market value descending: 600000.SH (2600) first.
    const symbolHeaderIndex = (() => {
      let idx = -1;
      container.querySelectorAll("th").forEach((cell, i) => {
        if (cell.textContent?.includes("代码")) idx = i;
      });
      return idx;
    })();
    expect(symbolHeaderIndex).toBeGreaterThanOrEqual(0);
    const firstRow = container.querySelectorAll("tbody tr")[0];
    const firstSymbolCell = firstRow?.querySelectorAll("td")[symbolHeaderIndex];
    expect(firstSymbolCell?.textContent?.includes("600000.SH")).toBe(true);
  });
});
