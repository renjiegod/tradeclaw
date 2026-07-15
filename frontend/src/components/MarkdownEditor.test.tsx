import { render, screen, within, waitFor, act, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fireEvent } from "@testing-library/dom";
import { describe, expect, it, vi, afterEach } from "vitest";
import { MarkdownEditor } from "./MarkdownEditor";

afterEach(() => {
  cleanup();
  // CodeMirror may append elements directly to document.body - clean those up
  const bodies = document.body.querySelectorAll(":scope > div");
  bodies.forEach((div) => {
    const hasCmEditor = div.querySelector(".cm-editor");
    const hasAntSpace = div.querySelector(".ant-space");
    // If it's a root div with CodeMirror or Ant Space that wasn't cleaned up
    if (hasCmEditor || hasAntSpace) {
      div.remove();
    }
  });
});

describe("MarkdownEditor", () => {
  it("renders CodeMirror editor in edit mode by default", () => {
    render(<MarkdownEditor value="# Hello" onChange={vi.fn()} />);
    expect(document.querySelector(".cm-editor")).toBeInTheDocument();
  });

  it("switches to preview mode when Preview tab is clicked", async () => {
    const user = userEvent.setup();
    render(<MarkdownEditor value="**bold**" onChange={vi.fn()} />);
    await user.click(screen.getAllByRole("button", { name: "Preview" })[0]);
    expect(screen.getByText("bold")).toBeInTheDocument();
  });

  it("switches back to edit mode when Edit tab is clicked", async () => {
    const user = userEvent.setup();
    render(<MarkdownEditor value="# Hello" onChange={vi.fn()} />);
    await user.click(screen.getAllByRole("button", { name: "Preview" })[0]);
    await user.click(screen.getAllByRole("button", { name: "Edit" })[0]);
    expect(document.querySelector(".cm-editor")).toBeInTheDocument();
  });

  it("shows placeholder when previewing empty content", async () => {
    render(<MarkdownEditor value="" onChange={vi.fn()} />);
    const previewButton = screen.getAllByRole("button", { name: "Preview" })[0];
    fireEvent.click(previewButton);
    await waitFor(() => {
      expect(screen.getByText("Nothing to preview")).toBeInTheDocument();
    });
  });

  it("calls onChange when CodeMirror content changes", async () => {
    const onChange = vi.fn();
    render(<MarkdownEditor value="old" onChange={onChange} />);
    const contenteditable = document.querySelector('[role="textbox"]') as HTMLElement;
    await act(async () => {
      fireEvent.input(contenteditable, { target: { textContent: "new content" } });
      fireEvent(
        contenteditable,
        new InputEvent("input", { bubbles: true, cancelable: true, data: "new content" })
      );
    });
    expect(onChange).toHaveBeenCalled();
  });
});
