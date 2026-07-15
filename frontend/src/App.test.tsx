import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import App from "./App";
import { getHealth, getRuntimeStatus, getSystemState, listPendingApprovals, listTasks } from "./api";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return {
    ...actual,
    getHealth: vi.fn(),
    getRuntimeStatus: vi.fn(),
    getSystemState: vi.fn(),
    listPendingApprovals: vi.fn(),
    listTasks: vi.fn(),
  };
});

vi.mock("./pages/ModelInvocationsPage", async () => {
  const React = await import("react");
  const { usePageRefreshToken } = await import("./pageRefreshContext");
  return {
    ModelInvocationsPage: () => {
      const token = usePageRefreshToken();
      return <div data-testid="page-refresh-token">{token}</div>;
    },
  };
});

describe("App shell refresh", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
    vi.mocked(getHealth).mockResolvedValue({ status: "ok" });
    vi.mocked(getRuntimeStatus).mockResolvedValue({
      health: "ok",
      capabilities: { total: 11, kinds: ["channel", "data_provider", "model_provider"] },
      assistant: { available: true, tool_count: 5 },
      channels: {
        manager_available: false,
        repository_available: false,
        registered_ids: [],
        repository_count: null,
      },
      cron: { available: false, run_repository_available: false },
      observability: { model_invocations_available: false },
      checks: {},
    });
    vi.mocked(listTasks).mockResolvedValue([]);
    vi.mocked(listPendingApprovals).mockResolvedValue([]);
    vi.mocked(getSystemState).mockResolvedValue({
      kill_switch_enabled: false,
      task_count: 0,
      running_count: 0,
    });
  });

  it("signals the current content page to reload data when the header refresh button is clicked", async () => {
    render(
      <MemoryRouter initialEntries={["/model_invocations"]}>
        <App />
      </MemoryRouter>,
    );

    expect(await screen.findByTestId("page-refresh-token")).toHaveTextContent("0");
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /刷新/ })).toBeEnabled();
    });

    fireEvent.click(screen.getByRole("button", { name: /刷新/ }));

    await waitFor(() => {
      expect(screen.getByTestId("page-refresh-token")).toHaveTextContent("1");
    });
  });
});
