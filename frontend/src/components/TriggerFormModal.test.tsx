import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TriggerFormModal } from "./TriggerFormModal";
import { createTaskTrigger, listAssistantAgents, listFeishuChats, updateTaskTrigger } from "../api";
import type { Agent, TaskTrigger } from "../types";

vi.mock("../api", () => ({
  createTaskTrigger: vi.fn(),
  updateTaskTrigger: vi.fn(),
  listFeishuChats: vi.fn(),
  listAssistantAgents: vi.fn(),
}));

function assistantAgent(overrides: Partial<Agent> = {}): Agent {
  return {
    id: "ag-1",
    name: "解析助手",
    status: "active",
    system_prompt: "",
    model_route_name: "default",
    tool_names: [],
    skill_names: [],
    max_turns: 8,
    context_compaction: { enabled: false } as Agent["context_compaction"],
    is_default: false,
    is_builtin: false,
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    ...overrides,
  };
}

function savedTrigger(overrides: Partial<TaskTrigger> = {}): TaskTrigger {
  return {
    id: "trg-1",
    task_id: "task-1",
    name: "T",
    enabled: true,
    status: "active",
    schedule_kind: "cron",
    interval_seconds: null,
    cron_expression: "50 14 * * mon-fri",
    timezone: "Asia/Shanghai",
    at_iso: null,
    range_start: null,
    range_end: null,
    bar_interval: null,
    trading_session: "ashare",
    delete_after_run: false,
    execution_intent: "signal_only",
    delivery_json: null,
    last_fired_at: null,
    next_fire_at: null,
    last_run_id: null,
    last_error: "",
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    ...overrides,
  };
}

