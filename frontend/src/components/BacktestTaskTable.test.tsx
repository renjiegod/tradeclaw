import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { BacktestTaskTable } from "./BacktestTaskTable";
import { getInstrumentCatalogItem } from "../api";
import type { BacktestSummary, TaskStatus } from "../types";

vi.mock("../api", () => ({
  getInstrumentCatalogItem: vi.fn(),
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return {
    ...actual,
    useNavigate: () => vi.fn(),
  };
});

const summary: Partial<BacktestSummary> = {
  range_start_utc: "2024-01-01T00:00:00Z",
  range_end_utc: "2024-03-01T00:00:00Z",
  return_pct: "12.34",
  max_drawdown_pct: "5.67",
  fills_count: 42,
};

const baseTask: TaskStatus = {
  task_id: "task-bt",
  name: "回测任务",
  mode: "backtest",
  description: "",
  status: "completed",
  cycles: null,
  last_error: "",
  data_provider: "baostock",
  data_provider_effective: "baostock",
  universe: [],
  settings: {},
  backtest_summary: summary as BacktestSummary,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("BacktestTaskTable", () => {
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
    vi.mocked(getInstrumentCatalogItem).mockResolvedValue(null as never);
  });

  it("renders backtest-specific columns: return, drawdown, trade count", () => {
    render(
      <MemoryRouter>
        <BacktestTaskTable tasks={[baseTask]} loading={false} />
      </MemoryRouter>,
    );
    expect(screen.getByText("+12.34%")).toBeInTheDocument();
    expect(screen.getByText("-5.67%")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
    // The backtest list has no 模式 / 触发器 columns.
    expect(screen.queryByText("触发器")).not.toBeInTheDocument();
  });

  it("resolves the display status from the latest run", () => {
    render(
      <MemoryRouter>
        <BacktestTaskTable
          tasks={[{ ...baseTask, status: "configured" }]}
          loading={false}
          latestRunStatusByTaskId={{ "task-bt": "failed" }}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText("异常")).toBeInTheDocument();
  });
});
