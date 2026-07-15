import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChannelsPage } from "./ChannelsPage";
import {
  copyAssistantChannelSecret,
  createAssistantChannel,
  deleteAssistantChannel,
  listAssistantAgents,
  listAssistantChannels,
  updateAssistantChannel,
} from "../api";

vi.mock("../api", () => ({
  copyAssistantChannelSecret: vi.fn(),
  createAssistantChannel: vi.fn(),
  deleteAssistantChannel: vi.fn(),
  listAssistantAgents: vi.fn(),
  listAssistantChannels: vi.fn(),
  updateAssistantChannel: vi.fn(),
}));

const agent = {
  id: "agent-alpha",
  name: "Alpha",
  status: "active",
  system_prompt: "hi",
  model_route_name: "",
  tool_names: [],
  skill_names: [],
  max_turns: 6,
  is_default: false,
  is_builtin: false,
  created_at: "2026-04-30T00:00:00",
  updated_at: "2026-04-30T00:00:00",
};

const channel = {
  id: "channel-alpha",
  name: "Feishu Alpha",
  type: "feishu",
  enabled: true,
  agent_id: "agent-alpha",
  status: "connected",
  last_error: "",
  last_connected_at: "2026-04-30T00:00:00",
  config: { app_id: "cli_alpha", domain: "feishu" },
  secret_keys: ["app_secret"],
  created_at: "2026-04-30T00:00:00",
  updated_at: "2026-04-30T00:00:00",
};

describe("ChannelsPage", () => {
  afterEach(() => {
    cleanup();
  });

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
    vi.mocked(listAssistantAgents).mockResolvedValue({ items: [agent], total: 1 });
    vi.mocked(listAssistantChannels).mockResolvedValue({ items: [channel], total: 1 });
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
  });

  it("renders channels without plaintext secrets", async () => {
    render(<ChannelsPage />);

    expect(await screen.findByText("Feishu Alpha")).toBeInTheDocument();
    expect(screen.getByText("connected")).toBeInTheDocument();
    expect(screen.queryByText("secret-alpha")).not.toBeInTheDocument();
  });

  it("creates a Feishu channel", async () => {
    vi.mocked(createAssistantChannel).mockResolvedValue(channel);
    render(<ChannelsPage />);

    fireEvent.click(await screen.findByRole("button", { name: /New Channel/i }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Feishu Beta" } });
    fireEvent.change(screen.getByLabelText("App ID"), { target: { value: "cli_beta" } });
    fireEvent.change(screen.getByLabelText("App Secret"), { target: { value: "secret-beta" } });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    await waitFor(() => {
      expect(createAssistantChannel).toHaveBeenCalledWith(
        expect.objectContaining({
          name: "Feishu Beta",
          type: "feishu",
          agent_id: "agent-alpha",
          config: expect.objectContaining({ app_id: "cli_beta", domain: "feishu" }),
          secrets: expect.objectContaining({ app_secret: "secret-beta" }),
        }),
      );
    });
  });

  it("updates a channel without sending blank secrets", async () => {
    vi.mocked(updateAssistantChannel).mockResolvedValue(channel);
    render(<ChannelsPage />);

    fireEvent.click(await screen.findByRole("button", { name: /Edit Feishu Alpha/i }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Feishu Alpha 2" } });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));

    await waitFor(() => {
      expect(updateAssistantChannel).toHaveBeenCalledWith(
        "channel-alpha",
        expect.objectContaining({
          name: "Feishu Alpha 2",
          secrets: {},
        }),
      );
    });
  });

  it("copies and deletes channels", async () => {
    vi.mocked(copyAssistantChannelSecret).mockResolvedValue({
      secret_key: "app_secret",
      value: "secret-alpha",
    });
    render(<ChannelsPage />);

    fireEvent.click(await screen.findByRole("button", { name: /Copy app_secret for Feishu Alpha/i }));
    await waitFor(() => {
      expect(copyAssistantChannelSecret).toHaveBeenCalledWith("channel-alpha", "app_secret");
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith("secret-alpha");
    });

    fireEvent.click(screen.getByRole("button", { name: /Delete Feishu Alpha/i }));
    await waitFor(() => {
      expect(deleteAssistantChannel).toHaveBeenCalledWith("channel-alpha");
    });
  });
});
