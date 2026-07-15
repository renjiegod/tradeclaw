import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { SymbolRoleCards } from "./SymbolRoleCards";
import type { SymbolRoles } from "../types";
import { getSymbolRoles } from "../api";

// SymbolRoleCards only imports getSymbolRoles from ../api.
vi.mock("../api", () => ({
  getSymbolRoles: vi.fn(),
}));

const roles: SymbolRoles = {
  items: [
    {
      symbol: "600519",
      name: "贵州茅台",
      role: "龙头",
      note: "板块绝对核心，情绪风向标",
      strategy_hint: "打板策略优先跟随",
      updated_at: "2026-06-30T10:30:00",
    },
    {
      symbol: "000001",
      name: "平安银行",
      role: "中军",
      note: "跟随主线，仓位承接",
      // No strategy hint — should render without the 策略建议 row.
      strategy_hint: "",
      updated_at: "2026-06-28T09:15:00",
    },
    {
      symbol: "300750",
      name: "宁德时代",
      // Blank note — must render as "—", never fabricated.
      role: "杂毛",
      note: "",
      strategy_hint: "",
      updated_at: "2026-06-25T14:00:00",
    },
  ],
};

describe("SymbolRoleCards", () => {
  beforeAll(() => {
    // antd Empty/Tag rely on matchMedia in jsdom.
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

  it("renders one card per symbol with its symbol, name and role tag", async () => {
    vi.mocked(getSymbolRoles).mockResolvedValue(roles);

    render(<SymbolRoleCards />);

    expect(getSymbolRoles).toHaveBeenCalledTimes(1);

    const cards = await screen.findAllByTestId("symbol-role-card");
    expect(cards).toHaveLength(3);

    // The 龙头 card carries its symbol / name / role, note and strategy hint.
    const leaderCard = cards.find((c) => c.getAttribute("data-symbol") === "600519");
    expect(leaderCard).toBeDefined();
    expect(leaderCard?.getAttribute("data-role")).toBe("龙头");
    expect(leaderCard?.textContent).toContain("600519");
    expect(leaderCard?.textContent).toContain("贵州茅台");
    expect(leaderCard?.textContent).toContain("板块绝对核心，情绪风向标");
    expect(leaderCard?.textContent).toContain("打板策略优先跟随");
  });

  it("colours the 龙头 role tag with the red palette", async () => {
    vi.mocked(getSymbolRoles).mockResolvedValue(roles);

    render(<SymbolRoleCards />);

    const leaderCard = (await screen.findAllByTestId("symbol-role-card")).find(
      (c) => c.getAttribute("data-symbol") === "600519",
    );
    const tag = leaderCard?.querySelector('[data-testid="symbol-role-tag"]');
    expect(tag).toBeTruthy();
    // 龙头 = red / strong per the A-share convention palette.
    expect(tag?.className).toContain("!text-red-700");
    expect(tag?.className).toContain("!bg-red-50");
  });

  it("shows — for missing fields and omits the strategy hint when blank", async () => {
    vi.mocked(getSymbolRoles).mockResolvedValue(roles);

    render(<SymbolRoleCards />);

    const cards = await screen.findAllByTestId("symbol-role-card");

    // 中军 card has no strategy hint → no 策略建议 row.
    const midCard = cards.find((c) => c.getAttribute("data-symbol") === "000001");
    expect(midCard?.textContent).not.toContain("策略建议");

    // 杂毛 card has a blank note → renders the — dash, never a fabricated value.
    const noiseCard = cards.find((c) => c.getAttribute("data-symbol") === "300750");
    expect(noiseCard?.textContent).toContain("备注：—");
    expect(noiseCard?.textContent).not.toContain("策略建议");
  });

  it("shows the friendly empty state when no roles have been tagged", async () => {
    vi.mocked(getSymbolRoles).mockResolvedValue({ items: [] });

    render(<SymbolRoleCards />);

    expect(
      await screen.findByText("暂无标的角色记录（对话里说「把这票记成龙头」即可添加）"),
    ).toBeInTheDocument();
    // No cards and no grid when empty.
    expect(screen.queryByTestId("symbol-role-card")).toBeNull();
    expect(screen.queryByTestId("symbol-role-grid")).toBeNull();
  });
});
