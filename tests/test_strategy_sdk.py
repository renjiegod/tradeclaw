import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal

from doyoutrade.strategy_sdk.examples._synthetic import make_ohlcv
from doyoutrade.strategy_sdk.examples.grid_target_exposure import (
    GridTargetExposureStrategy,
)
from doyoutrade.strategy_sdk.examples.grid_target_quantity import (
    GridTargetQuantityStrategy,
)
import doyoutrade.strategy_sdk as strategy_sdk
from doyoutrade.strategy_sdk import (
    Decimal as SdkDecimal,
    Direction,
    ExitReason,
    IntParameter,
    Signal,
    Strategy,
    StrategyDescriptor,
    decimal_from_number,
    patterns,
)
from doyoutrade.strategy_runtime.compiler import _SDK_NAMESPACE


class StrategySdkTests(unittest.TestCase):
    def test_public_exports_include_authoring_surface(self) -> None:
        self.assertIs(SdkDecimal, Decimal)
        self.assertEqual(decimal_from_number("1.25"), Decimal("1.25"))

        class DemoStrategy(Strategy):
            timeframe = "1d"
            startup_history = 2

            def on_bar(self, df, ctx):
                return Signal.buy(tag="demo_long")

        # Strategy is instantiable; on_bar can be called against a stub df/ctx.
        self.assertIsInstance(DemoStrategy(), Strategy)

    def test_base_strategy_requires_on_bar(self) -> None:
        class DemoStrategy(Strategy):
            pass

        with self.assertRaises(TypeError):
            DemoStrategy()  # type: ignore[abstract]

    def test_signal_buy_requires_tag(self) -> None:
        # Tag mandatory for BUY/SELL.
        Signal.buy(tag="ok")
        Signal.sell(tag="ok")
        # HOLD does not require tag.
        Signal.hold()
        with self.assertRaises(Exception):
            Signal.buy(tag="")
        with self.assertRaises(Exception):
            Signal.sell(tag="")

    def test_signal_direction_and_target_state_projection(self) -> None:
        self.assertEqual(Signal.buy(tag="x").direction, Direction.BUY)
        self.assertEqual(Signal.sell(tag="x").direction, Direction.SELL)
        self.assertEqual(Signal.hold().direction, Direction.HOLD)
        self.assertEqual(
            Signal.target_exposure(target=0.5, tag="grid").direction,
            Direction.TARGET_EXPOSURE,
        )
        self.assertEqual(
            Signal.target_quantity(quantity=300, tag="grid").direction,
            Direction.TARGET_QUANTITY,
        )
        self.assertEqual(Signal.buy(tag="x").to_target_state(), 1)
        self.assertEqual(Signal.sell(tag="x").to_target_state(), 0)
        self.assertIsNone(Signal.hold().to_target_state())
        self.assertIsNone(
            Signal.target_exposure(target=0.5, tag="grid").to_target_state()
        )
        self.assertIsNone(
            Signal.target_quantity(quantity=300, tag="grid").to_target_state()
        )

    def test_int_parameter_binds_and_clamps(self) -> None:
        p = IntParameter(5, 20, default=10)
        self.assertEqual(p.value, 10)
        p.bind(15)
        self.assertEqual(p.value, 15)
        with self.assertRaises(Exception):
            p.bind(100)  # outside [5, 20]
        p.bind(None)
        self.assertEqual(p.value, 10)  # back to default

    def test_strategy_descriptor_is_optional_metadata(self) -> None:
        descriptor = StrategyDescriptor(
            name="momentum",
            description="signal generator",
            parameter_schema={"window": {"type": "integer"}},
            capabilities={"supports_children": True},
        )

        self.assertEqual(descriptor.name, "momentum")
        self.assertEqual(descriptor.parameter_schema["window"]["type"], "integer")
        self.assertTrue(descriptor.capabilities["supports_children"])

        with self.assertRaises((FrozenInstanceError, AttributeError)):
            descriptor.name = "value"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            descriptor.parameter_schema["window"] = {}  # type: ignore[index]

    def test_patterns_module_is_in_sdk_namespace(self) -> None:
        # The strategy compiler exposes `patterns` to authored strategies
        # via _SDK_NAMESPACE. Author-time code (`patterns.is_hammer(...)`)
        # will only resolve if this binding is in place AND points to the
        # same module object that `from doyoutrade.strategy_sdk import
        # patterns` returns — otherwise authors get a confusing
        # ImportError or NameError at compile time.
        self.assertIn("patterns", _SDK_NAMESPACE)
        # Identity on the module object is sufficient: if these are the same
        # object, every attribute (is_hammer, swing_high, ...) trivially
        # matches. Avoids a Pyright union-attr complaint from the
        # heterogeneous _SDK_NAMESPACE value type.
        self.assertIs(_SDK_NAMESPACE["patterns"], patterns)
        self.assertIn("patterns", strategy_sdk.__all__)

    def test_startup_history_default_is_30(self) -> None:
        class Minimal(Strategy):
            timeframe = "1d"

            def on_bar(self, df, ctx):
                return Signal.hold()

        self.assertEqual(Minimal.startup_history, 30)


