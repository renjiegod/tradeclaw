import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { CronJobsPage } from "./CronJobsPage";
import { listAssistantAgents, listCronJobs } from "../api";

vi.mock("../api", () => ({
  listAssistantAgents: vi.fn(),
  listCronJobs: vi.fn(),
  deleteCronJob: vi.fn(),
  pauseCronJob: vi.fn(),
  resumeCronJob: vi.fn(),
  triggerCronJob: vi.fn(),
}));

vi.mock("../pageRefreshContext", () => ({
  usePageRefreshToken: () => 0,
}));

vi.mock("../components/CronJobFormModal", () => ({
  CronJobFormModal: () => <div>Cron job form</div>,
}));

vi.mock("../components/CronJobRunHistoryModal", () => ({
  CronJobRunHistoryModal: () => <div>Cron job history</div>,
}));

describe("CronJobsPage", () => {
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
    vi.mocked(listAssistantAgents).mockResolvedValue({
      items: [
        {
          id: "asst-1",
          name: "Alpha Agent",
          system_prompt: "",
          status: "active",
          model_route_name: "route-a",
          tool_names: [],
          skill_names: [],
          max_turns: 12,
          context_compaction: {
            enabled: false,
            mode: "off",
            trigger_strategy: "auto",
            auto_threshold_tokens: 0,
            warning_threshold_tokens: 0,
            preserve_recent_messages: 0,
            preserve_recent_tool_pairs: 0,
            micro_compaction_enabled: false,
            tool_result_max_chars: 0,
            full_compaction_enabled: false,
            summary_model_route_name: "",
            allow_slash_compact: false,
          },
          is_default: false,
          is_builtin: false,
          created_at: "",
          updated_at: "",
        },
        {
          id: "asst-2",
          name: "Beta Agent",
          system_prompt: "",
          status: "active",
          model_route_name: "route-b",
          tool_names: [],
          skill_names: [],
          max_turns: 12,
          context_compaction: {
            enabled: false,
            mode: "off",
            trigger_strategy: "auto",
            auto_threshold_tokens: 0,
            warning_threshold_tokens: 0,
            preserve_recent_messages: 0,
            preserve_recent_tool_pairs: 0,
            micro_compaction_enabled: false,
            tool_result_max_chars: 0,
            full_compaction_enabled: false,
            summary_model_route_name: "",
            allow_slash_compact: false,
          },
          is_default: false,
          is_builtin: false,
          created_at: "",
          updated_at: "",
        },
      ],
      total: 2,
    });
    vi.mocked(listCronJobs).mockImplementation(async (agentId: string) => ({
      items: agentId === "asst-1"
        ? [
            {
              id: "cj-1",
              agent_id: "asst-1",
              name: "Morning Review",
              cron_expression: "0 9 * * 1-5",
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
              task_params_json: {},
              last_run_at: null,
              last_run_session_id: null,
              last_status: null,
              last_error: null,
              effective_status: "waiting",
              created_at: "2026-06-01T00:00:00Z",
              updated_at: "2026-06-01T00:00:00Z",
            },
          ]
        : [
            {
              id: "cj-2",
              agent_id: "asst-2",
              name: "Signal Push",
              cron_expression: "*/5 9-15 * * 1-5",
              timezone: "UTC",
              schedule_kind: "cron",
              at_iso: null,
              delete_after_run: false,
              enabled: true,
              input_template: null,
              max_concurrency: 1,
              timeout_seconds: 120,
              pre_action: null,
              task_kind: "strategy_signal_alert",
              task_params_json: {},
              last_run_at: null,
              last_run_session_id: null,
              last_status: null,
              last_error: null,
              effective_status: "waiting",
              created_at: "2026-06-01T00:00:00Z",
              updated_at: "2026-06-01T00:00:00Z",
            },
          ],
      total: 1,
    }));
  });

  afterEach(() => {
    cleanup();
  });

  it("shows cron jobs from all agents by default", async () => {
    render(<CronJobsPage />);

    await waitFor(() => {
      expect(listAssistantAgents).toHaveBeenCalledWith({ include_inactive: true });
      expect(listCronJobs).toHaveBeenCalledWith("asst-1");
      expect(listCronJobs).toHaveBeenCalledWith("asst-2");
    });

    expect(await screen.findByText("Morning Review")).toBeInTheDocument();
    expect(screen.getByText("Signal Push")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New Reminder" })).toBeDisabled();
  });

  it("can narrow the list to one agent after the default all-agents view", async () => {
    render(<CronJobsPage />);

    await screen.findByText("Morning Review");
    fireEvent.mouseDown(screen.getByRole("combobox"));
    const betaAgentNodes = await screen.findAllByText("Beta Agent");
    fireEvent.click(betaAgentNodes.at(-1)!);

    await waitFor(() => {
      expect(screen.queryByText("Morning Review")).toBeNull();
    });
    expect(screen.getByText("Signal Push")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New Reminder" })).toBeEnabled();
  });
});
