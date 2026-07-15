"""DataProvider — the ``ctx.dp`` facade strategies use to fetch market data.

This is the **only** sanctioned data-access surface inside a Strategy. The
:class:`StrategyCompiler` AST pass refuses ``import requests / akshare /
doyoutrade.data.*`` so user code is forced through here, and every method is
auto-instrumented with:

- An OTel span named ``strategy.dp.<method>`` carrying ``run_id`` / ``symbol``
  / call args summary. Exports to ``debug_session_spans`` via the existing
  worker span exporter (CLAUDE.md "trace 贯穿" requirement).
- A structured debug event ``strategy_dp_<method>`` (success) or
  ``strategy_dp_<method>_failed`` (with ``error_code`` and ``hint``).
- A cycle-scoped cache keyed on the call signature, so repeated lookups
  inside a single ``on_bar`` invocation (or across ``populate_indicators``
  → ``on_bar``) hit memory instead of the underlying provider.
- Typed errors (subclasses of :class:`DataAccessError` /
  :class:`InformativeDataError`) — no silent ``return None`` /
  ``return empty_df`` fallbacks (CLAUDE.md "错误可见性").

Symbolic references resolved here:

- ``symbol=None`` or ``symbol="$self"`` → :attr:`current_symbol`
- ``industry=None`` or ``industry="$self.industry"`` →
  :meth:`resolve_industry` of the current symbol
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Protocol, runtime_checkable

import pandas as pd
from opentelemetry import trace as trace_api

from doyoutrade.debug import emit_debug_event_sync
from doyoutrade.strategy_sdk.data_requests import SELF, SELF_INDUSTRY
from doyoutrade.strategy_sdk.watchlist_snapshot import WatchlistSnapshot
from doyoutrade.strategy_sdk.errors import (
    DATA_INSUFFICIENT,
    INDUSTRY_RESOLUTION_FAILED,
    INFORMATIVE_DATA_NOT_DECLARED,
    INVALID_ARGUMENT,
    INVALID_SYMBOL,
    LIVE_ONLY_METHOD,
    DataAccessError,
    InformativeDataError,
    StrategyError,
)

logger = logging.getLogger(__name__)
_tracer = trace_api.get_tracer(__name__)


# ---------------------------------------------------------------------------
# Backing providers — protocols the DataProvider delegates to. These are
# kept narrow so the runner can wire concrete implementations in bootstrap
# without DataProvider knowing about the broader data stack.
# ---------------------------------------------------------------------------


@runtime_checkable
class HistoryFetcher(Protocol):
    """Fetches a tail window of bars for ONE symbol. Async because the
    underlying TradingDataProvider is async."""

    async def fetch(
        self, symbol: str, *, as_of: datetime, lookback: int, freq: str
    ) -> pd.DataFrame: ...


@runtime_checkable
class IndustryResolver(Protocol):
    """Maps symbol → industry code; lists top-N peers by ranking metric.

    Phase 1 ships with a stub implementation that raises
    ``industry_resolution_failed`` — Phase 2 wires a real industry map.
    """

    def industry_of(self, symbol: str) -> str: ...

    def members_of(
        self, industry: str, *, top_n: int, rank_by: str
    ) -> list[str]: ...


@runtime_checkable
class FundamentalsFetcher(Protocol):
    async def fetch(
        self, symbol: str, *, fields: tuple[str, ...]
    ) -> Mapping[str, Any]: ...


# ---------------------------------------------------------------------------
# Instrumentation decorator
# ---------------------------------------------------------------------------


def _summarize_arg(value: Any) -> Any:
    """Render a single arg into something cheap and JSON-safe for spans."""
    if isinstance(value, pd.DataFrame):
        return {"_type": "DataFrame", "rows": len(value), "cols": list(value.columns)}
    if isinstance(value, (list, tuple)):
        return [_summarize_arg(v) for v in value][:20]
    if isinstance(value, dict):
        return {str(k): _summarize_arg(v) for k, v in list(value.items())[:20]}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return repr(value)[:200]


def _summarize_result(value: Any) -> dict[str, Any]:
    if isinstance(value, pd.DataFrame):
        return {"type": "DataFrame", "rows": len(value), "cols": list(value.columns)}
    if isinstance(value, list):
        return {"type": "list", "len": len(value)}
    if isinstance(value, dict):
        return {"type": "dict", "keys": len(value)}
    return {"type": type(value).__name__}


def _instrument(method_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Wrap a DataProvider method with span + debug event + typed error reporting.

    The wrapper assumes ``self`` is a :class:`DataProvider` so it can pull
    ``run_id`` / ``current_symbol`` for span attributes.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(self: "DataProvider", *args: Any, **kwargs: Any) -> Any:
            span_name = f"strategy.dp.{method_name}"
            with _tracer.start_as_current_span(span_name) as span:
                span.set_attribute("run_id", self._run_id)
                span.set_attribute("current_symbol", self._current_symbol or "")
                span.set_attribute("dp_method", method_name)
                args_summary = {
                    "args": [_summarize_arg(a) for a in args],
                    "kwargs": {k: _summarize_arg(v) for k, v in kwargs.items()},
                }
                try:
                    result = fn(self, *args, **kwargs)
                    summary = _summarize_result(result)
                    span.set_attribute("result_type", summary.get("type", "unknown"))
                    if "rows" in summary:
                        span.set_attribute("result_rows", summary["rows"])
                    emit_debug_event_sync(
                        f"strategy_dp_{method_name}",
                        {
                            "run_id": self._run_id,
                            "current_symbol": self._current_symbol,
                            "method": method_name,
                            "args": args_summary,
                            "result": summary,
                        },
                    )
                    return result
                except StrategyError as e:
                    span.set_attribute("error_code", e.error_code)
                    span.record_exception(e)
                    emit_debug_event_sync(
                        f"strategy_dp_{method_name}_failed",
                        {
                            "run_id": self._run_id,
                            "current_symbol": self._current_symbol,
                            "method": method_name,
                            "args": args_summary,
                            **e.to_dict(),
                        },
                    )
                    logger.warning(
                        "ctx.dp.%s failed (error_code=%s): %s",
                        method_name,
                        e.error_code,
                        e,
                    )
                    raise
                except Exception as e:
                    span.record_exception(e)
                    emit_debug_event_sync(
                        f"strategy_dp_{method_name}_failed",
                        {
                            "run_id": self._run_id,
                            "current_symbol": self._current_symbol,
                            "method": method_name,
                            "args": args_summary,
                            "error_type": type(e).__name__,
                            "message": str(e),
                        },
                    )
                    logger.exception(
                        "ctx.dp.%s raised unexpected %s: %s",
                        method_name,
                        type(e).__name__,
                        e,
                    )
                    raise

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# DataProvider
# ---------------------------------------------------------------------------


@dataclass
class DataProvider:
    """Sync facade over async data backends, scoped to one cycle invocation.

    The runner constructs a fresh DataProvider per ``(cycle × symbol)`` and
    seeds its cache with informative-data prefetches before invoking
    ``populate_indicators`` / ``on_bar``. Methods are sync from the
    strategy's POV — the runner has already awaited the necessary
    background fetches.
    """

    current_symbol: str
    now: datetime
    is_backtest: bool
    declared_symbols: frozenset[str] = field(default_factory=frozenset)
    declared_indexes: frozenset[str] = field(default_factory=frozenset)
    declared_industries: frozenset[str] = field(default_factory=frozenset)
    history_fetcher: HistoryFetcher | None = None
    industry_resolver: IndustryResolver | None = None
    fundamentals_fetcher: FundamentalsFetcher | None = None
    # Frozen per-cycle watchlist view (symbol → tags). Populated by the worker
    # assembly path (Phase B) and read by :meth:`watchlist_symbols`. ``None``
    # means the runtime didn't wire a watchlist — calls raise, never return
    # an empty list silently (CLAUDE.md "错误可见性").
    _watchlist_snapshot: WatchlistSnapshot | None = None
    _cache: dict[tuple, Any] = field(default_factory=dict)
    _run_id: str = ""
    _trace_id: str = ""
    _current_symbol: str = ""

    def __post_init__(self) -> None:
        # Mirror to private attributes used by the instrument decorator.
        self._current_symbol = self.current_symbol

    # ----- Cache management (used by the runner during prefetch) -----

    def seed_cache(self, key: tuple, value: Any) -> None:
        """Insert a precomputed value into the cycle cache.

        Called by the runner during the ``prefetch_informative`` phase to
        warm the cache with declared-data fetches so strategy code reads
        them via the normal ``ctx.dp.*`` API and hits memory.
        """
        self._cache[key] = value

    def cache_size(self) -> int:
        return len(self._cache)

    # ----- Helpers -----

    def _resolve_symbol(self, symbol: str | None) -> str:
        """Resolve ``symbol`` argument: None / "$self" → current_symbol."""
        if symbol is None or symbol == SELF:
            return self.current_symbol
        if not isinstance(symbol, str) or not symbol.strip():
            raise DataAccessError(
                f"symbol must be a non-empty string or None, got {symbol!r}",
                error_code=INVALID_SYMBOL,
            )
        return symbol.strip()

    def _resolve_industry(self, industry: str | None) -> str:
        if industry is None or industry == SELF_INDUSTRY:
            if self.industry_resolver is None:
                raise InformativeDataError(
                    "industry_resolver not wired; $self.industry cannot be resolved",
                    error_code=INDUSTRY_RESOLUTION_FAILED,
                    hint=(
                        "The runtime did not provide an IndustryResolver. "
                        "Wire one in doyoutrade/bootstrap.py before using "
                        "industry-aware data requests."
                    ),
                )
            try:
                return self.industry_resolver.industry_of(self.current_symbol)
            except Exception as e:
                raise InformativeDataError(
                    f"industry_of({self.current_symbol!r}) failed: {e}",
                    error_code=INDUSTRY_RESOLUTION_FAILED,
                    hint=(
                        "Check that the symbol exists in the industry map. "
                        "$self.industry only works for listed equities with "
                        "a registered industry mapping."
                    ),
                ) from e
        return industry.strip()

    def _check_symbol_declared(self, resolved_symbol: str) -> None:
        """Reject access to symbols not covered by informative_data.

        The current symbol is always allowed. Any other symbol must have
        been declared in :meth:`Strategy.informative_data` so the worker
        could prefetch its data.
        """
        if resolved_symbol == self.current_symbol:
            return
        if resolved_symbol in self.declared_symbols:
            return
        raise InformativeDataError(
            f"symbol {resolved_symbol!r} not declared in informative_data",
            error_code=INFORMATIVE_DATA_NOT_DECLARED,
            hint=(
                f"Add DataRequest.bars(symbol={resolved_symbol!r}, "
                "window=...) to your informative_data() return list so the "
                "framework prefetches it before on_bar runs."
            ),
            context={
                "declared_symbols": sorted(self.declared_symbols),
                "requested_symbol": resolved_symbol,
            },
        )

    def _check_index_declared(self, code: str) -> None:
        if code in self.declared_indexes:
            return
        raise InformativeDataError(
            f"index {code!r} not declared in informative_data",
            error_code=INFORMATIVE_DATA_NOT_DECLARED,
            hint=f"Add DataRequest.index_bars({code!r}, window=...) to informative_data().",
            context={"declared_indexes": sorted(self.declared_indexes)},
        )

    # ----- Public data methods -----

    @_instrument("get_bars")
    def get_bars(
        self,
        symbol: str | None = None,
        *,
        window: int,
        freq: str = "1d",
        fields: tuple[str, ...] | None = None,
    ) -> pd.DataFrame:
        """Return historical OHLCV bars for ``symbol`` (defaults to current).

        - ``window``: number of most-recent bars to return; rejects ``<= 0``.
        - ``freq``: bar frequency; defaults to daily.
        - ``fields``: column whitelist; ``None`` returns all OHLCV columns.

        Returns a DataFrame indexed by timestamp (ascending), guaranteed to
        have at least ``window`` rows. If fewer are available raises
        :class:`DataAccessError` with ``data_insufficient`` — never returns
        a short frame silently.
        """
        if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
            raise DataAccessError(
                f"window must be a positive int, got {window!r}",
                error_code=INVALID_ARGUMENT,
            )
        resolved = self._resolve_symbol(symbol)
        self._check_symbol_declared(resolved)

        cache_key = ("bars", resolved, freq, window)
        if cache_key in self._cache:
            df = self._cache[cache_key]
        else:
            df = _await_sync(
                self.history_fetcher.fetch(
                    resolved, as_of=self.now, lookback=window, freq=freq
                )
                if self.history_fetcher is not None
                else None,
                method="get_bars",
                hint="history_fetcher not wired",
            )
            self._cache[cache_key] = df

        if df is None or len(df) < window:
            raise DataAccessError(
                f"insufficient bars for {resolved!r}: requested {window}, "
                f"got {0 if df is None else len(df)}",
                error_code=DATA_INSUFFICIENT,
                hint=(
                    "Either reduce window, lower startup_history, or check "
                    f"that {resolved!r} has enough listed history by ctx.now."
                ),
                context={"symbol": resolved, "requested": window, "got": 0 if df is None else len(df)},
            )

        if fields is not None:
            missing = [f for f in fields if f not in df.columns]
            if missing:
                raise DataAccessError(
                    f"fields not in DataFrame: {missing}",
                    error_code=INVALID_ARGUMENT,
                )
            return df[list(fields)].tail(window).copy()
        return df.tail(window).copy()

    @_instrument("get_index_bars")
    def get_index_bars(
        self, code: str, *, window: int, freq: str = "1d"
    ) -> pd.DataFrame:
        """Return historical OHLCV bars for a market index / ETF."""
        if not isinstance(code, str) or not code.strip():
            raise DataAccessError(
                f"index code must be a non-empty string, got {code!r}",
                error_code=INVALID_SYMBOL,
            )
        if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
            raise DataAccessError(
                f"window must be a positive int, got {window!r}",
                error_code=INVALID_ARGUMENT,
            )
        code = code.strip()
        self._check_index_declared(code)

        cache_key = ("index_bars", code, freq, window)
        if cache_key in self._cache:
            df = self._cache[cache_key]
        else:
            df = _await_sync(
                self.history_fetcher.fetch(
                    code, as_of=self.now, lookback=window, freq=freq
                )
                if self.history_fetcher is not None
                else None,
                method="get_index_bars",
                hint="history_fetcher not wired",
            )
            self._cache[cache_key] = df

        if df is None or len(df) < window:
            raise DataAccessError(
                f"insufficient index bars for {code!r}: requested {window}, "
                f"got {0 if df is None else len(df)}",
                error_code=DATA_INSUFFICIENT,
                context={"code": code, "requested": window, "got": 0 if df is None else len(df)},
            )
        return df.tail(window).copy()

    @_instrument("get_industry_members")
    def get_industry_members(
        self, industry: str | None = None, *, top_n: int = 20, rank_by: str = "market_cap"
    ) -> list[str]:
        """Return top-N industry peer symbols.

        ``industry`` defaults to ``$self.industry`` (current symbol's
        industry). ``rank_by`` selects the ranking metric.
        """
        if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n <= 0:
            raise DataAccessError(
                f"top_n must be a positive int, got {top_n!r}",
                error_code=INVALID_ARGUMENT,
            )
        resolved = self._resolve_industry(industry)
        if self.industry_resolver is None:
            raise InformativeDataError(
                "industry_resolver not wired",
                error_code=INDUSTRY_RESOLUTION_FAILED,
                hint="Wire IndustryResolver in bootstrap.",
            )
        cache_key = ("industry_members", resolved, rank_by, top_n)
        if cache_key in self._cache:
            return list(self._cache[cache_key])
        members = self.industry_resolver.members_of(resolved, top_n=top_n, rank_by=rank_by)
        if not isinstance(members, list):
            raise InformativeDataError(
                f"IndustryResolver.members_of returned {type(members).__name__}, expected list",
                error_code=INDUSTRY_RESOLUTION_FAILED,
            )
        self._cache[cache_key] = list(members)
        return list(members)

    @_instrument("get_peer_bars")
    def get_peer_bars(
        self,
        *,
        window: int,
        top_n: int = 20,
        industry: str | None = None,
        rank_by: str = "market_cap",
        freq: str = "1d",
    ) -> dict[str, pd.DataFrame]:
        """Return ``{peer_symbol: DataFrame}`` for top-N industry peers.

        Internally composes :meth:`get_industry_members` +
        :meth:`get_bars`. All peer symbols must have been declared via
        ``DataRequest.peers(...)`` in ``informative_data``.
        """
        members = self.get_industry_members(industry, top_n=top_n, rank_by=rank_by)
        out: dict[str, pd.DataFrame] = {}
        for sym in members:
            out[sym] = self.get_bars(symbol=sym, window=window, freq=freq)
        return out

    @_instrument("get_fundamentals")
    def get_fundamentals(
        self,
        symbol: str | None = None,
        *,
        fields: tuple[str, ...] | None = None,
    ) -> Mapping[str, Any]:
        """Return latest fundamental metrics for ``symbol`` (defaults to current).

        ``fields`` is required (no "give me everything" mode) — declare
        what you need so prefetch is bounded.
        """
        resolved = self._resolve_symbol(symbol)
        self._check_symbol_declared(resolved)
        if fields is None or not isinstance(fields, tuple) or len(fields) == 0:
            raise DataAccessError(
                "fields must be a non-empty tuple of column names",
                error_code=INVALID_ARGUMENT,
                hint=(
                    "Pass fields=('pe', 'pb', ...) so the framework knows "
                    "what to prefetch."
                ),
            )
        cache_key = ("fundamentals", resolved, fields)
        if cache_key in self._cache:
            return dict(self._cache[cache_key])
        if self.fundamentals_fetcher is None:
            raise InformativeDataError(
                "fundamentals_fetcher not wired",
                error_code=INDUSTRY_RESOLUTION_FAILED,
                hint="Wire FundamentalsFetcher in bootstrap.",
            )
        result = _await_sync(
            self.fundamentals_fetcher.fetch(resolved, fields=fields),
            method="get_fundamentals",
            hint="fundamentals_fetcher not wired",
        )
        if result is None:
            raise DataAccessError(
                f"fundamentals for {resolved!r} returned None",
                error_code=DATA_INSUFFICIENT,
            )
        self._cache[cache_key] = dict(result)
        return dict(result)

    @_instrument("watchlist_symbols")
    def watchlist_symbols(self, *, tag: str | None = None) -> list[str]:
        """Return the user's watchlist symbols, optionally filtered by ``tag``.

        - ``tag=None`` → every symbol in the watchlist.
        - ``tag="核心池"`` → only symbols carrying that tag.

        Reads a **per-cycle frozen snapshot** of the watchlist (symbol → tags)
        seeded at worker assembly time — there is no live DB read inside
        strategy code, so the result is deterministic for the whole cycle and
        the prefetch contract is untouched.

        Raises :class:`DataAccessError` with ``invalid_argument`` when the
        runtime did not wire a watchlist snapshot — we never return an empty
        list silently, so a misconfigured runtime is visible rather than
        masquerading as "the watchlist is empty".
        """
        if self._watchlist_snapshot is None:
            raise DataAccessError(
                "watchlist_symbols() requires a watchlist snapshot, but none "
                "was wired into this runtime",
                error_code=INVALID_ARGUMENT,
                hint=(
                    "The worker did not provide a WatchlistSnapshot. Ensure the "
                    "runtime assembly path seeds DataProvider._watchlist_snapshot "
                    "before running this strategy."
                ),
            )
        if tag is not None and (not isinstance(tag, str) or not tag.strip()):
            raise DataAccessError(
                f"tag must be a non-empty string or None, got {tag!r}",
                error_code=INVALID_ARGUMENT,
            )
        if tag is None:
            return list(self._watchlist_snapshot.all_symbols())
        return list(self._watchlist_snapshot.symbols_for_tag(tag))

    @_instrument("ticker")
    def ticker(self, symbol: str | None = None) -> dict[str, Any]:
        """Latest real-time quote. Raises in backtest mode."""
        if self.is_backtest:
            raise DataAccessError(
                "ticker() is not available in backtest mode",
                error_code=LIVE_ONLY_METHOD,
                hint="Use get_bars(window=1) for last bar instead.",
            )
        resolved = self._resolve_symbol(symbol)
        raise DataAccessError(
            f"ticker({resolved!r}) not implemented in this build",
            error_code=LIVE_ONLY_METHOD,
            hint="Live ticker wiring is provided by the live runtime; not "
            "available in backtest / dry-run.",
        )

    @_instrument("orderbook")
    def orderbook(
        self, symbol: str | None = None, *, depth: int = 5
    ) -> dict[str, Any]:
        """Latest order book snapshot. Raises in backtest mode."""
        if self.is_backtest:
            raise DataAccessError(
                "orderbook() is not available in backtest mode",
                error_code=LIVE_ONLY_METHOD,
            )
        resolved = self._resolve_symbol(symbol)
        raise DataAccessError(
            f"orderbook({resolved!r}, depth={depth}) not implemented in this build",
            error_code=LIVE_ONLY_METHOD,
        )


# ---------------------------------------------------------------------------
# Async-to-sync bridge for the cache-miss path.
# ---------------------------------------------------------------------------


def _await_sync(
    awaitable: Awaitable[Any] | None,
    *,
    method: str,
    hint: str = "",
) -> Any:
    """Drive an awaitable to completion from a sync context.

    Used only on cache miss, which should be rare — the runner pre-warms
    the cache during ``prefetch_informative`` so strategy hot paths read
    from memory. When called from outside an event loop, ``asyncio.run``
    creates an ephemeral loop; when called from inside one, falls back to
    a thread-based runner.
    """
    if awaitable is None:
        raise DataAccessError(
            f"{method}: no async backend wired",
            error_code=INVALID_ARGUMENT,
            hint=hint,
        )
    import asyncio

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _coerce_awaitable(awaitable)).result()
    return asyncio.run(_coerce_awaitable(awaitable))


async def _coerce_awaitable(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


__all__ = [
    "DataProvider",
    "FundamentalsFetcher",
    "HistoryFetcher",
    "IndustryResolver",
    "WatchlistSnapshot",
]
