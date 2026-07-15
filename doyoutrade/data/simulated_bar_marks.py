"""Simulated-clock bar closes for MTM equity and consistent fill pricing.

When ``CycleRunState.clock_mode == "simulated"``, merge each symbol's **daily** (or
configured interval) **close** for the logical cycle time into
:class:`~doyoutrade.domain.models.MarketContext` and into
:class:`~doyoutrade.data.mock_provider.MockTradingDataProvider` ``_symbol_to_price`` so
paper / backtest equity matches historical marks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from doyoutrade.account.store_reader import StoreBackedAccountReader
from doyoutrade.core.cycle_state import CycleRunState
from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.mock_provider import MockTradingDataProvider
from doyoutrade.core.models import MarketContext, PositionSnapshot
from doyoutrade.debug import emit_debug_event
from doyoutrade.money.decimal_helpers import decimal_from_number

_INTRADAY_INTERVALS = frozenset({"1m", "5m", "15m", "30m", "60m"})

#: ``ResolvedMark.source`` values. Each maps to a distinct failure / success
#: mode so the overlay and ``PositionManager`` can branch on a structured
#: field instead of free text (§错误可见性).
MARK_EXACT = "exact"
MARK_CARRY_FORWARD = "carry_forward"
MARK_SUSPENDED_CARRY_FORWARD = "suspended_carry_forward"
MARK_SUSPENDED_NO_PRIOR = "suspended_no_prior"
MARK_NO_DATA = "no_data"


def _is_intraday_interval(interval: str) -> bool:
    return str(interval or "").strip().lower() in _INTRADAY_INTERVALS


@dataclass(frozen=True)
class ResolvedMark:
    """Outcome of resolving a symbol's reference close for one cycle time.

    ``close`` is the best available mark: the cycle-day bar when present, else
    the most recent bar at or before the cycle time carried forward (the
    standard last-trade convention for MTM). ``close`` is ``None`` only when the
    symbol has no bar at or before the cycle time at all.

    ``tradeable`` is ``False`` only when the cycle day is a *confirmed halt*
    (the upstream reported ``tradestatus==0`` for it). A buy must not fill on a
    halt even though ``close`` carries a mark for position MTM — the
    distinction the backtest needs between a genuine suspension and a plain
    upstream data gap. When suspension info is unavailable (provider doesn't
    track halts, or a warm-cache window predating the capture feature) a missing
    bar is treated as a gap (``tradeable=True``), matching how full-coverage
    sources like QMT behave.
    """

    close: float | None
    source: str
    as_of_day: str | None
    staleness_days: int
    tradeable: bool


def _staleness_days(cycle_day: str, as_of_day: str | None) -> int:
    if not as_of_day:
        return 0
    try:
        return max(
            0, (date.fromisoformat(cycle_day[:10]) - date.fromisoformat(as_of_day[:10])).days
        )
    except ValueError:
        return 0


async def _suspended_on(
    data_provider: Any,
    *,
    symbol: str,
    cycle_day: str,
    start: str,
    end: str,
    interval: str,
) -> bool:
    """True when *cycle_day* is a persisted halt for *symbol* (best-effort)."""
    accessor = getattr(data_provider, "suspended_days_in_range", None)
    if accessor is None:
        return False
    suspended = await accessor(symbol, start, end, interval=interval)
    return cycle_day[:10] in {str(d)[:10] for d in (suspended or ())}


async def resolve_mark_for_cycle_time(
    data_provider: Any,
    *,
    symbol: str,
    cycle_time: datetime,
    interval: str = "1d",
) -> ResolvedMark:
    """Resolve the reference close for *symbol* at *cycle_time*.

    Daily intervals resolve by trading-day date; intraday intervals match the
    exact bar timestamp first. When the cycle-point bar is absent the most
    recent bar at or before it is carried forward, and the cycle day is
    classified as a halt vs a data gap via the persisted suspension signal —
    see :class:`ResolvedMark`.
    """
    interval_key = str(interval or "1d").strip().lower() or "1d"
    trading_day = cycle_time.date()
    day_str = trading_day.isoformat()
    start = (trading_day - timedelta(days=21)).isoformat()
    end = (trading_day + timedelta(days=21)).isoformat()
    intraday = _is_intraday_interval(interval_key)
    target_point = (
        normalize_bar_timestamp(cycle_time.isoformat()) if intraday else day_str
    )

    bars = await data_provider.get_bars(symbol, start, end, interval=interval_key)

    # ``same_day_close`` is the normal cycle-day resolution (the daily bar, or
    # for intraday the latest bar at/before the cycle time on the cycle day).
    # ``prior_*`` is the carry-forward candidate: the latest bar on a strictly
    # earlier day. Splitting them keeps an ordinary intraday "fired between
    # bars" resolve from being mislabelled as a cross-day carry-forward.
    same_day_ts: str | None = None
    same_day_close: float | None = None
    prior_ts: str | None = None
    prior_close: float | None = None
    prior_day: str | None = None
    for b in bars:
        ts = normalize_bar_timestamp(b.timestamp)
        if len(ts) < 10:
            continue
        day = ts[:10]
        if day == day_str:
            # Cycle-day bar. For daily there is at most one; for intraday keep
            # the latest bar at or before the cycle time.
            if not intraday:
                same_day_close = float(b.close)
            elif ts <= target_point and (same_day_ts is None or ts >= same_day_ts):
                same_day_ts = ts
                same_day_close = float(b.close)
        elif day < day_str and (prior_ts is None or ts > prior_ts):
            # Latest bar on a strictly earlier day → carry-forward candidate.
            prior_ts = ts
            prior_close = float(b.close)
            prior_day = day

    if same_day_close is not None and same_day_close > 0.0:
        return ResolvedMark(same_day_close, MARK_EXACT, day_str, 0, tradeable=True)

    is_suspended = await _suspended_on(
        data_provider,
        symbol=symbol,
        cycle_day=day_str,
        start=start,
        end=end,
        interval=interval_key,
    )

    if prior_close is not None and prior_close > 0.0:
        staleness = _staleness_days(day_str, prior_day)
        if is_suspended:
            return ResolvedMark(
                prior_close, MARK_SUSPENDED_CARRY_FORWARD, prior_day, staleness, tradeable=False
            )
        return ResolvedMark(
            prior_close, MARK_CARRY_FORWARD, prior_day, staleness, tradeable=True
        )

    if is_suspended:
        return ResolvedMark(None, MARK_SUSPENDED_NO_PRIOR, None, 0, tradeable=False)
    return ResolvedMark(None, MARK_NO_DATA, None, 0, tradeable=True)


async def bar_close_for_trading_day(
    data_provider: Any,
    *,
    symbol: str,
    trading_day: date,
    interval: str = "1d",
) -> float | None:
    """Return the bar **close** for *trading_day*, carrying the last close forward.

    Thin wrapper over :func:`resolve_mark_for_cycle_time`: returns the resolved
    mark (exact or carried-forward), or ``None`` when no bar exists at or before
    the day. Used by the MTM-seed helpers, which only need the price.
    """
    resolved = await resolve_mark_for_cycle_time(
        data_provider,
        symbol=symbol,
        cycle_time=datetime.combine(trading_day, datetime.min.time()),
        interval=interval,
    )
    return resolved.close


async def bar_close_for_cycle_time(
    data_provider: Any,
    *,
    symbol: str,
    cycle_time: datetime,
    interval: str = "1d",
) -> float | None:
    """Return the resolved reference close for *cycle_time* (carry-forward aware)."""
    resolved = await resolve_mark_for_cycle_time(
        data_provider, symbol=symbol, cycle_time=cycle_time, interval=interval
    )
    return resolved.close


def mock_trading_store_from_account_reader(account_reader: Any) -> MockTradingDataProvider | None:
    if isinstance(account_reader, StoreBackedAccountReader):
        store = getattr(account_reader, "_store", None)
        if isinstance(store, MockTradingDataProvider):
            return store
    return None


def reset_mock_ledger_for_fresh_backtest(account_reader: Any) -> bool:
    """Clear mock cash/positions to factory defaults (bars unchanged). Returns False if no mock store."""
    store = mock_trading_store_from_account_reader(account_reader)
    if store is None:
        return False
    store.reset_ledger_to_factory_defaults()
    return True


async def backtest_mtm_seed_symbol_list(account_reader: Any, cycle_task: Any) -> list[str]:
    """Symbols to MTM-seed for backtest (universe + held positions)."""
    positions = await account_reader.get_positions()
    ordered: list[str] = []
    if cycle_task is not None:
        cfg = getattr(cycle_task, "config", None)
        if cfg is not None:
            ordered.extend(list(getattr(cfg, "universe", ()) or ()))
    for p in positions:
        ordered.append(p.symbol)
    return list(dict.fromkeys(s for s in ordered if s))


async def seed_mock_ledger_prices_for_trading_day(
    *,
    data_provider: Any,
    account_reader: Any,
    trading_day: date,
    symbols: list[str],
    bar_interval: str = "1d",
) -> None:
    """Write historical closes into the mock store for *trading_day* (MTM only, no fills).

    Used so ``reference_starting_equity`` matches the same bar-close convention as each
    ``run_cycle`` (avoids valuing positions at ``cost_price`` before the first merge).
    """
    store = mock_trading_store_from_account_reader(account_reader)
    if store is None or not symbols:
        return
    for sym in symbols:
        close = await bar_close_for_trading_day(
            data_provider,
            symbol=sym,
            trading_day=trading_day,
            interval=bar_interval or "1d",
        )
        if close is None or close <= 0.0:
            continue
        store._symbol_to_price[sym] = decimal_from_number(close)
    store._mark_equity_from_positions()


async def seed_mock_ledger_prices_for_cycle_time(
    *,
    data_provider: Any,
    account_reader: Any,
    cycle_time: datetime,
    symbols: list[str],
    bar_interval: str = "1d",
) -> None:
    """Write historical closes into the mock store for the exact cycle time."""
    store = mock_trading_store_from_account_reader(account_reader)
    if store is None or not symbols:
        return
    for sym in symbols:
        close = await bar_close_for_cycle_time(
            data_provider,
            symbol=sym,
            cycle_time=cycle_time,
            interval=bar_interval or "1d",
        )
        if close is None or close <= 0.0:
            continue
        store._symbol_to_price[sym] = decimal_from_number(close)
    store._mark_equity_from_positions()


def sim_mtm_symbol_list(cycle_state: CycleRunState, positions: list[PositionSnapshot]) -> list[str]:
    ordered: list[str] = []
    inst = cycle_state.cycle_task
    if inst is not None:
        ordered.extend(inst.config.universe)
    for p in positions:
        ordered.append(p.symbol)
    return list(dict.fromkeys(s for s in ordered if s))


async def merge_simulated_bar_marks_into_market(
    *,
    data_provider: Any,
    account_reader: Any,
    cycle_state: CycleRunState,
    market_context: MarketContext,
    positions_preview: list[PositionSnapshot],
    bar_interval: str = "1d",
) -> MarketContext:
    """Overlay simulated-day bar closes onto *market_context* when the cycle clock is simulated.

    The price/tick overlay is the **sole** source of quote data for a backtest
    cycle: ``CachedBarsDataProvider`` returns an empty :class:`MarketContext`
    when ``scope == "backtest"`` precisely so that live providers
    (QMT / akshare / baostock) never get asked for wall-clock realtime ticks
    during an offline replay. This function populates the empty context from
    ``data_provider.get_bars`` (which the cached provider serves out of the
    preloaded bar cache). The in-place mock-ledger MTM write is the only
    step gated by the mock-store presence.
    """
    if cycle_state.clock_mode != "simulated" or cycle_state.cycle_time is None:
        return market_context
    if data_provider is None or not hasattr(data_provider, "get_bars"):
        return market_context
    ct = cycle_state.cycle_time
    assert ct is not None
    mock_store = mock_trading_store_from_account_reader(account_reader)

    symbols = sim_mtm_symbol_list(cycle_state, positions_preview)
    if not symbols:
        return market_context

    prices = dict(market_context.symbol_to_price or {})
    ticks = dict(market_context.symbol_to_tick or {})
    interval = bar_interval or "1d"
    cycle_day = ct.date().isoformat()

    for sym in symbols:
        resolved = await resolve_mark_for_cycle_time(
            data_provider, symbol=sym, cycle_time=ct, interval=interval
        )
        close = resolved.close

        if close is None or close <= 0.0:
            # No mark at all — the symbol gets no price, so PositionManager
            # will skip it. Replace the legacy silent ``continue`` with a
            # structured event so the operator can tell a confirmed halt from a
            # plain data gap instead of only seeing the downstream
            # ``no_reference_price`` skip (§错误可见性).
            if resolved.source == MARK_SUSPENDED_NO_PRIOR:
                await emit_debug_event(
                    "simulated_mark_suspended",
                    {
                        "symbol": sym,
                        "cycle_day": cycle_day,
                        "source": resolved.source,
                        "tradeable": False,
                        "reason": "symbol_suspended_no_prior_close",
                        "hint": (
                            "symbol was halted on the cycle day and has no prior "
                            "cached close to carry forward; it cannot be marked or "
                            "traded this cycle — backfill earlier history if a mark "
                            "is required"
                        ),
                    },
                )
            else:
                await emit_debug_event(
                    "simulated_mark_unavailable",
                    {
                        "symbol": sym,
                        "cycle_day": cycle_day,
                        "source": resolved.source,
                        "reason": "no_bar_at_or_before_cycle_day",
                        "hint": (
                            "no cached bar at or before the cycle day (symbol not yet "
                            "listed, delisted, or the preload window misses it) — check "
                            "universe provisioning / the backtest preload range"
                        ),
                    },
                )
            continue

        prices[sym] = close
        if mock_store is not None:
            mock_store._symbol_to_price[sym] = decimal_from_number(close)
        prev = ticks.get(sym) if isinstance(ticks.get(sym), dict) else {}
        base = dict(prev) if isinstance(prev, dict) else {}
        base["close"] = close
        base["last"] = close
        # Tradeability flag consumed by PositionManager: a halted symbol still
        # carries a mark (for held-position MTM) but must not accept a buy.
        base["tradeable"] = resolved.tradeable
        base["mark_source"] = resolved.source
        base["mark_as_of_day"] = resolved.as_of_day
        base["mark_staleness_days"] = resolved.staleness_days
        ticks[sym] = base

        if resolved.source == MARK_CARRY_FORWARD:
            await emit_debug_event(
                "simulated_mark_carry_forward",
                {
                    "symbol": sym,
                    "cycle_day": cycle_day,
                    "source": resolved.source,
                    "mark_as_of_day": resolved.as_of_day,
                    "staleness_days": resolved.staleness_days,
                    "tradeable": True,
                    "reason": "cycle_day_bar_missing_data_gap",
                    "hint": (
                        "no cycle-day bar but not a confirmed halt — carried the last "
                        "close forward so the buy can price; if this repeats, the "
                        "upstream (e.g. baostock) is missing trading-day rows and the "
                        "window should be backfilled"
                    ),
                },
            )
        elif resolved.source == MARK_SUSPENDED_CARRY_FORWARD:
            await emit_debug_event(
                "simulated_mark_suspended",
                {
                    "symbol": sym,
                    "cycle_day": cycle_day,
                    "source": resolved.source,
                    "mark_as_of_day": resolved.as_of_day,
                    "staleness_days": resolved.staleness_days,
                    "tradeable": False,
                    "reason": "symbol_suspended_carry_forward",
                    "hint": (
                        "symbol was halted on the cycle day; last close carried forward "
                        "for MTM only — PositionManager will block a buy with "
                        "reason=symbol_suspended"
                    ),
                },
            )

    return MarketContext(symbol_to_price=prices, symbol_to_tick=ticks)
