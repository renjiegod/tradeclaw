"""Unit tests for :mod:`doyoutrade.execution.position_manager`."""

from __future__ import annotations

import unittest
from decimal import Decimal

from doyoutrade.core.models import (
    AccountSnapshot,
    MarketContext,
    PositionSnapshot,
    TaskBudgetSnapshot,
)
from doyoutrade.execution.position_manager import (
    PositionConstraints,
    PositionManager,
)
from doyoutrade.execution.position_manager import PositionSignal as Signal


def _mc(prices: dict[str, float]) -> MarketContext:
    return MarketContext(symbol_to_price=dict(prices))


def _account(cash: float | str, equity: float | str) -> AccountSnapshot:
    return AccountSnapshot(cash=Decimal(str(cash)), equity=Decimal(str(equity)))


def _position(symbol: str, quantity: float, cost: float = 10.0) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol, quantity=quantity, cost_price=Decimal(str(cost))
    )


class PositionConstraintsValidationTest(unittest.TestCase):
    def test_default_constraints(self):
        c = PositionConstraints()
        self.assertEqual(c.equity_fraction, 1.0)
        self.assertIsNone(c.max_single_order_amount)
        self.assertEqual(c.max_position_ratio, 1.0)
        self.assertIsNone(c.max_task_position_amount)
        self.assertIsNone(c.max_task_position_ratio)

    def test_equity_fraction_must_be_in_range(self):
        with self.assertRaises(ValueError):
            PositionConstraints(equity_fraction=0.0)
        with self.assertRaises(ValueError):
            PositionConstraints(equity_fraction=1.5)
        with self.assertRaises(ValueError):
            PositionConstraints(equity_fraction=-0.1)

    def test_max_single_order_amount_must_be_positive_or_none(self):
        with self.assertRaises(ValueError):
            PositionConstraints(max_single_order_amount=0.0)
        with self.assertRaises(ValueError):
            PositionConstraints(max_single_order_amount=-100.0)
        PositionConstraints(max_single_order_amount=None)  # ok
        PositionConstraints(max_single_order_amount=100.0)  # ok

    def test_max_position_ratio_must_be_in_range(self):
        with self.assertRaises(ValueError):
            PositionConstraints(max_position_ratio=0.0)
        with self.assertRaises(ValueError):
            PositionConstraints(max_position_ratio=1.5)

    def test_task_budget_constraints_validate(self):
        with self.assertRaises(ValueError):
            PositionConstraints(max_task_position_amount=0.0)
        with self.assertRaises(ValueError):
            PositionConstraints(max_task_position_ratio=0.0)
        with self.assertRaises(ValueError):
            PositionConstraints(max_task_position_ratio=1.5)


