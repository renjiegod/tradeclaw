import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { KnowledgeJournalsPanel } from "./KnowledgeJournalsPanel";
import type { KnowledgeJournal, KnowledgeJournalList } from "../types";
import { getKnowledgeJournal, listKnowledgeJournals } from "../api";

// KnowledgeJournalsPanel only imports these two functions from ../api; include
// both so the mocked module satisfies every import the component makes.
vi.mock("../api", () => ({
  listKnowledgeJournals: vi.fn(),
  getKnowledgeJournal: vi.fn(),
}));

const list: KnowledgeJournalList = {
  root_exists: true,
  items: [
    { path: "2026/2026-05-30.md", title: "2026-05-30", size: 200, mtime: "2026-05-30T01:00:00Z" },
    { path: "2026/2026-05-29.md", title: "2026-05-29", size: 120, mtime: "2026-05-29T01:00:00Z" },
  ],
};

const newest: KnowledgeJournal = {
  path: "2026/2026-05-30.md",
  title: "2026-05-30",
  content: "---\ndate: 2026-05-30\n---\n\n# 五月三十复盘\n\n今天追了首板。",
  size: 200,
  mtime: "2026-05-30T01:00:00Z",
};

const older: KnowledgeJournal = {
  path: "2026/2026-05-29.md",
  title: "2026-05-29",
  content: "# 五月二十九复盘\n\n空仓观望。",
  size: 120,
  mtime: "2026-05-29T01:00:00Z",
};

describe("KnowledgeJournalsPanel", () => {
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
  });

  afterEach(cleanup);

  it("renders the journal list and auto-loads the newest entry's markdown", async () => {
    vi.mocked(listKnowledgeJournals).mockResolvedValue(list);
    vi.mocked(getKnowledgeJournal).mockResolvedValue(newest);

    render(<KnowledgeJournalsPanel />);

    // Both list entries appear (newest-first).
    expect(await screen.findByText("2026-05-30")).toBeInTheDocument();
    expect(screen.getByText("2026-05-29")).toBeInTheDocument();

    // Auto-selected the newest (first) entry and rendered its markdown.
    await waitFor(() => {
      expect(getKnowledgeJournal).toHaveBeenCalledWith("2026/2026-05-30.md");
    });
    expect(await screen.findByText("五月三十复盘")).toBeInTheDocument();
    // Frontmatter is stripped, so the raw YAML key never renders.
    expect(screen.queryByText(/date: 2026-05-30/)).not.toBeInTheDocument();
  });

  it("shows the empty hint when root_exists is false / no items", async () => {
    vi.mocked(listKnowledgeJournals).mockResolvedValue({ items: [], root_exists: false });

    render(<KnowledgeJournalsPanel />);

    expect(
      await screen.findByText("还没有复盘日记 —— 在对话里让 agent 记一次复盘即可"),
    ).toBeInTheDocument();
    // No content fetch should fire when the list is empty.
    expect(getKnowledgeJournal).not.toHaveBeenCalled();
  });

  it("loads an entry's content when clicked", async () => {
    vi.mocked(listKnowledgeJournals).mockResolvedValue(list);
    vi.mocked(getKnowledgeJournal).mockImplementation(async (path: string) =>
      path === older.path ? older : newest,
    );

    render(<KnowledgeJournalsPanel />);

    // Wait for auto-load of the newest entry first.
    expect(await screen.findByText("五月三十复盘")).toBeInTheDocument();

    // Click the older entry → fetch + render its content.
    fireEvent.click(screen.getByText("2026-05-29"));

    await waitFor(() => {
      expect(getKnowledgeJournal).toHaveBeenCalledWith("2026/2026-05-29.md");
    });
    expect(await screen.findByText("五月二十九复盘")).toBeInTheDocument();
  });
});
