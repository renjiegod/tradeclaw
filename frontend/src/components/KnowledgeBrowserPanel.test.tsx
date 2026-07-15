import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { KnowledgeBrowserPanel } from "./KnowledgeBrowserPanel";
import type { KnowledgeFile, KnowledgeIndex } from "../types";
import { getKnowledgeFile, getKnowledgeIndex } from "../api";

vi.mock("../api", () => ({
  getKnowledgeIndex: vi.fn(),
  getKnowledgeFile: vi.fn(),
}));

const index: KnowledgeIndex = {
  root_exists: true,
  total_files: 3,
  weak_title_count: 1,
  skipped_count: 0,
  weak_titles: ["cycles/2026-05/noheading.md"],
  generated_at: "2026-06-17T00:00:00Z",
  partitions: [
    {
      name: "cycles",
      label: "情绪周期 / 题材 / 龙头",
      file_count: 2,
      groups: [
        {
          name: "2026-05",
          entries: [
            { rel_path: "2026-05/_overview.md", title: "2026-05 周期总览", is_overview: true, weak: false, suffix: ".md" },
            { rel_path: "2026-05/noheading.md", title: "noheading", is_overview: false, weak: true, suffix: ".md" },
          ],
        },
      ],
    },
    {
      name: "trades",
      label: "个人交割单（券商导出）",
      file_count: 1,
      groups: [
        {
          name: "2026-05",
          entries: [
            { rel_path: "2026-05/raw.csv", title: "raw", is_overview: false, weak: false, suffix: ".csv" },
          ],
        },
      ],
    },
  ],
};

const markdownFile: KnowledgeFile = {
  partition: "cycles",
  path: "2026-05/_overview.md",
  title: "_overview",
  size: 80,
  mtime: "2026-05-30T01:00:00Z",
  suffix: ".md",
  kind: "markdown",
  content: "# 2026-05 周期总览\n\n储能铅酸主升龙头见顶。",
};

const csvFile: KnowledgeFile = {
  partition: "trades",
  path: "2026-05/raw.csv",
  title: "raw",
  size: 60,
  mtime: "2026-05-30T01:00:00Z",
  suffix: ".csv",
  kind: "csv",
  columns: ["code", "price", "qty"],
  rows: [["600519", "1800", "100"], ["000001", "15", "200"]],
  row_count: 2,
  truncated: false,
};

describe("KnowledgeBrowserPanel", () => {
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

  it("renders the index tree with partitions and the weak-title alert", async () => {
    vi.mocked(getKnowledgeIndex).mockResolvedValue(index);

    render(<KnowledgeBrowserPanel />);

    // Partition node (label unique to the tree, not the subtitle) + entries render.
    expect(await screen.findByText(/题材 \/ 龙头/)).toBeInTheDocument();
    expect(screen.getByText("2026-05 周期总览")).toBeInTheDocument();
    expect(screen.getByText("noheading")).toBeInTheDocument();
    // Weak-title health alert surfaces (1 weak file).
    expect(await screen.findByTestId("knowledge-weak-alert")).toBeInTheDocument();
    expect(screen.getByText(/1 个弱标题文件/)).toBeInTheDocument();
    // Total count in the subtitle.
    expect(screen.getByText(/共 3 个文件/)).toBeInTheDocument();
    // No file fetched until a leaf is clicked.
    expect(getKnowledgeFile).not.toHaveBeenCalled();
  });

  it("fetches and renders a markdown file when its leaf is clicked", async () => {
    vi.mocked(getKnowledgeIndex).mockResolvedValue(index);
    vi.mocked(getKnowledgeFile).mockResolvedValue(markdownFile);

    render(<KnowledgeBrowserPanel />);

    // Wait for the tree to render.
    await screen.findByText("2026-05 周期总览");

    // Click the overview markdown leaf.
    fireEvent.click(screen.getByText("2026-05 周期总览"));

    await waitFor(() => {
      expect(getKnowledgeFile).toHaveBeenCalledWith("cycles", "2026-05/_overview.md");
    });
    // Markdown body renders (heading text appears).
    expect(await screen.findByText(/储能铅酸主升龙头见顶/)).toBeInTheDocument();
    expect(screen.getByTestId("knowledge-file-markdown")).toBeInTheDocument();
  });

  it("renders a CSV file as a paginated table", async () => {
    vi.mocked(getKnowledgeIndex).mockResolvedValue(index);
    vi.mocked(getKnowledgeFile).mockResolvedValue(csvFile);

    render(<KnowledgeBrowserPanel />);

    await screen.findByText("raw");
    // Click the CSV leaf.
    fireEvent.click(screen.getByText("raw"));

    await waitFor(() => {
      expect(getKnowledgeFile).toHaveBeenCalledWith("trades", "2026-05/raw.csv");
    });
    // CSV reader + column headers + cell values render. antd Table with
    // scroll duplicates the header (fixed + body), so use getAllByText for the
    // column name; the data cell value is unique.
    expect(await screen.findByTestId("knowledge-file-csv")).toBeInTheDocument();
    expect(screen.getAllByText("code").length).toBeGreaterThan(0);
    expect(screen.getByText("600519")).toBeInTheDocument();
    expect(screen.getByText("1800")).toBeInTheDocument();
  });

  it("shows the empty hint when the knowledge base has no files", async () => {
    vi.mocked(getKnowledgeIndex).mockResolvedValue({
      root_exists: true,
      total_files: 0,
      weak_title_count: 0,
      skipped_count: 0,
      weak_titles: [],
      generated_at: "2026-06-17T00:00:00Z",
      partitions: [],
    });

    render(<KnowledgeBrowserPanel />);

    expect(
      await screen.findByText("知识库还没有任何文件 —— 在对话里让 agent 记一次复盘 / 角色笔记即可"),
    ).toBeInTheDocument();
    expect(getKnowledgeFile).not.toHaveBeenCalled();
  });
});