class PositionManagerBuyTest(unittest.TestCase):
    def test_enter_long_uses_full_equity_when_uncapped(self):
        pm = PositionManager()  # default constraints: f=1.0, no cap
        signals = [Signal(symbol="600000", value=1)]
        out = pm.compute_intents(
            signals,
            _account(cash=10000, equity=10000),
            [],
            _mc({"600000": 10.0}),
        )
        self.assertEqual(len(out), 1)
        oi = out[0]
        self.assertEqual(oi.action, "buy")
        self.assertEqual(oi.symbol, "600000")
        # T = equity * 1.0 = 10000; budget = min(T, cash) = 10000; shares = 1000
        self.assertAlmostEqual(oi.amount, 10000.0)
        self.assertEqual(oi.price_reference, 10.0)

    def test_buy_respects_single_order_cap(self):
        pm = PositionManager(
            constraints=PositionConstraints(
                equity_fraction=1.0, max_single_order_amount=3000.0
            )
        )
        signals = [Signal(symbol="X", value=1)]
        out = pm.compute_intents(
            signals,
            _account(cash=10000, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        self.assertEqual(out[0].amount, 3000.0)

    def test_buy_scaled_down_by_max_position_ratio(self):
        # ratio 0.3 of 10000 equity → single-name target 3000, scaled from the
        # full 10000 equity-fraction budget. (Previously the ratio was ignored.)
        pm = PositionManager(
            constraints=PositionConstraints(equity_fraction=1.0, max_position_ratio=0.3)
        )
        out = pm.compute_intents(
            [Signal(symbol="X", value=1)],
            _account(cash=10000, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        self.assertEqual(out[0].amount, 3000.0)

    def test_default_ratio_is_non_binding(self):
        # Backward-compat: default ratio 1.0 must not change sizing.
        pm = PositionManager(constraints=PositionConstraints(equity_fraction=0.5))
        out = pm.compute_intents(
            [Signal(symbol="X", value=1)],
            _account(cash=10000, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        # T = equity * 0.5 = 5000 (ratio 1.0 doesn't bind)
        self.assertEqual(out[0].amount, 5000.0)

    def test_ratio_cap_combines_with_order_cap(self):
        # min(equity*fraction=10000, equity*ratio=4000, order_cap=2000) = 2000
        pm = PositionManager(
            constraints=PositionConstraints(
                equity_fraction=1.0, max_position_ratio=0.4, max_single_order_amount=2000.0
            )
        )
        out = pm.compute_intents(
            [Signal(symbol="X", value=1)],
            _account(cash=10000, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        self.assertEqual(out[0].amount, 2000.0)

    def test_buy_respects_cash_when_lower_than_T(self):
        pm = PositionManager()  # T = 10000
        signals = [Signal(symbol="X", value=1)]
        out = pm.compute_intents(
            signals,
            _account(cash=500, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        # budget = min(10000, 500) = 500; shares = 50; notional = 500
        self.assertEqual(out[0].amount, 500.0)

    def test_buy_scaled_down_by_remaining_task_budget(self):
        pm = PositionManager(
            constraints=PositionConstraints(
                equity_fraction=1.0,
                max_task_position_ratio=0.5,
            )
        )
        out = pm.compute_intents(
            [Signal(symbol="X", value=1)],
            _account(cash=10000, equity=10000),
            [],
            _mc({"X": 10.0}),
            task_budget_snapshot=TaskBudgetSnapshot(
                max_task_position_ratio=0.5,
                budget_cap=5000,
                current_usage=3000,
                remaining_budget=2000,
            ),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].amount, 2000.0)

    def test_buy_skipped_when_task_budget_exhausted(self):
        pm = PositionManager(
            constraints=PositionConstraints(
                equity_fraction=1.0,
                max_task_position_ratio=0.5,
            )
        )
        out = pm.compute_intents(
            [Signal(symbol="X", value=1)],
            _account(cash=10000, equity=10000),
            [],
            _mc({"X": 10.0}),
            task_budget_snapshot=TaskBudgetSnapshot(
                max_task_position_ratio=0.5,
                budget_cap=5000,
                current_usage=5000,
                remaining_budget=0,
            ),
        )
        self.assertEqual(out, [])

    def test_zero_cash_skips_buy(self):
        pm = PositionManager()
        signals = [Signal(symbol="X", value=1)]
        out = pm.compute_intents(
            signals,
            _account(cash=0, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        self.assertEqual(out, [])

    def test_zero_price_skips_buy(self):
        pm = PositionManager()
        signals = [Signal(symbol="X", value=1)]
        out = pm.compute_intents(
            signals,
            _account(cash=1000, equity=10000),
            [],
            _mc({"X": 0.0}),
        )
        self.assertEqual(out, [])

    def test_buy_below_one_share_skipped(self):
        pm = PositionManager(
            constraints=PositionConstraints(
                equity_fraction=1.0, max_single_order_amount=5.0
            )
        )
        signals = [Signal(symbol="X", value=1)]
        out = pm.compute_intents(
            signals,
            _account(cash=10000, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        # cap=5, price=10 -> 0 shares affordable
        self.assertEqual(out, [])

    def test_no_intent_when_already_long(self):
        pm = PositionManager()
        signals = [Signal(symbol="X", value=1)]
        out = pm.compute_intents(
            signals,
            _account(cash=1000, equity=10000),
            [_position("X", quantity=100)],
            _mc({"X": 10.0}),
        )
        self.assertEqual(out, [])

    def test_whole_share_rounding_floors_down(self):
        pm = PositionManager()
        signals = [Signal(symbol="X", value=1)]
        # cash=1037, price=10 -> 103 shares affordable -> notional=1030
        out = pm.compute_intents(
            signals,
            _account(cash=1037, equity=1037),
            [],
            _mc({"X": 10.0}),
        )
        self.assertEqual(out[0].amount, 1030.0)

    def test_target_exposure_buy_rebalances_from_flat(self):
        pm = PositionManager()
        out = pm.compute_intents(
            [Signal(symbol="X", target_exposure=0.25, tag="grid_l1")],
            _account(cash=10000, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].action, "buy")
        self.assertEqual(out[0].amount, 2500.0)
        self.assertEqual(out[0].signal_tag, "grid_l1")

    def test_target_exposure_buy_increases_existing_position_by_delta(self):
        pm = PositionManager()
        out = pm.compute_intents(
            [Signal(symbol="X", target_exposure=0.5, tag="grid_l2")],
            _account(cash=10000, equity=10000),
            [_position("X", quantity=200)],  # 200 * 10 = 2000 current notional
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].action, "buy")
        # target=5000 current=2000 -> buy 3000 more
        self.assertEqual(out[0].amount, 3000.0)

    def test_target_exposure_honors_equity_fraction_cap(self):
        pm = PositionManager(constraints=PositionConstraints(equity_fraction=0.4))
        out = pm.compute_intents(
            [Signal(symbol="X", target_exposure=0.8, tag="grid_l4")],
            _account(cash=10000, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        # requested 8000 capped to equity_fraction budget 4000
        self.assertEqual(out[0].amount, 4000.0)

    def test_target_quantity_buy_from_flat_uses_share_delta_only(self):
        pm = PositionManager()
        out = pm.compute_intents(
            [Signal(symbol="X", target_quantity=300, tag="grid_l3")],
            _account(cash=10000, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].action, "buy")
        self.assertEqual(out[0].amount, 3000.0)
        self.assertEqual(out[0].signal_tag, "grid_l3")

    def test_target_quantity_no_intent_when_inventory_already_matches(self):
        pm = PositionManager()
        out = pm.compute_intents(
            [Signal(symbol="X", target_quantity=300, tag="grid_l3")],
            _account(cash=10000, equity=12000),
            [_position("X", quantity=300)],
            _mc({"X": 12.0}),
        )
        self.assertEqual(out, [])

    def test_target_quantity_buy_adds_only_missing_shares(self):
        pm = PositionManager()
        out = pm.compute_intents(
            [Signal(symbol="X", target_quantity=500, tag="grid_l5")],
            _account(cash=10000, equity=10000),
            [_position("X", quantity=200)],
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].action, "buy")
        self.assertEqual(out[0].amount, 3000.0)


class PositionManagerSellTest(unittest.TestCase):
    def test_exit_long_emits_sell_all(self):
        pm = PositionManager()
        signals = [Signal(symbol="X", value=0)]
        out = pm.compute_intents(
            signals,
            _account(cash=0, equity=10000),
            [_position("X", quantity=100)],
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        oi = out[0]
        self.assertEqual(oi.action, "sell")
        self.assertEqual(oi.symbol, "X")
        # sell: amount = share count
        self.assertEqual(oi.amount, 100.0)

    def test_no_intent_when_flat_and_signal_zero(self):
        pm = PositionManager()
        signals = [Signal(symbol="X", value=0)]
        out = pm.compute_intents(
            signals,
            _account(cash=1000, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        self.assertEqual(out, [])

    def test_sell_ignores_cash(self):
        # Selling should not depend on cash balance.
        pm = PositionManager()
        signals = [Signal(symbol="X", value=0)]
        out = pm.compute_intents(
            signals,
            _account(cash=0, equity=0),
            [_position("X", quantity=42)],
            _mc({"X": 5.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].amount, 42.0)

    def test_sell_propagates_exit_reason_to_intent(self):
        pm = PositionManager()
        signals = [Signal(symbol="X", value=0, exit_reason="take_profit")]
        out = pm.compute_intents(
            signals,
            _account(cash=0, equity=10000),
            [_position("X", quantity=100)],
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].action, "sell")
        self.assertEqual(out[0].exit_reason, "take_profit")

    def test_sell_without_exit_reason_defaults_none(self):
        pm = PositionManager()
        signals = [Signal(symbol="X", value=0)]
        out = pm.compute_intents(
            signals,
            _account(cash=0, equity=10000),
            [_position("X", quantity=100)],
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0].exit_reason)

    def test_partial_fraction_scales_sell_quantity(self):
        pm = PositionManager()
        # fraction 0.5 of 100 held shares → sell 50.
        out = pm.compute_intents(
            [Signal(symbol="X", value=0, fraction=0.5)],
            _account(cash=0, equity=10000),
            [_position("X", quantity=100)],
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].action, "sell")
        self.assertEqual(out[0].amount, 50.0)

    def test_full_fraction_sells_all_unchanged(self):
        pm = PositionManager()
        # Default fraction 1.0 (and explicit) → full exit, byte-identical sizing.
        out = pm.compute_intents(
            [Signal(symbol="X", value=0, fraction=1.0)],
            _account(cash=0, equity=10000),
            [_position("X", quantity=100)],
            _mc({"X": 10.0}),
        )
        self.assertEqual(out[0].amount, 100.0)

    def test_target_exposure_sell_rebalances_down_by_delta(self):
        pm = PositionManager()
        out = pm.compute_intents(
            [Signal(symbol="X", target_exposure=0.4, tag="grid_trim")],
            _account(cash=0, equity=10000),
            [_position("X", quantity=1000)],  # current notional 10000
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].action, "sell")
        # target=4000, current=10000 -> sell 600 shares
        self.assertEqual(out[0].amount, 600.0)

    def test_target_quantity_sell_reduces_only_excess_inventory(self):
        pm = PositionManager()
        out = pm.compute_intents(
            [Signal(symbol="X", target_quantity=300, tag="grid_l3")],
            _account(cash=0, equity=10000),
            [_position("X", quantity=500)],
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].action, "sell")
        self.assertEqual(out[0].amount, 200.0)


class PositionManagerCashBudgetTest(unittest.TestCase):
    def test_multiple_buys_share_cash_budget(self):
        # f=0.6, equity=10000 -> T per symbol = 6000.
        # cash=10000, A first: budget=min(6000,10000)=6000 -> 600 sh @10 = 6000.
        # cash remaining = 4000; B: budget=min(6000,4000)=4000 -> 400 sh @10 = 4000.
        pm = PositionManager(constraints=PositionConstraints(equity_fraction=0.6))
        signals = [Signal(symbol="A", value=1), Signal(symbol="B", value=1)]
        out = pm.compute_intents(
            signals,
            _account(cash=10000, equity=10000),
            [],
            _mc({"A": 10.0, "B": 10.0}),
        )
        self.assertEqual(len(out), 2)
        amounts = {oi.symbol: oi.amount for oi in out}
        self.assertEqual(amounts["A"], 6000.0)
        self.assertEqual(amounts["B"], 4000.0)

    def test_second_buy_skipped_when_cash_exhausted(self):
        pm = PositionManager(constraints=PositionConstraints(equity_fraction=1.0))
        signals = [Signal(symbol="A", value=1), Signal(symbol="B", value=1)]
        out = pm.compute_intents(
            signals,
            _account(cash=10000, equity=10000),
            [],
            _mc({"A": 10.0, "B": 10.0}),
        )
        # A consumes all 10000; B sees 0 cash -> skipped
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].symbol, "A")


class PositionManagerSkipEventTest(unittest.TestCase):
    """Each silent skip surfaces a ``position_manager_skipped`` debug event."""

    def _capture(self):
        from doyoutrade.execution import position_manager as pm_module

        events: list[tuple[str, dict]] = []
        original = pm_module.emit_debug_event_sync

        def _emit(evt, payload):
            events.append((evt, dict(payload)))

        pm_module.emit_debug_event_sync = _emit
        return events, original

    def _restore(self, original):
        from doyoutrade.execution import position_manager as pm_module

        pm_module.emit_debug_event_sync = original

    def test_no_reference_price_emits_skip_event(self):
        events, original = self._capture()
        try:
            PositionManager().compute_intents(
                [Signal(symbol="X", value=1)],
                _account(cash=10000, equity=10000),
                [],
                _mc({"X": 0.0}),  # missing / zero price
            )
        finally:
            self._restore(original)
        skip_events = [p for evt, p in events if evt == "position_manager_skipped"]
        self.assertEqual(len(skip_events), 1)
        self.assertEqual(skip_events[0]["reason"], "no_reference_price")
        self.assertEqual(skip_events[0]["symbol"], "X")
        self.assertEqual(skip_events[0]["target_state"], 1)

    def test_insufficient_cash_emits_skip_event(self):
        events, original = self._capture()
        try:
            PositionManager().compute_intents(
                [Signal(symbol="X", value=1)],
                _account(cash=0, equity=10000),  # no cash
                [],
                _mc({"X": 10.0}),
            )
        finally:
            self._restore(original)
        skip = next(p for evt, p in events if evt == "position_manager_skipped")
        self.assertEqual(skip["reason"], "insufficient_cash_budget")

    def test_sub_one_share_at_price_emits_skip_event(self):
        events, original = self._capture()
        try:
            PositionManager(
                constraints=PositionConstraints(
                    equity_fraction=1.0, max_single_order_amount=5.0
                )
            ).compute_intents(
                [Signal(symbol="X", value=1)],
                _account(cash=10000, equity=10000),
                [],
                _mc({"X": 10.0}),  # cap=5 < price=10
            )
        finally:
            self._restore(original)
        skip = next(p for evt, p in events if evt == "position_manager_skipped")
        self.assertEqual(skip["reason"], "sub_one_share_at_price")

    def test_partial_exit_rounds_to_zero_emits_skip_event(self):
        events, original = self._capture()
        try:
            out = PositionManager().compute_intents(
                # fraction 0.4 of 2 held shares → floor(0.8) = 0 whole shares.
                [Signal(symbol="X", value=0, fraction=0.4)],
                _account(cash=0, equity=10000),
                [_position("X", quantity=2)],
                _mc({"X": 10.0}),
            )
        finally:
            self._restore(original)
        # No phantom zero-share order, and the skip is visible.
        self.assertEqual(out, [])
        skip = next(p for evt, p in events if evt == "position_manager_skipped")
        self.assertEqual(skip["reason"], "partial_exit_rounds_to_zero")
        # _emit_skip spreads detail to the payload top level.
        self.assertEqual(skip["fraction"], 0.4)


class PositionManagerMixedTest(unittest.TestCase):
    def test_sell_then_buy_cash_budget(self):
        # Sell signal doesn't add cash to budget within the same cycle (fills
        # are realized later). Buy signal still sees pre-cycle cash.
        pm = PositionManager()
        signals = [
            Signal(symbol="A", value=0),  # sell A
            Signal(symbol="B", value=1),  # buy B
        ]
        out = pm.compute_intents(
            signals,
            _account(cash=5000, equity=15000),
            [_position("A", quantity=50)],
            _mc({"A": 100.0, "B": 10.0}),
        )
        by_symbol = {oi.symbol: oi for oi in out}
        self.assertEqual(by_symbol["A"].action, "sell")
        self.assertEqual(by_symbol["A"].amount, 50.0)
        self.assertEqual(by_symbol["B"].action, "buy")
        # B budget = min(T=15000, cash=5000) = 5000 -> 500 sh @10 = 5000
        self.assertEqual(by_symbol["B"].amount, 5000.0)

    def test_intent_rationale_includes_signal_rationale(self):
        pm = PositionManager()
        signals = [Signal(symbol="X", value=1, rationale="MACD golden cross")]
        out = pm.compute_intents(
            signals,
            _account(cash=1000, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        self.assertIn("MACD golden cross", out[0].rationale)
        self.assertIn("signal=1", out[0].rationale)

    def test_strategy_tag_propagated(self):
        pm = PositionManager(strategy_tag="macd-v1")
        signals = [Signal(symbol="X", value=1)]
        out = pm.compute_intents(
            signals,
            _account(cash=1000, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        self.assertEqual(out[0].strategy_tag, "macd-v1")


class PositionConstraintsLotValidationTest(unittest.TestCase):
    def test_lot_size_and_hysteresis_defaults(self):
        c = PositionConstraints()
        self.assertEqual(c.lot_size, 1)
        self.assertEqual(c.rebalance_hysteresis_lots, 0)

    def test_lot_size_must_be_int_ge_1(self):
        with self.assertRaises(ValueError):
            PositionConstraints(lot_size=0)
        with self.assertRaises(ValueError):
            PositionConstraints(lot_size=-100)
        with self.assertRaises(ValueError):
            PositionConstraints(lot_size=100.5)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            PositionConstraints(lot_size=True)  # bool is not an int lot
        PositionConstraints(lot_size=100)  # ok

    def test_hysteresis_must_be_int_ge_0(self):
        with self.assertRaises(ValueError):
            PositionConstraints(rebalance_hysteresis_lots=-1)
        with self.assertRaises(ValueError):
            PositionConstraints(rebalance_hysteresis_lots=1.5)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            PositionConstraints(rebalance_hysteresis_lots=True)
        PositionConstraints(rebalance_hysteresis_lots=0)  # ok
        PositionConstraints(rebalance_hysteresis_lots=2)  # ok


class PositionManagerLotSizeTest(unittest.TestCase):
    """A股 100-share lot alignment on the explicit-target rebalance paths."""

    def _capture(self):
        from doyoutrade.execution import position_manager as pm_module

        events: list[tuple[str, dict]] = []
        original = pm_module.emit_debug_event_sync
        pm_module.emit_debug_event_sync = lambda evt, payload: events.append(
            (evt, dict(payload))
        )
        return events, original

    def _restore(self, original):
        from doyoutrade.execution import position_manager as pm_module

        pm_module.emit_debug_event_sync = original

    def _pm(self, **kwargs):
        return PositionManager(constraints=PositionConstraints(lot_size=100, **kwargs))

    def test_target_quantity_buy_floors_delta_to_lot_when_cash_binds(self):
        # cash only affords 137 shares; lot=100 → buy exactly 100.
        pm = self._pm()
        out = pm.compute_intents(
            [Signal(symbol="X", target_quantity=300, tag="grid_l3")],
            _account(cash=1375, equity=1375),
            [],
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].action, "buy")
        self.assertEqual(out[0].amount, 1000.0)  # 100 shares @ 10

    def test_target_quantity_buy_below_one_lot_skips(self):
        events, original = self._capture()
        try:
            # equity ample (cap won't bind), but cash affords only 99 < 1 lot.
            out = self._pm().compute_intents(
                [Signal(symbol="X", target_quantity=300, tag="grid_l3")],
                _account(cash=990, equity=100000),
                [],
                _mc({"X": 10.0}),
            )
        finally:
            self._restore(original)
        self.assertEqual(out, [])
        skip = next(
            p
            for evt, p in events
            if evt == "position_manager_skipped"
            and p["reason"] == "target_quantity_buy_below_one_lot"
        )
        self.assertEqual(skip["lot_size"], 100)
        self.assertEqual(skip["affordable_shares"], 99)

    def test_target_quantity_non_lot_target_is_lot_aligned_with_event(self):
        events, original = self._capture()
        try:
            out = self._pm().compute_intents(
                [Signal(symbol="X", target_quantity=250, tag="grid")],
                _account(cash=10000, equity=10000),
                [],
                _mc({"X": 10.0}),
            )
        finally:
            self._restore(original)
        # 250 floored to 200 shares.
        self.assertEqual(out[0].amount, 2000.0)
        align = next(
            p for evt, p in events if evt == "position_manager_target_quantity_lot_aligned"
        )
        self.assertEqual(align["applied_target_quantity"], 200)

    def test_target_quantity_cap_is_lot_aligned(self):
        # ratio cap 0.137 * 10000 = 1370 notional → 137 shares → lot-aligned 100.
        events, original = self._capture()
        try:
            pm = PositionManager(
                constraints=PositionConstraints(lot_size=100, max_position_ratio=0.137)
            )
            out = pm.compute_intents(
                [Signal(symbol="X", target_quantity=500, tag="grid")],
                _account(cash=10000, equity=10000),
                [],
                _mc({"X": 10.0}),
            )
        finally:
            self._restore(original)
        self.assertEqual(out[0].amount, 1000.0)  # capped to 100 shares
        cap = next(
            p for evt, p in events if evt == "position_manager_target_quantity_capped"
        )
        self.assertEqual(cap["cap_target_quantity"], 100)

    def test_target_quantity_full_exit_clears_odd_lot(self):
        # Hold an odd 150; target 0 must sell all 150 (odd lots exempt).
        pm = self._pm()
        out = pm.compute_intents(
            [Signal(symbol="X", target_quantity=0, tag="grid_l0")],
            _account(cash=0, equity=1500),
            [_position("X", quantity=150)],
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].action, "sell")
        self.assertEqual(out[0].amount, 150.0)

    def test_target_quantity_partial_reduce_floors_sell_to_lot(self):
        # Hold 350, target 100 → reduce 250 → lot-aligned 200.
        pm = self._pm()
        out = pm.compute_intents(
            [Signal(symbol="X", target_quantity=100, tag="grid_l1")],
            _account(cash=0, equity=3500),
            [_position("X", quantity=350)],
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].action, "sell")
        self.assertEqual(out[0].amount, 200.0)

    def test_target_quantity_partial_reduce_below_one_lot_skips(self):
        events, original = self._capture()
        try:
            # Hold 150, target 100 → reduce 50 < 1 lot → skip (not full exit).
            out = self._pm().compute_intents(
                [Signal(symbol="X", target_quantity=100, tag="grid_l1")],
                _account(cash=0, equity=1500),
                [_position("X", quantity=150)],
                _mc({"X": 10.0}),
            )
        finally:
            self._restore(original)
        self.assertEqual(out, [])
        skip = next(
            p
            for evt, p in events
            if p.get("reason") == "target_quantity_sell_below_one_lot"
        )
        self.assertEqual(skip["desired_sell_shares"], 50)

    def test_target_exposure_buy_floors_to_lot(self):
        # target 0.137 of 10000 = 1370 notional → 137 shares → lot-aligned 100.
        pm = self._pm()
        out = pm.compute_intents(
            [Signal(symbol="X", target_exposure=0.137, tag="grid")],
            _account(cash=10000, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        self.assertEqual(out[0].amount, 1000.0)

    def test_target_exposure_full_exit_clears_odd_lot(self):
        pm = self._pm()
        out = pm.compute_intents(
            [Signal(symbol="X", target_exposure=0.0, tag="grid_l0")],
            _account(cash=0, equity=1500),
            [_position("X", quantity=150)],
            _mc({"X": 10.0}),
        )
        self.assertEqual(out[0].action, "sell")
        self.assertEqual(out[0].amount, 150.0)

    def test_default_lot_size_one_keeps_odd_share_delta(self):
        # Regression: default lot_size=1 path is byte-identical to pre-lot.
        pm = PositionManager()
        out = pm.compute_intents(
            [Signal(symbol="X", target_quantity=137, tag="grid")],
            _account(cash=10000, equity=10000),
            [],
            _mc({"X": 10.0}),
        )
        self.assertEqual(out[0].amount, 1370.0)  # 137 shares, unaligned


class PositionManagerHysteresisTest(unittest.TestCase):
    """Rebalance dead band suppresses sub-threshold churn; exits bypass it."""

    def _capture(self):
        from doyoutrade.execution import position_manager as pm_module

        events: list[tuple[str, dict]] = []
        original = pm_module.emit_debug_event_sync
        pm_module.emit_debug_event_sync = lambda evt, payload: events.append(
            (evt, dict(payload))
        )
        return events, original

    def _restore(self, original):
        from doyoutrade.execution import position_manager as pm_module

        pm_module.emit_debug_event_sync = original

    def test_target_quantity_within_dead_band_skips(self):
        events, original = self._capture()
        try:
            # lot=100, hysteresis=2 lots → 200-share dead band.
            # Hold 300, target 400 → delta 100 < 200 → skip.
            pm = PositionManager(
                constraints=PositionConstraints(
                    lot_size=100, rebalance_hysteresis_lots=2
                )
            )
            out = pm.compute_intents(
                [Signal(symbol="X", target_quantity=400, tag="grid_l4")],
                _account(cash=100000, equity=100000),
                [_position("X", quantity=300)],
                _mc({"X": 10.0}),
            )
        finally:
            self._restore(original)
        self.assertEqual(out, [])
        skip = next(
            p for evt, p in events if p.get("reason") == "hysteresis_dead_band"
        )
        self.assertEqual(skip["delta_shares"], 100)
        self.assertEqual(skip["hysteresis_lots"], 2)

    def test_target_quantity_beyond_dead_band_trades(self):
        # Same band, delta 300 >= 200 → trade.
        pm = PositionManager(
            constraints=PositionConstraints(lot_size=100, rebalance_hysteresis_lots=2)
        )
        out = pm.compute_intents(
            [Signal(symbol="X", target_quantity=600, tag="grid_l6")],
            _account(cash=100000, equity=100000),
            [_position("X", quantity=300)],
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].amount, 3000.0)  # 300 shares @ 10

    def test_full_exit_bypasses_dead_band(self):
        # Hold 100, target 0, delta 100 < 200 band, but exit must flatten.
        pm = PositionManager(
            constraints=PositionConstraints(lot_size=100, rebalance_hysteresis_lots=2)
        )
        out = pm.compute_intents(
            [Signal(symbol="X", target_quantity=0, tag="grid_l0")],
            _account(cash=0, equity=1000),
            [_position("X", quantity=100)],
            _mc({"X": 10.0}),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].action, "sell")
        self.assertEqual(out[0].amount, 100.0)

    def test_target_exposure_within_dead_band_skips(self):
        events, original = self._capture()
        try:
            pm = PositionManager(
                constraints=PositionConstraints(
                    lot_size=100, rebalance_hysteresis_lots=2
                )
            )
            # Hold 300 (notional 3000), target exposure 0.4 → 4000 → delta 100 < 200.
            out = pm.compute_intents(
                [Signal(symbol="X", target_exposure=0.4, tag="grid")],
                _account(cash=100000, equity=10000),
                [_position("X", quantity=300)],
                _mc({"X": 10.0}),
            )
        finally:
            self._restore(original)
        self.assertEqual(out, [])
        skip = next(
            p for evt, p in events if p.get("reason") == "hysteresis_dead_band"
        )
        self.assertEqual(skip["delta_shares"], 100)


if __name__ == "__main__":
    unittest.main()