describe("TriggerFormModal", () => {
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
    vi.mocked(createTaskTrigger).mockResolvedValue(savedTrigger());
    vi.mocked(updateTaskTrigger).mockResolvedValue(savedTrigger());
    vi.mocked(listFeishuChats).mockResolvedValue([]);
    vi.mocked(listAssistantAgents).mockResolvedValue({
      items: [
        assistantAgent({ id: "ag-default", name: "默认助手", is_default: true }),
        assistantAgent({ id: "ag-x", name: "解析助手" }),
      ],
      total: 2,
    });
  });

  afterEach(() => cleanup());

  it("prefills the close-signal template and submits a snake_case card payload", async () => {
    const onSaved = vi.fn();
    render(<TriggerFormModal taskId="task-1" onSaved={onSaved} onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "收盘推送信号" }));

    // Template fills the cron expression.
    await waitFor(() => {
      expect(screen.getByLabelText("Cron 表达式")).toHaveValue("50 14 * * mon-fri");
    });

    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "收盘信号" } });
    fireEvent.click(screen.getByRole("button", { name: /创建触发器/ }));

    await waitFor(() => {
      expect(createTaskTrigger).toHaveBeenCalledWith(
        "task-1",
        expect.objectContaining({
          name: "收盘信号",
          schedule_kind: "cron",
          cron_expression: "50 14 * * mon-fri",
          timezone: "Asia/Shanghai",
          trading_session: "ashare",
          execution_intent: "signal_only",
          delivery_json: {
            mode: "card",
            target: { kind: "session", origin: true },
            no_signal_mode: "brief",
          },
        }),
      );
    });
    expect(onSaved).toHaveBeenCalled();
  }, 10000);

  it("sends delivery_json=null when push is disabled (intraday-trade template)", async () => {
    render(<TriggerFormModal taskId="task-1" onSaved={vi.fn()} onClose={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "盘中自动交易" }));
    await waitFor(() => {
      expect(screen.getByLabelText("Cron 表达式")).toHaveValue("*/5 9-11,13-15 * * mon-fri");
    });

    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "盘中" } });
    fireEvent.click(screen.getByRole("button", { name: /创建触发器/ }));

    await waitFor(() => {
      expect(createTaskTrigger).toHaveBeenCalledWith(
        "task-1",
        expect.objectContaining({
          execution_intent: "trade",
          delivery_json: null,
        }),
      );
    });
  }, 10000);

  it("loads the live Feishu groups into the channel dropdown", async () => {
    vi.mocked(listFeishuChats).mockResolvedValue([
      { channel_id: "ch-1", channel_name: "飞书Alpha", chat_id: "oc_test", name: "策略群" },
    ]);
    render(<TriggerFormModal taskId="task-1" onSaved={vi.fn()} onClose={vi.fn()} />);

    // delivery_mode defaults to card; switch the target from 当前会话 to 飞书频道.
    fireEvent.click(screen.getByRole("radio", { name: "飞书频道" }));

    // The picker is fed by listFeishuChats; the group shows up as a real option.
    fireEvent.mouseDown(await screen.findByLabelText("飞书群"));
    expect(await screen.findByRole("option", { name: "策略群" })).toBeInTheDocument();
    expect(listFeishuChats).toHaveBeenCalled();
  }, 10000);

  it("preserves the channel target (bot + chat_id) when editing", async () => {
    // Editing a trigger that already pushes to a Feishu group: the saved target
    // is resolved + re-emitted without re-picking from the dropdown (covers the
    // editingTarget fallback path in handleSubmit).
    vi.mocked(listFeishuChats).mockResolvedValue([]);
    render(
      <TriggerFormModal
        taskId="task-1"
        trigger={savedTrigger({
          delivery_json: {
            mode: "card",
            target: {
              kind: "channel",
              channel_id: "ch-1",
              chat_id: "oc_saved",
              chat_name: "策略群",
            },
            no_signal_mode: "brief",
          },
        })}
        onSaved={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /保存触发器/ }));

    await waitFor(() => {
      expect(updateTaskTrigger).toHaveBeenCalledWith(
        "task-1",
        "trg-1",
        expect.objectContaining({
          delivery_json: expect.objectContaining({
            mode: "card",
            target: {
              kind: "channel",
              channel_id: "ch-1",
              chat_id: "oc_saved",
              chat_name: "策略群",
              channel_type: "feishu",
            },
          }),
        }),
      );
    });
  }, 10000);

  it("updates an existing trigger via updateTaskTrigger", async () => {
    const onSaved = vi.fn();
    render(
      <TriggerFormModal
        taskId="task-1"
        trigger={savedTrigger({ name: "Old" })}
        onSaved={onSaved}
        onClose={vi.fn()}
      />,
    );

    // No template row when editing.
    expect(screen.queryByRole("button", { name: "收盘推送信号" })).toBeNull();

    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "New name" } });
    fireEvent.click(screen.getByRole("button", { name: /保存触发器/ }));

    await waitFor(() => {
      expect(updateTaskTrigger).toHaveBeenCalledWith(
        "task-1",
        "trg-1",
        expect.objectContaining({ name: "New name", schedule_kind: "cron" }),
      );
    });
    expect(onSaved).toHaveBeenCalled();
  }, 10000);

  it("reveals the composer agent picker (with loaded agents) when switching to prose", async () => {
    render(<TriggerFormModal taskId="task-1" onSaved={vi.fn()} onClose={vi.fn()} />);

    // delivery_mode defaults to card; the picker is hidden until prose is chosen.
    expect(screen.queryByLabelText("解析 Agent")).toBeNull();

    fireEvent.click(screen.getByRole("radio", { name: "文字" }));

    // The "解析 Agent" field appears and is fed by listAssistantAgents.
    expect(await screen.findByLabelText("解析 Agent")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByLabelText("解析 Agent"));
    expect(await screen.findByText("默认助手（默认）")).toBeInTheDocument();
    expect(await screen.findByText("解析助手")).toBeInTheDocument();
    expect(listAssistantAgents).toHaveBeenCalled();
  }, 10000);

  it("preserves the prose composer_agent_id when editing", async () => {
    // Editing a prose trigger that already pins a composer agent: the saved
    // composer_agent_id round-trips through initialValues → buildPayload without
    // re-picking from the dropdown (antd Select option clicks don't commit a
    // value under jsdom, so this path is asserted via the edit/initialValues).
    render(
      <TriggerFormModal
        taskId="task-1"
        trigger={savedTrigger({
          delivery_json: {
            mode: "prose",
            target: { kind: "session", session_id: "sess-1" },
            composer_agent_id: "ag-x",
            no_signal_mode: "full",
          },
        })}
        onSaved={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /保存触发器/ }));

    await waitFor(() => {
      expect(updateTaskTrigger).toHaveBeenCalledWith(
        "task-1",
        "trg-1",
        expect.objectContaining({
          delivery_json: expect.objectContaining({
            mode: "prose",
            composer_agent_id: "ag-x",
          }),
        }),
      );
    });
  }, 10000);
});
