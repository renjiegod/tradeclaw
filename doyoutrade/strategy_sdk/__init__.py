"""Public surface of the strategy SDK.

This module re-exports the small, stable set of types user / agent
strategies are expected to import. Anything not exported here is internal
and may change between releases.

The shape of a Strategy module is::

    from doyoutrade.strategy_sdk import (
        Strategy,
        Signal,
        DataRequest,
        IntParameter, DecimalParameter, CategoricalParameter,
        informative,
    )

    class MyStrategy(Strategy):
        timeframe = "1d"
        startup_history = 50
        fast = IntParameter(5, 20, default=10)

        def informative_data(self, ctx):
            return [DataRequest.index_bars("000300.SH", window=30)]

        def populate_indicators(self, df, ctx):
            df["ma_fast"] = df["close"].rolling(self.fast.value).mean()
            return df

        def on_bar(self, df, ctx):
            if df["ma_fast"].iloc[-1] > df["close"].iloc[-1]:
                return Signal.target_quantity(
                    quantity=300,
                    tag="ma_fast_above_close",
                )
            return Signal.hold()
"""

from doyoutrade.strategy_sdk import indicators, patterns
from doyoutrade.strategy_sdk.context import (
    AccountView,
    PositionView,
    StrategyContext,
)
from doyoutrade.strategy_sdk.data_provider import DataProvider
from doyoutrade.strategy_sdk.data_requests import (
    BarsRequest,
    CrossSectionRequest,
    DataRequest,
    FundamentalsRequest,
    IndexBarsRequest,
    PeersRequest,
)
from doyoutrade.strategy_sdk.errors import (
    DataAccessError,
    InformativeDataError,
    StrategyCompileError,
    StrategyError,
    StrategyValidationError,
)
from doyoutrade.strategy_sdk.helpers import Decimal, decimal_from_number
from doyoutrade.strategy_sdk.informative import (
    InformativeSpec,
    informative,
    informative_each,
    merge_informative_pair,
)
from doyoutrade.strategy_sdk.parameter_annotations import (
    apply_parameter_annotations,
    parse_parameter_annotations,
)
from doyoutrade.strategy_sdk.parameters import (
    BooleanParameter,
    CategoricalParameter,
    DecimalParameter,
    IntParameter,
)
from doyoutrade.strategy_sdk.runner import StrategyRunner
from doyoutrade.strategy_sdk.signal import Direction, ExitReason, Signal
from doyoutrade.strategy_sdk.strategy import Strategy
from doyoutrade.strategy_sdk.types import StrategyDescriptor

__all__ = [
    # Core types
    "Strategy",
    "StrategyContext",
    "Signal",
    "Direction",
    "ExitReason",
    # Data dependency declaration
    "DataRequest",
    "BarsRequest",
    "IndexBarsRequest",
    "PeersRequest",
    "CrossSectionRequest",
    "FundamentalsRequest",
    "informative",
    "informative_each",
    "InformativeSpec",
    "merge_informative_pair",
    # Parameters
    "IntParameter",
    "DecimalParameter",
    "CategoricalParameter",
    "BooleanParameter",
    "apply_parameter_annotations",
    "parse_parameter_annotations",
    # Read-only context views
    "AccountView",
    "PositionView",
    # Errors
    "StrategyError",
    "StrategyCompileError",
    "StrategyValidationError",
    "DataAccessError",
    "InformativeDataError",
    # Auxiliary
    "DataProvider",
    "StrategyRunner",
    "StrategyDescriptor",
    "Decimal",
    "decimal_from_number",
    "indicators",
    "patterns",
]
