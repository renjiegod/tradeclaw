import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { Modal } from "antd";
import type { ReactNode } from "react";

import { StrategiesPage } from "./StrategiesPage";
import {
  deleteStrategyDefinition,
  deleteStrategyDefinitions,
  getStrategyDefinition,
  listStrategyDefinitions,
  updateStrategyDefinition,
} from "../api";

vi.mock("../api", () => ({
  deleteStrategyDefinition: vi.fn(),
  deleteStrategyDefinitions: vi.fn(),
  getStrategyDefinition: vi.fn(),
  listStrategyDefinitions: vi.fn(),
  compileStrategyDefinition: vi.fn(),
  updateStrategyDefinition: vi.fn(),
}));

vi.mock("../components/StrategyFileTree", () => ({
  StrategyFileTree: ({ files }: { files: { path: string; content: string | null }[] }) => (
    <pre>{files.map((f) => f.path).join(",")}</pre>
  ),
}));

vi.mock("../components/JsonCodeBlock", () => ({
  JsonCodeBlock: ({ value }: { value: unknown }) => <pre>{JSON.stringify(value)}</pre>,
}));

vi.mock("../components/PageIntro", () => ({
  PageIntro: ({ title, extra }: { title: string; extra?: ReactNode }) => (
    <div>
      <h1>{title}</h1>
      {extra}
    </div>
  ),
}));

vi.mock("../pageRefreshContext", () => ({
  usePageRefreshToken: () => 0,
}));

const baseDefinition = {
  definition_id: "def-1",
  name: "Definition 1",
  class_name: "MomentumStrategy",
  current_version: "v0001-abc123",
  api_version: "v1",
  parameter_schema: {},
  default_parameters: {},
  capabilities: {},
  provenance: {},
  code_hash: "hash-1",
  status: "active",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

describe("StrategiesPage", () => {
  let modalConfirmSpy: ReturnType<typeof vi.spyOn>;

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
    modalConfirmSpy = vi.spyOn(Modal, "confirm").mockImplementation((config) => {
      void config.onOk?.();
      return {
        destroy: vi.fn(),
        update: vi.fn(),
      } as never;
    });
    vi.mocked(deleteStrategyDefinition).mockResolvedValue(undefined);
    vi.mocked(deleteStrategyDefinitions).mockResolvedValue(undefined);
    vi.mocked(updateStrategyDefinition).mockResolvedValue({
      ...baseDefinition,
      name: "Renamed Definition",
      input_contract: {},
      generation_prompt: "",
      generation_model: "",
      generation_metadata: {},
      files: [{ path: "strategy.py", content: "class MomentumStrategy: pass" }],
    });
    vi.mocked(getStrategyDefinition).mockResolvedValue({
      ...baseDefinition,
      input_contract: {},
      generation_prompt: "",
      generation_model: "",
      generation_metadata: {},
      files: [{ path: "strategy.py", content: "class MomentumStrategy: pass" }],
    });
    vi.mocked(listStrategyDefinitions).mockResolvedValue({
      items: [baseDefinition],
    });
  });

  afterEach(() => {
    modalConfirmSpy.mockRestore();
    cleanup();
  });

  it("deletes a single strategy definition and refreshes the list", async () => {
    render(<StrategiesPage />);

    await waitFor(() => {
      expect(listStrategyDefinitions).toHaveBeenCalled();
    });

    fireEvent.click(screen.getAllByRole("button", { name: /删\s*除/ })[1]!);

    await waitFor(() => {
      expect(deleteStrategyDefinition).toHaveBeenCalledWith("def-1");
      expect(listStrategyDefinitions).toHaveBeenCalledTimes(2);
    });
  });

  it("renames a strategy definition and refreshes the list", async () => {
    render(<StrategiesPage />);

    await waitFor(() => {
      expect(listStrategyDefinitions).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole("button", { name: "重命名" }));

    const input = await screen.findByPlaceholderText("输入新的策略定义名称");
    fireEvent.change(input, { target: { value: "Renamed Definition" } });
    fireEvent.click(screen.getByRole("button", { name: "保 存" }));

    await waitFor(() => {
      expect(updateStrategyDefinition).toHaveBeenCalledWith("def-1", { name: "Renamed Definition" });
      expect(listStrategyDefinitions).toHaveBeenCalledTimes(2);
    });
  });

  it("rejects an empty rename and does not call the api", async () => {
    render(<StrategiesPage />);

    await waitFor(() => {
      expect(listStrategyDefinitions).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByRole("button", { name: "重命名" }));

    const input = await screen.findByPlaceholderText("输入新的策略定义名称");
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "保 存" }));

    await waitFor(() => {
      expect(updateStrategyDefinition).not.toHaveBeenCalled();
    });
  });

  it("deletes selected strategy definitions in bulk and refreshes the list", async () => {
    vi.mocked(listStrategyDefinitions).mockResolvedValueOnce({
      items: [
        baseDefinition,
        { ...baseDefinition, definition_id: "def-2", name: "Definition 2" },
      ],
    });

    render(<StrategiesPage />);

    await waitFor(() => {
      expect(listStrategyDefinitions).toHaveBeenCalled();
    });

    fireEvent.click(screen.getByLabelText("Select all"));
    fireEvent.click(screen.getByRole("button", { name: "删除选中" }));

    await waitFor(() => {
      expect(deleteStrategyDefinitions).toHaveBeenCalledWith(["def-1", "def-2"]);
      expect(listStrategyDefinitions).toHaveBeenCalledTimes(2);
    });
  });
});
