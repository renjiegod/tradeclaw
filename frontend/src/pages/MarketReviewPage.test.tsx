import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { MarketReviewPage } from "./MarketReviewPage";
import { getDragonTigerBoard, getFundFlowRanking, getMarketBreadth, getSectorHeat } from "../api";
import type { FundFlowData, LhbData, MarketBreadthData, SectorHeatData } from "../types";

// The page's ApiError branch keys off ``errorCode`` to render the friendly
// "无数据（可能非交易日或盘后未更新）" state, so the mocked ApiError must carry it.
vi.mock("../api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    errorCode: string | null;
    constructor(message: string, status: number, errorCode: string | null = null) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.errorCode = errorCode;
    }
  },
  getMarketBreadth: vi.fn(),
  getDragonTigerBoard: vi.fn(),
  getFundFlowRanking: vi.fn(),
  getSectorHeat: vi.fn(),
}));

// Import the mocked ApiError class so tests can construct rejections.
import { ApiError } from "../api";

const breadthData: MarketBreadthData = {
  status: "ok",
  trade_date: "20260703",
  data_source: "akshare",
  limit_up_count: 62,
  limit_down_count: 8,
  broken_board_count: 15,
  broken_board_rate: 0.195,
  max_streak: 6,
  ladder: { "1": 40, "2": 12, "3": 6, "6": 1 },
  sentiment: {
    label: "发酵/活跃",
    reason: "涨停 62 家、跌停 8 家、炸板 15 家、最高 6 连板、炸板率 20%",
    disclaimer:
      "本标签基于当日涨跌停/连板/炸板的规则描述，是单日快照，非预测、非投资建议；完整情绪周期需结合多日趋势。",
    inputs: {
      limit_up_count: 62,
      limit_down_count: 8,
      broken_board_count: 15,
      max_streak: 6,
      broken_board_rate: 0.195,
    },
  },
};

const lhbData: LhbData = {
  status: "ok",
  start_date: "20260703",
  end_date: "20260703",
  count: 2,
  latest: [
    {
      symbol: "600519.SH",
      code: "600519",
      name: "贵州茅台",
      on_date: "2026-07-03",
      reason: "日涨幅偏离值达7%",
      interpretation: "机构净买入",
      change_pct: 9.98,
      close_price: 1800,
      net_buy_amount: 123_456_789,
      buy_amount: 300_000_000,
      sell_amount: 176_543_211,
      turnover_rate: 3.2,
      circulating_mv: 2_000_000_000_000,
    },
    {
      symbol: "000001.SZ",
      code: "000001",
      name: "平安银行",
      on_date: "2026-07-03",
      reason: "连续三个交易日跌幅偏离",
      interpretation: "游资出货",
      change_pct: -5.1,
      close_price: 12.3,
      net_buy_amount: -45_000_000,
      buy_amount: 20_000_000,
      sell_amount: 65_000_000,
      turnover_rate: 2.1,
      circulating_mv: 200_000_000_000,
    },
  ],
};

const fundFlowData: FundFlowData = {
  status: "ok",
  scope: "individual",
  period: "今日",
  count: 2,
  top: 30,
  latest: [
    {
      name: "宁德时代",
      symbol: "300750.SZ",
      code: "300750",
      latest_price: 210.5,
      change_pct: 4.2,
      main_net_amount: 980_000_000,
      main_net_pct: 12.3,
      super_large_net_amount: 600_000_000,
      large_net_amount: 380_000_000,
      medium_net_amount: -100_000_000,
      small_net_amount: -50_000_000,
      lead_stock: null,
    },
    {
      name: "东方财富",
      symbol: "300059.SZ",
      code: "300059",
      latest_price: 15.6,
      change_pct: -1.8,
      main_net_amount: -320_000_000,
      main_net_pct: -6.1,
      super_large_net_amount: -200_000_000,
      large_net_amount: -120_000_000,
      medium_net_amount: 80_000_000,
      small_net_amount: 40_000_000,
      lead_stock: null,
    },
  ],
};

const sectorHeatData: SectorHeatData = {
  status: "ok",
  sector_type: "concept",
  count: 2,
  top: 30,
  latest: [
    {
      board_name: "半导体",
      board_code: "BK1036",
      sector_type: "concept",
      change_pct: 5.2,
      total_mv: 4_000_000_000_000,
      turnover_rate: 3.1,
      up_count: 80,
      down_count: 5,
      leader_stock: "中芯国际",
      leader_change_pct: 9.98,
      provider: "akshare",
    },
    {
      board_name: "白酒",
      board_code: "BK0477",
      sector_type: "concept",
      change_pct: 1.0,
      total_mv: 3_000_000_000_000,
      turnover_rate: 1.2,
      up_count: 12,
      down_count: 8,
      // Distinct from the LHB row's 贵州茅台 so the test's single-match
      // findByText queries stay unambiguous (the page itself allows the same
      // name across tables; this is purely test-data hygiene).
      leader_stock: "五粮液",
      leader_change_pct: 4.0,
      provider: "akshare",
    },
  ],
};

