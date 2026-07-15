import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { CronJobFormModal } from "./CronJobFormModal";
import { createCronJob, updateCronJob } from "../api";

vi.mock("../api", () => ({
  createCronJob: vi.fn(),
  updateCronJob: vi.fn(),
}));

describe("CronJobFormModal", () => {
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
    vi.mocked(createCronJob).mockResolvedValue({
      id: "cj-1",
      agent_id: "asst-1",
      name: "Morning reminder",
      cron_expression: "00 09 * * *",
      timezone: "UTC",
      schedule_kind: "cron",
      at_iso: null,
      delete_after_run: false,
      enabled: true,
      input_template: null,
      max_concurrency: 1,
      timeout_seconds: 120,
      pre_action: null,
      task_kind: "agent_chat_reply",
      task_params_json: {
        user_request: "提醒我复盘",
        agent_id: "asst-1",
      },
      last_run_at: null,
      last_run_session_id: null,
      last_status: null,
      last_error: null,
      effective_status: "waiting",
      created_at: "2026-06-01T00:00:00Z",
      updated_at: "2026-06-01T00:00:00Z",
    });
    vi.mocked(updateCronJob).mockRejectedValue(new Error("not used"));
  });

  afterEach(() => cleanup());

  it("submits an agent_chat_reply reminder task", async () => {
    const onSaved = vi.fn();
    render(
      <MemoryRouter>
        <CronJobFormModal agentId="asst-1" onSaved={onSaved} onClose={vi.fn()} />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText("提醒名称"), {
      target: { value: "Morning reminder" },
    });
    fireEvent.change(screen.getByLabelText("请求内容"), {
      target: { value: "提醒我复盘" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建提醒" }));

    await waitFor(() => {
      expect(createCronJob).toHaveBeenCalledWith(
        "asst-1",
        expect.objectContaining({
          name: "Morning reminder",
          pre_action: null,
          task: {
            kind: "agent_chat_reply",
            params: expect.objectContaining({
              agent_id: "asst-1",
              user_request: "提醒我复盘",
            }),
          },
        }),
      );
      expect(onSaved).toHaveBeenCalled();
    });
  }, 10000);

  it("does not offer strategy signal alert / legacy modes", async () => {
    render(
      <MemoryRouter>
        <CronJobFormModal agentId="asst-1" onSaved={vi.fn()} onClose={vi.fn()} />
      </MemoryRouter>,
    );

    // The Task / job-mode selector and strategy-task picker are gone now that
    // strategy push lives on Tasks (triggers).
    expect(screen.queryByText(/Strategy signal alert/i)).toBeNull();
    expect(screen.queryByText(/Legacy strategy_cycle/i)).toBeNull();
    expect(screen.queryByText(/Strategy tasks/i)).toBeNull();
    expect(screen.getByLabelText("请求内容")).toBeInTheDocument();
  });
});
