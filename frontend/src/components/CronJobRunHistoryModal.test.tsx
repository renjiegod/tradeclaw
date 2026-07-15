import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { CronJobRunHistoryModal } from "./CronJobRunHistoryModal";
import { getCronJobRunTrace, listCronJobRuns } from "../api";
import type { CronJobRun } from "../types";

vi.mock("../api", () => ({
  getCronJobRunTrace: vi.fn(),
  listCronJobRuns: vi.fn(),
}));

vi.mock("../hooks/modelInvocation", () => ({
  buildModelInvocationCollapseItems: vi.fn(() => []),
}));

vi.mock("./TraceViewer", () => ({
  TraceViewer: () => <div>trace viewer</div>,
}));

const sampleRun: CronJobRun = {
  id: "crun-1",
  job_id: "cron-1",
  fired_at: "2026-06-04T10:00:00Z",
  started_at: "2026-06-04T10:00:01Z",
  finished_at: "2026-06-04T10:00:05Z",
  status: "success",
  trace_id: "trace-1",
  pre_kind: null,
  pre_status: null,
  pre_run_id: null,
  pre_debug_session_id: null,
  pre_result_json: null,
  pre_error: null,
  agent_session_id: "asst-cron-1",
  agent_error: null,
  cron_task_kind: "agent_chat_reply",
  delivery_status: "delivered",
  created_at: "2026-06-04T10:00:00Z",
};

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

describe("CronJobRunHistoryModal", () => {
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
    vi.mocked(listCronJobRuns).mockResolvedValue({ items: [sampleRun] });
    vi.mocked(getCronJobRunTrace).mockResolvedValue({
      run_id: sampleRun.id,
      session_ids: [sampleRun.agent_session_id ?? ""],
      spans: [],
      model_invocations: [],
      related: [],
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("navigates to the corresponding assistant session from history", async () => {
    render(
      <MemoryRouter initialEntries={["/cron_jobs"]}>
        <Routes>
          <Route
            path="/cron_jobs"
            element={
              <>
                <CronJobRunHistoryModal jobId="cron-1" jobName="Cron One" onClose={() => {}} />
                <LocationProbe />
              </>
            }
          />
          <Route path="/assistant" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText("asst-cron-1")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "查看会话" }));

    await waitFor(() => {
      expect(screen.getByTestId("location").textContent).toBe(
        "/assistant?session_id=asst-cron-1",
      );
    });
  });
});
