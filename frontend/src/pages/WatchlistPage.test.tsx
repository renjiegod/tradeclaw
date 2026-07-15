import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { WatchlistPage } from "./WatchlistPage";
import type { QuoteSnapshot, WatchlistEntry } from "../types";
import {
  addWatchlistEntry,
  listInstrumentCatalog,
  listWatchlist,
  listWatchlistTags,
  searchInstrumentUniverse,
} from "../api";

vi.mock("../api", () => ({
  // ApiError is referenced in WatchlistPage's catch blocks.
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  },
  listWatchlist: vi.fn(),
  listWatchlistTags: vi.fn(),
  addWatchlistEntry: vi.fn(),
  updateWatchlistEntry: vi.fn(),
  deleteWatchlistEntry: vi.fn(),
  listInstrumentCatalog: vi.fn(),
  searchInstrumentUniverse: vi.fn(),
}));

// Control the realtime stream deterministically.
const streamState = {
  quotes: {} as Record<string, QuoteSnapshot>,
  connected: true,
  qmtDisconnected: false,
};
vi.mock("../hooks/useMarketQuoteStream", () => ({
  useMarketQuoteStream: () => streamState,
}));

const navigateMock = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return {
    ...actual,
    useNavigate: () => navigateMock,
  };
});

function renderWatchlistPage() {
  return render(
    <MemoryRouter>
      <WatchlistPage />
    </MemoryRouter>,
  );
}

function makeQuote(symbol: string, overrides: Partial<QuoteSnapshot> = {}): QuoteSnapshot {
  return {
    symbol,
    price: 100,
    prev_close: 90,
    change: 10,
    change_pct: 5,
    open: 95,
    high: 105,
    low: 92,
    volume: 1000,
    amount: 123_456_789,
    timestamp: "2026-06-07T01:00:00Z",
    status: "ok",
    ...overrides,
  };
}

const upEntry: WatchlistEntry = {
  id: "wl-aaa",
  symbol: "600519.SH",
  display_name: "贵州茅台",
  tags: ["白酒"],
  note: "",
  sort_order: 0,
  created_at: null,
  updated_at: null,
};
const downEntry: WatchlistEntry = {
  id: "wl-bbb",
  symbol: "000001.SZ",
  display_name: "平安银行",
  tags: ["银行"],
  note: "",
  sort_order: 1,
  created_at: null,
  updated_at: null,
};

