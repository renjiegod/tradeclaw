import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { PlaybookPanel } from "./PlaybookPanel";
import type { KnowledgeFile, Playbook } from "../types";
import { getKnowledgeFile, getPlaybook } from "../api";

// PlaybookPanel imports getPlaybook + getKnowledgeFile from ../api.
vi.mock("../api", () => ({
  getPlaybook: vi.fn(),
  getKnowledgeFile: vi.fn(),
}));

const playbook: Playbook = {
  items: [
    {
      path: "2026-06/首板打板.md",
      title: "首板打板笔记",
      pattern: "首板打板",
      // 高潮/亢奋 → 热红 (red) stage tag.
      stage: "高潮/亢奋",
      summary: "情绪高潮期首板低吸打板，次日竞价出。",
      tags: ["打板", "情绪"],
      updated_at: "2026-06-20T10:30:00",
    },
    {
      path: "2026-06/退潮低吸.md",
      // No pattern → falls back to title as the 打法名.
      pattern: null,
      title: "退潮低吸兜底",
      // 退潮/低迷 → 冷绿 (green) stage tag.
      stage: "退潮/低迷",
      summary: "退潮期只做超跌低吸，不追高。",
      tags: null,
      updated_at: "2026-06-18T09:00:00",
    },
    {
      path: "2026-05/未知阶段.md",
      pattern: "中军补涨",
      // Missing summary / unknown stage must render "—" / fallback, never fabricated.
      stage: null,
      title: "中军补涨",
      summary: null,
      tags: [],
      updated_at: "2026-05-30T15:00:00",
    },
  ],
};

const markdownFile: Extract<KnowledgeFile, { kind: "markdown" }> = {
  partition: "playbook",
  path: "2026-06/首板打板.md",
  title: "首板打板笔记",
  size: 512,
  mtime: "2026-06-20T10:30:00",
  suffix: ".md",
  kind: "markdown",
  content: "# 首板打板\n\n情绪高潮期首板低吸，次日竞价择机出局。",
};

describe("PlaybookPanel", () => {
  beforeAll(() => {
    // antd Empty / Modal / Tag rely on matchMedia in jsdom.
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

  it("renders one card per playbook entry with pattern + stage tag colour", async () => {
    vi.mocked(getPlaybook).mockResolvedValue(playbook);

    render(<PlaybookPanel />);

    expect(getPlaybook).toHaveBeenCalledTimes(1);

    const grid = await screen.findByTestId("playbook-grid");
    const cards = within(grid).getAllByTestId("playbook-card");
    expect(cards).toHaveLength(3);

    // Card 1: pattern shown, 高潮/亢奋 stage tag coloured 热红 (red border/text).
    const climaxCard = cards.find((c) => c.getAttribute("data-path") === "2026-06/首板打板.md");
    expect(climaxCard).toBeDefined();
    expect(climaxCard?.textContent).toContain("首板打板");
    expect(climaxCard?.textContent).toContain("情绪高潮期首板低吸打板");
    const climaxTag = within(climaxCard as HTMLElement).getByTestId("playbook-stage-tag");
    expect(climaxTag.textContent).toContain("高潮/亢奋");
    expect(climaxTag.className).toContain("!text-red-700");

    // Card 2: no pattern → title fallback; 退潮/低迷 stage coloured 冷绿 (green).
    const ebbCard = cards.find((c) => c.getAttribute("data-path") === "2026-06/退潮低吸.md");
    expect(ebbCard).toBeDefined();
    expect(ebbCard?.textContent).toContain("退潮低吸兜底"); // title fallback for the 打法名
    const ebbTag = within(ebbCard as HTMLElement).getByTestId("playbook-stage-tag");
    expect(ebbTag.textContent).toContain("退潮/低迷");
    expect(ebbTag.className).toContain("!text-emerald-700");
  });

  it("renders tags, omits the stage tag when blank, and shows — for missing summary", async () => {
    vi.mocked(getPlaybook).mockResolvedValue(playbook);

    render(<PlaybookPanel />);

    const grid = await screen.findByTestId("playbook-grid");

    // Tags rendered on the first card.
    const climaxCard = within(grid)
      .getAllByTestId("playbook-card")
      .find((c) => c.getAttribute("data-path") === "2026-06/首板打板.md") as HTMLElement;
    const tagWrap = within(climaxCard).getByTestId("playbook-tags");
    const chips = within(tagWrap).getAllByTestId("playbook-tag");
    expect(chips.map((c) => c.textContent)).toEqual(["打板", "情绪"]);

    // Third card: 情绪阶段 is optional — a null stage renders NO stage tag
    // (a swing / trend 战法 leaves it blank), rather than a bare "—" chip.
    const unknownCard = within(grid)
      .getAllByTestId("playbook-card")
      .find((c) => c.getAttribute("data-path") === "2026-05/未知阶段.md") as HTMLElement;
    expect(within(unknownCard).queryByTestId("playbook-stage-tag")).toBeNull();
    // No tag row for an empty tags array.
    expect(within(unknownCard).queryByTestId("playbook-tags")).toBeNull();
    // Summary "—" still present (missing summary is never fabricated).
    expect(unknownCard.textContent).toContain("—");
  });

  it("shows the friendly empty state when the base has no playbook entries", async () => {
    vi.mocked(getPlaybook).mockResolvedValue({ items: [] });

    render(<PlaybookPanel />);

    expect(
      await screen.findByText("暂无战法（对话里说「把这个打法记进战法库」即可添加）"),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("playbook-grid")).toBeNull();
  });

  it("opens a modal with the full markdown when a card is clicked", async () => {
    vi.mocked(getPlaybook).mockResolvedValue(playbook);
    vi.mocked(getKnowledgeFile).mockResolvedValue(markdownFile);

    render(<PlaybookPanel />);

    const grid = await screen.findByTestId("playbook-grid");
    const climaxCard = within(grid)
      .getAllByTestId("playbook-card")
      .find((c) => c.getAttribute("data-path") === "2026-06/首板打板.md") as HTMLElement;

    fireEvent.click(climaxCard);

    // Fetches the full text from the playbook partition by path.
    await waitFor(() => {
      expect(getKnowledgeFile).toHaveBeenCalledWith("playbook", "2026-06/首板打板.md");
    });

    // Modal body renders the markdown content.
    expect(await screen.findByTestId("playbook-detail-markdown")).toBeInTheDocument();
    expect(await screen.findByText(/次日竞价择机出局/)).toBeInTheDocument();
  });
});
