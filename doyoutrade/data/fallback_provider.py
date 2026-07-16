"""Ordered-fallback wrapper for ``data_source=auto`` market-data dispatch.

``CachedBarsDataProvider`` previously delegated to exactly one inner
provider, so any runtime failure (QMT proxy down, akshare rate-limited,
tushare token expired) surfaced as a hard error mid-cycle. The replacement
strategy is: pick a primary based on capabilities + config and chain
secondary providers behind it; on a ``get_bars`` failure or empty result
fall through to the next, emitting a ``market_data_provider_skipped``
debug event each time so the cycle's debug session shows exactly why
the eventual answer came from a different source.

Non-bar methods (``get_market_context`` / ``is_trading_day`` /
``get_trading_dates``) stay on the primary because:

* ``get_market_context`` returns live ticks — only the broker-backed
  provider (QMT) serves real exchange data; the others approximate
  with last-close, which would silently corrupt a live cycle.
* Trading calendars do diverge across providers (akshare uses a weekday
  heuristic, baostock hits a real calendar API) — falling back here
  would flip the calendar source mid-cycle and break invariants the
  strategy relies on. If the primary's calendar fails, fail loudly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from doyoutrade.core.models import Bar, MarketContext, QuoteSnapshot
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.protocols import ProviderCapabilities
from doyoutrade.debug import emit_debug_event

logger = logging.getLogger(__name__)


def _is_terminal_provider_error(exc: Exception) -> bool:
    from doyoutrade.infra.qmt_proxy_client import QmtRealtimeKlineUnsupportedError

    return isinstance(exc, QmtRealtimeKlineUnsupportedError)


def _provider_name(provider: Any) -> str:
    caps = getattr(provider, "capabilities", None)
    if caps is None:
        return "unknown"
    return getattr(caps, "name", None) or "unknown"


def _supports_interval(provider: Any, interval: str) -> bool:
    """Return ``True`` when ``provider.capabilities`` advertises support for ``interval``.

    Providers that don't declare capabilities are assumed to support any
    interval — keeps legacy / test providers (mock, hand-rolled
    in-memory fakes) working without forcing them to add metadata.
    """
    caps = getattr(provider, "capabilities", None)
    if caps is None:
        return True
    supported = getattr(caps, "supported_intervals", None)
    if not supported:
        return True
    return interval in supported


class FallbackHistoricalDataProvider:
    """Tries inner providers in order; surfaces non-bar APIs from the primary.

    Failure modes are visible by design — every skip emits
    ``market_data_provider_skipped`` with ``reason`` and ``hint`` so
    an operator can see why a backup provider ended up answering.
    """

    def __init__(self, providers: list[Any], *, capabilities: ProviderCapabilities | None = None):
        if not providers:
            raise ValueError(
                "FallbackHistoricalDataProvider needs at least one inner provider"
            )
        self.providers = list(providers)
        # Cache-key uses the wrapper's ``capabilities.name`` so cached
        # bars from this auto-chain are scoped under a stable id even
        # when the actual upstream rotated mid-session. Callers that
        # need the specific source of a given row should look at the
        # ``provider`` field on the debug event, not the cache key.
        if capabilities is not None:
            self.capabilities = capabilities
        else:
            primary = self.providers[0]
            primary_caps = getattr(primary, "capabilities", None)
            self.capabilities = (
                primary_caps
                if primary_caps is not None
                else ProviderCapabilities(name="fallback")
            )
        # Set on the first ``get_bars`` that returned non-empty bars.
        # ``GetMarketDataTool`` reads this for the ``provider_used``
        # envelope field so the model / operator can see which source
        # actually answered even when the request was ``data_source=auto``.
        self.last_used_provider: str | None = None
        # Suspension days reported by whichever inner provider served the most
        # recent ``get_bars`` (only baostock populates this today). Forwarded so
        # the write-time continuity check can read it off the data stack without
        # knowing which concrete provider answered. Empty when the served
        # provider exposes no per-date suspension signal (e.g. qmt).
        self.last_suspended_days: set[str] = set()

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> list[Bar]:
        last_error: Exception | None = None
        attempts: list[str] = []
        for provider in self.providers:
            name = _provider_name(provider)
            attempts.append(name)
            if not _supports_interval(provider, interval):
                await emit_debug_event(
                    "market_data_provider_skipped",
                    {
                        "provider": name,
                        "reason": "interval_unsupported",
                        "interval": interval,
                        "symbol": symbol,
                        "start": start_time,
                        "end": end_time,
                        "adjust": adjust,
                        "hint": (
                            "provider's capabilities.supported_intervals does not "
                            f"include {interval!r}; trying next provider"
                        ),
                    },
                )
                continue
            try:
                bars = list(
                    await provider.get_bars(
                        symbol, start_time, end_time, interval=interval, adjust=adjust
                    )
                )
            except Exception as exc:
                if _is_terminal_provider_error(exc):
                    await emit_debug_event(
                        "market_data_provider_failed_terminal",
                        {
                            "provider": name,
                            "reason": "terminal_error",
                            "interval": interval,
                            "symbol": symbol,
                            "start": start_time,
                            "end": end_time,
                            "exc_type": type(exc).__name__,
                            "exc_message": str(exc),
                            "hint": (
                                "upstream raised a terminal error that would change "
                                "the data semantics if we fell through to a backup "
                                "provider; aborting the fallback chain"
                            ),
                        },
                    )
                    logger.warning(
                        "market_data fallback: %s.get_bars raised terminal %s — aborting chain",
                        name,
                        type(exc).__name__,
                        exc_info=True,
                    )
                    raise
                last_error = exc
                await emit_debug_event(
                    "market_data_provider_skipped",
                    {
                        "provider": name,
                        "reason": "exception",
                        "interval": interval,
                        "symbol": symbol,
                        "start": start_time,
                        "end": end_time,
                        "exc_type": type(exc).__name__,
                        "exc_message": str(exc),
                        "hint": "upstream raised; trying next provider in the fallback chain",
                    },
                )
                logger.warning(
                    "market_data fallback: %s.get_bars raised %s — trying next",
                    name, type(exc).__name__, exc_info=True,
                )
                continue
            if not bars:
                await emit_debug_event(
                    "market_data_provider_skipped",
                    {
                        "provider": name,
                        "reason": "empty_result",
                        "interval": interval,
                        "symbol": symbol,
                        "start": start_time,
                        "end": end_time,
                        "hint": "upstream returned no bars; trying next provider in the fallback chain",
                    },
                )
                continue
            self.last_used_provider = name
            self.last_suspended_days = set(
                getattr(provider, "last_suspended_days", None) or set()
            )
            return bars

        # All providers exhausted. Surface the cause so the caller
        # (CachedBarsDataProvider / GetMarketDataTool) can attach it to
        # the structured error envelope per CLAUDE.md §错误可见性.
        if last_error is not None:
            raise last_error
        return []

    async def get_market_context(self) -> MarketContext:
        return await self.providers[0].get_market_context()

    async def is_trading_day(self, value: str) -> bool:
        return await self.providers[0].is_trading_day(value)

    async def get_trading_dates(self, start: str, end: str) -> list[str]:
        return await self.providers[0].get_trading_dates(start, end)

    async def aclose(self) -> None:
        """Close every inner provider that has an ``aclose``; surface failures.

        Errors are downgraded to a warning per provider but the function
        still raises the *last* failure once all providers have been
        given a chance to close — masking those would let leaked sockets
        accumulate without anyone seeing why.
        """
        last_error: Exception | None = None
        for provider in self.providers:
            close = getattr(provider, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "fallback aclose: %s.aclose raised %s",
                    _provider_name(provider), type(exc).__name__,
                    exc_info=True,
                )
        if last_error is not None:
            raise last_error


def _quote_provider_name(provider: Any) -> str:
    return type(provider).__name__


class FallbackRealtimeQuoteProvider:
    """Chains ``RealtimeQuoteProvider`` sources per-symbol (mootdx -> akshare today).

    ``QuoteStreamService``'s ``fallback_provider`` seam expects a single
    ``RealtimeQuoteProvider`` that answers every requested symbol (per-symbol
    placeholders for the unknown, never a silent drop — see
    :class:`doyoutrade.data.protocols.RealtimeQuoteProvider`). This wrapper
    tries the first provider for the full symbol set, then retries only the
    symbols that came back ``status != "ok"`` (or that a provider raised on)
    against the next provider in the chain, so one source's outage or partial
    coverage does not blank the whole watchlist.

    Every skip is visible per CLAUDE.md §错误可见性: a provider raising, or
    leaving symbols unanswered, emits ``realtime_quote_provider_skipped`` with
    the provider name, reason, and the still-missing symbols. Symbols no
    provider in the chain can answer keep the last provider's ``status`` (by
    default ``"no_data"``) rather than being dropped.
    """

    def __init__(self, providers: list[Any]):
        if not providers:
            raise ValueError(
                "FallbackRealtimeQuoteProvider needs at least one inner provider"
            )
        self.providers = list(providers)

    async def fetch_quotes(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        requested = list(dict.fromkeys(symbols))
        result: dict[str, QuoteSnapshot] = {}
        pending = list(requested)

        for provider in self.providers:
            if not pending:
                break
            name = _quote_provider_name(provider)
            try:
                answered = await provider.fetch_quotes(pending)
            except Exception as exc:
                logger.warning(
                    "realtime_quote fallback: %s.fetch_quotes raised %s — trying next provider",
                    name, type(exc).__name__, exc_info=True,
                )
                await emit_debug_event(
                    "realtime_quote_provider_skipped",
                    {
                        "provider": name,
                        "reason": "exception",
                        "symbols": list(pending),
                        "exc_type": type(exc).__name__,
                        "exc_message": str(exc),
                        "hint": "provider raised; trying next provider in the fallback chain",
                    },
                )
                continue

            still_missing: list[str] = []
            for sym in pending:
                snap = answered.get(sym)
                if snap is not None and getattr(snap, "status", None) == "ok":
                    result[sym] = snap
                else:
                    still_missing.append(sym)
                    if snap is not None:
                        # Keep the best placeholder seen so far (e.g. a real
                        # "suspended" beats a later "no_data").
                        result.setdefault(sym, snap)

            if still_missing:
                await emit_debug_event(
                    "realtime_quote_provider_skipped",
                    {
                        "provider": name,
                        "reason": "partial_result",
                        "symbols": still_missing,
                        "hint": "provider left these symbols unanswered; trying next provider in the fallback chain",
                    },
                )
            pending = still_missing

        for sym in pending:
            result.setdefault(sym, QuoteSnapshot(symbol=sym, status="no_data"))

        return result

    async def aclose(self) -> None:
        """Close every inner provider that has an ``aclose``; surface failures.

        Mirrors :meth:`FallbackHistoricalDataProvider.aclose` — errors are
        downgraded to a warning per provider but the last one still raises so
        a leaked connection does not go unnoticed.
        """
        last_error: Exception | None = None
        for provider in self.providers:
            close = getattr(provider, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "realtime_quote fallback aclose: %s.aclose raised %s",
                    _quote_provider_name(provider), type(exc).__name__,
                    exc_info=True,
                )
        if last_error is not None:
            raise last_error


__all__ = ["FallbackHistoricalDataProvider", "FallbackRealtimeQuoteProvider"]
