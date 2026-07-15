import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AgentCard } from "./AgentCard";
import { ApiError, deleteAssistantAgent } from "../api";
import type { Agent } from "../types";

// Keep the real ApiError class so the component's `instanceof ApiError` check
// works; only stub the network-touching functions.
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    deleteAssistantAgent: vi.fn(),
    cloneAssistantAgent: vi.fn(),
  };
});

const deleteMock = vi.mocked(deleteAssistantAgent);

const agent: Agent = {
  id: "agent-1",
  name: "Test Agent",
  status: "active",
  system_prompt: "hi",
  model_route_name: "",
  tool_names: [],
  skill_names: [],
  max_turns: 6,
  context_compaction: {} as Agent["context_compaction"],
  is_default: false,
  is_builtin: false,
  created_at: "2026-05-30T00:00:00",
  updated_at: "2026-05-30T00:00:00",
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AgentCard delete", () => {
  beforeEach(() => {
    vi.spyOn(window, "confirm").mockReset();
  });

  function renderCard(onDeleted = vi.fn()) {
    render(
      <AgentCard
        agent={agent}
        onEdit={vi.fn()}
        onDeleted={onDeleted}
        onCloneSuccess={vi.fn()}
      />,
    );
    return onDeleted;
  }

  it("deletes directly when the agent has no sessions", async () => {
    deleteMock.mockResolvedValueOnce(undefined);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const onDeleted = renderCard();

    fireEvent.click(screen.getByText("删除"));

    await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1));
    expect(deleteMock).toHaveBeenCalledTimes(1);
    expect(deleteMock).toHaveBeenCalledWith(agent.id);
  });

  it("offers a cascade delete on a 409 and retries with force when confirmed", async () => {
    deleteMock
      .mockRejectedValueOnce(new ApiError("agent agent-1 still has 1 assistant session(s)", 409))
      .mockResolvedValueOnce(undefined);
    // First confirm: the initial delete prompt. Second confirm: the cascade prompt.
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const onDeleted = renderCard();

    fireEvent.click(screen.getByText("删除"));

    await waitFor(() => expect(deleteMock).toHaveBeenCalledTimes(2));
    expect(deleteMock).toHaveBeenNthCalledWith(1, agent.id);
    expect(deleteMock).toHaveBeenNthCalledWith(2, agent.id, { force: true });
    expect(onDeleted).toHaveBeenCalledTimes(1);
  });

  it("does not force delete when the cascade prompt is declined", async () => {
    deleteMock.mockRejectedValueOnce(
      new ApiError("agent agent-1 still has 1 assistant session(s)", 409),
    );
    // First confirm (initial prompt) → true; second confirm (cascade) → false.
    vi.spyOn(window, "confirm").mockReturnValueOnce(true).mockReturnValueOnce(false);
    const onDeleted = renderCard();

    fireEvent.click(screen.getByText("删除"));

    await waitFor(() => expect(deleteMock).toHaveBeenCalledTimes(1));
    expect(onDeleted).not.toHaveBeenCalled();
  });

  it("surfaces a non-409 error and does not retry", async () => {
    // Non-409 (e.g. 400) must not cascade. We assert the behavioral invariant
    // (single delete attempt, onDeleted never called) rather than a blocking
    // native alert, which now renders as an antd message.
    deleteMock.mockRejectedValueOnce(new ApiError("Cannot delete default agent", 400));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const onDeleted = renderCard();

    fireEvent.click(screen.getByText("删除"));

    await waitFor(() => expect(deleteMock).toHaveBeenCalledTimes(1));
    expect(onDeleted).not.toHaveBeenCalled();
  });

  it("shows the builtin badge and hides Delete for the fixed main agent", () => {
    render(
      <AgentCard
        agent={{ ...agent, is_builtin: true }}
        onEdit={vi.fn()}
        onDeleted={vi.fn()}
        onCloneSuccess={vi.fn()}
      />,
    );

    expect(screen.getByText("固定主智能体")).toBeInTheDocument();
    expect(screen.queryByText("删除")).not.toBeInTheDocument();
    // Edit and Clone stay available even for the builtin agent.
    expect(screen.getByText("编辑")).toBeInTheDocument();
    expect(screen.getByText("克隆")).toBeInTheDocument();
  });
});
