import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AgentFormModal } from "./AgentFormModal";
import {
  createAssistantAgent,
  listAssistantAgentPromptTemplates,
  listAssistantAgentSkills,
  listAssistantAgentTools,
  listModelRoutes,
  updateAssistantAgent,
} from "../api";

async function chooseSelectOption(label: string, optionName: string) {
  fireEvent.mouseDown(screen.getByLabelText(label));
  await screen.findByRole("option", { name: optionName });
  const optionContent = screen.getAllByText(optionName).at(-1);
  if (!optionContent) {
    throw new Error(`Option ${optionName} was not rendered`);
  }
  fireEvent.click(optionContent);
}

vi.mock("../api", () => ({
  createAssistantAgent: vi.fn(),
  updateAssistantAgent: vi.fn(),
  listAssistantAgentPromptTemplates: vi.fn(),
  listAssistantAgentTools: vi.fn(),
  listAssistantAgentSkills: vi.fn(),
  listModelRoutes: vi.fn(),
}));

describe("AgentFormModal", () => {
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
    vi.mocked(listModelRoutes).mockResolvedValue({
      items: [
        {
          id: "route-1",
          route_name: "default",
          provider_id: "provider-1",
          target_model: "claude-test",
          settings: null,
          created_at: "2026-04-30T00:00:00",
          updated_at: "2026-04-30T00:00:00",
        },
      ],
    });
    vi.mocked(listAssistantAgentTools).mockResolvedValue({
      tools: [{ name: "data_bars_relative", description: "bars" }],
    });
    vi.mocked(listAssistantAgentSkills).mockResolvedValue({
      items: [{ name: "research", description: "Research", category: "custom" }],
    });
    vi.mocked(listAssistantAgentPromptTemplates).mockResolvedValue({
      items: [
        {
          template_id: "swing-trader",
          name: "Swing Trader",
          system_prompt: "# Swing Trader\n\nFocus on medium-term setups.",
          description: "Medium-term prompt",
        },
      ],
      total: 1,
    });
    vi.mocked(createAssistantAgent).mockResolvedValue({
      id: "agent-1",
      name: "Agent One",
      status: "active",
      system_prompt: "hi",
      system_prompt_template_id: "swing-trader",
      model_route_name: "default",
      tool_configs: [{ name: "data_bars_relative", load_mode: "base" }],
      tool_names: ["data_bars_relative"],
      skill_names: ["research"],
      max_turns: 6,
      context_compaction: {
        enabled: true,
        mode: "auto",
        trigger_strategy: "token_estimate",
        auto_threshold_tokens: 24000,
        warning_threshold_tokens: 20000,
        preserve_recent_messages: 12,
        preserve_recent_tool_pairs: 4,
        micro_compaction_enabled: true,
        tool_result_max_chars: 4000,
        full_compaction_enabled: true,
        summary_model_route_name: "",
        allow_slash_compact: true,
      },
      is_default: false,
      is_builtin: false,
      created_at: "2026-04-30T00:00:00",
      updated_at: "2026-04-30T00:00:00",
    });
    vi.mocked(updateAssistantAgent).mockResolvedValue({
      id: "agent-1",
      name: "Agent One",
      status: "active",
      system_prompt: "hi",
      system_prompt_template_id: "swing-trader",
      model_route_name: "default",
      tool_configs: [{ name: "data_bars_relative", load_mode: "base" }],
      tool_names: ["data_bars_relative"],
      skill_names: ["research"],
      max_turns: 6,
      context_compaction: {
        enabled: true,
        mode: "manual",
        trigger_strategy: "token_estimate",
        auto_threshold_tokens: 12345,
        warning_threshold_tokens: 20000,
        preserve_recent_messages: 8,
        preserve_recent_tool_pairs: 2,
        micro_compaction_enabled: true,
        tool_result_max_chars: 2000,
        full_compaction_enabled: true,
        summary_model_route_name: "default",
        allow_slash_compact: false,
      },
      is_default: false,
      is_builtin: false,
      created_at: "2026-04-30T00:00:00",
      updated_at: "2026-04-30T00:00:00",
    });
  });

  afterEach(() => cleanup());

  it("loads model routes, tools, and skills from backend APIs", async () => {
    render(<AgentFormModal onSaved={vi.fn()} onClose={vi.fn()} />);

    await waitFor(() => {
      expect(listModelRoutes).toHaveBeenCalled();
      expect(listAssistantAgentTools).toHaveBeenCalled();
      expect(listAssistantAgentSkills).toHaveBeenCalled();
      expect(listAssistantAgentPromptTemplates).toHaveBeenCalled();
    });

    fireEvent.mouseDown(screen.getByLabelText("使用的模型"));
    expect(await screen.findByRole("option", { name: "default → claude-test" })).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByLabelText("工具"));
    expect(await screen.findByRole("option", { name: "data_bars_relative" })).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByLabelText("Skills"));
    expect(await screen.findByRole("option", { name: "research" })).toBeInTheDocument();
    // Renders the full modal (now with collapsed advanced panels) in jsdom;
    // sits near the 5s default under parallel load, so give it headroom.
  }, 15000);

  it("submits selected route, tools, and skills", async () => {
    const onSaved = vi.fn();
    render(<AgentFormModal onSaved={onSaved} onClose={vi.fn()} />);

    await screen.findByLabelText("工具");
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "Agent One" } });
    const editor = document.querySelector(".cm-editor");
    const editableDiv = editor?.querySelector('[contenteditable="true"]') ?? editor?.querySelector("[role='textbox']");
    if (editableDiv) {
      fireEvent.input(editableDiv, { target: { textContent: "hi" } });
    }

    await chooseSelectOption("使用的模型", "default → claude-test");
    await chooseSelectOption("工具", "data_bars_relative");
    await chooseSelectOption("Skills", "research");

    fireEvent.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() => {
      expect(createAssistantAgent).toHaveBeenCalledWith(
        expect.objectContaining({
          model_route_name: "default",
          tool_configs: [{ name: "data_bars_relative", load_mode: "base" }],
          tool_names: ["data_bars_relative"],
          skill_names: ["research"],
        }),
      );
      expect(onSaved).toHaveBeenCalled();
    });
  }, 15000);

  it("template mode hides the markdown editor and submits an empty raw prompt", async () => {
    const onSaved = vi.fn();
    render(<AgentFormModal onSaved={onSaved} onClose={vi.fn()} />);

    await screen.findByLabelText("提示词模板");
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "Agent One" } });

    // The editor is initially visible in custom mode so the user can author
    // a prompt before deciding to link a template.
    expect(document.querySelector(".cm-editor")).toBeTruthy();

    await chooseSelectOption("提示词模板", "Swing Trader");

    // Selecting a template flips the form into linked mode: no editor, just
    // a read-only markdown preview backed by the template's .j2 render.
    await waitFor(() => {
      expect(document.querySelector(".cm-editor")).toBeFalsy();
    });
    const preview = screen.getByTestId("prompt-template-readonly-preview");
    expect(preview).toBeInTheDocument();
    // Markdown is rendered as real headings, not raw "# Swing Trader" text.
    expect(preview.querySelector("h1")).toHaveTextContent("Swing Trader");
    expect(preview).toHaveTextContent("Focus on medium-term setups.");

    fireEvent.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() => {
      expect(createAssistantAgent).toHaveBeenCalledWith(
        expect.objectContaining({
          system_prompt: "",
          system_prompt_template_id: "swing-trader",
        }),
      );
      expect(onSaved).toHaveBeenCalled();
    });
  }, 15000);

  it("switching an existing custom agent to a template clears its stored raw prompt", async () => {
    const onSaved = vi.fn();
    render(
      <AgentFormModal
        agent={{
          id: "agent-1",
          name: "Agent One",
          status: "active",
          system_prompt: "Old prompt",
          system_prompt_template_id: undefined,
          model_route_name: "",
          tool_configs: [],
          tool_names: [],
          skill_names: [],
          max_turns: 6,
          context_compaction: {
            enabled: true,
            mode: "auto",
            trigger_strategy: "token_estimate",
            auto_threshold_tokens: 24000,
            warning_threshold_tokens: 20000,
            preserve_recent_messages: 12,
            preserve_recent_tool_pairs: 4,
            micro_compaction_enabled: true,
            tool_result_max_chars: 4000,
            full_compaction_enabled: true,
            summary_model_route_name: "",
            allow_slash_compact: true,
          },
          is_default: false,
          is_builtin: false,
          created_at: "2026-04-30T00:00:00",
          updated_at: "2026-04-30T00:00:00",
        }}
        onSaved={onSaved}
        onClose={vi.fn()}
      />,
    );

    await screen.findByLabelText("提示词模板");

    await chooseSelectOption("提示词模板", "Swing Trader");
    expect(screen.getByText("Medium-term prompt")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(updateAssistantAgent).toHaveBeenCalledWith(
        "agent-1",
        expect.objectContaining({
          system_prompt: "",
          system_prompt_template_id: "swing-trader",
        }),
      );
      expect(onSaved).toHaveBeenCalled();
    });
  }, 15000);

  it("submits editable context compaction settings", async () => {
    const onSaved = vi.fn();
    render(<AgentFormModal onSaved={onSaved} onClose={vi.fn()} />);

    await screen.findByLabelText("压缩模式");
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "Agent One" } });
    const editor = document.querySelector(".cm-editor");
    const editableDiv = editor?.querySelector('[contenteditable="true"]') ?? editor?.querySelector("[role='textbox']");
    if (editableDiv) {
      fireEvent.input(editableDiv, { target: { textContent: "hi" } });
    }

    await chooseSelectOption("压缩模式", "手动");
    fireEvent.change(screen.getByLabelText("自动压缩阈值（tokens）"), { target: { value: "12345" } });
    fireEvent.change(screen.getByLabelText("保留最近消息数"), { target: { value: "8" } });
    fireEvent.change(screen.getByLabelText("保留最近工具对数"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("工具结果最大字符数"), { target: { value: "2000" } });
    await chooseSelectOption("摘要用的模型", "default → claude-test");
    await userEvent.click(screen.getByLabelText("允许 /compact"));

    fireEvent.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() => {
      expect(createAssistantAgent).toHaveBeenCalledWith(
        expect.objectContaining({
          context_compaction: expect.objectContaining({
            mode: "manual",
            auto_threshold_tokens: 12345,
            preserve_recent_messages: 8,
            preserve_recent_tool_pairs: 2,
            tool_result_max_chars: 2000,
            summary_model_route_name: "default",
            allow_slash_compact: false,
          }),
        }),
      );
      expect(onSaved).toHaveBeenCalled();
    });
  }, 15000);

  it("submits per-tool load modes", async () => {
    const onSaved = vi.fn();
    render(
      <AgentFormModal
        agent={{
          id: "agent-1",
          name: "Agent One",
          status: "active",
          system_prompt: "hi",
          model_route_name: "",
          tool_configs: [{ name: "data_bars_relative", load_mode: "deferred" }],
          tool_names: ["data_bars_relative"],
          skill_names: [],
          max_turns: 6,
          context_compaction: {
            enabled: true,
            mode: "auto",
            trigger_strategy: "token_estimate",
            auto_threshold_tokens: 24000,
            warning_threshold_tokens: 20000,
            preserve_recent_messages: 12,
            preserve_recent_tool_pairs: 4,
            micro_compaction_enabled: true,
            tool_result_max_chars: 4000,
            full_compaction_enabled: true,
            summary_model_route_name: "",
            allow_slash_compact: true,
          },
          is_default: false,
          is_builtin: false,
          created_at: "2026-04-30T00:00:00",
          updated_at: "2026-04-30T00:00:00",
        }}
        onSaved={onSaved}
        onClose={vi.fn()}
      />,
    );

    await screen.findByLabelText("工具");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(updateAssistantAgent).toHaveBeenCalledWith(
        "agent-1",
        expect.objectContaining({
          tool_configs: [{ name: "data_bars_relative", load_mode: "deferred" }],
        }),
      );
      expect(onSaved).toHaveBeenCalled();
    });
  }, 15000);

  it("locks max_turns in cloud mode (operator-controlled) but leaves it editable locally", async () => {
    // Cloud: the 最大轮数 input is disabled — the operator sets it in the dytc
    // admin console and the server clamps it regardless.
    const { unmount } = render(
      <AgentFormModal deploymentMode="cloud" onSaved={vi.fn()} onClose={vi.fn()} />,
    );
    const cloudInput = await screen.findByLabelText("最大轮数");
    expect((cloudInput as HTMLInputElement).disabled).toBe(true);
    unmount();
    cleanup();

    // Local (default): the same input is editable.
    render(<AgentFormModal onSaved={vi.fn()} onClose={vi.fn()} />);
    const localInput = await screen.findByLabelText("最大轮数");
    expect((localInput as HTMLInputElement).disabled).toBe(false);
  }, 15000);

  it("renders a restricted form for the builtin agent and submits only the runtime knobs", async () => {
    const onSaved = vi.fn();
    render(
      <AgentFormModal
        agent={{
          id: "agent-builtin",
          name: "默认智能体",
          status: "active",
          system_prompt: "",
          system_prompt_template_id: "main_agent",
          model_route_name: "default",
          tool_configs: [],
          tool_names: [],
          skill_names: [],
          max_turns: 6,
          context_compaction: {
            enabled: true,
            mode: "auto",
            trigger_strategy: "token_estimate",
            auto_threshold_tokens: 24000,
            warning_threshold_tokens: 20000,
            preserve_recent_messages: 12,
            preserve_recent_tool_pairs: 4,
            micro_compaction_enabled: true,
            tool_result_max_chars: 4000,
            full_compaction_enabled: true,
            summary_model_route_name: "",
            allow_slash_compact: true,
          },
          is_default: true,
          is_builtin: true,
          editable_fields: ["model_route_name", "context_compaction", "max_turns"],
          created_at: "2026-04-30T00:00:00",
          updated_at: "2026-04-30T00:00:00",
        }}
        onSaved={onSaved}
        onClose={vi.fn()}
      />,
    );

    // Restricted form: name is disabled, prompt template + tools/skills are gone.
    await screen.findByText("固定主智能体");
    expect((screen.getByLabelText("名称") as HTMLInputElement).disabled).toBe(true);
    expect(screen.queryByLabelText("提示词模板")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("工具")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Skills")).not.toBeInTheDocument();
    // The runtime knobs remain editable.
    expect(screen.getByLabelText("使用的模型")).toBeInTheDocument();
    expect(screen.getByLabelText("压缩模式")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(updateAssistantAgent).toHaveBeenCalledTimes(1);
      expect(onSaved).toHaveBeenCalled();
    });
    const [, builtinPayload] = vi.mocked(updateAssistantAgent).mock.calls[0];
    // Only the three runtime knobs — never name / prompt / tools / skills.
    expect(Object.keys(builtinPayload as Record<string, unknown>).sort()).toEqual(
      ["context_compaction", "max_turns", "model_route_name"],
    );
  }, 15000);
});
