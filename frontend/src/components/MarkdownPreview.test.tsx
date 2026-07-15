import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import MarkdownPreview from "./MarkdownPreview";

describe("MarkdownPreview", () => {
  it("renders markdown body", () => {
    render(<MarkdownPreview source={"# Title\n\nHello"} />);
    expect(screen.getByText("Title")).toBeInTheDocument();
    expect(screen.getByText("Hello")).toBeInTheDocument();
  });

  it("strips YAML frontmatter before rendering", () => {
    const src = "---\nname: x\n---\n\n# Body\n";
    render(<MarkdownPreview source={src} stripFrontmatter />);
    expect(screen.getByText("Body")).toBeInTheDocument();
    expect(screen.queryByText("name: x")).toBeNull();
  });
});
