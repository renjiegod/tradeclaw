import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { StockDetailPage } from "./StockDetailPage";
import {
  addWatchlistEntry,
  deleteWatchlistEntry,
  getInstrumentCatalogItem,
  listWatchlist,
} from "../api";
import type { InstrumentCatalogRow, WatchlistEntry } from "../types";

vi.mock("../api", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.status = status;
    }
  },
  getInstrumentCatalogItem: vi.fn(),
  syncInstrumentCatalog: vi.fn(),
  listWatchlist: vi.fn(),
  addWatchlistEntry: vi.fn(),
  deleteWatchlistEntry: vi.fn(),
}));

vi.mock("../components/LocalMarketKlinePanel", () => ({
  LocalMarketKlinePanel: ({ symbol }: { symbol: string }) => (
    <div data-testid="kline-panel">{symbol}</div>
  ),
}));

const catalogRow: InstrumentCatalogRow = {
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
};

const watchlistRow: WatchlistEntry = {
  id: "wl-aaa",
  symbol: "600519.SH",
  display_name: "贵州茅台",
  tags: ["白酒", "龙头"],
  note: "",
  sort_order: 0,
  created_at: null,
  updated_at: null,
};

function renderDetail(symbol = "600519.SH") {
  return render(
    <MemoryRouter initialEntries={[`/stocks/detail?symbol=${encodeURIComponent(symbol)}`]}>
      <Routes>
        <Route path="/stocks/detail" element={<StockDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("StockDetailPage", () => {
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
    vi.mocked(getInstrumentCatalogItem).mockResolvedValue(catalogRow);
    vi.mocked(listWatchlist).mockResolvedValue({ items: [] });
  });

  afterEach(() => {
    cleanup();
  });

  it("shows watchlist tags and remove button when the symbol is in watchlist", async () => {
    vi.mocked(listWatchlist).mockResolvedValue({ items: [watchlistRow] });

    renderDetail();
    await waitFor(() => expect(getInstrumentCatalogItem).toHaveBeenCalled());

    expect(await screen.findByText("白酒")).toBeInTheDocument();
    expect(screen.getByText("龙头")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "移除自选" })).toBeInTheDocument();
  });

  it("renders compact information and chart sections", async () => {
    renderDetail();
    await waitFor(() => expect(getInstrumentCatalogItem).toHaveBeenCalled());

    expect(screen.getByRole("heading", { name: /贵州茅台/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "本地 K 线" })).toBeInTheDocument();
  });

  it("adds the symbol to watchlist from the toggle button", async () => {
    vi.mocked(addWatchlistEntry).mockResolvedValue(watchlistRow);

    renderDetail();
    await waitFor(() => expect(getInstrumentCatalogItem).toHaveBeenCalled());

    fireEvent.click(await screen.findByRole("button", { name: /加入自选/ }));

    await waitFor(() =>
      expect(addWatchlistEntry).toHaveBeenCalledWith({
        symbol: "600519.SH",
        display_name: "贵州茅台",
      }),
    );
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /移除自选/ })).toBeInTheDocument();
    });
  });

  it("removes the symbol from watchlist from the toggle button", async () => {
    vi.mocked(listWatchlist).mockResolvedValue({ items: [watchlistRow] });
    vi.mocked(deleteWatchlistEntry).mockResolvedValue(undefined);

    renderDetail();
    await waitFor(() => expect(getInstrumentCatalogItem).toHaveBeenCalled());

    fireEvent.click(await screen.findByRole("button", { name: /移除自选/ }));

    await waitFor(() => expect(deleteWatchlistEntry).toHaveBeenCalledWith("wl-aaa"));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /加入自选/ })).toBeInTheDocument();
    });
  });

  it("uses the resolved catalog symbol for watchlist and chart actions", async () => {
    vi.mocked(getInstrumentCatalogItem).mockResolvedValue({
      ...catalogRow,
      symbol: "000001.SZ",
      display_name: "平安银行",
    });
    vi.mocked(addWatchlistEntry).mockResolvedValue({
      ...watchlistRow,
      symbol: "000001.SZ",
      display_name: "平安银行",
      tags: [],
    });

    renderDetail("000001.SH");
    await waitFor(() => expect(getInstrumentCatalogItem).toHaveBeenCalledWith("000001.SH"));

    expect(screen.getByTestId("kline-panel")).toHaveTextContent("000001.SZ");

    fireEvent.click(await screen.findByRole("button", { name: /加入自选/ }));

    await waitFor(() =>
      expect(addWatchlistEntry).toHaveBeenCalledWith({
        symbol: "000001.SZ",
        display_name: "平安银行",
      }),
    );
  });
});
