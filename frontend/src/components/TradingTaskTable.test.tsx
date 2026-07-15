import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TradingTaskTable } from "./TradingTaskTable";
import type { TriggerSummary } from "./taskTableShared";
import { getInstrumentCatalogItem } from "../api";
import type { TaskStatus } from "../types";

vi.mock("../api", () => ({
  getInstrumentCatalogItem: vi.fn(),
}));

const navigateMock = vi.fn();

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return {
    ...actual,
    useNavigate: () => navigateMock,
  };
});

const baseTask: TaskStatus = {
  task_id: "task-1",
  name: "实盘任务",
  mode: "live",
  description: "",
  status: "running",
  cycles: 12,
  last_error: "",
  data_provider: "qmt",
  data_provider_effective: "qmt",
  universe: [],
  settings: {},
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function renderTable(
  tasks: TaskStatus[],
  triggerSummaryByTaskId?: Record<string, TriggerSummary | undefined>,
) {
  return render(
    <MemoryRouter>
      <TradingTaskTable
        tasks={tasks}
        loading={false}
        triggerSummaryByTaskId={triggerSummaryByTaskId}
      />
    </MemoryRouter>,
  );
}

describe("TradingTaskTable", () => {
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

  it("renders the 实盘 mode label and cycle count", () => {
    renderTable([baseTask], { "task-1": { total: 1, active: 1, nextFireAt: null } });
    expect(screen.getByText("实盘")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
  });

  it("flags a running task with no triggers", () => {
    renderTable([baseTask], { "task-1": { total: 0, active: 0, nextFireAt: null } });
    expect(screen.getByText("无触发器")).toBeInTheDocument();
  });

  it("shows the active trigger count and next-fire preview", () => {
    renderTable([baseTask], {
      "task-1": { total: 2, active: 2, nextFireAt: "2026-06-12T06:50:00Z" },
    });
    expect(screen.getByText("2 个")).toBeInTheDocument();
    expect(screen.getByText(/下次/)).toBeInTheDocument();
  });

  it("navigates to the task detail on row click", () => {
    renderTable([baseTask], { "task-1": { total: 1, active: 1, nextFireAt: null } });
    fireEvent.click(screen.getAllByText("实盘任务")[0]!);
    expect(navigateMock).toHaveBeenCalledWith("/tasks/task-1");
  });
});
