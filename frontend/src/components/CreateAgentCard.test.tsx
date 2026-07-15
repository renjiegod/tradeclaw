import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { CreateAgentCard } from "./CreateAgentCard";
import { createTask, listAccounts, listDataProviders, listStrategyDefinitions, updateTask } from "../api";
import type { TaskStatus } from "../types";

vi.mock("../api", () => ({
  createTask: vi.fn(),
  listAccounts: vi.fn(),
  listDataProviders: vi.fn(),
  listStrategyDefinitions: vi.fn(),
  startTaskRun: vi.fn(),
  updateTask: vi.fn(),
}));

vi.mock("./CatalogSymbolSelect", () => ({
  CatalogSymbolSelect: () => <div data-testid="catalog-symbol-select" />,
}));

vi.mock("./SettingsJsonEditorModal", () => ({
  SettingsJsonEditorModal: ({
    open,
    onApply,
  }: {
    open: boolean;
    onApply: (obj: Record<string, unknown>) => void;
  }) =>
    open ? (
      <button
        type="button"
        onClick={() =>
          onApply({
            custom_from_json_modal: true,
            agent: {
              custom_agent_value: "json-kept",
            },
          })
        }
      >
        apply-settings-json
      </button>
    ) : null,
}));

const baseTaskStatus: TaskStatus = {
  task_id: "task-1",
  name: "task-name",
  mode: "paper",
  description: "",
  status: "configured",
  cycles: null,
  last_error: "",
  data_provider: null,
  data_provider_effective: "mock",
  universe: ["000001.SZ"],
  settings: {},
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("CreateAgentCard deprecated task fields removal", () => {
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
    vi.mocked(listDataProviders).mockResolvedValue({ providers: ["mock"] });
    vi.mocked(listAccounts).mockResolvedValue({ items: [] });
    vi.mocked(listStrategyDefinitions).mockResolvedValue({
      items: [
        {
          definition_id: "sd-main",
          name: "Main Definition",
          class_name: "MainStrategy",
          current_version: "v0001",
          api_version: "v1",
          parameter_schema: {},
          default_parameters: {},
          capabilities: {},
          provenance: {},
          code_hash: "hash-main",
          status: "active",
          created_at: "",
          updated_at: "",
        },
        {
          definition_id: "sd-default",
          name: "Default Definition",
          class_name: "DefaultStrategy",
          current_version: "v0001",
          api_version: "v1",
          parameter_schema: {},
          default_parameters: {},
          capabilities: {},
          provenance: {},
          code_hash: "hash-default",
          status: "active",
          created_at: "",
          updated_at: "",
        },
      ],
    });
    vi.mocked(createTask).mockResolvedValue(baseTaskStatus);
    vi.mocked(updateTask).mockResolvedValue(baseTaskStatus);
  });

  afterEach(() => {
    cleanup();
  });

  it("does not render deprecated form fields", async () => {
    render(<CreateAgentCard onCreated={vi.fn()} />);
    await screen.findByLabelText("名称");

    expect(screen.queryByLabelText("模板")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("编排模式")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("观察标的")).not.toBeInTheDocument();
  });

  it("create payload excludes deprecated keys", async () => {
    const { container } = render(<CreateAgentCard onCreated={vi.fn()} />);
    const nameInput = await screen.findByLabelText("名称");
    fireEvent.change(nameInput, { target: { value: "new-task" } });
    fireEvent.mouseDown(screen.getByLabelText("策略定义"));
    fireEvent.click(await screen.findByText("Main Definition (sd-main)"));
    const submitButton = container.querySelector('button[type="submit"]');
    expect(submitButton).not.toBeNull();
    fireEvent.click(submitButton!);

    await waitFor(() => {
      expect(createTask).toHaveBeenCalledTimes(1);
    });

    const payload = vi.mocked(createTask).mock.calls[0]?.[0] as Record<string, unknown>;
    expect(payload).not.toHaveProperty("template_id");
    expect(payload).not.toHaveProperty("orchestrator_mode");
    expect(payload).not.toHaveProperty("watch_symbols");
    expect(payload).not.toHaveProperty("model_route_name");
    expect(payload).toHaveProperty("settings");
    const settings = payload.settings as Record<string, unknown>;
    expect(settings).not.toHaveProperty("watch_symbols");
    expect(settings).not.toHaveProperty("model_route_name");
    expect(settings).toHaveProperty("strategy");
    const agent = settings.agent as Record<string, unknown>;
    expect(agent).not.toHaveProperty("react_max_turns");
    expect(agent).not.toHaveProperty("signal_tool_names");
    expect(agent).not.toHaveProperty("enabled_skills");
    expect(agent).toHaveProperty("approval");
    expect((agent.approval as Record<string, unknown>).min_notional_for_approval).toBe(1000);
    expect((agent.approval as Record<string, unknown>).min_notational_for_approval).toBeUndefined();
  });

  it("update payload excludes deprecated keys", async () => {
    const { container } = render(
      <CreateAgentCard
        onCreated={vi.fn()}
        mode="edit"
        editTask={{
          ...baseTaskStatus,
          settings: {
            strategy: { definition_id: "sd-main", parameter_overrides: {}, execution_profile: "default" },
          },
        }}
      />,
    );
    const nameInput = await screen.findByLabelText("名称");
    fireEvent.change(nameInput, { target: { value: "edited-task" } });
    const submitButton = container.querySelector('button[type="submit"]');
    expect(submitButton).not.toBeNull();
    fireEvent.click(submitButton!);

    await waitFor(() => {
      expect(updateTask).toHaveBeenCalledTimes(1);
    });

    const payload = vi.mocked(updateTask).mock.calls[0]?.[1] as Record<string, unknown>;
    expect(payload).not.toHaveProperty("template_id");
    expect(payload).not.toHaveProperty("orchestrator_mode");
    expect(payload).not.toHaveProperty("watch_symbols");
    expect(payload).not.toHaveProperty("model_route_name");
    expect(payload).toHaveProperty("settings");
    const settings = payload.settings as Record<string, unknown>;
    expect(settings).not.toHaveProperty("watch_symbols");
    expect(settings).not.toHaveProperty("model_route_name");
    expect(settings).toHaveProperty("strategy");
    // Regression: the prefilled universe must survive the save round-trip.
    // Previously universe_symbols was collected but never written to the
    // payload, so editing silently dropped the task's universe.
    expect(settings.universe).toEqual(["000001.SZ"]);
  });

  it("applies create initial values in create mode", async () => {
    render(
        <CreateAgentCard
        onCreated={vi.fn()}
        createInitialValues={{
          name: "task-a-copy",
          mode: "paper",
        }}
      />,
    );
    const nameInput = await screen.findByLabelText("名称");
    await waitFor(() => {
      expect((nameInput as HTMLInputElement).value).toBe("task-a-copy");
    });
  });

  it("applies strategy definition values from create initial values in create mode", async () => {
    const { container } = render(
      <CreateAgentCard
        onCreated={vi.fn()}
        createInitialValues={{
          name: "graph-a-copy",
          mode: "paper",
          strategy_definition_id: "sd-main",
          strategy_parameter_overrides: JSON.stringify({ lookback: 20 }, null, 2),
          strategy_execution_profile: "default",
        }}
      />,
    );
    const nameInput = await screen.findByLabelText("名称");
    await waitFor(() => {
      expect((nameInput as HTMLInputElement).value).toBe("graph-a-copy");
    });
    const definitionSelect = await screen.findByLabelText("策略定义");
    await waitFor(() => {
      expect(definitionSelect).toBeInTheDocument();
    });

    const submitButton = container.querySelector('button[type="submit"]');
    expect(submitButton).not.toBeNull();
    fireEvent.click(submitButton!);

    await waitFor(() => {
      expect(createTask).toHaveBeenCalledTimes(1);
    });

    const payload = vi.mocked(createTask).mock.calls[0]?.[0] as Record<string, unknown>;
    const settings = payload.settings as Record<string, unknown>;
    expect(settings.strategy).toBeDefined();
    expect(settings).toMatchObject({
      strategy: {
        definition_id: "sd-main",
        parameter_overrides: { lookback: 20 },
        execution_profile: "default",
      },
    });
  });

  it("renders definition-first form and submits strategy binding payload", async () => {
    const { container } = render(
      <CreateAgentCard
        onCreated={vi.fn()}
        createInitialValues={{
          name: "agent-task",
          mode: "paper",
          strategy_definition_id: "sd-default",
        }}
      />,
    );
    const nameInput = await screen.findByLabelText("名称");
    await waitFor(() => {
      expect((nameInput as HTMLInputElement).value).toBe("agent-task");
    });

    await screen.findByLabelText("策略定义");
    await screen.findByLabelText("单标的仓位占比上限");
    await screen.findByLabelText("人工审批金额门槛");
    await screen.findByLabelText("审批超时（秒）");

    const submitButton = container.querySelector('button[type="submit"]');
    expect(submitButton).not.toBeNull();
    fireEvent.click(submitButton!);

    await waitFor(() => {
      expect(createTask).toHaveBeenCalledTimes(1);
    });

    const payload = vi.mocked(createTask).mock.calls[0]?.[0] as Record<string, unknown>;
    const settings = payload.settings as Record<string, unknown>;
    expect(settings.strategy).toBeDefined();
    expect(settings.agent).toBeDefined();
  });

  it("submits the visible approval and position form fields into settings.agent", async () => {
    const { container } = render(
      <CreateAgentCard
        onCreated={vi.fn()}
        createInitialValues={{
          name: "agent-thresholds",
          mode: "live",
          strategy_definition_id: "sd-default",
        }}
      />,
    );

    fireEvent.change(await screen.findByLabelText("单标的仓位占比上限"), { target: { value: "0.55" } });
    fireEvent.change(screen.getByLabelText("人工审批金额门槛"), { target: { value: "2500" } });
    fireEvent.change(screen.getByLabelText("审批超时（秒）"), { target: { value: "900" } });

    const submitButton = container.querySelector('button[type="submit"]');
    expect(submitButton).not.toBeNull();
    fireEvent.click(submitButton!);

    await waitFor(() => {
      expect(createTask).toHaveBeenCalledTimes(1);
    });

    const payload = vi.mocked(createTask).mock.calls[0]?.[0] as Record<string, unknown>;
    const settings = payload.settings as Record<string, unknown>;
    const agent = settings.agent as Record<string, unknown>;
    const positionConstraints = agent.position_constraints as Record<string, unknown>;
    const approval = agent.approval as Record<string, unknown>;

    expect(positionConstraints.max_position_ratio).toBe(0.55);
    expect(approval.min_notional_for_approval).toBe(2500);
    expect(approval.timeout_seconds).toBe(900);
  });

  it("preserves custom settings from JSON modal on submit", async () => {
    const { container } = render(<CreateAgentCard onCreated={vi.fn()} />);
    const nameInput = await screen.findByLabelText("名称");
    fireEvent.change(nameInput, { target: { value: "json-merge-task" } });
    fireEvent.mouseDown(screen.getByLabelText("策略定义"));
    fireEvent.click(await screen.findByText("Main Definition (sd-main)"));

    fireEvent.click(screen.getByRole("button", { name: "编辑 Settings JSON" }));
    fireEvent.click(await screen.findByRole("button", { name: "apply-settings-json" }));

    const submitButton = container.querySelector('button[type="submit"]');
    expect(submitButton).not.toBeNull();
    fireEvent.click(submitButton!);

    await waitFor(() => {
      expect(createTask).toHaveBeenCalledTimes(1);
    });

    const payload = vi.mocked(createTask).mock.calls[0]?.[0] as Record<string, unknown>;
    const settings = payload.settings as Record<string, unknown>;
    expect(settings.custom_from_json_modal).toBe(true);
    const agent = settings.agent as Record<string, unknown>;
    expect(agent.custom_agent_value).toBe("json-kept");
    expect(agent).not.toHaveProperty("react_max_turns");
    expect(agent).not.toHaveProperty("signal_tool_names");
    expect(agent).not.toHaveProperty("enabled_skills");
    // This flow (definition pick + JSON modal + submit) renders the heavy antd
    // form repeatedly and sits right at the default 5s budget in jsdom; give it
    // explicit headroom so slower machines don't flake.
  }, 15000);

  it("omits settings.data_cache from the create payload when the data_cache fields are untouched", async () => {
    const { container } = render(<CreateAgentCard onCreated={vi.fn()} />);
    const nameInput = await screen.findByLabelText("名称");
    fireEvent.change(nameInput, { target: { value: "no-data-cache" } });
    fireEvent.mouseDown(screen.getByLabelText("策略定义"));
    fireEvent.click(await screen.findByText("Main Definition (sd-main)"));
    const submitButton = container.querySelector('button[type="submit"]');
    expect(submitButton).not.toBeNull();
    fireEvent.click(submitButton!);

    await waitFor(() => {
      expect(createTask).toHaveBeenCalledTimes(1);
    });

    const payload = vi.mocked(createTask).mock.calls[0]?.[0] as Record<string, unknown>;
    const settings = payload.settings as Record<string, unknown>;
    // Omitted = backend defaults. We must not submit a (possibly empty)
    // data_cache object that would override that behavior.
    expect(settings).not.toHaveProperty("data_cache");
  });

  it("writes the chosen data_cache fields into settings.data_cache on create", async () => {
    const { container } = render(<CreateAgentCard onCreated={vi.fn()} />);
    const nameInput = await screen.findByLabelText("名称");
    fireEvent.change(nameInput, { target: { value: "with-data-cache" } });
    fireEvent.mouseDown(screen.getByLabelText("策略定义"));
    fireEvent.click(await screen.findByText("Main Definition (sd-main)"));

    // continuity.on_unverifiable_gap = degrade.
    fireEvent.mouseDown(screen.getByLabelText("不可验证缺口（on_unverifiable_gap）"));
    fireEvent.click(await screen.findByText("degrade（无法证明时降级放行）"));

    // source_priority: pick QMT then Mock (selection order = priority order).
    fireEvent.mouseDown(screen.getByLabelText("回填数据源优先级"));
    fireEvent.click(await screen.findByText("QMT"));
    fireEvent.click(await screen.findByText("Mock"));

    // Toggle local_first on (an explicit boolean, distinct from the "unset"
    // state of the switches we never touch). A switch renders OFF by default,
    // so the first click sets it to true.
    const localFirstSwitch = container.querySelector("#data_cache_local_first");
    expect(localFirstSwitch).not.toBeNull();
    fireEvent.click(localFirstSwitch!);

    const submitButton = container.querySelector('button[type="submit"]');
    expect(submitButton).not.toBeNull();
    fireEvent.click(submitButton!);

    await waitFor(() => {
      expect(createTask).toHaveBeenCalledTimes(1);
    });

    const payload = vi.mocked(createTask).mock.calls[0]?.[0] as Record<string, unknown>;
    const settings = payload.settings as Record<string, unknown>;
    const dataCache = settings.data_cache as Record<string, unknown>;
    expect(dataCache).toBeDefined();
    expect(dataCache.source_priority).toEqual(["qmt", "mock"]);
    expect(dataCache.local_first).toBe(true);
    // Untouched switches stay unset (omitted), preserving backend defaults.
    expect(dataCache).not.toHaveProperty("auto_backfill");
    expect(dataCache.continuity).toEqual({ on_unverifiable_gap: "degrade" });
  });

  it("prefills the data_cache fields in edit mode and round-trips them on save", async () => {
    const { container } = render(
      <CreateAgentCard
        onCreated={vi.fn()}
        mode="edit"
        editTask={{
          ...baseTaskStatus,
          settings: {
            strategy: { definition_id: "sd-main", parameter_overrides: {}, execution_profile: "default" },
            data_cache: {
              source_priority: ["baostock", "akshare"],
              auto_backfill: false,
              continuity: { on_unverifiable_gap: "fail" },
            },
          },
        }}
      />,
    );
    const nameInput = await screen.findByLabelText("名称");
    fireEvent.change(nameInput, { target: { value: "edit-data-cache" } });
    const submitButton = container.querySelector('button[type="submit"]');
    expect(submitButton).not.toBeNull();
    fireEvent.click(submitButton!);

    await waitFor(() => {
      expect(updateTask).toHaveBeenCalledTimes(1);
    });

    const payload = vi.mocked(updateTask).mock.calls[0]?.[1] as Record<string, unknown>;
    const settings = payload.settings as Record<string, unknown>;
    const dataCache = settings.data_cache as Record<string, unknown>;
    expect(dataCache).toBeDefined();
    expect(dataCache.source_priority).toEqual(["baostock", "akshare"]);
    expect(dataCache.auto_backfill).toBe(false);
    expect(dataCache.continuity).toEqual({ on_unverifiable_gap: "fail" });
    // Fields the original settings omitted must not be fabricated on save.
    expect(dataCache).not.toHaveProperty("local_first");
  });
});

