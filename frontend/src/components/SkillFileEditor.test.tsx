import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import SkillFileEditor from "./SkillFileEditor";

beforeEach(() => {
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

const baseFile = {
  path: "SKILL.md",
  content: "---\nname: x\ndescription: y\n---\n\n# Body\n",
  encoding: "utf-8" as const,
  size: 10,
  mtime: "2026-05-12T00:00:00+00:00",
  mime: "text/markdown",
};

describe("SkillFileEditor", () => {
  it("starts in preview for markdown and shows body", () => {
    render(<SkillFileEditor file={baseFile} onSave={vi.fn()} onRefresh={vi.fn()} />);
    expect(screen.getByText("Body")).toBeInTheDocument();
  });

  it("save is disabled when not dirty", () => {
    render(<SkillFileEditor file={baseFile} onSave={vi.fn()} onRefresh={vi.fn()} />);
    const btns = screen.getAllByRole("button", { name: /保.?存/ });
    expect(btns[0]).toBeDisabled();
  });
});
