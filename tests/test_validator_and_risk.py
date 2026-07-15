import unittest

from doyoutrade.core.models import AccountSnapshot, OrderIntent, PositionSnapshot
from doyoutrade.execution.risk import BasicRiskEngine, PassThroughRiskEngine, RiskConfig, merge_risk_config
from doyoutrade.execution.validator import OrderIntentValidator


class ValidatorAndRiskTests(unittest.TestCase):
    def test_validator_rejects_missing_amount(self):
        validator = OrderIntentValidator()
        intent = OrderIntent(
            intent_id="intent-1",
            symbol="600000.SH",
            action="buy",
            amount=None,
            order_type="market",
            tif="day",
            strategy_tag="test",
            price_reference=10.0,
            rationale="test",
        )

        result = validator.validate(intent)

        self.assertFalse(result.ok)
        self.assertIn("amount is required", result.error)

    def test_risk_engine_vetoes_oversized_order(self):
        engine = BasicRiskEngine(RiskConfig(max_single_order_amount=2000.0, max_position_ratio=0.5))
        account = AccountSnapshot(cash=10000.0, equity=10000.0)
        intent = OrderIntent(
            intent_id="intent-2",
            symbol="600000.SH",
            action="buy",
            amount=5000.0,
            order_type="market",
            tif="day",
            strategy_tag="test",
            price_reference=10.0,
            rationale="oversized",
        )

        decisions = engine.evaluate([intent], account_snapshot=account, positions=[])

        self.assertEqual(decisions[0].action, "veto")
        self.assertIn("max_single_order_amount", decisions[0].reason)

    def test_risk_engine_skips_notional_cap_when_unlimited(self) -> None:
        engine = BasicRiskEngine(RiskConfig(max_single_order_amount=None, max_position_ratio=0.5))
        account = AccountSnapshot(cash=50_000_000.0, equity=20_000_000.0)
        intent = OrderIntent(
            intent_id="intent-unl",
            symbol="600000.SH",
            action="buy",
            amount=500_000.0,
            order_type="market",
            tif="day",
            strategy_tag="test",
            price_reference=10.0,
            rationale="large",
        )
        decisions = engine.evaluate([intent], account_snapshot=account, positions=[])
        self.assertEqual(decisions[0].action, "pass")

    def test_merge_risk_config_keeps_base_when_no_overrides(self) -> None:
        base = RiskConfig(max_single_order_amount=1000.0, max_position_ratio=0.2)
        merged = merge_risk_config(base)
        self.assertEqual(merged.max_single_order_amount, 1000.0)
        self.assertEqual(merged.max_position_ratio, 0.2)

    def test_merge_risk_config_applies_instance_overrides(self) -> None:
        base = RiskConfig(max_single_order_amount=1000.0, max_position_ratio=0.2)
        merged = merge_risk_config(base, max_single_order_amount=500.0)
        self.assertEqual(merged.max_single_order_amount, 500.0)
        self.assertEqual(merged.max_position_ratio, 0.2)
        merged2 = merge_risk_config(base, max_position_ratio=0.05)
        self.assertEqual(merged2.max_single_order_amount, 1000.0)
        self.assertEqual(merged2.max_position_ratio, 0.05)

    def test_pass_through_risk_engine_never_vetoes(self) -> None:
        engine = PassThroughRiskEngine()
        account = AccountSnapshot(cash=1.0, equity=10_000.0)
        intent = OrderIntent(
            intent_id="intent-pt",
            symbol="600000.SH",
            action="buy",
            amount=9_000_000.0,
            order_type="market",
            tif="day",
            strategy_tag="test",
            price_reference=10.0,
            rationale="huge",
        )
        positions = [PositionSnapshot(symbol="600000.SH", quantity=5000.0, cost_price=10.0)]

        decisions = engine.evaluate([intent], account_snapshot=account, positions=positions)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "pass")


if __name__ == "__main__":
    unittest.main()
