import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { formatSymbolWithName, useSymbolNames } from "./useSymbolNames";
import { getInstrumentCatalogItem } from "../api";

vi.mock("../api", () => ({
  getInstrumentCatalogItem: vi.fn(),
}));

const mockGet = vi.mocked(getInstrumentCatalogItem);

function Probe({ symbols }: { symbols: string[] }) {
  const names = useSymbolNames(symbols);
  return (
    <ul>
      {symbols.map((sym) => (
        <li key={sym} data-testid={`row-${sym}`}>
          {formatSymbolWithName(sym, names)}
        </li>
      ))}
    </ul>
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("formatSymbolWithName", () => {
  it("renders 中文名 (代码) when a name is known, else the bare code", () => {
    const names = { "002855.SZ": "惠城环保", "600519.SH": null };
    expect(formatSymbolWithName("002855.SZ", names)).toBe("惠城环保 (002855.SZ)");
    expect(formatSymbolWithName("600519.SH", names)).toBe("600519.SH");
    expect(formatSymbolWithName("000001.SZ", names)).toBe("000001.SZ");
  });
});

describe("useSymbolNames", () => {
  it("resolves names from the catalog and falls back to the code on lookup failure", async () => {
    mockGet.mockImplementation(async (symbol: string) => {
      if (symbol === "002855.SZ") {
        return { symbol, display_name: "惠城环保" } as Awaited<
          ReturnType<typeof getInstrumentCatalogItem>
        >;
      }
      throw new Error("symbol not in catalog");
    });

    render(<Probe symbols={["002855.SZ", "999999.SZ"]} />);

    // Before resolution the row shows the raw code.
    expect(screen.getByTestId("row-002855.SZ").textContent).toBe("002855.SZ");

    await waitFor(() =>
      expect(screen.getByTestId("row-002855.SZ").textContent).toBe("惠城环保 (002855.SZ)"),
    );
    // Missing symbol stays as the code (lookup threw).
    expect(screen.getByTestId("row-999999.SZ").textContent).toBe("999999.SZ");
  });
});
