import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import SkillFrontmatterForm from "./SkillFrontmatterForm";

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

describe("SkillFrontmatterForm", () => {
  it("calls onSave with edited fields", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(
      <SkillFrontmatterForm
        value={{ name: "alpha", description: "old", license: null }}
        onSave={onSave}
      />
    );
    const desc = screen.getByLabelText(/description/i) as HTMLTextAreaElement;
    fireEvent.change(desc, { target: { value: "new desc" } });
    fireEvent.click(screen.getByText(/save frontmatter|保存 frontmatter/i));
    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({ description: "new desc" })
      )
    );
  });
});
