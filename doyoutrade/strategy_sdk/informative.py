"""``@informative`` / ``@informative_each`` — declare cross-timeframe and/or
cross-symbol indicator passes.

These decorators let a strategy compute indicators on data **other than the
current symbol at the base timeframe** in a vectorized way, with the result
auto-merged onto the main DataFrame consumed by ``on_bar``.

Three flavors:

1. **Same symbol, different timeframe** —
   ``@informative("1w")`` on a method ``populate_weekly(self, df, ctx)``.
   Fetches current symbol's weekly bars, runs the method, merges
   ``column → column_1w`` onto the daily DataFrame.

2. **Specific other symbol** —
   ``@informative("1d", symbol="600519.SH")`` on a method
   ``populate_moutai(self, df, ctx)``. Fetches Moutai daily bars, runs the
   method, merges ``column → column_600519_SH_1d`` onto the main frame.

3. **Iterate over many symbols with the same logic** —
   ``@informative_each("1d", symbols=("600519.SH", "000858.SZ"))`` on a
   method ``populate_leader(self, df, ctx, symbol)``. Runs the method
   once per declared symbol, merging each with that symbol's suffix.

Example::

    LEADERS = ("600519.SH", "000858.SZ")

    class MyStrategy(Strategy):
        timeframe = "1d"

        def informative_data(self, ctx):
            return [DataRequest.bars(symbol=s, window=30) for s in LEADERS]

        def populate_indicators(self, df, ctx):
            df["rsi"] = ta.RSI(df, 14)
            return df

        @informative("1w")
        def populate_weekly(self, df, ctx):
            df["ma20"] = df["close"].rolling(20).mean()
            return df

        @informative("1d", symbol="000300.SH")
        def populate_csi300(self, df, ctx):
            df["ma20"] = df["close"].rolling(20).mean()
            return df

        @informative_each("1d", symbols=LEADERS)
        def populate_leader(self, df, ctx, symbol):
            df["rsi"] = ta.RSI(df, 14)
            return df

        def on_bar(self, df, ctx):
            # Available columns merged onto df:
            #   df["rsi"]              — daily current-symbol RSI
            #   df["ma20_1w"]          — weekly MA20 of current symbol
            #   df["ma20_000300_SH_1d"]— daily MA20 of CSI300
            #   df["rsi_600519_SH_1d"] — daily RSI of Moutai
            #   df["rsi_000858_SZ_1d"] — daily RSI of Wuliangye
            ...

All cross-symbol references MUST also be declared in
:meth:`Strategy.informative_data` (with ``DataRequest.bars(symbol=...)``)
so the runner prefetches them. The decorator only describes how to
compute on already-prefetched data; the prefetch declaration is the
gating contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import pandas as pd

from doyoutrade.strategy_sdk.errors import (
    INVALID_INFORMATIVE_DECORATOR,
    StrategyCompileError,
)

#: Attribute holding the InformativeSpec(s) on a decorated method. May be
#: a single :class:`InformativeSpec` (``@informative``) or a list of them
#: (``@informative_each``).
INFORMATIVE_ATTR = "_strategy_informative_spec"

#: Marker attribute distinguishing ``@informative_each`` methods from
#: ``@informative`` ones. ``True`` means the method takes an extra
#: ``symbol`` keyword arg in addition to ``(self, df, ctx)``.
INFORMATIVE_EACH_ATTR = "_strategy_informative_each"

# Must mirror the data layer's canonical interval names (see
# doyoutrade/strategy_runtime/compiler.py::_VALID_TIMEFRAMES and each provider's
# ProviderCapabilities.supported_intervals). Hourly is ``60m``, monthly is
# ``1mo``; ``4h`` is not served by any provider.
_VALID_TIMEFRAMES: frozenset[str] = frozenset(
    {"1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mo"}
)


@dataclass(frozen=True)
class InformativeSpec:
    """One pass of informative indicator computation.

    ``symbol`` of ``None`` means "the current cycle symbol" (cross-timeframe
    only). A concrete value (e.g. ``"600519.SH"``) means cross-symbol —
    that symbol must also appear in ``informative_data`` so the runner can
    prefetch it.
    """

    timeframe: str
    method_name: str
    column_suffix: str
    ffill: bool = True
    symbol: str | None = None
    #: When True, the method has an extra ``symbol`` kwarg and the spec
    #: was produced by ``@informative_each``. The runner passes the
    #: concrete symbol on each invocation.
    each: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def _sanitize_symbol(symbol: str) -> str:
    """Make a symbol string safe to embed in a DataFrame column name.

    ``"600519.SH"`` → ``"600519_SH"``. Keeps lookups via ``df["col"]``
    while also making ``df.col`` attribute access viable.
    """
    return symbol.replace(".", "_").replace("-", "_").replace("/", "_")


def _default_suffix(timeframe: str, symbol: str | None) -> str:
    if symbol is None:
        return f"_{timeframe}"
    return f"_{_sanitize_symbol(symbol)}_{timeframe}"


def _validate_timeframe(timeframe: str) -> str:
    if not isinstance(timeframe, str) or timeframe not in _VALID_TIMEFRAMES:
        raise StrategyCompileError(
            f"timeframe must be one of {sorted(_VALID_TIMEFRAMES)}, "
            f"got {timeframe!r}",
            error_code=INVALID_INFORMATIVE_DECORATOR,
            hint='Use "1d" / "1w" / "60m" / "5m" etc.',
        )
    return timeframe


def _validate_symbol(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    if not isinstance(symbol, str) or not symbol.strip():
        raise StrategyCompileError(
            f"symbol must be a non-empty string or None, got {symbol!r}",
            error_code=INVALID_INFORMATIVE_DECORATOR,
        )
    return symbol.strip()


def informative(
    timeframe: str,
    *,
    symbol: str | None = None,
    column_suffix: str | None = None,
    ffill: bool = True,
) -> Callable[[Callable[..., pd.DataFrame]], Callable[..., pd.DataFrame]]:
    """Decorate a method to compute indicators on (symbol, timeframe).

    - ``timeframe``: e.g. "1d" / "1w" / "5m". Required.
    - ``symbol``: ``None`` (current cycle symbol, default) or a specific
      symbol string. When provided, the symbol must also appear in
      :meth:`Strategy.informative_data` via ``DataRequest.bars(...)``.
    - ``column_suffix``: override the default column suffix
      (``"_{tf}"`` for current symbol; ``"_{symbol}_{tf}"`` for cross).
    - ``ffill``: forward-fill higher-tf values onto base index (default
      True, matches freqtrade).

    The decorated method MUST have signature ``(self, df, ctx) -> DataFrame``.
    For ``@informative_each``, see that decorator instead.
    """
    tf = _validate_timeframe(timeframe)
    sym = _validate_symbol(symbol)
    suffix = column_suffix if column_suffix is not None else _default_suffix(tf, sym)

    def decorator(fn: Callable[..., pd.DataFrame]) -> Callable[..., pd.DataFrame]:
        spec = InformativeSpec(
            timeframe=tf,
            method_name=fn.__name__,
            column_suffix=suffix,
            ffill=ffill,
            symbol=sym,
            each=False,
        )
        setattr(fn, INFORMATIVE_ATTR, spec)
        setattr(fn, INFORMATIVE_EACH_ATTR, False)
        return fn

    return decorator


def informative_each(
    timeframe: str,
    *,
    symbols: Sequence[str],
    column_suffix_template: str | None = None,
    ffill: bool = True,
) -> Callable[[Callable[..., pd.DataFrame]], Callable[..., pd.DataFrame]]:
    """Decorate a method to compute the same indicators on EACH listed symbol.

    The method signature is ``(self, df, ctx, symbol) -> DataFrame``. The
    runner invokes it once per declared symbol with that symbol passed as
    ``symbol`` kwarg; each pass merges with a symbol-specific suffix so
    the on_bar DataFrame ends up with all merged columns side-by-side.

    - ``timeframe``: shared by all passes (same for every symbol).
    - ``symbols``: tuple/list of symbol strings; all must also appear in
      ``informative_data`` so prefetch fires.
    - ``column_suffix_template``: optional override. Use ``{symbol}`` and
      ``{tf}`` placeholders, e.g. ``"_{symbol}_{tf}"``. Default behaves
      identically to :func:`informative` with the symbol set.
    - ``ffill``: forward-fill (default True).

    Example::

        @informative_each("1d", symbols=("600519.SH", "000858.SZ"))
        def populate_leader(self, df, ctx, symbol):
            df["rsi"] = ta.RSI(df, 14)
            return df

    Each pass receives a DataFrame containing **that symbol's** bars.
    Column ``"rsi"`` becomes ``"rsi_600519_SH_1d"`` for Moutai and
    ``"rsi_000858_SZ_1d"`` for Wuliangye on the merged base DataFrame.
    """
    tf = _validate_timeframe(timeframe)
    syms = tuple(symbols)
    if len(syms) == 0:
        raise StrategyCompileError(
            "@informative_each requires at least one symbol",
            error_code=INVALID_INFORMATIVE_DECORATOR,
        )
    for s in syms:
        if not isinstance(s, str) or not s.strip():
            raise StrategyCompileError(
                f"@informative_each: bad symbol {s!r}",
                error_code=INVALID_INFORMATIVE_DECORATOR,
            )
    syms = tuple(s.strip() for s in syms)

    def decorator(fn: Callable[..., pd.DataFrame]) -> Callable[..., pd.DataFrame]:
        specs: list[InformativeSpec] = []
        for s in syms:
            if column_suffix_template is None:
                suffix = _default_suffix(tf, s)
            else:
                suffix = column_suffix_template.format(symbol=_sanitize_symbol(s), tf=tf)
            specs.append(
                InformativeSpec(
                    timeframe=tf,
                    method_name=fn.__name__,
                    column_suffix=suffix,
                    ffill=ffill,
                    symbol=s,
                    each=True,
                )
            )
        setattr(fn, INFORMATIVE_ATTR, specs)
        setattr(fn, INFORMATIVE_EACH_ATTR, True)
        return fn

    return decorator


def collect_informative_specs(strategy_cls: type) -> list[InformativeSpec]:
    """Walk ``strategy_cls.__mro__`` and return all informative specs (flat).

    Used by the compiler to enumerate timeframe + symbol dependencies for
    prefetch planning and by the runner to invoke the right methods in
    the right order. Both :func:`informative` (single spec) and
    :func:`informative_each` (list of specs) contribute.
    """
    out: list[InformativeSpec] = []
    seen: set[str] = set()
    for cls in strategy_cls.__mro__:
        for name, attr in vars(cls).items():
            if name in seen:
                continue
            stored: Any = getattr(attr, INFORMATIVE_ATTR, None)
            if isinstance(stored, InformativeSpec):
                out.append(stored)
                seen.add(name)
            elif isinstance(stored, list) and all(
                isinstance(s, InformativeSpec) for s in stored
            ):
                out.extend(stored)
                seen.add(name)
    return out


def merge_informative_pair(
    base_df: pd.DataFrame,
    informative_df: pd.DataFrame,
    *,
    base_timeframe: str,
    informative_timeframe: str,
    column_suffix: str,
    ffill: bool = True,
) -> pd.DataFrame:
    """Merge ``informative_df`` columns onto ``base_df`` aligned by timestamp.

    Higher-timeframe bars are aligned to the *end* of their period (the
    bar labeled "Mon 2026-05-18" for a weekly bar covers Mon-Fri of that
    week and is only known *after* Friday's close). To prevent lookahead
    bias we shift the informative index forward by one informative period
    before merging.

    The informative columns receive ``column_suffix`` so they don't collide
    with base-timeframe columns of the same name (``ma20`` → ``ma20_1w``,
    or ``ma20_600519_SH_1d`` for a cross-symbol informative).
    """
    if base_df.empty:
        return base_df
    if informative_df.empty:
        # Return base_df unchanged but with placeholder NaN columns so
        # downstream code referencing the merged column doesn't KeyError.
        result = base_df.copy()
        for col in informative_df.columns:
            result[f"{col}{column_suffix}"] = float("nan")
        return result

    _ = base_timeframe  # reserved for future alignment validation
    shift_offset = _timeframe_offset(informative_timeframe)
    shifted = informative_df.copy()
    shifted.index = shifted.index + shift_offset
    shifted = shifted.add_suffix(column_suffix)

    reindexed = shifted.reindex(base_df.index, method="ffill" if ffill else None)

    result = base_df.copy()
    for col in reindexed.columns:
        result[col] = reindexed[col]
    return result


def _timeframe_offset(timeframe: str) -> pd.Timedelta:
    seconds_map: dict[str, int] = {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "60m": 3600,
        "1d": 86400,
        "1w": 604800,
        "1mo": 86400 * 30,
    }
    if timeframe not in seconds_map:
        raise StrategyCompileError(
            f"unknown timeframe {timeframe!r}",
            error_code=INVALID_INFORMATIVE_DECORATOR,
        )
    result: pd.Timedelta = pd.Timedelta(seconds=seconds_map[timeframe])  # type: ignore[assignment]
    return result


__all__ = [
    "INFORMATIVE_ATTR",
    "INFORMATIVE_EACH_ATTR",
    "InformativeSpec",
    "collect_informative_specs",
    "informative",
    "informative_each",
    "merge_informative_pair",
]
