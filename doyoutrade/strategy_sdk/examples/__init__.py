"""Runnable :class:`Strategy` examples.

Each module in this package defines a self-contained ``Strategy`` subclass
plus an ``if __name__ == "__main__"`` block that synthesizes OHLCV bars,
invokes ``populate_indicators`` + ``on_bar`` directly, and prints the
resulting :class:`Signal`.

Run an example with::

    python -m doyoutrade.strategy_sdk.examples.macd_trend
    python -m doyoutrade.strategy_sdk.examples.rsi_mean_reversion
    python -m doyoutrade.strategy_sdk.examples.bollinger_breakout
    python -m doyoutrade.strategy_sdk.examples.dual_sma_crossover
    python -m doyoutrade.strategy_sdk.examples.grid_target_exposure
    python -m doyoutrade.strategy_sdk.examples.grid_target_quantity

These examples illustrate the canonical authoring patterns:

- Subclass :class:`Strategy`, set ``timeframe`` and ``startup_history``.
- Declare tunable knobs as ``IntParameter`` / ``DecimalParameter`` etc.
- Compute indicators (vectorized) in ``populate_indicators``.
- Read ``df.iloc[-1]`` in ``on_bar`` and return
  ``Signal.buy(tag=...)`` / ``Signal.sell(tag=...)`` /
  ``Signal.target_exposure(target=..., tag=...)`` /
  ``Signal.target_quantity(quantity=..., tag=...)`` / ``Signal.hold()``.
- Use :mod:`doyoutrade.strategy_sdk.indicators` for MACD / RSI / ADX /
  Bollinger / ATR / SMA rather than hand-rolled ``ewm`` / ``rolling``.
- Compare *levels* (``a > b``) for buy/sell decisions; for grid/inventory
  logic map price bands to explicit exposure or share-inventory levels.
  Never encode *cross events* — the diff between cycles drives entries / exits.
"""
