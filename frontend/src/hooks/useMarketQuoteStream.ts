import { useEffect, useRef, useState } from "react";

import { WS_BASE } from "../api";
import type { QuoteSnapshot } from "../types";

/** Shape returned to consumers of {@link useMarketQuoteStream}. */
export type MarketQuoteStreamState = {
  /** Latest quote per symbol, keyed by canonical symbol. */
  quotes: Record<string, QuoteSnapshot>;
  /** True while the underlying WebSocket is OPEN. */
  connected: boolean;
  /**
   * True when the backend pushed a ``status: "qmt_disconnected"`` frame (no
   * default QMT account / upstream unavailable). The page should surface this
   * as a banner and render ``—`` for every quote column.
   */
  qmtDisconnected: boolean;
};

/** Server → client frame on ``/ws/market/quotes``. */
type ServerFrame =
  | { type: "snapshot"; quotes: QuoteSnapshot[] }
  | { type: "quote"; quote: QuoteSnapshot }
  | { type: "status"; status: "qmt_disconnected" };

function parseFrame(raw: unknown): ServerFrame | null {
  if (typeof raw !== "object" || raw === null) {
    return null;
  }
  const frame = raw as Record<string, unknown>;
  if (frame.type === "snapshot" && Array.isArray(frame.quotes)) {
    return { type: "snapshot", quotes: frame.quotes as QuoteSnapshot[] };
  }
  if (frame.type === "quote" && frame.quote && typeof frame.quote === "object") {
    return { type: "quote", quote: frame.quote as QuoteSnapshot };
  }
  if (frame.type === "status" && frame.status === "qmt_disconnected") {
    return { type: "status", status: "qmt_disconnected" };
  }
  return null;
}

/**
 * Subscribe to live quotes for ``symbols`` over a single WebSocket.
 *
 * Lifecycle:
 * - Opens ``${WS_BASE}/ws/market/quotes`` on mount and sends a ``subscribe``
 *   frame on ``onopen``.
 * - When ``symbols`` changes the same socket is reused: it re-sends a
 *   ``subscribe`` frame with the new set (no reconnect).
 * - ``snapshot`` / ``quote`` frames update the per-symbol cache; a ``status``
 *   ``qmt_disconnected`` frame flips ``qmtDisconnected`` on (cleared again once
 *   a real quote/snapshot arrives).
 * - On ``document.visibilitychange`` the socket is closed while hidden and
 *   reopened when the tab becomes visible again, to avoid wasted streaming.
 * - The socket is closed on unmount.
 *
 * Passing an empty ``symbols`` array still opens the socket but subscribes to
 * nothing, so a later non-empty update flows over the same connection.
 */
export function useMarketQuoteStream(symbols: string[]): MarketQuoteStreamState {
  const [quotes, setQuotes] = useState<Record<string, QuoteSnapshot>>({});
  const [connected, setConnected] = useState(false);
  const [qmtDisconnected, setQmtDisconnected] = useState(false);

  const socketRef = useRef<WebSocket | null>(null);
  // Hold the latest desired subscription so a freshly (re)opened socket can
  // pick it up in ``onopen`` without needing it in the connect effect deps.
  const symbolsRef = useRef<string[]>(symbols);
  symbolsRef.current = symbols;

  const sendSubscribe = (socket: WebSocket, list: string[]) => {
    if (socket.readyState !== WebSocket.OPEN) {
      return;
    }
    socket.send(JSON.stringify({ action: "subscribe", symbols: list }));
  };

  // Single long-lived socket bound to the component lifetime. Visibility
  // changes tear down / rebuild it; ``symbols`` changes are pushed over the
  // existing one by the effect below.
  useEffect(() => {
    let disposed = false;

    const open = () => {
      if (disposed || socketRef.current) {
        return;
      }
      const socket = new WebSocket(`${WS_BASE}/ws/market/quotes`);
      socketRef.current = socket;

      socket.onopen = () => {
        setConnected(true);
        sendSubscribe(socket, symbolsRef.current);
      };

      socket.onmessage = (event: MessageEvent) => {
        let data: unknown;
        try {
          data = JSON.parse(event.data as string);
        } catch {
          // A non-JSON frame is unexpected; ignore it rather than crashing the
          // stream. The connection stays open for well-formed frames.
          return;
        }
        const frame = parseFrame(data);
        if (!frame) {
          return;
        }
        if (frame.type === "snapshot") {
          setQmtDisconnected(false);
          setQuotes((prev) => {
            const next = { ...prev };
            for (const quote of frame.quotes) {
              next[quote.symbol] = quote;
            }
            return next;
          });
        } else if (frame.type === "quote") {
          setQmtDisconnected(false);
          setQuotes((prev) => ({ ...prev, [frame.quote.symbol]: frame.quote }));
        } else if (frame.type === "status") {
          setQmtDisconnected(true);
        }
      };

      socket.onclose = () => {
        setConnected(false);
        if (socketRef.current === socket) {
          socketRef.current = null;
        }
      };

      socket.onerror = () => {
        // ``onclose`` fires right after ``onerror``; clearing connected state
        // happens there. Nothing to do here beyond letting it close.
      };
    };

    const close = () => {
      const socket = socketRef.current;
      socketRef.current = null;
      setConnected(false);
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onclose = null;
        socket.onerror = null;
        socket.close();
      }
    };

    const handleVisibility = () => {
      if (document.hidden) {
        close();
      } else {
        open();
      }
    };

    if (!document.hidden) {
      open();
    }
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", handleVisibility);
      close();
    };
  }, []);

  // Push subscription updates over the existing socket (no reconnect). Joining
  // on a stable string key avoids re-sending when only the array identity
  // changed but the contents did not.
  const symbolsKey = symbols.join(",");
  useEffect(() => {
    const socket = socketRef.current;
    if (socket) {
      sendSubscribe(socket, symbols);
    }
    // ``symbolsKey`` is the intentional dependency; ``symbols`` is read fresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbolsKey]);

  return { quotes, connected, qmtDisconnected };
}
