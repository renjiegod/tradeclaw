import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TradeImportCard } from "./TradeImportCard";
import type {
  PortfolioImportCommitResponse,
  PortfolioImportParseResponse,
  TradeAttributionSummary,
} from "../types";
import {
  ApiError,
  commitTradesCsv,
  listPortfolioImportBrokers,
  parseTradesCsv,
} from "../api";

// TradeImportCard imports the three import endpoints plus the ApiError class
// (for structured message/hint display) from ../api. The mock ApiError mirrors
// the real class's shape closely enough for instanceof branching.
vi.mock("../api", () => {
  class MockApiError extends Error {
    readonly status: number;
    readonly hint: string | null;
    readonly errorCode: string | null;

    constructor(
      message: string,
      status: number,
      options?: { hint?: string | null; errorCode?: string | null },
    ) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.hint = options?.hint ?? null;
      this.errorCode = options?.errorCode ?? null;
    }
  }
  return {
    ApiError: MockApiError,
    listPortfolioImportBrokers: vi.fn(),
    parseTradesCsv: vi.fn(),
    commitTradesCsv: vi.fn(),
  };
});

const parseResponse: PortfolioImportParseResponse = {
  status: "ok",
  broker: "huatai",
  fills_total: 12,
  new_count: 10,
  duplicate_count: 2,
  unparsed_count: 0,
  records: [
    {
      date: "2026-06-01",
      time: "09:31:02",
      symbol: "600519.SH",
      name: "贵州茅台",
      side: "buy",
      price: "1700.5",
      qty: "100",
      amount: "170050",
      month: "2026-06",
      duplicate: false,
    },
    {
      date: "2026-06-02",
      time: "10:05:11",
      symbol: "300750.SZ",
      name: "宁德时代",
      side: "sell",
      price: "230.0",
      qty: "200",
      amount: "46000",
      month: "2026-06",
      duplicate: true,
    },
  ],
  records_truncated: false,
  unparsed: [],
};

const attributionSummary: TradeAttributionSummary = {
  round_trips: 3,
  win_count: 2,
  loss_count: 1,
  win_rate: 0.6667,
  total_realized_pnl: "128000.00",
  avg_win: "90000.00",
  avg_loss: "-52000.00",
  profit_factor: 3.46,
  avg_hold_days: 4.5,
  best: null,
  worst: null,
  open_positions: 1,
};

const commitResponse: PortfolioImportCommitResponse = {
  status: "ok",
  broker: "huatai",
  dry_run: false,
  appended_total: 10,
  duplicates_skipped: 2,
  fills_total: 12,
  written: { "trades/huatai/2026-06.csv": 10 },
  unparsed_count: 0,
  unparsed: [],
  review: {
    affected_months: ["2026-06"],
    attribution_summary: attributionSummary,
    attribution_error: null,
  },
};

const dryRunResponse: PortfolioImportCommitResponse = {
  ...commitResponse,
  dry_run: true,
  written: {},
  review: null,
};

function makeCsvFile(name = "statement.csv"): File {
  return new File(["日期,证券代码\n2026-06-01,600519"], name, { type: "text/csv" });
}

/** Fill broker (free input into the AutoComplete) + pick the CSV file. */
function fillBrokerAndFile(file: File = makeCsvFile()) {
  const brokerWrap = screen.getByTestId("trade-import-broker");
  const brokerInput = within(brokerWrap).getByRole("combobox");
  fireEvent.change(brokerInput, { target: { value: "huatai" } });

  const fileInput = screen.getByTestId("trade-import-file-input");
  fireEvent.change(fileInput, { target: { files: [file] } });
  return file;
}

