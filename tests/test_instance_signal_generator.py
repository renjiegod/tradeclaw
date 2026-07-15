"""Tests for :class:`doyoutrade.bootstrap.InstanceSignalGenerator`.

Verifies the lazy compile + execute path used by the ``Strategy`` based
signal pipeline. StrategyInstance / ``si-`` bindings have been removed: the
runtime resolves the strategy purely from ``settings.strategy.definition_id``
plus ``parameter_overrides``. Uses a test double for the definition repository
to avoid touching the database.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd

from doyoutrade.bootstrap import InstanceSignalGenerator, StrategyConfigurationError
from doyoutrade.core.models import (
    AccountSnapshot,
    Bar,
    MarketContext,
)
from doyoutrade.core.signal_generator_protocol import SignalGenerationContext
from doyoutrade.persistence.strategy_storage import StrategyStorage
from doyoutrade.runtime.cycle_task import CycleTaskConfig
from doyoutrade.strategy_runtime.compiler import StrategyCompiler


# ---------- test doubles ----------


@dataclass
class _StubDefinition:
    definition_id: str
    class_name: str
    current_version: str = "v0001-deadbeef"
    code_hash: str = "deadbeef"
    default_parameters_json: dict | None = None


class _StubDefinitionRepo:
    def __init__(self, definitions: dict[str, _StubDefinition]):
        self._defs = definitions

    async def get_definition(self, definition_id: str) -> _StubDefinition:
        return self._defs[definition_id]


class _StubDataProvider:
    def __init__(self, bars: list[Bar]):
        self.bars = bars

    async def get_market_context(self):
        return MarketContext()

    async def get_bars(self, symbol, start_time, end_time, *, interval="1d", adjust="qfq"):
        return list(self.bars)

    async def is_trading_day(self, _date):
        return True

    async def get_trading_dates(self, _start, _end):
        return []


# ---------- canned strategy sources ----------
# NOTE: validate_directory always looks for a class named "Strategy", so the
# on-disk files must use that name regardless of what the test originally used.

_STRATEGY_SOURCE_ONDISK = """
from doyoutrade.strategy_sdk import Strategy, Signal


class Strategy(Strategy):
    timeframe = "1d"
    startup_history = 1

    def on_bar(self, df, ctx):
        return Signal.buy(tag="always_long")
"""


_LEGACY_SIGNAL_ENGINE_SOURCE_ONDISK = """
from doyoutrade.strategy_sdk import SignalEngine


class Strategy(SignalEngine):
    required_history = 1

    def generate(self, data_map, ctx):
        return {sym: 1 for sym in data_map}
"""


_CONFIGURABLE_STRATEGY_SOURCE_ONDISK = """
from doyoutrade.strategy_sdk import Strategy, Signal, IntParameter


class Strategy(Strategy):
    timeframe = "1d"
    startup_history = 1

    threshold = IntParameter(0, 100, default=5)

    def on_bar(self, df, ctx):
        if self.threshold.value > 0:
            return Signal.buy(tag="threshold_positive")
        return Signal.hold()
