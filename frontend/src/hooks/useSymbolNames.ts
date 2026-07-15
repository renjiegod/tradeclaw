import { useEffect, useState } from "react";

import { getInstrumentCatalogItem } from "../api";

// Module-level cache shared across components/renders so the same symbol is
// resolved at most once per session. Value is the display name, or null when
// the symbol has no catalog name / the lookup failed (fall back to the code).
const symbolNameCache = new Map<string, string | null>();
const inflight = new Map<string, Promise<void>>();

/** Format a symbol as ``中文名 (代码)``, falling back to the raw code. */
export function formatSymbolWithName(
  symbol: string,
  names: Record<string, string | null>,
): string {
  const name = names[symbol];
  return name ? `${name} (${symbol})` : symbol;
}

async function resolveSymbol(symbol: string): Promise<void> {
  if (symbolNameCache.has(symbol)) return;
  let pending = inflight.get(symbol);
  if (!pending) {
    pending = (async () => {
      try {
        const row = await getInstrumentCatalogItem(symbol);
        symbolNameCache.set(symbol, row?.display_name?.trim() || null);
      } catch {
        // 404 (symbol not in catalog) or network error: no name available, so
        // callers fall back to the raw code. Cached as null to avoid retries.
        symbolNameCache.set(symbol, null);
      } finally {
        inflight.delete(symbol);
      }
    })();
    inflight.set(symbol, pending);
  }
  await pending;
}

/**
 * Resolve a set of symbols to their catalog display names.
 *
 * Returns a ``{ symbol -> name | null }`` map; symbols still resolving are
 * simply absent, so {@link formatSymbolWithName} shows the code until the name
 * arrives. Backtest universes are validated into the instrument catalog at task
 * creation, so lookups almost always hit.
 */
export function useSymbolNames(symbols: string[]): Record<string, string | null> {
  const [names, setNames] = useState<Record<string, string | null>>(() => {
    const seed: Record<string, string | null> = {};
    for (const symbol of symbols) {
      if (symbolNameCache.has(symbol)) seed[symbol] = symbolNameCache.get(symbol) ?? null;
    }
    return seed;
  });

  // Stable dependency: only refire when the distinct symbol set changes.
  const key = Array.from(new Set(symbols)).sort().join(",");

  useEffect(() => {
    let cancelled = false;
    const unique = Array.from(new Set(symbols)).filter((symbol) => symbol);
    if (unique.length === 0) return;

    void Promise.all(unique.map(resolveSymbol)).then(() => {
      if (cancelled) return;
      const next: Record<string, string | null> = {};
      for (const symbol of unique) next[symbol] = symbolNameCache.get(symbol) ?? null;
      setNames(next);
    });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return names;
}
