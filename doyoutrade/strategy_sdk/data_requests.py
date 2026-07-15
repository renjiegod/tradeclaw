"""DataRequest — declarative spec for cross-symbol data the strategy needs.

A strategy declares extra data dependencies by returning a list of
:class:`DataRequest` instances from :meth:`Strategy.informative_data`. The
worker resolves the symbolic references (e.g. ``"$self.industry"``), batches
the actual data fetches, and seeds the result into ``ctx.dp``'s cycle cache
*before* ``populate_indicators`` / ``on_bar`` run. The strategy code then just
calls ``ctx.dp.get_bars(symbol=...)`` and gets a cache hit.

Why factories instead of free-form dicts:

- The compiler can statically enumerate which symbols / indexes / industries
  the strategy will touch, refusing references that aren't covered by any
  declaration (``informative_data_not_declared``).
- Each factory has a fixed schema, so the prefetch phase doesn't need a
  generic "what kind of thing is this?" dispatch.
- The skill's ``list_data_requests`` assistant tool returns exactly this set
  of factories with parameter schemas — agents discover types by listing,
  not by guessing.

Symbolic references supported in ``symbol`` / ``industry`` fields:

- ``"$self"`` — current cycle symbol
- ``"$self.industry"`` — industry of the current symbol (resolved against
  the registered industry mapping)

These are resolved during the prefetch phase, not at strategy declaration
time, so the same strategy class can run against any symbol in the universe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Sequence

from doyoutrade.strategy_sdk.errors import (
    INVALID_ARGUMENT,
    StrategyValidationError,
)

SELF = "$self"
SELF_INDUSTRY = "$self.industry"

# Canonical interval names the data layer can serve — keep in sync with
# doyoutrade/strategy_runtime/compiler.py::_VALID_TIMEFRAMES and each provider's
# ProviderCapabilities.supported_intervals. Hourly is ``60m`` (not ``1h``),
# monthly is ``1mo`` (not ``1M``); ``4h`` is served by no provider.
_VALID_FREQS: frozenset[str] = frozenset(
    {"1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mo"}
)


def _validate_window(window: int, name: str = "window") -> int:
    if not isinstance(window, int) or isinstance(window, bool):
        raise StrategyValidationError(
            f"{name} must be a positive int, got {type(window).__name__}={window!r}",
            error_code=INVALID_ARGUMENT,
        )
    if window <= 0:
        raise StrategyValidationError(
            f"{name} must be > 0, got {window}",
            error_code=INVALID_ARGUMENT,
        )
    return window


def _validate_freq(freq: str) -> str:
    if not isinstance(freq, str) or freq not in _VALID_FREQS:
        raise StrategyValidationError(
            f"freq must be one of {sorted(_VALID_FREQS)}, got {freq!r}",
            error_code=INVALID_ARGUMENT,
            hint="Common values: '1d' (daily), '1w' (weekly), '60m' (hourly)",
        )
    return freq


def _validate_symbol_ref(symbol: str, *, allow_self: bool = True) -> str:
    if not isinstance(symbol, str) or not symbol.strip():
        raise StrategyValidationError(
            f"symbol must be a non-empty string, got {symbol!r}",
            error_code=INVALID_ARGUMENT,
        )
    cleaned = symbol.strip()
    if cleaned.startswith("$") and not allow_self:
        raise StrategyValidationError(
            f"symbolic reference {cleaned!r} not allowed here; use a concrete symbol",
            error_code=INVALID_ARGUMENT,
        )
    return cleaned


@dataclass(frozen=True)
class _BaseRequest:
    """Common header for all DataRequest variants."""

    #: Stable factory identifier. Used by the compiler's
    #: ``unknown_data_request_type`` check and by ``list_data_requests``.
    kind: ClassVar[str] = "base"

    #: Stable key the runtime uses to address this request's result in the
    #: ctx.dp cache. Subclasses override to compose from their fields.
    def cache_key(self) -> tuple:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass(frozen=True)
class BarsRequest(_BaseRequest):
    """Historical OHLCV bars for a specific symbol.

    Use this to ask the framework to prefetch bars for cross-symbol use in
    ``on_bar``. After the prefetch phase, ``ctx.dp.get_bars(symbol=X,
    window=W)`` returns the cached result.
    """

    kind: ClassVar[str] = "bars"
    symbol: str
    window: int
    freq: str = "1d"

    def cache_key(self) -> tuple:
        return (self.kind, self.symbol, self.freq, self.window)


@dataclass(frozen=True)
class IndexBarsRequest(_BaseRequest):
    """Historical OHLCV bars for a market index / ETF.

    ``code`` should be the index's tradeable symbol (e.g. ``"000300.SH"``
    for CSI 300). Data layer routes to the index-aware provider.
    """

    kind: ClassVar[str] = "index_bars"
    code: str
    window: int
    freq: str = "1d"

    def cache_key(self) -> tuple:
        return (self.kind, self.code, self.freq, self.window)


@dataclass(frozen=True)
class PeersRequest(_BaseRequest):
    """OHLCV bars for top-N industry peers of the current symbol.

    ``industry`` defaults to ``"$self.industry"`` — the industry of the
    cycle's current symbol, resolved at prefetch time from the registered
    industry mapping. ``rank_by`` controls which top-N to take (market_cap,
    turnover, etc.).
    """

    kind: ClassVar[str] = "peers"
    window: int
    top_n: int = 20
    industry: str = SELF_INDUSTRY
    rank_by: str = "market_cap"
    freq: str = "1d"

    def cache_key(self) -> tuple:
        return (self.kind, self.industry, self.rank_by, self.top_n, self.freq, self.window)


@dataclass(frozen=True)
class CrossSectionRequest(_BaseRequest):
    """A panel snapshot of multiple fields across the cycle's universe.

    Returns a DataFrame indexed by symbol with one column per requested
    field. Useful for ranking / percentile factors.
    """

    kind: ClassVar[str] = "cross_section"
    fields: tuple[str, ...] = ()
    universe: str = "$cycle"  # "$cycle" = current cycle universe; future: industry codes

    def cache_key(self) -> tuple:
        return (self.kind, self.universe, self.fields)


@dataclass(frozen=True)
class FundamentalsRequest(_BaseRequest):
    """Latest fundamental metrics for a symbol.

    ``symbol`` defaults to ``"$self"`` (current cycle symbol). Returns a
    flat ``dict[field, value]`` from ``ctx.dp.get_fundamentals``.
    """

    kind: ClassVar[str] = "fundamentals"
    fields: tuple[str, ...] = ()
    symbol: str = SELF

    def cache_key(self) -> tuple:
        return (self.kind, self.symbol, self.fields)


class DataRequest:
    """Factory hub for declarative data dependencies.

    Strategies don't construct request dataclasses directly — they call
    these classmethods so the compiler can statically verify factory names
    against the registered set (``unknown_data_request_type``)::

        def informative_data(self, ctx):
            return [
                DataRequest.bars(symbol="000300.SH", window=60),
                DataRequest.peers(top_n=20, window=30),
                DataRequest.index_bars(code="000905.SH", window=60),
            ]
    """

    @staticmethod
    def bars(*, symbol: str, window: int, freq: str = "1d") -> BarsRequest:
        return BarsRequest(
            symbol=_validate_symbol_ref(symbol, allow_self=False),
            window=_validate_window(window),
            freq=_validate_freq(freq),
        )

    @staticmethod
    def index_bars(code: str, *, window: int, freq: str = "1d") -> IndexBarsRequest:
        if not isinstance(code, str) or not code.strip():
            raise StrategyValidationError(
                f"index code must be a non-empty string, got {code!r}",
                error_code=INVALID_ARGUMENT,
            )
        return IndexBarsRequest(
            code=code.strip(),
            window=_validate_window(window),
            freq=_validate_freq(freq),
        )

    @staticmethod
    def peers(
        *,
        window: int,
        top_n: int = 20,
        industry: str = SELF_INDUSTRY,
        rank_by: str = "market_cap",
        freq: str = "1d",
    ) -> PeersRequest:
        if not isinstance(top_n, int) or top_n <= 0 or isinstance(top_n, bool):
            raise StrategyValidationError(
                f"top_n must be a positive int, got {top_n!r}",
                error_code=INVALID_ARGUMENT,
            )
        if not isinstance(industry, str) or not industry.strip():
            raise StrategyValidationError(
                f"industry must be a non-empty string, got {industry!r}",
                error_code=INVALID_ARGUMENT,
            )
        if rank_by not in ("market_cap", "turnover", "volume"):
            raise StrategyValidationError(
                f"rank_by must be one of (market_cap, turnover, volume), got {rank_by!r}",
                error_code=INVALID_ARGUMENT,
            )
        return PeersRequest(
            window=_validate_window(window),
            top_n=top_n,
            industry=industry.strip(),
            rank_by=rank_by,
            freq=_validate_freq(freq),
        )

    @staticmethod
    def cross_section(
        *,
        fields: Sequence[str],
        universe: str = "$cycle",
    ) -> CrossSectionRequest:
        fields_tuple = tuple(fields)
        if not fields_tuple or not all(
            isinstance(f, str) and f.strip() for f in fields_tuple
        ):
            raise StrategyValidationError(
                f"fields must be a non-empty sequence of strings, got {fields!r}",
                error_code=INVALID_ARGUMENT,
            )
        return CrossSectionRequest(fields=fields_tuple, universe=universe)

    @staticmethod
    def fundamentals(
        *,
        fields: Sequence[str],
        symbol: str = SELF,
    ) -> FundamentalsRequest:
        fields_tuple = tuple(fields)
        if not fields_tuple or not all(
            isinstance(f, str) and f.strip() for f in fields_tuple
        ):
            raise StrategyValidationError(
                f"fields must be a non-empty sequence of strings, got {fields!r}",
                error_code=INVALID_ARGUMENT,
            )
        return FundamentalsRequest(
            fields=fields_tuple,
            symbol=_validate_symbol_ref(symbol, allow_self=True),
        )


# Names enumerated by the ``unknown_data_request_type`` compiler check and by
# ``list_data_requests`` assistant tool.
REGISTERED_REQUEST_TYPES: frozenset[str] = frozenset(
    {"bars", "index_bars", "peers", "cross_section", "fundamentals"}
)


__all__ = [
    "BarsRequest",
    "CrossSectionRequest",
    "DataRequest",
    "FundamentalsRequest",
    "IndexBarsRequest",
    "PeersRequest",
    "REGISTERED_REQUEST_TYPES",
    "SELF",
    "SELF_INDUSTRY",
]
