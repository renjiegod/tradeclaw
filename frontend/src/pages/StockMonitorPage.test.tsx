import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { StockMonitorPage } from "./StockMonitorPage";
import type { MonitorAlert, MonitorRule } from "../types";
import {
  disableMonitor,
  enableMonitor,
  listMonitorAlerts,
  listMonitors,
} from "../api";

vi.mock("../api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  },
  listMonitors: vi.fn(),
  getMonitor: vi.fn(),
  createMonitor: vi.fn(),
  updateMonitor: vi.fn(),
  deleteMonitor: vi.fn(),
  enableMonitor: vi.fn(),
  disableMonitor: vi.fn(),
  listMonitorAlerts: vi.fn(),
  runMonitorOnce: vi.fn(),
  // referenced by the lazily-rendered MonitorFormModal (channel picker)
  listFeishuChats: vi.fn().mockResolvedValue([]),
}));

const presetRule: MonitorRule = {
  id: "mon-aaa",
  name: "白酒涨停盯盘",
  enabled: true,
  status: "active",
  scope_kind: "watchlist_tag",
  scope_json: { tag: "白酒" },
  condition_json: { preset: "limit_up" },
  delivery_json: null,
  cooldown_seconds: 300,
  last_error: "",
  created_at: "2026-06-19T01:00:00Z",
  updated_at: "2026-06-19T01:00:00Z",
};

const alert: MonitorAlert = {
  id: 1,
  monitor_rule_id: "mon-aaa",
  symbol: "600519.SH",
  condition_name: "涨停",
  transition_key: "limit_up",
  triggered_at: "2026-06-19T05:00:00Z",
  last_price: 1800,
  limit_price: 1820,
  diagnostics_json: {},
  run_id: null,
  delivery_status: "delivered",
  delivered_at: "2026-06-19T05:00:01Z",
  created_at: "2026-06-19T05:00:00Z",
};

function renderPage() {
  return render(
    <MemoryRouter>
      <StockMonitorPage />
    </MemoryRouter>,
  );
}

describe("StockMonitorPage", () => {
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
    vi.mocked(listMonitorAlerts).mockResolvedValue({ items: [alert], total: 1 });
  });

  afterEach(() => {
    cleanup();
  });

  it("renders a rule with its condition summary and auto-loads its alerts", async () => {
    vi.mocked(listMonitors).mockResolvedValue({ items: [presetRule], total: 1 });

    renderPage();

    await waitFor(() => expect(listMonitors).toHaveBeenCalled());

    // Rule row renders name + preset condition summary (中文 label).
    expect(await screen.findByText("白酒涨停盯盘")).toBeInTheDocument();
    const ruleRow = screen.getByText("白酒涨停盯盘").closest("tr")!;
    expect(within(ruleRow).getByText("涨停")).toBeInTheDocument();

    // The first rule is auto-selected, so its alerts load.
    await waitFor(() =>
      expect(listMonitorAlerts).toHaveBeenCalledWith("mon-aaa", { limit: 50 }),
    );
    expect(await screen.findByText("600519.SH")).toBeInTheDocument();
  });

  it("pauses an enabled rule via the 暂停 action", async () => {
    vi.mocked(listMonitors).mockResolvedValue({ items: [presetRule], total: 1 });
    vi.mocked(disableMonitor).mockResolvedValue({ ...presetRule, enabled: false, status: "paused" });

    renderPage();
    await waitFor(() => expect(listMonitors).toHaveBeenCalled());

    // antd inserts a space between the two CJK chars, so match "暂 停" loosely.
    fireEvent.click(await screen.findByRole("button", { name: /暂\s*停/ }));

    await waitFor(() => expect(disableMonitor).toHaveBeenCalledWith("mon-aaa"));
    expect(enableMonitor).not.toHaveBeenCalled();
  });
});
