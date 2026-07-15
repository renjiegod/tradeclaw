"""Strategy — abstract base for signal-generating strategies.

A Strategy is **pure signal generation**: given market data, output a
:class:`Signal`. It does not manage positions, sizing, stop-loss, order
routing, or order events. Those are downstream concerns owned by
``PositionManager`` / ``OrderManager`` / risk components.

Concretely, a Strategy gets to:

- Declare its **data needs** (``timeframe`` / ``startup_history`` /
  ``informative_data`` / ``@informative``-decorated methods).
- Declare **tunable parameters** (``IntParameter`` / ``DecimalParameter`` /
  ``CategoricalParameter`` / ``BooleanParameter`` class attributes).
- Compute **indicators** (``populate_indicators``, vectorized).
- Issue a **per-bar signal** (``on_bar``, the only required method).
- Read **current position / account** state (via ``ctx.position`` /
  ``ctx.account``) to inform its signal — but never to **change** them.

It does NOT get to:

- Manage stop-loss / take-profit (PositionManager / RiskManager job).
- Override entry / exit prices, stake amounts (PositionManager job).
- React to fill events (OrderManager / event-bus subscribers do that).
- Decide whether to enter short or close long (the signal direction is
  the strategy's output; how it maps to orders is PositionManager's call).

The lifecycle for one cycle, per symbol:

::

    runner.bind_parameters(strategy, cycle_params)
    strategy.on_strategy_start(ctx)        # once per runner lifetime
    strategy.on_cycle_start(ctx)           # once per cycle
    declared = strategy.informative_data(ctx)
    runner.prefetch(declared)
    df = strategy.populate_indicators(df_base, ctx)
    for spec in strategy._informative_specs:
        df = runner.run_informative(strategy, spec, df, ctx)
    signal = strategy.on_bar(df, ctx)       # required, returns Signal

All methods are sync from the strategy's view — the runner handles async
I/O outside.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, Sequence

import pandas as pd

from doyoutrade.strategy_sdk.errors import (
    INVALID_INFORMATIVE_DATA_RETURN,
    StrategyValidationError,
)

if TYPE_CHECKING:
    from doyoutrade.strategy_sdk.context import StrategyContext
    from doyoutrade.strategy_sdk.data_requests import _BaseRequest
    from doyoutrade.strategy_sdk.signal import Signal


class Strategy(ABC):
    """Abstract base for all strategies — signal generation only.

    The compiler enforces:

    - Subclass implements :meth:`on_bar`.
    - ``timeframe`` is a valid timeframe string.
    - ``startup_history`` is a positive int.
    - No disallowed imports inside the strategy module.
    - No silent ``except Exception: pass`` / typed-coercion patterns.
    - No lookahead access (``df.iloc[i]`` for ``i >= 0``, ``df.shift(-n)``).
    - Every ``Signal.buy()`` / ``Signal.sell()`` / ``Signal.target_exposure()`` /
      ``Signal.target_quantity()`` call has a ``tag`` keyword.

    Subclasses MUST override :meth:`on_bar`.

    Subclasses MAY override :meth:`informative_data`,
    :meth:`populate_indicators`, :meth:`on_strategy_start`,
    :meth:`on_cycle_start`. They may also define ``@informative``-decorated
    methods for cross-timeframe indicators.
    """

    # ----- Class metadata -----

    #: Human-readable name shown in the strategy registry / UI.
    name: ClassVar[str] = ""

    #: Base bar frequency for ``on_bar`` evaluation. Common values:
    #: "1d" (daily), "60m" (hourly), "5m" (5 minute). Must be one of the
    #: timeframes the data layer can fetch (see ``_VALID_TIMEFRAMES``).
    timeframe: ClassVar[str] = "1d"

    #: Minimum number of base-timeframe bars the runner provides before
    #: calling ``populate_indicators`` / ``on_bar``. Must accommodate the
    #: longest rolling window the strategy uses. Setting it too low causes
    #: ``populate_indicators`` to receive a too-short DataFrame and either
    #: silently produce NaN-heavy indicators or fail the compiler's
    #: ``history_check_literal_disallowed`` check.
    startup_history: ClassVar[int] = 30

    # ----- Lifecycle hooks -----

    def on_strategy_start(self, ctx: "StrategyContext") -> None:
        """Called once per strategy instance, before the first cycle.

        Use for one-time setup (e.g. loading auxiliary data into
        ``self._cache``). Default is a no-op.
        """

    def on_cycle_start(self, ctx: "StrategyContext") -> None:
        """Called once per cycle, before any per-symbol work.

        Symbol-independent setup belongs here (e.g. caching cycle timestamp,
        computing a universe-wide ranking). The runner passes the same
        ``ctx`` to subsequent per-symbol hooks.
        """

    # ----- Data declaration -----

    def informative_data(self, ctx: "StrategyContext") -> Sequence["_BaseRequest"]:
        """Declare cross-symbol / index / peer / fundamental dependencies.

        Return a list of :class:`DataRequest` instances. The runner
        prefetches all of them in batch before invoking
        ``populate_indicators`` so strategy code reads via ``ctx.dp.*``
        with cache hits. Symbols not declared here CANNOT be accessed at
        runtime — ``ctx.dp.get_bars(symbol=undeclared)`` raises
        ``informative_data_not_declared``.

        Default returns ``[]`` — strategy only sees current-symbol bars.
        """
        return []

    # ----- Indicator computation -----

    def populate_indicators(
        self, df: pd.DataFrame, ctx: "StrategyContext"
    ) -> pd.DataFrame:
        """Vectorized indicator computation for the current symbol.

        Receives the base-timeframe DataFrame (``startup_history`` rows of
        OHLCV up to ``ctx.now``) and returns the same DataFrame with
        additional indicator columns. This is the **only** place the
        strategy should perform expensive rolling computations; ``on_bar``
        should read precomputed columns via ``df.iloc[-1]``.

        Inside ``populate_indicators``, ``ctx.dp.get_bars(symbol=other)``
        is forbidden (cross-symbol access must go through
        ``informative_data``). Reading ``ctx.dp.get_bars()`` (current
        symbol) is allowed and idempotent.

        Default returns ``df`` unchanged. Strategies relying purely on
        OHLCV (e.g. simple ``df["close"]`` rules) can omit this method.
        """
        return df

    # ----- Signal generation -----

    @abstractmethod
    def on_bar(self, df: pd.DataFrame, ctx: "StrategyContext") -> "Signal":
        """Per-bar decision: read ``df.iloc[-1]`` and return a :class:`Signal`.

        - ``df.iloc[-1]`` is the current bar.
        - ``df.iloc[-2]``, ``df.iloc[-3]`` etc. are prior bars.
        - Reading ``df.iloc[i]`` with ``i >= 0`` (positive index) is
          lookahead and rejected by the compiler.

        Returns ``Signal.buy(tag=...)`` / ``Signal.sell(tag=...)`` /
        ``Signal.target_exposure(target=..., tag=...)`` /
        ``Signal.target_quantity(quantity=..., tag=...)`` / ``Signal.hold()``.
        ``tag`` is mandatory for every actionable signal — see :class:`Signal`.

        Reading ``ctx.position`` / ``ctx.account`` is allowed and useful
        (e.g. exit conditions that depend on current_profit). But never
        try to *change* position state here — that's PositionManager's
        responsibility downstream.
        """

    # ----- Internal: return-value validation called by the runner -----

    @classmethod
    def validate_informative_data_return(
        cls, value: Any
    ) -> tuple["_BaseRequest", ...]:
        """Coerce the return value of ``informative_data`` into a typed tuple.

        Raises :class:`StrategyValidationError` with
        ``invalid_informative_data_return`` if the return shape is wrong.
        Called by the runner before the prefetch phase.
        """
        from doyoutrade.strategy_sdk.data_requests import _BaseRequest

        if value is None:
            return ()
        try:
            seq = list(value)
        except TypeError as e:
            raise StrategyValidationError(
                f"informative_data must return a sequence, got "
                f"{type(value).__name__}={value!r}",
                error_code=INVALID_INFORMATIVE_DATA_RETURN,
            ) from e
        out: list[_BaseRequest] = []
        for i, item in enumerate(seq):
            if not isinstance(item, _BaseRequest):
                raise StrategyValidationError(
                    f"informative_data[{i}] is not a DataRequest: "
                    f"{type(item).__name__}={item!r}",
                    error_code=INVALID_INFORMATIVE_DATA_RETURN,
                    hint=(
                        "Use DataRequest.bars(...) / DataRequest.index_bars(...) / "
                        "DataRequest.peers(...) factories instead of raw dicts."
                    ),
                )
            out.append(item)
        return tuple(out)


__all__ = ["Strategy"]
