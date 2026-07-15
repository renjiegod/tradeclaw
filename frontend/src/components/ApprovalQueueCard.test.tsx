import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { ApprovalQueueCard } from "./ApprovalQueueCard";
import { approve, reject } from "../api";
import type { PendingApproval } from "../types";

vi.mock("../api", () => ({
  approve: vi.fn().mockResolvedValue({ status: "ok" }),
  reject: vi.fn().mockResolvedValue({ status: "ok" }),
}));

const navigateMock = vi.fn();

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return {
    ...actual,
    useNavigate: () => navigateMock,
  };
});

const approveMock = vi.mocked(approve);
const rejectMock = vi.mocked(reject);

// antd inserts a space between two adjacent CJK characters in a <Button>
// (e.g. "同意" renders as "同 意"), which breaks exact text matching. Match on
// the button whose collapsed text content equals the label.
function findButton(label: string): HTMLElement {
  const target = label.replace(/\s+/g, "");
  const button = screen
    .getAllByRole("button")
    .find((el) => (el.textContent ?? "").replace(/\s+/g, "") === target);
  if (!button) {
    throw new Error(`button "${label}" not found`);
  }
  return button;
}

const enriched: PendingApproval = {
  approval_id: "appr-1",
  intent_id: "intent-1",
  created_at: "2026-06-14T01:00:00Z",
  expires_at: "2026-06-14T02:00:00Z",
  status: "pending",
  mode: "live",
  task_id: "task-99",
  run_id: "run-7",
  account_id: "acct-1",
  symbol: "600519.SH",
  action: "buy",
  notional: "10000.00",
  decision_source: "web",
  resolver_id: null,
  dispatched_at: null,
  decided_at: null,
};

function renderCard(items: PendingApproval[]) {
  return render(
    <MemoryRouter>
      <ApprovalQueueCard items={items} loading={false} onMutated={() => {}} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ApprovalQueueCard", () => {
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

  it("renders enriched fields: symbol, buy tag, formatted notional, mode", () => {
    renderCard([enriched]);
    expect(screen.getByText("600519.SH")).toBeTruthy();
    expect(screen.getByText("买入")).toBeTruthy();
    // Thousands separator applied for display only.
    expect(screen.getByText("10,000.00")).toBeTruthy();
    expect(screen.getByText("live")).toBeTruthy();
    expect(screen.getByText("待处理")).toBeTruthy();
  });

  it("renders the rich 信号 section (现价/限价/订单/方向/策略/理由) when present", () => {
    renderCard([
      {
        ...enriched,
        approval_id: "appr-signal",
        strategy_tag: "grid_target_exposure",
        signal_tag: "grid_buy_1",
        rationale: "网格下轨触发买入",
        price_reference: "7.80",
        order_type: "limit",
        tif: "day",
        last_price: "7.78",
        pct_change: "+1.2%",
        direction: "buy",
      },
    ]);
    expect(screen.getByText("信号")).toBeTruthy();
    expect(screen.getByText("grid_target_exposure")).toBeTruthy();
    expect(screen.getByText(/grid_buy_1/)).toBeTruthy(); // rendered as [grid_buy_1] in 方向
    expect(screen.getByText(/网格下轨触发买入/)).toBeTruthy();
    // Rich, signal-digest-parity fields.
    expect(screen.getByText(/7\.78 \(\+1\.2%\)/)).toBeTruthy(); // 现价 + 涨跌幅
    expect(screen.getByText("7.80")).toBeTruthy(); // 限价
    expect(screen.getByText(/limit · day/)).toBeTruthy(); // 订单
  });

  it("names the stock (display name + symbol) when symbol_name is present", () => {
    renderCard([{ ...enriched, approval_id: "appr-name", symbol: "601398.SH", symbol_name: "工商银行" }]);
    expect(screen.getByText(/工商银行/)).toBeTruthy();
    expect(screen.getByText("601398.SH")).toBeTruthy();
  });

  it("renders sell action with the 卖出 tag", () => {
    renderCard([{ ...enriched, approval_id: "appr-sell", action: "sell" }]);
    expect(screen.getByText("卖出")).toBeTruthy();
  });

  it("navigates to task detail when 查看任务 is clicked", () => {
    renderCard([enriched]);
    fireEvent.click(screen.getByText("查看任务"));
    expect(navigateMock).toHaveBeenCalledWith("/tasks/task-99");
  });

  it("hides 查看任务 when there is no task_id", () => {
    renderCard([{ ...enriched, approval_id: "appr-no-task", task_id: null }]);
    expect(screen.queryByText("查看任务")).toBeNull();
  });

  it("invokes approve/reject and onMutated callback", async () => {
    const onMutated = vi.fn();
    render(
      <MemoryRouter>
        <ApprovalQueueCard items={[enriched]} loading={false} onMutated={onMutated} />
      </MemoryRouter>,
    );
    fireEvent.click(findButton("同意"));
    await waitFor(() => expect(approveMock).toHaveBeenCalledWith("appr-1"));
    await waitFor(() => expect(onMutated).toHaveBeenCalled());

    fireEvent.click(findButton("拒绝"));
    await waitFor(() => expect(rejectMock).toHaveBeenCalledWith("appr-1"));
  });

  it("tolerates legacy rows with null enrichment fields", () => {
    const legacy: PendingApproval = {
      approval_id: "appr-legacy",
      intent_id: "intent-legacy",
      created_at: null,
      expires_at: null,
    };
    renderCard([legacy]);
    expect(screen.getByText("意图: intent-legacy")).toBeTruthy();
    // No symbol / action / notional rendered, but the row is still actionable.
    expect(findButton("同意")).toBeTruthy();
    expect(screen.queryByText("查看任务")).toBeNull();
  });

  it("hides decision buttons for already-resolved approvals", () => {
    renderCard([{ ...enriched, approval_id: "appr-done", status: "approved" }]);
    expect(screen.getByText("已同意")).toBeTruthy();
    expect(screen.queryByText("同意")).toBeNull();
    expect(screen.queryByText("拒绝")).toBeNull();
  });

  it("shows the empty state when there are no items", () => {
    renderCard([]);
    expect(screen.getByText("暂无待审批请求")).toBeTruthy();
  });

  // Regression: the API returns naive UTC timestamps (no Z/offset). The expiry
  // countdown must parse them as UTC (via parseBackendDateTime), not via
  // Date.parse() which reads a naive string as LOCAL time. In a UTC+8 zone the
  // buggy path turned a 30-min-future pending into "已过期", hiding a perfectly
  // valid live order from the operator.
  it("treats naive (offset-less) expires_at as UTC, not local, so a future pending is not mislabeled 已过期", () => {
    const originalTz = process.env.TZ;
    process.env.TZ = "Asia/Shanghai";
    vi.useFakeTimers({ toFake: ["Date"] });
    // now = 01:30Z; naive expiry "02:00:00" is +30 min when read as UTC, but
    // 7.5h in the PAST when wrongly read as Asia/Shanghai local (18:00Z prev).
    vi.setSystemTime(new Date("2026-06-14T01:30:00Z"));
    try {
      renderCard([{ ...enriched, approval_id: "appr-tz", expires_at: "2026-06-14T02:00:00" }]);
      expect(screen.queryByText("已过期")).toBeNull();
      expect(screen.getByText(/剩余/)).toBeTruthy();
    } finally {
      vi.useRealTimers();
      process.env.TZ = originalTz;
    }
  });
});
