import tempfile
import unittest
from pathlib import Path

from doyoutrade.persistence import (
    Base,
    SqlAlchemyStrategyDefinitionRepository,
    StrategyDefinitionSnapshot,
    create_engine_and_session_factory,
    dispose_engine,
)
from doyoutrade.persistence.errors import RecordNotFoundError, StateConflictError
from doyoutrade.strategy_registry import StrategyDefinitionCreate, StrategyRegistryService
from doyoutrade.strategy_runtime.compiler import StrategyCompiler


_VALID_STRATEGY_SOURCE = """
from doyoutrade.strategy_sdk import Strategy, Signal


class MomentumStrategy(Strategy):
    timeframe = "1d"
    startup_history = 1

    def on_bar(self, df, ctx):
        return Signal.buy(tag="always_long")
"""


_VALID_INDICATOR_SOURCE = """
from doyoutrade.strategy_sdk import Strategy, Signal, indicators


class MacdStrategy(Strategy):
    timeframe = "1d"
    startup_history = 60

    def populate_indicators(self, df, ctx):
        macd = indicators.macd(df["close"], fast=12, slow=26, signal=9)
        df["macd_hist"] = macd.hist
        return df

    def on_bar(self, df, ctx):
        last = df["macd_hist"].iloc[-1]
        if last > 0:
            return Signal.buy(tag="macd_positive")
        return Signal.hold()
"""


class StrategyRegistryPersistenceSurfaceTests(unittest.TestCase):
    def test_strategy_persistence_exports_are_available(self):
        self.assertIsNotNone(SqlAlchemyStrategyDefinitionRepository)
        self.assertIsNotNone(StrategyDefinitionSnapshot)


class StrategyCompilerTests(unittest.TestCase):
    def test_validate_definition_accepts_strategy_subclass(self) -> None:
        result = StrategyCompiler().validate_definition(
            _VALID_STRATEGY_SOURCE, "MomentumStrategy"
        )
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.errors, ())
        self.assertTrue(result.code_hash)

    def test_validate_definition_rejects_non_strategy_class(self) -> None:
        source = """
class MomentumStrategy:
    pass
"""
        result = StrategyCompiler().validate_definition(source, "MomentumStrategy")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "invalid_base_class")
        self.assertIn("Strategy", result.errors[0])

    def test_validate_definition_rejects_disallowed_import(self) -> None:
        source = """
import os
from doyoutrade.strategy_sdk import Strategy, Signal

class MomentumStrategy(Strategy):
    timeframe = "1d"
    startup_history = 1
    def on_bar(self, df, ctx):
        return Signal.buy(tag=os.getcwd())
"""
        result = StrategyCompiler().validate_definition(source, "MomentumStrategy")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "disallowed_import")

    def test_validate_definition_accepts_pandas_numpy(self) -> None:
        result = StrategyCompiler().validate_definition(
            _VALID_INDICATOR_SOURCE, "MacdStrategy"
        )
        self.assertTrue(result.success, result.errors)

    def test_validate_definition_returns_class_not_found_when_name_missing(self) -> None:
        source = """
from doyoutrade.strategy_sdk import Strategy, Signal

class GeneratedStrategy(Strategy):
    timeframe = "1d"
    startup_history = 1
    def on_bar(self, df, ctx):
        return Signal.hold()
"""
        result = StrategyCompiler().validate_definition(source, "MACDStrategy")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_required_class")
        self.assertEqual(result.validation_errors[0]["expected"], "MACDStrategy")

    def test_validate_definition_returns_missing_on_bar_error(self) -> None:
        source = """
from doyoutrade.strategy_sdk import Strategy

class MomentumStrategy(Strategy):
    timeframe = "1d"
    startup_history = 1
"""
        result = StrategyCompiler().validate_definition(source, "MomentumStrategy")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "missing_on_bar")

    def test_validate_definition_rejects_rolling_literal_exceeding_startup_history(self) -> None:
        """rolling(N) literal must not exceed startup_history (which sets
        the data window provisioned for both smoke and live cycles)."""
        source = """
from doyoutrade.strategy_sdk import Strategy, Signal

class HiddenDriftStrategy(Strategy):
    timeframe = "1d"
    startup_history = 20

    def populate_indicators(self, df, ctx):
        df["ma"] = df["close"].rolling(50).mean()
        return df

    def on_bar(self, df, ctx):
        return Signal.hold()
"""
        result = StrategyCompiler().validate_definition(source, "HiddenDriftStrategy")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "history_check_literal_disallowed")

    def test_validate_definition_accepts_rolling_within_startup_history(self) -> None:
        source = """
from doyoutrade.strategy_sdk import Strategy, Signal

class CompliantStrategy(Strategy):
    timeframe = "1d"
    startup_history = 50

    def populate_indicators(self, df, ctx):
        df["ma"] = df["close"].rolling(20).mean()
        return df

    def on_bar(self, df, ctx):
        return Signal.hold()
"""
        result = StrategyCompiler().validate_definition(source, "CompliantStrategy")
        self.assertTrue(result.success, result.errors)


