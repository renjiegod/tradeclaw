"""Per-symbol intraday monitoring state (seal-volume peak + board-open flag).

This is the SOURCE OF TRUTH for the two preset families that need history:

- 大减 (seal shrink): needs the intraday PEAK of the limit seal volume, so a
  drop from that peak can be measured. ``seal_peak_bid`` / ``seal_peak_ask``.
- 打开 (board open): needs the PRIOR tick's sealed flag, so a price leaving the
  limit (sealed → unsealed) is a detectable transition. ``last_sealed_up`` /
  ``last_sealed_down``.

State is keyed by SYMBOL (a market fact, shared across rules), reset at the start
of each A-share trading day. It is intentionally NOT persisted: it is hot,
per-tick mutable, and re-derivable from the stream. A daemon restart mid-session
re-seeds peaks from subsequent ticks; the daemon emits ``monitor_state_rehydrated``
so the gap is visible rather than silent (CLAUDE.md §错误可见性).

Detectors read state reflecting PRIOR ticks only; the daemon calls
``update_from_snapshot`` AFTER evaluation so the current tick folds into the
peak/flag for the next tick. This keeps detectors pure functions of
(snapshot, prior_state) and makes board-open a true rising-edge transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")

# Price tolerance (yuan) for "is this price at the limit". Absorbs tick rounding
# so a price one cent inside the limit still counts as sealed.
PRICE_EPS = 0.005


def trading_day_for(instant: datetime, *, timezone: str = "Asia/Shanghai") -> str:
    """Asia/Shanghai local date (``YYYY-MM-DD``) used as the daily-reset key."""
    return instant.astimezone(ZoneInfo(timezone)).date().isoformat()


def at_limit_up(price: float | None, limit_up_price: float | None) -> bool:
    return (
        price is not None
        and limit_up_price is not None
        and price >= limit_up_price - PRICE_EPS
    )


def at_limit_down(price: float | None, limit_down_price: float | None) -> bool:
    return (
        price is not None
        and limit_down_price is not None
        and price <= limit_down_price + PRICE_EPS
    )


@dataclass
class SymbolIntradayState:
    """Mutable per-symbol state for one trading day."""

    symbol: str
    trading_day: str
    seal_peak_bid: int | None = None  # 涨停封单量峰值 (bid_vol[0])
    seal_peak_ask: int | None = None  # 跌停封单量峰值 (ask_vol[0])
    last_sealed_up: bool = False
    last_sealed_down: bool = False
    last_price: float | None = None
    last_seen_ts: str | None = None

    def fold_snapshot(self, snapshot) -> None:
        """Fold the current tick into peaks/flags (called AFTER evaluation)."""
        price = getattr(snapshot, "price", None)
        lu = getattr(snapshot, "limit_up_price", None)
        ld = getattr(snapshot, "limit_down_price", None)
        bid = getattr(snapshot, "bid_vol1", None)
        ask = getattr(snapshot, "ask_vol1", None)

        sealed_up = at_limit_up(price, lu)
        sealed_down = at_limit_down(price, ld)

        if sealed_up and bid is not None:
            self.seal_peak_bid = bid if self.seal_peak_bid is None else max(self.seal_peak_bid, bid)
        if sealed_down and ask is not None:
            self.seal_peak_ask = ask if self.seal_peak_ask is None else max(self.seal_peak_ask, ask)

        self.last_sealed_up = sealed_up
        self.last_sealed_down = sealed_down
        self.last_price = price
        self.last_seen_ts = getattr(snapshot, "timestamp", None)


class IntradayStateStore:
    """In-process per-symbol state with lazy daily reset."""

    def __init__(self) -> None:
        self._states: dict[str, SymbolIntradayState] = {}

    def get_or_reset(self, symbol: str, trading_day: str) -> SymbolIntradayState:
        """Return the symbol's state, resetting it if the trading day rolled over.

        Returns the state reflecting PRIOR ticks of ``trading_day`` (a fresh,
        empty state on the first tick of a new day).
        """
        state = self._states.get(symbol)
        if state is None or state.trading_day != trading_day:
            state = SymbolIntradayState(symbol=symbol, trading_day=trading_day)
            self._states[symbol] = state
        return state

    def reset_day(self, trading_day: str) -> int:
        """Drop all states not belonging to ``trading_day``. Returns dropped count."""
        stale = [s for s, st in self._states.items() if st.trading_day != trading_day]
        for symbol in stale:
            del self._states[symbol]
        return len(stale)

    def forget(self, symbols: set[str]) -> int:
        """Drop state for symbols no longer monitored. Returns dropped count."""
        gone = [s for s in self._states if s not in symbols]
        for symbol in gone:
            del self._states[symbol]
        return len(gone)

    def size(self) -> int:
        return len(self._states)