class SignalExitReasonTests(unittest.TestCase):
    def test_exit_reason_exported(self) -> None:
        self.assertEqual(ExitReason.TAKE_PROFIT.value, "take_profit")
        self.assertIn(ExitReason, (strategy_sdk.ExitReason,))

    def test_default_signal_omits_exit_reason_byte_identical(self) -> None:
        # A SELL with no exit_reason must serialize identically to the
        # pre-feature shape (golden cycle_runs / debug snapshots stay stable).
        sell = Signal.sell(tag="ma_cross")
        self.assertIsNone(sell.exit_reason)
        self.assertEqual(
            sell.to_dict(),
            {
                "direction": "sell",
                "tag": "ma_cross",
                "rationale": "",
                "diagnostics": {},
            },
        )
        self.assertNotIn("exit_reason", sell.to_dict())

    def test_exit_reason_set_appears_in_to_dict(self) -> None:
        from doyoutrade.strategy_sdk import ExitReason

        sell = Signal.sell(tag="ma_cross", exit_reason="take_profit")
        self.assertEqual(sell.exit_reason, "take_profit")
        self.assertEqual(sell.to_dict()["exit_reason"], "take_profit")
        # Enum value is accepted and normalized to its string form.
        sell2 = Signal.sell(tag="ma_cross", exit_reason=ExitReason.STOP_LOSS)
        self.assertEqual(sell2.exit_reason, "stop_loss")
        # Case-insensitive string normalization.
        sell3 = Signal.sell(tag="t", exit_reason="TRAILING_STOP")
        self.assertEqual(sell3.exit_reason, "trailing_stop")

    def test_unknown_exit_reason_rejected_with_error_code(self) -> None:
        from doyoutrade.strategy_sdk.errors import (
            INVALID_EXIT_REASON,
            StrategyValidationError,
        )

        with self.assertRaises(StrategyValidationError) as ctx:
            Signal.sell(tag="t", exit_reason="moon")
        self.assertEqual(ctx.exception.error_code, INVALID_EXIT_REASON)


class SignalFractionTests(unittest.TestCase):
    def test_default_fraction_is_full_and_omitted_from_to_dict(self) -> None:
        sell = Signal.sell(tag="ma_cross")
        self.assertEqual(sell.fraction, 1.0)
        # fraction=1.0 must NOT appear in to_dict (byte-identity for full exits).
        self.assertNotIn("fraction", sell.to_dict())

    def test_partial_fraction_set_and_serialized(self) -> None:
        sell = Signal.sell(tag="ma_cross", fraction=0.5)
        self.assertEqual(sell.fraction, 0.5)
        self.assertEqual(sell.to_dict()["fraction"], 0.5)

    def test_fraction_one_int_accepted(self) -> None:
        self.assertEqual(Signal.sell(tag="t", fraction=1).fraction, 1.0)

    def test_invalid_fraction_rejected_with_error_code(self) -> None:
        from doyoutrade.strategy_sdk.errors import (
            INVALID_SIGNAL_FRACTION,
            StrategyValidationError,
        )

        for bad in (0, 1.5, -0.1, float("nan")):
            with self.subTest(fraction=bad):
                with self.assertRaises(StrategyValidationError) as ctx:
                    Signal.sell(tag="t", fraction=bad)
                self.assertEqual(ctx.exception.error_code, INVALID_SIGNAL_FRACTION)