class StrategyCompilerSmokeTests(unittest.TestCase):
    """Single-cycle smoke gate inside :meth:`StrategyCompiler.smoke_test`.

    Verifies that strategies which compile cleanly but call hallucinated
    helpers or return the wrong shape get caught before a backtest_job is
    created.
    """

    def test_smoke_catches_hallucinated_self_method(self) -> None:
        source = """
from doyoutrade.strategy_sdk import Strategy, Signal


class Bad(Strategy):
    timeframe = "1d"
    startup_history = 5

    def on_bar(self, df, ctx):
        # Hallucinated helper — compile-clean, fails at first bar.
        qty = self.get_position_qty(ctx.symbol)
        return Signal.buy(tag="qty") if qty > 0 else Signal.hold()
"""
        compiler = StrategyCompiler()
        compile_result = compiler.validate_definition(source, "Bad")
        self.assertTrue(compile_result.success, compile_result.errors)
        assert compile_result.artifact is not None
        smoke = compiler.smoke_test(compile_result.artifact)
        self.assertFalse(smoke.success)
        self.assertEqual(smoke.error_code, "runtime_smoke_failed")
        self.assertEqual(smoke.error_type, "AttributeError")
        assert smoke.error_message is not None
        self.assertIn("get_position_qty", smoke.error_message)

    def test_smoke_accepts_clean_strategy(self) -> None:
        compiler = StrategyCompiler()
        compile_result = compiler.validate_definition(
            _VALID_INDICATOR_SOURCE, "MacdStrategy"
        )
        self.assertTrue(compile_result.success, compile_result.errors)
        assert compile_result.artifact is not None
        smoke = compiler.smoke_test(compile_result.artifact)
        self.assertTrue(smoke.success, smoke.repair_hints)
        self.assertIsNone(smoke.error_code)

    def test_smoke_catches_ctor_taking_arguments(self) -> None:
        source = """
from doyoutrade.strategy_sdk import Strategy, Signal


class NeedsCtorArgs(Strategy):
    timeframe = "1d"
    startup_history = 1

    def __init__(self, threshold):
        super().__init__()
        self.threshold = threshold

    def on_bar(self, df, ctx):
        return Signal.buy(tag="x")
"""
        compiler = StrategyCompiler()
        compile_result = compiler.validate_definition(source, "NeedsCtorArgs")
        self.assertTrue(compile_result.success, compile_result.errors)
        assert compile_result.artifact is not None
        smoke = compiler.smoke_test(compile_result.artifact)
        self.assertFalse(smoke.success)
        self.assertEqual(smoke.error_code, "runtime_smoke_failed")
        self.assertEqual(smoke.validation_errors[0]["stage"], "__init__")

    def test_smoke_catches_non_signal_return(self) -> None:
        source = """
from doyoutrade.strategy_sdk import Strategy


class ReturnsString(Strategy):
    timeframe = "1d"
    startup_history = 1

    def on_bar(self, df, ctx):
        return "not_a_signal"
"""
        compiler = StrategyCompiler()
        compile_result = compiler.validate_definition(source, "ReturnsString")
        self.assertTrue(compile_result.success, compile_result.errors)
        assert compile_result.artifact is not None
        smoke = compiler.smoke_test(compile_result.artifact)
        self.assertFalse(smoke.success)
        self.assertEqual(smoke.error_code, "smoke_output_invalid")


class StrategyRegistryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "strategy-registry.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.definition_repo = SqlAlchemyStrategyDefinitionRepository(self.session_factory)
        self.service = StrategyRegistryService(self.definition_repo)

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    def _payload(self, definition_id: str, name: str = "Momentum") -> StrategyDefinitionCreate:
        return StrategyDefinitionCreate(
            definition_id=definition_id,
            name=name,
            api_version="v1",
            parameter_schema={"type": "object"},
            default_parameters={"lookback": 20},
            input_contract={"mode": "signal"},
            capabilities={"composition": ["ensemble"]},
            provenance={"source": "test"},
        )

    async def test_register_definition_persists_metadata(self) -> None:
        # The service no longer compiles source on write; metadata is stored
        # directly.  code_hash is empty unless explicitly supplied.
        created = await self.service.create_definition(self._payload("sd-momentum"))

        self.assertEqual(created.definition_id, "sd-momentum")
        self.assertEqual(created.name, "Momentum")
        self.assertEqual(created.default_parameters_json, {"lookback": 20})

        loaded = await self.definition_repo.get_definition("sd-momentum")
        self.assertEqual(loaded.definition_id, "sd-momentum")

    async def test_register_definition_with_explicit_code_hash(self) -> None:
        # Callers that have already compiled (e.g. register_definition) can
        # pass code_hash explicitly and it is stored.
        payload = StrategyDefinitionCreate(
            definition_id="sd-with-hash",
            name="WithHash",
            api_version="v1",
            code_hash="abc123",
            provenance={"source": "test"},
        )
        created = await self.service.create_definition(payload)
        self.assertEqual(created.code_hash, "abc123")

    async def test_register_definition_raises_conflict_for_duplicate_definition_id(self) -> None:
        payload = self._payload("sd-duplicate")
        await self.service.create_definition(payload)
        with self.assertRaises(StateConflictError):
            await self.service.create_definition(payload)

    async def test_delete_definition_removes_definition(self) -> None:
        await self.service.create_definition(self._payload("sd-delete", name="Delete Me"))
        await self.service.delete_definition("sd-delete")
        with self.assertRaises(RecordNotFoundError):
            await self.definition_repo.get_definition("sd-delete")


if __name__ == "__main__":
    unittest.main()