describe("CreateAgentCard run-mode restriction", () => {
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
    vi.mocked(listDataProviders).mockResolvedValue({ providers: ["mock"] });
    vi.mocked(listAccounts).mockResolvedValue({ items: [] });
    vi.mocked(listStrategyDefinitions).mockResolvedValue({ items: [] });
    vi.mocked(createTask).mockResolvedValue(baseTaskStatus);
  });

  afterEach(() => {
    cleanup();
  });

  it("offers only the trading modes when allowedModes excludes backtest", async () => {
    render(<CreateAgentCard onCreated={vi.fn()} allowedModes={["paper", "live"]} />);
    await screen.findByLabelText("名称");

    fireEvent.mouseDown(screen.getByLabelText("运行模式"));
    const labels = (await screen.findAllByRole("option")).map((option) => option.getAttribute("aria-label"));
    expect(labels).toContain("模拟盘");
    expect(labels).toContain("实盘");
    expect(labels).not.toContain("回测");
  });

  it("locks the selector to backtest on the backtest tab", async () => {
    render(
      <CreateAgentCard
        onCreated={vi.fn()}
        allowedModes={["backtest"]}
        createInitialValues={{ mode: "backtest" }}
      />,
    );
    await screen.findByLabelText("名称");

    // The backtest-only field is shown (mode resolved to backtest)...
    expect(screen.getByText("回测区间")).toBeInTheDocument();
    // ...and the run-mode selector is locked so you cannot switch away.
    expect(document.getElementById("mode")).toBeDisabled();
  });

  it("does not offer backtest in edit mode for trading tasks", async () => {
    render(
      <CreateAgentCard
        onCreated={vi.fn()}
        mode="edit"
        editTask={{
          ...baseTaskStatus,
          mode: "paper",
          settings: {
            strategy: { definition_id: "sd-main", parameter_overrides: {}, execution_profile: "default" },
          },
        }}
      />,
    );
    await screen.findByLabelText("名称");

    fireEvent.mouseDown(screen.getByLabelText("运行模式"));
    const labels = (await screen.findAllByRole("option")).map((option) => option.getAttribute("aria-label"));
    expect(labels).toContain("模拟盘");
    expect(labels).toContain("实盘");
    expect(labels).not.toContain("回测");
  });
});