describe("TradeImportCard", () => {
  beforeAll(() => {
    // antd Select / Table / Popconfirm rely on matchMedia in jsdom.
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
    vi.mocked(listPortfolioImportBrokers).mockResolvedValue({
      items: [
        { broker: "huatai", display_name: "华泰证券", existing: true },
        { broker: "guotai", display_name: "国泰君安", existing: false },
      ],
    });
  });

  afterEach(cleanup);

  it("loads the broker list on mount and shows preset options", async () => {
    render(<TradeImportCard />);

    expect(listPortfolioImportBrokers).toHaveBeenCalledTimes(1);

    // Open the AutoComplete dropdown to see the preset options with the
    // existing marker.
    const brokerWrap = screen.getByTestId("trade-import-broker");
    const brokerInput = within(brokerWrap).getByRole("combobox");
    fireEvent.mouseDown(brokerInput);
    fireEvent.change(brokerInput, { target: { value: "" } });
    fireEvent.focus(brokerInput);

    await waitFor(() => {
      expect(document.body.textContent).toContain("华泰证券");
    });
    expect(document.body.textContent).toContain("国泰君安");
    expect(document.body.textContent).toContain("已有数据");
  });

  it("parses the CSV and renders the preview table with counts and duplicate marks", async () => {
    vi.mocked(parseTradesCsv).mockResolvedValue(parseResponse);

    render(<TradeImportCard />);
    const file = fillBrokerAndFile();

    fireEvent.click(screen.getByTestId("trade-import-parse"));

    await waitFor(() => {
      expect(parseTradesCsv).toHaveBeenCalledWith(file, "huatai");
    });

    const summary = await screen.findByTestId("trade-import-preview-summary");
    expect(summary.textContent).toContain("共 12 条");
    expect(summary.textContent).toContain("新增 10");
    expect(summary.textContent).toContain("重复 2");
    expect(summary.textContent).toContain("未解析 0");

    const table = screen.getByTestId("trade-import-preview-table");
    expect(table.textContent).toContain("600519.SH");
    expect(table.textContent).toContain("贵州茅台");
    expect(table.textContent).toContain("买入");
    expect(table.textContent).toContain("卖出");
    expect(table.textContent).toContain("2026-06");

    // Duplicate row is tagged 重复 and greyed out.
    const dupRow = within(table).getByText("300750.SZ").closest("tr");
    expect(dupRow?.textContent).toContain("重复");
    expect(dupRow?.className).toContain("opacity-50");

    // No truncation hint when records_truncated is false.
    expect(screen.queryByTestId("trade-import-truncated")).toBeNull();
  });

  it("shows the truncation hint when records_truncated is true", async () => {
    vi.mocked(parseTradesCsv).mockResolvedValue({ ...parseResponse, records_truncated: true });

    render(<TradeImportCard />);
    fillBrokerAndFile();
    fireEvent.click(screen.getByTestId("trade-import-parse"));

    const truncated = await screen.findByTestId("trade-import-truncated");
    expect(truncated.textContent).toContain("预览记录已截断");
  });

  it("runs a dry-run import: commitTradesCsv(dryRun=true), no onImported, no review", async () => {
    vi.mocked(commitTradesCsv).mockResolvedValue(dryRunResponse);
    const onImported = vi.fn();

    render(<TradeImportCard onImported={onImported} />);
    const file = fillBrokerAndFile();

    fireEvent.click(screen.getByTestId("trade-import-dry-run"));

    await waitFor(() => {
      expect(commitTradesCsv).toHaveBeenCalledWith(file, "huatai", true);
    });

    const result = await screen.findByTestId("trade-import-result");
    expect(result.textContent).toContain("预演完成");
    expect(result.textContent).toContain("将新增 10 条");
    expect(result.textContent).toContain("跳过重复 2 条");
    expect(onImported).not.toHaveBeenCalled();
    expect(screen.queryByTestId("trade-import-review")).toBeNull();
  });

  it("commits for real after a preview: fires onImported and renders the review block", async () => {
    vi.mocked(parseTradesCsv).mockResolvedValue(parseResponse);
    vi.mocked(commitTradesCsv).mockResolvedValue(commitResponse);
    const onImported = vi.fn();

    render(<TradeImportCard onImported={onImported} />);
    const file = fillBrokerAndFile();

    // Preview first, so the real import goes straight through (no Popconfirm).
    fireEvent.click(screen.getByTestId("trade-import-parse"));
    await screen.findByTestId("trade-import-preview");

    fireEvent.click(screen.getByTestId("trade-import-commit"));

    await waitFor(() => {
      expect(commitTradesCsv).toHaveBeenCalledWith(file, "huatai", false);
    });

    const result = await screen.findByTestId("trade-import-result");
    expect(result.textContent).toContain("导入成功");
    expect(result.textContent).toContain("新增 10 条");
    expect(screen.getByTestId("trade-import-written").textContent).toContain(
      "trades/huatai/2026-06.csv",
    );

    // Review block: affected months + attribution key stats.
    const review = screen.getByTestId("trade-import-review");
    expect(review.textContent).toContain("2026-06");
    expect(review.textContent).toContain("66.7%"); // win_rate
    expect(review.textContent).toContain("3.46"); // profit_factor
    expect(review.textContent).toContain("12.80 万"); // total_realized_pnl
    expect(screen.queryByTestId("trade-import-attribution-error")).toBeNull();

    expect(onImported).toHaveBeenCalledTimes(1);
  });

  it("asks for confirmation when committing without a prior preview / dry-run", async () => {
    vi.mocked(commitTradesCsv).mockResolvedValue(commitResponse);
    const onImported = vi.fn();

    render(<TradeImportCard onImported={onImported} />);
    fillBrokerAndFile();

    fireEvent.click(screen.getByTestId("trade-import-commit"));

    // Popconfirm shows instead of firing the request immediately.
    expect(commitTradesCsv).not.toHaveBeenCalled();
    const confirm = await screen.findByText("尚未解析预览或预演导入，确定直接正式导入吗？");
    expect(confirm).toBeInTheDocument();

    fireEvent.click(screen.getByText("确定导入"));

    await waitFor(() => {
      expect(commitTradesCsv).toHaveBeenCalledTimes(1);
    });
    await screen.findByTestId("trade-import-result");
    expect(onImported).toHaveBeenCalledTimes(1);
  });

  it("surfaces the attribution_error warning when the post-import review failed", async () => {
    vi.mocked(commitTradesCsv)
      .mockResolvedValueOnce(dryRunResponse)
      .mockResolvedValueOnce({
        ...commitResponse,
        review: {
          affected_months: ["2026-06"],
          attribution_summary: null,
          attribution_error: "trades/huatai/2026-06.csv: 缺少成交方向列",
        },
      });

    render(<TradeImportCard />);
    fillBrokerAndFile();

    fireEvent.click(screen.getByTestId("trade-import-dry-run"));
    await screen.findByTestId("trade-import-result");

    fireEvent.click(screen.getByTestId("trade-import-commit"));

    const alert = await screen.findByTestId("trade-import-attribution-error");
    expect(alert.textContent).toContain("归因刷新失败");
    expect(alert.textContent).toContain("缺少成交方向列");
  });

  it("shows the ApiError message and hint when parsing fails", async () => {
    vi.mocked(parseTradesCsv).mockRejectedValue(
      new ApiError("无法识别的券商列名", 400, { hint: "确认选择的券商与文件来源一致" }),
    );

    render(<TradeImportCard />);
    fillBrokerAndFile();
    fireEvent.click(screen.getByTestId("trade-import-parse"));

    const alert = await screen.findByTestId("trade-import-error");
    expect(alert.textContent).toContain("无法识别的券商列名");
    expect(alert.textContent).toContain("确认选择的券商与文件来源一致");
    expect(screen.queryByTestId("trade-import-preview")).toBeNull();
  });
});