function renderPage() {
  return render(
    <MemoryRouter>
      <MarketReviewPage />
    </MemoryRouter>,
  );
}

describe("MarketReviewPage", () => {
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
    // AntD's Segmented/Table use ResizeObserver in jsdom.
    if (!("ResizeObserver" in window)) {
      (window as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      };
    }
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("renders the sentiment label, disclaimer, ladder, LHB table and fund-flow table", async () => {
    vi.mocked(getMarketBreadth).mockResolvedValue(breadthData);
    vi.mocked(getDragonTigerBoard).mockResolvedValue(lhbData);
    vi.mocked(getFundFlowRanking).mockResolvedValue(fundFlowData);
    vi.mocked(getSectorHeat).mockResolvedValue(sectorHeatData);

    renderPage();

    // Sentiment thermometer.
    await waitFor(() => expect(getMarketBreadth).toHaveBeenCalled());
    const sentiment = await screen.findByTestId("sentiment-label");
    expect(sentiment).toHaveTextContent("发酵/活跃");

    // Compliance disclaimer must be visible (never hidden).
    const disclaimer = screen.getByTestId("sentiment-disclaimer");
    expect(disclaimer).toHaveTextContent(/单日快照，非预测、非投资建议/);

    // 连板梯队: one row per ladder height (4 heights → 4 rows).
    const ladderRows = screen.getAllByTestId("ladder-row");
    expect(ladderRows.length).toBe(4);
    // "6 板" appears both in the ladder (highest height) and as the sentiment
    // card's 最高连板 tile — assert the ladder row specifically carries it.
    expect(within(ladderRows[0]).getByText("6 板")).toBeInTheDocument();

    // 龙虎榜 table row + signed net-buy amount (red-up / green-down).
    await waitFor(() => expect(getDragonTigerBoard).toHaveBeenCalled());
    const maotaiRow = (await screen.findByText("贵州茅台")).closest("tr")!;
    expect(within(maotaiRow).getByText("+1.23 亿")).toBeInTheDocument();
    // Positive change_pct renders a red tag.
    const upTag = within(maotaiRow).getByText("+9.98%");
    expect(upTag.closest(".ant-tag")?.className).toMatch(/ant-tag-red/);

    // 资金流 table row + signed main net inflow.
    await waitFor(() => expect(getFundFlowRanking).toHaveBeenCalled());
    const catlRow = (await screen.findByText("宁德时代")).closest("tr")!;
    expect(within(catlRow).getByText("+9.80 亿")).toBeInTheDocument();

    // 题材热度 table row: board name + board change tag + leader stock.
    await waitFor(() => expect(getSectorHeat).toHaveBeenCalled());
    const semiRow = (await screen.findByText("半导体")).closest("tr")!;
    // Board change_pct renders a red (up) tag.
    const heatUpTag = within(semiRow).getByText("+5.20%");
    expect(heatUpTag.closest(".ant-tag")?.className).toMatch(/ant-tag-red/);
    // Leader stock name is shown.
    expect(within(semiRow).getByText("中芯国际")).toBeInTheDocument();
  });

  it("shows a friendly empty state when breadth returns market_breadth_empty (4xx)", async () => {
    vi.mocked(getMarketBreadth).mockRejectedValue(
      new ApiError("no limit-up data", 400, "market_breadth_empty"),
    );
    vi.mocked(getDragonTigerBoard).mockResolvedValue({ ...lhbData, latest: [] });
    vi.mocked(getFundFlowRanking).mockResolvedValue({ ...fundFlowData, latest: [] });

    renderPage();

    await waitFor(() => expect(getMarketBreadth).toHaveBeenCalled());
    // The friendly, non-fabricated message — no numbers invented. It surfaces
    // in BOTH the 市场情绪 Alert and the 连板梯队 Empty, so match all of them.
    await waitFor(() =>
      expect(
        screen.getAllByText(/无数据（可能非交易日或盘后未更新）/).length,
      ).toBeGreaterThanOrEqual(1),
    );
    // The sentiment label must NOT render when breadth is empty.
    expect(screen.queryByTestId("sentiment-label")).toBeNull();
  });
});
