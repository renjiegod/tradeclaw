"""StrategyRunner — bridges :class:`Strategy` to the worker's
:class:`SignalGeneratorProtocol`.

Responsibilities, per cycle:

1. Instantiate the strategy (parameters already bound by the assistant /
   compiler artifact loader).
2. Per universe symbol: build a :class:`StrategyContext` + per-cycle
   :class:`DataProvider`, prefetch declared informative data, run
   ``populate_indicators`` + ``@informative`` methods, call ``on_bar``,
   collect the resulting :class:`Signal`.
3. Convert ``{symbol: Signal}`` to legacy target-state ``{symbol: int}`` and
   hand it to :class:`PositionManager` for sizing.

The runner produces OTel spans for each phase
(``strategy.runner.prefetch`` / ``strategy.runner.populate_indicators`` /
``strategy.runner.on_bar``) plus structured debug events that include the
strategy class name, symbol, signal tag, and any error_code surfaced from
``ctx.dp`` failures or strategy validation errors.

All async I/O happens in this layer (prefetch + history fetches); the
strategy itself sees only sync APIs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Mapping

import pandas as pd
from opentelemetry import trace as trace_api

from doyoutrade.core.models import OrderIntent
from doyoutrade.core.signal_generator_protocol import SignalGenerationContext
from doyoutrade.debug import emit_debug_event
from doyoutrade.execution.position_manager import PositionManager
from doyoutrade.strategy_sdk.context import (
    AccountView,
    PositionView,
    StrategyContext,
)
from doyoutrade.strategy_sdk.data_provider import (
    DataProvider,
    FundamentalsFetcher,
    HistoryFetcher,
    IndustryResolver,
)
from doyoutrade.strategy_sdk.watchlist_snapshot import WatchlistSnapshot
from doyoutrade.strategy_sdk.data_requests import (
    SELF,
    BarsRequest,
    IndexBarsRequest,
    PeersRequest,
)
from doyoutrade.strategy_sdk.errors import (
    INVALID_ON_BAR_RETURN,
    INVALID_POPULATE_INDICATORS_RETURN,
    StrategyError,
    StrategyValidationError,
)
from doyoutrade.strategy_sdk.informative import (
    collect_informative_specs,
    merge_informative_pair,
)
from doyoutrade.strategy_sdk.parameters import collect_parameters
from doyoutrade.strategy_sdk.signal import Signal
from doyoutrade.strategy_sdk.strategy import Strategy

logger = logging.getLogger(__name__)
_tracer = trace_api.get_tracer(__name__)


@dataclass
class StrategyRunner:
    """Drive a :class:`Strategy` through one cycle, producing OrderIntent rows.

    Implements :class:`SignalGeneratorProtocol` so the worker can invoke
    it through the same single-method contract used by all signal
    generators.

    ``parameters`` is the per-cycle parameter mapping — see
    :func:`parameters.bind`. ``parameter_descriptors`` (filled by
    ``__post_init__``) is the parameter object map collected from the
    strategy class so the runner can call ``.bind(value)`` for each.
    """

    strategy: Strategy
    position_manager: PositionManager
    history_fetcher: HistoryFetcher
    industry_resolver: IndustryResolver | None = None
    fundamentals_fetcher: FundamentalsFetcher | None = None
    # Frozen per-cycle watchlist view, injected by the worker assembly path
    # (Phase B). ``None`` means no watchlist wired — ctx.dp.watchlist_symbols()
    # then raises rather than silently returning [].
    watchlist_snapshot: WatchlistSnapshot | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)

    _parameter_descriptors: dict[str, Any] = field(default_factory=dict, init=False)
    _strategy_started: bool = field(default=False, init=False)
    _informative_specs: list[Any] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, Strategy):
            raise StrategyValidationError(
                f"StrategyRunner expects a Strategy instance, got "
                f"{type(self.strategy).__name__}",
                error_code="invalid_strategy_instance",
            )
        self._parameter_descriptors = collect_parameters(type(self.strategy))
        self._informative_specs = collect_informative_specs(type(self.strategy))
        self._bind_parameters()

    # ----- Parameter binding -----

    def _bind_parameters(self) -> None:
        """Bind ``self.parameters`` overrides onto the strategy's tunable params.

        Any descriptor not overridden retains its declared ``default``.
        Unknown keys in ``self.parameters`` are kept in ``ctx.params`` but
        do not affect the typed parameter descriptors (free-form params).
        """
        for name, descriptor in self._parameter_descriptors.items():
            if name in self.parameters:
                descriptor.bind(self.parameters[name])
            else:
                descriptor.bind(None)

    # ----- SignalGeneratorProtocol entry -----

    async def generate_intents(
        self, ctx: SignalGenerationContext
    ) -> List[OrderIntent]:
        as_of = _resolve_as_of(ctx)
        is_backtest = _is_backtest_run(ctx)
        positions_by_symbol = {p.symbol: p for p in ctx.positions}

        # ----- on_strategy_start (once per runner lifetime) -----
        if not self._strategy_started:
            self._strategy_started = True
            try:
                self.strategy.on_strategy_start(
                    _make_lifecycle_context(ctx, as_of=as_of, is_backtest=is_backtest)
                )
            except Exception as e:
                await _emit_failure(
                    "strategy_on_strategy_start_failed",
                    self.strategy,
                    None,
                    e,
                )
                raise

        # ----- on_cycle_start -----
        try:
            self.strategy.on_cycle_start(
                _make_lifecycle_context(ctx, as_of=as_of, is_backtest=is_backtest)
            )
        except Exception as e:
            await _emit_failure(
                "strategy_on_cycle_start_failed", self.strategy, None, e
            )
            raise

        # ----- Per-symbol evaluation -----
        signals_by_symbol: dict[str, Signal] = {}
        for symbol in ctx.universe:
            position_view = PositionView.from_snapshot(
                symbol, positions_by_symbol.get(symbol)
            )
            account_view = AccountView.from_snapshot(ctx.account_snapshot)

            signal = await self._evaluate_symbol(
                symbol=symbol,
                as_of=as_of,
                is_backtest=is_backtest,
                universe=tuple(ctx.universe),
                position_view=position_view,
                account_view=account_view,
                run_id=_safe_run_id(ctx),
                trace_id=_safe_trace_id(ctx),
            )
            signals_by_symbol[symbol] = signal

        # ----- Diagnostic event for the cycle -----
        # ``per_symbol_tags`` MUST include every symbol in the universe, not
        # just the tagged ones — otherwise an untagged ``Signal.hold()``
        # disappears from the timeline and operators can't tell "MACD valid
        # but no cross" apart from "in warmup, returned Signal.hold(tag=
        # 'warmup')" (request1.json turn 2 false-diagnosed warmup for this
        # reason). Untagged signals get a ``<untagged_<direction>>``
        # sentinel so they show up alongside tagged ones; the strategy
        # author should add real tags via ``Signal.hold(tag='no_cross')``
        # etc. — buy/sell already require a tag at the SDK boundary.
        await emit_debug_event(
            "strategy_runner_cycle",
            {
                "strategy_class": type(self.strategy).__name__,
                "strategy_name": getattr(self.strategy, "name", "") or type(self.strategy).__name__,
                "universe_size": len(ctx.universe),
                "signals_buy": sum(1 for s in signals_by_symbol.values() if s.is_buy),
                "signals_sell": sum(1 for s in signals_by_symbol.values() if s.is_sell),
                "signals_hold": sum(1 for s in signals_by_symbol.values() if s.is_hold),
                "signals_target_exposure": sum(
                    1 for s in signals_by_symbol.values() if s.is_target_exposure
                ),
                "signals_target_quantity": sum(
                    1 for s in signals_by_symbol.values() if s.is_target_quantity
                ),
                "per_symbol_tags": {
                    sym: _resolve_diag_tag(signal)
                    for sym, signal in signals_by_symbol.items()
                },
            },
        )

        # Surface the full per-symbol decision factors (direction / tag /
        # rationale / diagnostics) back to the worker via the shared
        # SignalGenerationContext output channel, so they can be persisted to
        # cycle_runs.details (the debug event above only carries tags). The
        # worker reads ctx.signal_diagnostics after generate_intents returns.
        ctx.signal_diagnostics = {
            sym: signal.to_dict() for sym, signal in signals_by_symbol.items()
        }

        # PositionManager still expects {symbol: int} Signals — build them.
        legacy_signals = _build_legacy_signals(signals_by_symbol)
        settlement_mode = "t0"
        if ctx.cycle_state is not None:
            settlement_mode = str(
                getattr(ctx.cycle_state, "settlement_mode", None) or "t0"
            )

        return self.position_manager.compute_intents(
            legacy_signals,
            ctx.account_snapshot,
            ctx.positions,
            ctx.market_context,
            task_budget_snapshot=ctx.task_budget_snapshot,
            settlement_mode=settlement_mode,  # type: ignore[arg-type]
        )

    async def evaluate_signals_for_screen(
        self,
        symbols: list[str],
        *,
        as_of: datetime,
        account_view: AccountView,
        is_backtest: bool = True,
        run_id: str = "screen",
        trace_id: str = "",
    ) -> dict[str, Signal | None]:
        """Evaluate the strategy on the latest bar for each symbol (screening).

        Reuses the exact per-symbol pipeline (``populate_indicators`` +
        ``@informative`` + ``on_bar``) but returns the raw ``{symbol: Signal}``
        without the :class:`PositionManager` sizing step — so it is pure
        compute with no cycle / order semantics and never writes cycle_runs.
        A flat :class:`PositionView` is assumed (screening has no holdings).

        The optional cycle lifecycle hooks (``on_strategy_start`` /
        ``on_cycle_start``) are intentionally NOT called here: screening
        evaluates indicators + ``on_bar`` per symbol and does not run a cycle.
        A strategy that depends on lifecycle state isn't supported in screen
        mode. Per-symbol failures are isolated: the symbol maps to ``None``
        and a ``strategy_runner_screen_skip`` debug event fires, so one bad
        symbol never aborts the scan.
        """

        out: dict[str, Signal | None] = {}
        universe = tuple(symbols)
        for symbol in symbols:
            try:
                out[symbol] = await self._evaluate_symbol(
                    symbol=symbol,
                    as_of=as_of,
                    is_backtest=is_backtest,
                    universe=universe,
                    position_view=PositionView(symbol=symbol),
                    account_view=account_view,
                    run_id=run_id,
                    trace_id=trace_id,
                )
            except Exception as exc:  # noqa: BLE001 — isolate per symbol
                out[symbol] = None
                await emit_debug_event(
                    "strategy_runner_screen_skip",
                    {
                        "strategy_class": type(self.strategy).__name__,
                        "symbol": symbol,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "hint": "strategy raised while evaluating this symbol; excluded from screen",
                    },
                )
                logger.warning(
                    "strategy screen skip symbol=%s strategy=%s %s: %s",
                    symbol, type(self.strategy).__name__, type(exc).__name__, exc,
                )
        return out

    async def evaluate_one_signal(
        self,
        symbol: str,
        *,
        as_of: datetime,
        account_view: AccountView,
        position_view: PositionView | None = None,
        is_backtest: bool = False,
        run_id: str = "deviation",
        trace_id: str = "",
        universe: tuple[str, ...] | None = None,
    ) -> Signal:
        """Evaluate the strategy on the latest bar for ONE symbol with a real
        (injected) :class:`PositionView`.

        This reuses the exact per-symbol pipeline (``populate_indicators`` +
        ``@informative`` + ``on_bar``) as :meth:`evaluate_signals_for_screen`,
        but unlike screening — which assumes a flat position because it has no
        holdings — the caller supplies the actual ``position_view``. That makes
        cost-basis-aware logic (``ctx.position.cost_price`` / ``current_profit``
        / 跌破成本) correct for a monitoring / alerting caller (e.g. the
        ``deviation_monitor`` cron executor).

        Like screen mode this is pure signal compute: the cycle lifecycle hooks
        (``on_strategy_start`` / ``on_cycle_start``) are NOT called and no
        cycle_runs / OrderIntents are produced. Unlike screen mode there is no
        per-symbol isolation — this RAISES on failure so the caller owns the
        ``try/except`` and can emit a monitor-specific structured skip event.
        """
        return await self._evaluate_symbol(
            symbol=symbol,
            as_of=as_of,
            is_backtest=is_backtest,
            universe=universe if universe is not None else (symbol,),
            position_view=(
                position_view
                if position_view is not None
                else PositionView(symbol=symbol)
            ),
            account_view=account_view,
            run_id=run_id,
            trace_id=trace_id,
        )

    # ----- Per-symbol pipeline -----

    async def _evaluate_symbol(
        self,
        *,
        symbol: str,
        as_of: datetime,
        is_backtest: bool,
        universe: tuple[str, ...],
        position_view: PositionView,
        account_view: AccountView,
        run_id: str,
        trace_id: str,
    ) -> Signal:
        span_name = "strategy.runner.evaluate_symbol"
        with _tracer.start_as_current_span(span_name) as span:
            span.set_attribute("symbol", symbol)
            span.set_attribute("run_id", run_id)
            span.set_attribute("strategy", type(self.strategy).__name__)

            # ----- Phase 1: declare informative data -----
            try:
                declared = self.strategy.validate_informative_data_return(
                    self.strategy.informative_data(
                        _make_stub_context(
                            symbol=symbol,
                            as_of=as_of,
                            is_backtest=is_backtest,
                            universe=universe,
                            position_view=position_view,
                            account_view=account_view,
                            run_id=run_id,
                            trace_id=trace_id,
                            params=dict(self.parameters),
                        )
                    )
                )
            except StrategyError:
                raise

            declared_symbols: set[str] = set()
            declared_indexes: set[str] = set()
            declared_industries: set[str] = set()
            for req in declared:
                if isinstance(req, BarsRequest):
                    if req.symbol == SELF:
                        declared_symbols.add(symbol)
                    else:
                        declared_symbols.add(req.symbol)
                elif isinstance(req, IndexBarsRequest):
                    declared_indexes.add(req.code)
                elif isinstance(req, PeersRequest):
                    declared_industries.add(req.industry)
                # FundamentalsRequest / CrossSectionRequest: no symbol gating.

            # ----- Phase 2: build DataProvider, prefetch declared data -----
            dp = DataProvider(
                current_symbol=symbol,
                now=as_of,
                is_backtest=is_backtest,
                declared_symbols=frozenset(declared_symbols),
                declared_indexes=frozenset(declared_indexes),
                declared_industries=frozenset(declared_industries),
                history_fetcher=self.history_fetcher,
                industry_resolver=self.industry_resolver,
                fundamentals_fetcher=self.fundamentals_fetcher,
                _watchlist_snapshot=self.watchlist_snapshot,
                _run_id=run_id,
                _trace_id=trace_id,
            )
            await self._prefetch_declared(dp, declared, symbol=symbol, as_of=as_of)

            ctx = StrategyContext(
                symbol=symbol,
                now=as_of,
                run_id=run_id,
                trace_id=trace_id,
                universe=universe,
                position=position_view,
                account=account_view,
                params=dict(self.parameters),
                dp=dp,
            )

            # ----- Phase 3: load base bars -----
            try:
                base_df = await self.history_fetcher.fetch(
                    symbol,
                    as_of=as_of,
                    lookback=int(type(self.strategy).startup_history),
                    freq=str(type(self.strategy).timeframe),
                )
            except Exception as e:
                await _emit_failure(
                    "strategy_base_history_fetch_failed",
                    self.strategy,
                    symbol,
                    e,
                )
                raise

            if base_df is None or len(base_df) < int(type(self.strategy).startup_history):
                await emit_debug_event(
                    "strategy_base_history_insufficient",
                    {
                        "strategy_class": type(self.strategy).__name__,
                        "symbol": symbol,
                        "required": int(type(self.strategy).startup_history),
                        "got": 0 if base_df is None else len(base_df),
                        "hint": (
                            "Lower startup_history or check that the symbol "
                            "has enough history by ctx.now."
                        ),
                    },
                )
                return Signal.hold(
                    tag="data_insufficient",
                    rationale=(
                        f"insufficient base bars: got "
                        f"{0 if base_df is None else len(base_df)} of "
                        f"{int(type(self.strategy).startup_history)}"
                    ),
                )

            # ----- Phase 4: populate_indicators -----
            try:
                populated = self.strategy.populate_indicators(base_df, ctx)
            except StrategyError:
                raise
            except Exception as e:
                await _emit_failure(
                    "strategy_populate_indicators_failed",
                    self.strategy,
                    symbol,
                    e,
                )
                raise

            if not isinstance(populated, pd.DataFrame):
                raise StrategyValidationError(
                    f"populate_indicators must return DataFrame, got "
                    f"{type(populated).__name__}",
                    error_code=INVALID_POPULATE_INDICATORS_RETURN,
                )

            # ----- Phase 5: @informative / @informative_each methods -----
            # Each spec describes one pass. ``spec.symbol`` of None means
            # "current symbol at a different timeframe"; a concrete value
            # means cross-symbol (which MUST also appear in
            # informative_data so prefetch covered it). For @informative_each
            # the spec list contains one entry per declared symbol; each
            # entry passes that symbol as a kwarg to the method.
            base_tf = str(type(self.strategy).timeframe)
            for spec in self._informative_specs:
                informative_method = getattr(self.strategy, spec.method_name)
                fetch_symbol = spec.symbol if spec.symbol is not None else symbol
                try:
                    informative_df = await self.history_fetcher.fetch(
                        fetch_symbol,
                        as_of=as_of,
                        lookback=int(type(self.strategy).startup_history),
                        freq=spec.timeframe,
                    )
                except Exception as e:
                    await _emit_failure(
                        "strategy_informative_fetch_failed",
                        self.strategy,
                        fetch_symbol,
                        e,
                    )
                    raise
                try:
                    if spec.each:
                        informative_populated = informative_method(
                            informative_df, ctx, symbol=spec.symbol
                        )
                    else:
                        informative_populated = informative_method(
                            informative_df, ctx
                        )
                except Exception as e:
                    await _emit_failure(
                        "strategy_informative_populate_failed",
                        self.strategy,
                        fetch_symbol,
                        e,
                    )
                    raise
                populated = merge_informative_pair(
                    populated,
                    informative_populated,
                    base_timeframe=base_tf,
                    informative_timeframe=spec.timeframe,
                    column_suffix=spec.column_suffix,
                    ffill=spec.ffill,
                )

            # ----- Phase 6: on_bar -----
            try:
                signal = self.strategy.on_bar(populated, ctx)
            except StrategyError:
                raise
            except Exception as e:
                await _emit_failure(
                    "strategy_on_bar_failed", self.strategy, symbol, e
                )
                raise

            if not isinstance(signal, Signal):
                raise StrategyValidationError(
                    f"on_bar must return Signal, got {type(signal).__name__}",
                    error_code=INVALID_ON_BAR_RETURN,
                )
            span.set_attribute("signal_direction", signal.direction.value)
            if signal.tag:
                span.set_attribute("signal_tag", signal.tag)
            return signal

    async def _prefetch_declared(
        self,
        dp: DataProvider,
        declared: tuple,
        *,
        symbol: str,
        as_of: datetime,
    ) -> None:
        """Fetch every declared DataRequest and seed ``dp._cache``.

        Failures are surfaced via debug events but not raised — the
        per-method ``ctx.dp.get_bars`` calls inside the strategy will hit
        the cache miss path and produce a typed
        :class:`DataAccessError` with ``data_insufficient`` so the
        operator can attribute the failure precisely.
        """
        span_name = "strategy.runner.prefetch_informative"
        with _tracer.start_as_current_span(span_name) as span:
            span.set_attribute("symbol", symbol)
            span.set_attribute("declared_count", len(declared))
            tasks = []
            cache_keys: list[tuple] = []
            for req in declared:
                if isinstance(req, BarsRequest):
                    cache_key = ("bars", req.symbol, req.freq, req.window)
                    tasks.append(
                        self.history_fetcher.fetch(
                            req.symbol, as_of=as_of, lookback=req.window, freq=req.freq
                        )
                    )
                    cache_keys.append(cache_key)
                elif isinstance(req, IndexBarsRequest):
                    cache_key = ("index_bars", req.code, req.freq, req.window)
                    tasks.append(
                        self.history_fetcher.fetch(
                            req.code, as_of=as_of, lookback=req.window, freq=req.freq
                        )
                    )
                    cache_keys.append(cache_key)
                # PeersRequest / CrossSectionRequest / FundamentalsRequest:
                # resolved lazily inside dp methods because they need the
                # IndustryResolver / FundamentalsFetcher we may not have
                # wired in Phase 1.
            if not tasks:
                return
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for key, result in zip(cache_keys, results):
                if isinstance(result, BaseException):
                    await emit_debug_event(
                        "strategy_prefetch_failed",
                        {
                            "cache_key": list(key),
                            "error_type": type(result).__name__,
                            "message": str(result),
                            "hint": "Failed prefetch surfaces as data_insufficient at access time.",
                        },
                    )
                    continue
                dp.seed_cache(key, result)
            span.set_attribute("cache_size_after_prefetch", dp.cache_size())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_diag_tag(signal: Signal) -> str:
    """Return a tag suitable for ``per_symbol_tags`` diagnostics.

    Falls back to ``<untagged_<direction>>`` when ``signal.tag`` is empty
    so an untagged ``Signal.hold()`` is still visible in the cycle's
    timeline entry. The buy / sell factory methods already require a
    non-empty tag at the SDK boundary, so this fallback only fires for
    bare ``Signal.hold()`` — a pattern the strategy author should fix by
    tagging the hold explicitly (e.g. ``Signal.hold(tag='no_cross')``).
    """

    if signal.tag:
        return signal.tag
    if signal.is_hold:
        direction = "hold"
    elif signal.is_target_exposure:
        direction = "target_exposure"
    elif signal.is_target_quantity:
        direction = "target_quantity"
    elif signal.is_buy:
        direction = "buy"
    elif signal.is_sell:
        direction = "sell"
    else:
        direction = "unknown"
    return f"<untagged_{direction}>"


def _resolve_as_of(ctx: SignalGenerationContext) -> datetime:
    if ctx.cycle_state is not None and ctx.cycle_state.cycle_time is not None:
        return ctx.cycle_state.cycle_time
    return datetime.now(timezone.utc)


def _is_backtest_run(ctx: SignalGenerationContext) -> bool:
    # The cycle_state carries the run kind; default to False (live) when unset.
    cs = ctx.cycle_state
    if cs is None:
        return False
    kind = getattr(cs, "run_kind", "") or ""
    return str(kind).lower() in ("backtest", "simulation")


def _safe_run_id(ctx: SignalGenerationContext) -> str:
    if ctx.cycle_state is None:
        return ""
    return ctx.cycle_state.run_id or ""


def _safe_trace_id(ctx: SignalGenerationContext) -> str:
    if ctx.cycle_state is None:
        return ""
    return ctx.cycle_state.trace_id or ""


def _make_lifecycle_context(
    ctx: SignalGenerationContext, *, as_of: datetime, is_backtest: bool = False
) -> StrategyContext:
    _ = is_backtest  # noqa: F841  reserved for future is_backtest field on lifecycle ctx
    """Lightweight context for cycle-level hooks (no per-symbol state)."""
    universe = tuple(ctx.universe)
    first_symbol = universe[0] if universe else ""
    pos = next(
        (p for p in ctx.positions if p.symbol == first_symbol),
        None,
    )
    return StrategyContext(
        symbol=first_symbol,
        now=as_of,
        run_id=_safe_run_id(ctx),
        trace_id=_safe_trace_id(ctx),
        universe=universe,
        position=PositionView.from_snapshot(first_symbol, pos),
        account=AccountView.from_snapshot(ctx.account_snapshot),
        params={},
        dp=_NullDataProvider(),  # type: ignore[arg-type]
    )


def _make_stub_context(
    *,
    symbol: str,
    as_of: datetime,
    is_backtest: bool,
    universe: tuple[str, ...],
    position_view: PositionView,
    account_view: AccountView,
    run_id: str,
    trace_id: str,
    params: dict[str, Any],
) -> StrategyContext:
    """Context passed to ``informative_data`` (before dp is fully wired).

    The dp here is a minimal stub — informative_data shouldn't read from
    dp anyway; its job is to *declare* what dp should later provide.
    """
    _ = is_backtest  # noqa: F841  reserved
    return StrategyContext(
        symbol=symbol,
        now=as_of,
        run_id=run_id,
        trace_id=trace_id,
        universe=universe,
        position=position_view,
        account=account_view,
        params=params,
        dp=_NullDataProvider(),  # type: ignore[arg-type]
    )


class _NullDataProvider:
    """Stub used during informative_data() declaration phase.

    Strategy code MUST NOT read from dp inside informative_data — its sole
    purpose is to return a list of DataRequest factories. Any access here
    raises immediately to signal misuse.
    """

    is_backtest: bool = False

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        raise StrategyValidationError(
            f"ctx.dp.{name}() called inside informative_data — dp is not "
            "available there. Declare dependencies via DataRequest factories "
            "and access data in populate_indicators / on_bar.",
            error_code="dp_access_in_informative_data",
        )


def _build_legacy_signals(
    signals_by_symbol: dict[str, Signal],
) -> list[Any]:
    """Adapter: strategy-facing Signal → :class:`PositionSignal`.

    PositionManager works on a small per-symbol record (symbol + legacy
    target-state or explicit target_exposure / target_quantity + tag).
    Strategy.on_bar returns a richer :class:`Signal` (direction + tag +
    diagnostics) which we project onto that shape. HOLD signals are dropped —
    by contract PositionManager treats omitted symbols as
    "no opinion, keep current position".
    """
    from doyoutrade.execution.position_manager import PositionSignal

    out: list[Any] = []
    for symbol, signal in signals_by_symbol.items():
        if signal.target_exposure_value is not None:
            out.append(
                PositionSignal(
                    symbol=symbol,
                    value=None,
                    target_exposure=signal.target_exposure_value,
                    target_quantity=None,
                    rationale=signal.rationale,
                    tag=signal.tag,
                )
            )
            continue
        if signal.target_quantity_value is not None:
            out.append(
                PositionSignal(
                    symbol=symbol,
                    value=None,
                    target_exposure=None,
                    target_quantity=signal.target_quantity_value,
                    rationale=signal.rationale,
                    tag=signal.tag,
                )
            )
            continue
        target = signal.to_target_state()
        if target is None:
            continue
        out.append(
            PositionSignal(
                symbol=symbol,
                value=target,
                rationale=signal.rationale,
                tag=signal.tag,
                # Carry the optional exit categorization onto the position
                # signal so PositionManager can stamp it on the SELL intent.
                exit_reason=getattr(signal, "exit_reason", None),
                # Carry the partial-exit fraction so PositionManager scales
                # the sell quantity (1.0 = full exit, unchanged).
                fraction=getattr(signal, "fraction", 1.0),
            )
        )
    return out


async def _emit_failure(
    event_name: str, strategy: Strategy, symbol: str | None, exc: BaseException
) -> None:
    payload: dict[str, Any] = {
        "strategy_class": type(strategy).__name__,
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
    if symbol is not None:
        payload["symbol"] = symbol
    if isinstance(exc, StrategyError):
        payload["error_code"] = exc.error_code
        if exc.hint:
            payload["hint"] = exc.hint
        payload.update(exc.context)
    await emit_debug_event(event_name, payload)


__all__ = ["StrategyRunner"]