"""


# ---------- test-only subclass ----------

class _UnpinnedInstanceSignalGenerator(InstanceSignalGenerator):
    """Test-only subclass that allows generate_intents without a prior pin.

    All production code must use :class:`InstanceSignalGenerator` with
    ``_require_pin = True`` (the default).  This subclass exists solely to
    exercise the generate_intents logic in isolation, without the overhead of
    standing up a full worker that calls pin_code_version() first.
    """

    _require_pin: bool = False


# ---------- helpers ----------


def _config(
    definition_id: str = "def-1",
    *,
    parameter_overrides: dict | None = None,
) -> CycleTaskConfig:
    return CycleTaskConfig(
        name="signal-test",
        mode="paper",
        strategy_definition_id=definition_id,
        strategy_parameter_overrides=dict(parameter_overrides or {}),
        review_equity_fraction=1.0,
    )


def _ctx(universe: list[str], cash: float = 10000.0, equity: float = 10000.0) -> SignalGenerationContext:
    return SignalGenerationContext(
        market_context=MarketContext(
            symbol_to_price={sym: 10.0 for sym in universe}
        ),
        universe=list(universe),
        account_snapshot=AccountSnapshot(
            cash=Decimal(str(cash)), equity=Decimal(str(equity))
        ),
        positions=[],
        cycle_state=None,
    )


def _bars(symbol: str, count: int) -> list[Bar]:
    base = pd.Timestamp("2026-01-01")
    return [
        Bar(
            symbol=symbol,
            timestamp=(base + pd.Timedelta(days=i)).strftime("%Y-%m-%d"),
            open=10.0,
            high=10.5,
            low=9.5,
            close=10.0,
            volume=100.0,
        )
        for i in range(count)
    ]


def _materialize_version(
    storage: StrategyStorage,
    definition_id: str,
    version_label: str,
    source_code: str,
) -> None:
    """Write ``source_code`` to the versioned path the storage expects.

    Bypasses ``finalize_draft`` so the test controls the exact version label
    (including the ``deadbeef`` hash used in ``_StubDefinition``).
    """
    version_dir = storage.versions_dir(definition_id) / version_label
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "strategy.py").write_text(source_code)


class InstanceSignalGeneratorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.storage = StrategyStorage(self.tmp / "strategies")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_strategy_source_produces_intents(self):
        definition = _StubDefinition(
            definition_id="def-1",
            class_name="Strategy",
            current_version="v0001-deadbeef",
        )
        _materialize_version(
            self.storage, "def-1", "v0001-deadbeef", _STRATEGY_SOURCE_ONDISK
        )
        # Use the test subclass so generate_intents can run without a prior pin.
        generator = _UnpinnedInstanceSignalGenerator(
            config=_config(),
            definition_repository=_StubDefinitionRepo({"def-1": definition}),
            compiler=StrategyCompiler(),
            storage=self.storage,
            data_provider=_StubDataProvider(bars=_bars("X", 5)),
        )
        intents = await generator.generate_intents(_ctx(["X"]))
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].action, "buy")
        self.assertEqual(intents[0].symbol, "X")
        # The factor tag from Signal.buy(tag=...) flows onto the OrderIntent.
        self.assertEqual(intents[0].signal_tag, "always_long")

    async def test_compile_is_cached_across_cycles(self):
        """generate_intents must compile the strategy once and reuse the
        artifact on subsequent cycles with the same (definition_id, version).

        Recompiling on every cycle (``validate_directory`` re-parses and
        smoke-tests the source against synthetic regimes) was the dominant
        per-bar backtest cost — ~590ms/bar — so this guards the memoization.
        """
        definition = _StubDefinition(
            definition_id="def-1",
            class_name="Strategy",
            current_version="v0001-deadbeef",
        )
        _materialize_version(
            self.storage, "def-1", "v0001-deadbeef", _STRATEGY_SOURCE_ONDISK
        )

        real = StrategyCompiler()
        calls = {"n": 0}

        class _CountingCompiler:
            def validate_directory(self, code_root):
                calls["n"] += 1
                return real.validate_directory(code_root)

        generator = _UnpinnedInstanceSignalGenerator(
            config=_config(),
            definition_repository=_StubDefinitionRepo({"def-1": definition}),
            compiler=_CountingCompiler(),
            storage=self.storage,
            data_provider=_StubDataProvider(bars=_bars("X", 5)),
        )

        for _ in range(3):
            intents = await generator.generate_intents(_ctx(["X"]))
            self.assertEqual(len(intents), 1)

        self.assertEqual(
            calls["n"],
            1,
            "validate_directory must run once and be cached across cycles",
        )

    async def test_legacy_signal_engine_source_rejected(self):
        """SignalEngine sources predate the new API; compile fails because
        the symbol is no longer exported from strategy_sdk."""
        definition = _StubDefinition(
            definition_id="def-1",
            class_name="Strategy",
            current_version="v0001-deadbeef",
        )
        _materialize_version(
            self.storage, "def-1", "v0001-deadbeef", _LEGACY_SIGNAL_ENGINE_SOURCE_ONDISK
        )
        # Use the test subclass — we want to reach the compile step, not fail on pin.
        generator = _UnpinnedInstanceSignalGenerator(
            config=_config(),
            definition_repository=_StubDefinitionRepo({"def-1": definition}),
            compiler=StrategyCompiler(),
            storage=self.storage,
            data_provider=_StubDataProvider(bars=[]),
        )
        with self.assertRaisesRegex(ValueError, "failed to compile"):
            await generator.generate_intents(_ctx(["X"]))

    async def test_missing_definition_id_raises(self):
        # definition_id="" triggers strategy_definition_missing before the pin check.
        generator = InstanceSignalGenerator(
            config=CycleTaskConfig(
                name="x", mode="paper", strategy_definition_id=""
            ),
            definition_repository=_StubDefinitionRepo({}),
            compiler=StrategyCompiler(),
            storage=self.storage,
            data_provider=_StubDataProvider(bars=[]),
        )
        with self.assertRaises(StrategyConfigurationError) as cm:
            await generator.generate_intents(_ctx([]))
        self.assertEqual(cm.exception.error_code, "strategy_definition_missing")

    async def test_generate_intents_requires_pin_in_production(self) -> None:
        """Calling generate_intents without pin_code_version() raises StrategyConfigurationError
        with error_code='strategy_version_not_pinned' when _require_pin=True (default)."""
        definition = _StubDefinition(
            definition_id="def-pin-guard",
            class_name="Strategy",
            current_version="v0001-deadbeef",
        )
        # Production generator — _require_pin=True by default.
        generator = InstanceSignalGenerator(
            config=_config("def-pin-guard"),
            definition_repository=_StubDefinitionRepo({"def-pin-guard": definition}),
            compiler=StrategyCompiler(),
            storage=self.storage,
            data_provider=_StubDataProvider(bars=[]),
        )
        # Must NOT have been pinned.
        self.assertIsNone(generator._pinned_version)
        with self.assertRaises(StrategyConfigurationError) as cm:
            await generator.generate_intents(_ctx([]))
        self.assertEqual(cm.exception.error_code, "strategy_version_not_pinned")

    async def test_parameters_bind_via_descriptor(self):
        """``IntParameter`` declared on the class binds the supplied override
        at runner construction. Strategy reads via ``self.<name>.value``.

        The override now arrives via the task's ``parameter_overrides`` (merged
        on top of the definition's ``default_parameters_json``)."""
        definition = _StubDefinition(
            definition_id="def-1",
            class_name="Strategy",
            current_version="v0001-deadbeef",
        )
        _materialize_version(
            self.storage, "def-1", "v0001-deadbeef", _CONFIGURABLE_STRATEGY_SOURCE_ONDISK
        )
        # Use the test subclass so generate_intents can run without a prior pin.
        generator = _UnpinnedInstanceSignalGenerator(
            config=_config(parameter_overrides={"threshold": 10}),
            definition_repository=_StubDefinitionRepo({"def-1": definition}),
            compiler=StrategyCompiler(),
            storage=self.storage,
            data_provider=_StubDataProvider(bars=_bars("X", 5)),
        )
        intents = await generator.generate_intents(_ctx(["X"]))
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].action, "buy")
        self.assertEqual(intents[0].signal_tag, "threshold_positive")

    async def test_unknown_parameter_keys_dont_break_instantiation(self):
        """The Strategy API doesn't take __init__ kwargs — parameters bind via
        descriptors. Extra unknown keys in the override are simply ignored (the
        runner emits ``strategy_runner_cycle`` with the actual bound values)."""
        definition = _StubDefinition(
            definition_id="def-1",
            class_name="Strategy",
            current_version="v0001-deadbeef",
        )
        _materialize_version(
            self.storage, "def-1", "v0001-deadbeef", _STRATEGY_SOURCE_ONDISK
        )
        # Use the test subclass so generate_intents can run without a prior pin.
        generator = _UnpinnedInstanceSignalGenerator(
            config=_config(parameter_overrides={"unused_knob": 999}),
            definition_repository=_StubDefinitionRepo({"def-1": definition}),
            compiler=StrategyCompiler(),
            storage=self.storage,
            data_provider=_StubDataProvider(bars=_bars("X", 5)),
        )
        intents = await generator.generate_intents(_ctx(["X"]))
        self.assertEqual(len(intents), 1)

    async def test_no_current_version_raises_configuration_error(self) -> None:
        """A definition with no finalized version raises StrategyConfigurationError
        with error_code='strategy_no_current_version' rather than failing silently.

        Uses ``_UnpinnedInstanceSignalGenerator`` (_require_pin=False) so the test
        reaches the current_version=None check instead of the pin-required guard.
        """
        definition = _StubDefinition(
            definition_id="def-noversion",
            class_name="Strategy",
            current_version=None,  # type: ignore[arg-type]
        )
        # Must use unpinned subclass to reach the version check.
        generator = _UnpinnedInstanceSignalGenerator(
            config=_config("def-noversion"),
            definition_repository=_StubDefinitionRepo({"def-noversion": definition}),
            compiler=StrategyCompiler(),
            storage=self.storage,
            data_provider=_StubDataProvider(bars=[]),
        )
        with self.assertRaises(StrategyConfigurationError) as cm:
            await generator.generate_intents(_ctx([]))
        self.assertEqual(cm.exception.error_code, "strategy_no_current_version")


if __name__ == "__main__":
    unittest.main()
