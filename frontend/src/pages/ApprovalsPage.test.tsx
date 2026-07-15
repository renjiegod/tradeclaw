import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { ApprovalsPage } from "./ApprovalsPage";
import { approve, listApprovals, reject } from "../api";
import type { PendingApproval } from "../types";

vi.mock("../api", () => ({
  listApprovals: vi.fn(),
  approve: vi.fn().mockResolvedValue({ status: "approved" }),
  reject: vi.fn().mockResolvedValue({ status: "rejected" }),
}));

const navigateMock = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => navigateMock };
});

const listApprovalsMock = vi.mocked(listApprovals);
const approveMock = vi.mocked(approve);

// antd inserts a hairspace between adjacent CJK glyphs in a <Button>
// ("同意" → "同 意"); match on collapsed text content.
function findButton(label: string): HTMLElement {
  const target = label.replace(/\s+/g, "");
  const button = screen
    .getAllByRole("button")
    .find((el) => (el.textContent ?? "").replace(/\s+/g, "") === target);
  if (!button) throw new Error(`button "${label}" not found`);
  return button;
}

const pendingRow: PendingApproval = {
  approval_id: "appr-p",
  intent_id: "intent-p",
  status: "pending",
  mode: "live",
  symbol: "601398.SH",
  action: "buy",
  notional: "780",
  created_at: "2026-06-14T02:00:00",
  task_id: "task-1",
};

const rejectedRow: PendingApproval = {
  approval_id: "appr-r",
  intent_id: "intent-r",
  status: "rejected",
  mode: "live",
  symbol: "600519.SH",
  action: "buy",
  notional: "10000",
  created_at: "2026-06-13T02:00:00",
  decision_source: "web",
  resolver_id: "u1",
  decided_at: "2026-06-13T03:00:00",
  reason: "风控拒绝",
};

function renderPage() {
  return render(
    <MemoryRouter>
      <ApprovalsPage />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ApprovalsPage", () => {
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

  it("loads the full history and renders pending + resolved rows", async () => {
    listApprovalsMock.mockResolvedValue({
      items: [pendingRow, rejectedRow],
      total: 2,
      limit: 20,
      offset: 0,
    });
    renderPage();

    expect(await screen.findByText("601398.SH")).toBeTruthy();
    expect(screen.getByText("600519.SH")).toBeTruthy();
    expect(screen.getByText("待处理")).toBeTruthy();
    expect(screen.getByText("已拒绝")).toBeTruthy();
    // First page fetched with the default pagination window.
    expect(listApprovalsMock).toHaveBeenCalled();
    const firstCall = listApprovalsMock.mock.calls[0][0];
    expect(firstCall).toMatchObject({ limit: 20, offset: 0 });
  });

  it("only exposes decision buttons on pending rows and approve refetches", async () => {
    listApprovalsMock.mockResolvedValue({
      items: [pendingRow, rejectedRow],
      total: 2,
      limit: 20,
      offset: 0,
    });
    renderPage();
    await screen.findByText("601398.SH");

    // Exactly one approve button — the resolved row is read-only.
    const approveButtons = screen
      .getAllByRole("button")
      .filter((el) => (el.textContent ?? "").replace(/\s+/g, "") === "同意");
    expect(approveButtons.length).toBe(1);

    const callsBefore = listApprovalsMock.mock.calls.length;
    fireEvent.click(findButton("同意"));
    await waitFor(() => expect(approveMock).toHaveBeenCalledWith("appr-p"));
    // Decision triggers a silent refetch so the row's new state lands.
    await waitFor(() =>
      expect(listApprovalsMock.mock.calls.length).toBeGreaterThan(callsBefore),
    );
  });
});