describe("WatchlistPage", () => {
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
    streamState.quotes = {};
    streamState.connected = true;
    streamState.qmtDisconnected = false;
    vi.mocked(listWatchlistTags).mockResolvedValue({ items: [{ tag: "白酒", count: 1 }] });
  });

  afterEach(() => {
    cleanup();
  });

  it("renders entries with red-up / green-down change_pct tags", async () => {
    streamState.quotes = {
      "600519.SH": makeQuote("600519.SH", { change_pct: 5 }),
      "000001.SZ": makeQuote("000001.SZ", { change_pct: -3.2 }),
    };
    vi.mocked(listWatchlist).mockResolvedValue({ items: [upEntry, downEntry] });

    renderWatchlistPage();

    await waitFor(() => expect(listWatchlist).toHaveBeenCalled());

    const upRow = (await screen.findByText("贵州茅台")).closest("tr")!;
    const downRow = screen.getByText("平安银行").closest("tr")!;

    // Positive change is prefixed with + and rendered in a red (volcano/red) tag.
    const upTag = within(upRow).getByText("+5.00%");
    expect(upTag.closest(".ant-tag")?.className).toMatch(/ant-tag-red/);

    const downTag = within(downRow).getByText("-3.20%");
    expect(downTag.closest(".ant-tag")?.className).toMatch(/ant-tag-green/);
  });

  it("shows — placeholders for price and change when quotes are missing", async () => {
    // No quotes in the stream → every realtime cell falls back to —.
    streamState.quotes = {};
    vi.mocked(listWatchlist).mockResolvedValue({ items: [upEntry] });

    renderWatchlistPage();
    await waitFor(() => expect(listWatchlist).toHaveBeenCalled());

    const row = (await screen.findByText("贵州茅台")).closest("tr")!;
    // price + change_pct + amount all render the em-dash placeholder.
    expect(within(row).getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });

  it("renders the qmt-disconnected banner and dashes all quote columns", async () => {
    streamState.qmtDisconnected = true;
    streamState.quotes = { "600519.SH": makeQuote("600519.SH") };
    vi.mocked(listWatchlist).mockResolvedValue({ items: [upEntry] });

    renderWatchlistPage();
    await waitFor(() => expect(listWatchlist).toHaveBeenCalled());

    expect(await screen.findByText("行情未连接（需配置默认 QMT 账户）")).toBeInTheDocument();
    const row = (await screen.findByText("贵州茅台")).closest("tr")!;
    expect(within(row).getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });

  it("renders 停牌 instead of -100% for a suspended quote", async () => {
    // Halt sentinel from the backend: price/change_pct null, status suspended.
    // Regression: 中船特气 688146.SH used to show -100.00% while halted.
    streamState.quotes = {
      "600519.SH": makeQuote("600519.SH", {
        status: "suspended",
        price: null,
        change: null,
        change_pct: null,
        prev_close: 90,
      }),
    };
    vi.mocked(listWatchlist).mockResolvedValue({ items: [upEntry] });

    renderWatchlistPage();
    await waitFor(() => expect(listWatchlist).toHaveBeenCalled());

    const row = (await screen.findByText("贵州茅台")).closest("tr")!;
    // 停牌 shown in both the price and change_pct columns; never a % figure.
    expect(within(row).getAllByText("停牌").length).toBeGreaterThanOrEqual(2);
    expect(within(row).queryByText(/-100\.00%/)).toBeNull();
    expect(within(row).queryByText(/%/)).toBeNull();
  });

  it("navigates to stock detail when a row is clicked", async () => {
    vi.mocked(listWatchlist).mockResolvedValue({ items: [upEntry] });

    renderWatchlistPage();
    await waitFor(() => expect(listWatchlist).toHaveBeenCalled());

    fireEvent.click(await screen.findByText("贵州茅台"));
    expect(navigateMock).toHaveBeenCalledWith("/stocks/detail?symbol=600519.SH");
  });

  it("adds a stock chosen from the instrument catalog, not free text", async () => {
    vi.mocked(listWatchlist).mockResolvedValue({ items: [] });
    vi.mocked(listInstrumentCatalog).mockResolvedValue({
      items: [
        {
          symbol: "600519.SH",
          display_name: "贵州茅台",
          market: "SH",
          instrument_type: "stock",
          is_tradable: true,
          last_sync_source: "akshare",
          last_sync_at: null,
          raw: null,
          created_at: null,
          updated_at: null,
        },
      ],
      total: 1,
    });
    vi.mocked(addWatchlistEntry).mockResolvedValue(upEntry);

    renderWatchlistPage();
    await waitFor(() => expect(listWatchlist).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "添加股票" }));

    // Options come from the existing stock database (instrument catalog) — the
    // external universe search is never used (no manual / off-catalog entry).
    await waitFor(() => expect(listInstrumentCatalog).toHaveBeenCalled());
    expect(searchInstrumentUniverse).not.toHaveBeenCalled();

    // Pick a row from the catalog dropdown and submit. (antd inserts a space
    // between the two CJK chars of the OK button, so match "添 加" loosely and
    // scope to the dialog to avoid the page-level "添加股票" trigger.)
    fireEvent.mouseDown(screen.getByLabelText("股票"));
    fireEvent.click(await screen.findByText("贵州茅台 (600519.SH)"));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /添\s*加/ }));

    // The new entry carries the symbol AND the display name pulled from the catalog.
    await waitFor(() =>
      expect(addWatchlistEntry).toHaveBeenCalledWith(
        expect.objectContaining({ symbol: "600519.SH", display_name: "贵州茅台" }),
      ),
    );
  });
});
