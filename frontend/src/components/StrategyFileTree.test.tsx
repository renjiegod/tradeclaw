import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { StrategyFileTree } from "./StrategyFileTree";
import type { StrategyDefinitionFile } from "../types";

vi.mock("./CodeBlock", () => ({
  CodeBlock: ({ code }: { code: string }) => <pre data-testid="code-block">{code}</pre>,
}));

const files: StrategyDefinitionFile[] = [
  { path: "strategy.py", content: "class Strategy: pass" },
  { path: "helpers.py", content: "def helper(): pass" },
];

describe("StrategyFileTree", () => {
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

  afterEach(() => {
    cleanup();
  });

  it("renders file list and defaults to strategy.py", () => {
    render(<StrategyFileTree files={files} />);
    expect(screen.getByText("strategy.py")).toBeTruthy();
    expect(screen.getByText("helpers.py")).toBeTruthy();
    expect(screen.getByTestId("code-block").textContent).toBe("class Strategy: pass");
  });

  it("shows helpers.py content when clicked", () => {
    render(<StrategyFileTree files={files} />);
    fireEvent.click(screen.getByText("helpers.py"));
    expect(screen.getByTestId("code-block").textContent).toBe("def helper(): pass");
  });

  it("shows placeholder for too-large file", () => {
    const large: StrategyDefinitionFile[] = [
      { path: "big.py", content: null, skipped_reason: "too_large", size_bytes: 300000 },
    ];
    render(<StrategyFileTree files={large} />);
    expect(screen.getByText(/文件过大/)).toBeTruthy();
    expect(screen.getByText(/300,000/)).toBeTruthy();
  });

  it("shows empty state when no files", () => {
    render(<StrategyFileTree files={[]} />);
    expect(screen.getByText(/暂无版本文件/)).toBeTruthy();
  });

  it("defaults to first file when strategy.py is absent", () => {
    const noMain: StrategyDefinitionFile[] = [
      { path: "indicators.py", content: "# indicators" },
    ];
    render(<StrategyFileTree files={noMain} />);
    expect(screen.getByTestId("code-block").textContent).toBe("# indicators");
  });
});
