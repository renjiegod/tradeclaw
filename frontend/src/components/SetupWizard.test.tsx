import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SETUP_WIZARD_SKIPPED_KEY, SetupWizard } from "./SetupWizard";
import { completeSetup, getSetupProviders } from "../api";
import type { SetupProvider } from "../types";

vi.mock("../api", () => ({
  completeSetup: vi.fn(),
  getSetupProviders: vi.fn(),
}));

const PROVIDERS: SetupProvider[] = [
  {
    label: "DeepSeek",
    provider_kind: "openai_compatible",
    base_url: "https://api.deepseek.com",
    model_hint: "deepseek-chat",
    needs_key: true,
  },
  {
    label: "Ollama（本地）",
    provider_kind: "openai_compatible",
    base_url: "http://localhost:11434/v1",
    model_hint: "llama3.2",
    needs_key: false,
  },
];

describe("SetupWizard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
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
    vi.mocked(getSetupProviders).mockResolvedValue({ items: PROVIDERS });
  });

  afterEach(() => cleanup());

  it("renders the provider form (unconfigured state) with the fetched preset catalog", async () => {
    render(<SetupWizard onCompleted={vi.fn()} onSkip={vi.fn()} />);

    await screen.findByLabelText("供应商");
    expect(screen.getByText("欢迎使用 DoYouTrade")).toBeInTheDocument();
    expect(getSetupProviders).toHaveBeenCalledTimes(1);

    // First preset's defaults are prefilled.
    await waitFor(() => {
      expect((document.getElementById("target_model") as HTMLInputElement).value).toBe(
        "deepseek-chat",
      );
    });
  });

  it("does not re-implement its own provider catalog — it renders exactly what the API returned", async () => {
    render(<SetupWizard onCompleted={vi.fn()} onSkip={vi.fn()} />);

    await screen.findByLabelText("供应商");
    fireEvent.mouseDown(screen.getByLabelText("供应商"));
    const labels = (await screen.findAllByRole("option")).map((option) =>
      option.getAttribute("aria-label"),
    );
    expect(labels).toEqual(PROVIDERS.map((p) => p.label));
  });

  it("calls onCompleted (and the overlay is dismissed by the caller) after a successful submit", async () => {
    vi.mocked(completeSetup).mockResolvedValue({
      id: "mr-1",
      route_name: "default",
      provider_kind: "openai_compatible",
      base_url: "https://api.deepseek.com",
      api_key_masked: "****abcd",
      target_model: "deepseek-chat",
      settings: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    });
    const onCompleted = vi.fn();
    const { container } = render(<SetupWizard onCompleted={onCompleted} onSkip={vi.fn()} />);

    await screen.findByLabelText("供应商");
    fireEvent.change(screen.getByLabelText("API Key"), { target: { value: "sk-test" } });

    const submitButton = container.querySelector('button[type="submit"]');
    expect(submitButton).not.toBeNull();
    fireEvent.click(submitButton!);

    await waitFor(() => {
      expect(completeSetup).toHaveBeenCalledTimes(1);
    });
    expect(completeSetup).toHaveBeenCalledWith(
      expect.objectContaining({
        provider_kind: "openai_compatible",
        api_key: "sk-test",
        target_model: "deepseek-chat",
      }),
    );
    await waitFor(() => {
      expect(onCompleted).toHaveBeenCalledTimes(1);
    });
  });

  it("blocks submit with a validation error when the selected provider needs a key and none was given", async () => {
    const { container } = render(<SetupWizard onCompleted={vi.fn()} onSkip={vi.fn()} />);

    await screen.findByLabelText("供应商");
    const submitButton = container.querySelector('button[type="submit"]');
    expect(submitButton).not.toBeNull();
    fireEvent.click(submitButton!);

    await screen.findByText("该供应商需要填写 API Key");
    expect(completeSetup).not.toHaveBeenCalled();
  });

  it("allows an empty API key for a local provider that does not need one", async () => {
    vi.mocked(completeSetup).mockResolvedValue({
      id: "mr-2",
      route_name: "default",
      provider_kind: "openai_compatible",
      base_url: "http://localhost:11434/v1",
      api_key_masked: "",
      target_model: "llama3.2",
      settings: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    });
    const onCompleted = vi.fn();
    const { container } = render(<SetupWizard onCompleted={onCompleted} onSkip={vi.fn()} />);

    await screen.findByLabelText("供应商");
    fireEvent.mouseDown(screen.getByLabelText("供应商"));
    fireEvent.click(await screen.findByText("Ollama（本地）"));

    const submitButton = container.querySelector('button[type="submit"]');
    fireEvent.click(submitButton!);

    await waitFor(() => {
      expect(completeSetup).toHaveBeenCalledTimes(1);
    });
    expect(onCompleted).toHaveBeenCalledTimes(1);
  });

  it("surfaces a submit error inline and does not call onCompleted", async () => {
    vi.mocked(completeSetup).mockRejectedValue(new Error("route_name conflict"));
    const onCompleted = vi.fn();
    const { container } = render(<SetupWizard onCompleted={onCompleted} onSkip={vi.fn()} />);

    await screen.findByLabelText("供应商");
    fireEvent.change(screen.getByLabelText("API Key"), { target: { value: "sk-test" } });
    const submitButton = container.querySelector('button[type="submit"]');
    fireEvent.click(submitButton!);

    await screen.findByText("route_name conflict");
    expect(onCompleted).not.toHaveBeenCalled();
  });

  it("calls onSkip and sets the localStorage skip flag when 'skip' is clicked", async () => {
    const onSkip = vi.fn();
    render(<SetupWizard onCompleted={vi.fn()} onSkip={onSkip} />);

    await screen.findByLabelText("供应商");
    fireEvent.click(screen.getByRole("button", { name: "跳过，稍后在设置里配置" }));

    expect(onSkip).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem(SETUP_WIZARD_SKIPPED_KEY)).toBe("1");
    expect(completeSetup).not.toHaveBeenCalled();
  });

  it("shows a load error and still offers a skip button when providers fail to load", async () => {
    vi.mocked(getSetupProviders).mockRejectedValue(new Error("network down"));
    const onSkip = vi.fn();
    render(<SetupWizard onCompleted={vi.fn()} onSkip={onSkip} />);

    await screen.findByText("network down");
    fireEvent.click(screen.getByRole("button", { name: "跳过，稍后在设置里配置" }));
    expect(onSkip).toHaveBeenCalledTimes(1);
  });
});
