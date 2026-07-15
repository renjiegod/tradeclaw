import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useMarketQuoteStream } from "./useMarketQuoteStream";
import type { QuoteSnapshot } from "../types";

// ---- Fake WebSocket --------------------------------------------------------
// Records constructed instances + sent payloads, and lets the test drive
// onopen / onmessage manually.
class FakeWebSocket {
  static OPEN = 1;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  url: string;
  readyState = 0;
  sent: string[] = [];
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({});
  }

  // Test helpers
  fireOpen() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.({});
  }

  fireMessage(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }
}

const OPEN = FakeWebSocket.OPEN;

function makeQuote(symbol: string, overrides: Partial<QuoteSnapshot> = {}): QuoteSnapshot {
  return {
    symbol,
    price: 10,
    prev_close: 9,
    change: 1,
    change_pct: 11.11,
    open: 9.5,
    high: 10.5,
    low: 9.2,
    volume: 1000,
    amount: 123456,
    timestamp: "2026-06-07T01:00:00Z",
    status: "ok",
    ...overrides,
  };
}

type Captured = ReturnType<typeof useMarketQuoteStream>;

function Probe({ symbols, onState }: { symbols: string[]; onState: (s: Captured) => void }) {
  const state = useMarketQuoteStream(symbols);
  onState(state);
  return null;
}

describe("useMarketQuoteStream", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
    // Ensure document.hidden is false in jsdom.
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("opens a socket and sends a subscribe frame on open", () => {
    let latest: Captured | null = null;
    render(<Probe symbols={["600519.SH", "000001.SZ"]} onState={(s) => (latest = s)} />);

    expect(FakeWebSocket.instances).toHaveLength(1);
    const ws = FakeWebSocket.instances[0]!;
    expect(ws.url).toMatch(/\/ws\/market\/quotes$/);

    act(() => {
      ws.fireOpen();
    });

    expect(latest!.connected).toBe(true);
    expect(ws.sent).toHaveLength(1);
    expect(JSON.parse(ws.sent[0]!)).toEqual({
      action: "subscribe",
      symbols: ["600519.SH", "000001.SZ"],
    });
  });

  it("updates quotes from snapshot and quote frames", () => {
    let latest: Captured | null = null;
    render(<Probe symbols={["600519.SH"]} onState={(s) => (latest = s)} />);
    const ws = FakeWebSocket.instances[0]!;
    act(() => ws.fireOpen());

    act(() => {
      ws.fireMessage({ type: "snapshot", quotes: [makeQuote("600519.SH", { price: 1700 })] });
    });
    expect(latest!.quotes["600519.SH"]?.price).toBe(1700);

    act(() => {
      ws.fireMessage({ type: "quote", quote: makeQuote("600519.SH", { price: 1750 }) });
    });
    expect(latest!.quotes["600519.SH"]?.price).toBe(1750);
  });

  it("flips qmtDisconnected on a status frame and clears it on a later quote", () => {
    let latest: Captured | null = null;
    render(<Probe symbols={["600519.SH"]} onState={(s) => (latest = s)} />);
    const ws = FakeWebSocket.instances[0]!;
    act(() => ws.fireOpen());

    act(() => {
      ws.fireMessage({ type: "status", status: "qmt_disconnected" });
    });
    expect(latest!.qmtDisconnected).toBe(true);

    act(() => {
      ws.fireMessage({ type: "quote", quote: makeQuote("600519.SH") });
    });
    expect(latest!.qmtDisconnected).toBe(false);
  });

  it("re-sends subscribe over the same socket when symbols change (no reconnect)", () => {
    let latest: Captured | null = null;
    const { rerender } = render(
      <Probe symbols={["600519.SH"]} onState={(s) => (latest = s)} />,
    );
    const ws = FakeWebSocket.instances[0]!;
    act(() => ws.fireOpen());
    expect(ws.sent).toHaveLength(1);

    act(() => {
      rerender(<Probe symbols={["600519.SH", "000001.SZ"]} onState={(s) => (latest = s)} />);
    });

    // Same socket reused (no new instance), new subscribe sent.
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(ws.sent).toHaveLength(2);
    expect(JSON.parse(ws.sent[1]!)).toEqual({
      action: "subscribe",
      symbols: ["600519.SH", "000001.SZ"],
    });
    expect(latest!.connected).toBe(true);
  });

  it("closes the socket on unmount", () => {
    const { unmount } = render(<Probe symbols={["600519.SH"]} onState={() => undefined} />);
    const ws = FakeWebSocket.instances[0]!;
    act(() => ws.fireOpen());
    expect(ws.readyState).toBe(OPEN);

    act(() => {
      unmount();
    });
    expect(ws.readyState).toBe(FakeWebSocket.CLOSED);
  });
});