class SignalTargetExposureTests(unittest.TestCase):
    def test_target_exposure_serialized(self) -> None:
        signal = Signal.target_exposure(
            target=0.75,
            tag="grid_l3",
            diagnostics={"band": 3},
        )
        self.assertEqual(signal.target_exposure_value, 0.75)
        self.assertEqual(
            signal.to_dict(),
            {
                "direction": "target_exposure",
                "tag": "grid_l3",
                "rationale": "",
                "diagnostics": {"band": 3},
                "target_exposure": 0.75,
            },
        )

    def test_invalid_target_exposure_rejected_with_error_code(self) -> None:
        from doyoutrade.strategy_sdk.errors import (
            INVALID_TARGET_EXPOSURE,
            StrategyValidationError,
        )

        for bad in (-0.1, 1.1, float("nan")):
            with self.subTest(target=bad):
                with self.assertRaises(StrategyValidationError) as ctx:
                    Signal.target_exposure(target=bad, tag="grid")
                self.assertEqual(ctx.exception.error_code, INVALID_TARGET_EXPOSURE)


class SignalTargetQuantityTests(unittest.TestCase):
    def test_target_quantity_serialized(self) -> None:
        signal = Signal.target_quantity(
            quantity=300,
            tag="grid_l3",
            diagnostics={"level": 3},
        )
        self.assertEqual(signal.target_quantity_value, 300.0)
        self.assertEqual(
            signal.to_dict(),
            {
                "direction": "target_quantity",
                "tag": "grid_l3",
                "rationale": "",
                "diagnostics": {"level": 3},
                "target_quantity": 300.0,
            },
        )

    def test_invalid_target_quantity_rejected_with_error_code(self) -> None:
        from doyoutrade.strategy_sdk.errors import (
            INVALID_TARGET_QUANTITY,
            StrategyValidationError,
        )

        for bad in (-1, -100.0, float("nan")):
            with self.subTest(quantity=bad):
                with self.assertRaises(StrategyValidationError) as ctx:
                    Signal.target_quantity(quantity=bad, tag="grid")
                self.assertEqual(ctx.exception.error_code, INVALID_TARGET_QUANTITY)


class StrategySdkExampleTests(unittest.TestCase):
    def test_grid_target_exposure_example_emits_target_exposure_signal(self) -> None:
        strategy = GridTargetExposureStrategy()
        df = make_ohlcv("DEMO.GRID", bars=160, drift=-0.0015, seed=101)
        populated = strategy.populate_indicators(df.copy(), ctx=None)  # type: ignore[arg-type]
        signal = strategy.on_bar(populated, ctx=None)  # type: ignore[arg-type]
        self.assertTrue(signal.is_target_exposure)
        self.assertIsNotNone(signal.tag)
        self.assertGreaterEqual(signal.target_exposure_value or -1, 0.0)
        self.assertLessEqual(signal.target_exposure_value or 2, 1.0)

    def test_grid_target_quantity_example_emits_target_quantity_signal(self) -> None:
        strategy = GridTargetQuantityStrategy()
        df = make_ohlcv("DEMO.GRID", bars=160, drift=-0.0022, seed=202)
        populated = strategy.populate_indicators(df.copy(), ctx=None)  # type: ignore[arg-type]
        signal = strategy.on_bar(populated, ctx=None)  # type: ignore[arg-type]
        self.assertTrue(signal.is_target_quantity)
        self.assertIsNotNone(signal.tag)
        self.assertGreaterEqual(signal.target_quantity_value or -1, 0.0)



if __name__ == "__main__":
    unittest.main()
