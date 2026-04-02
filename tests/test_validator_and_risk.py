import unittest

from tradeclaw.domain.models import AccountSnapshot, OrderIntent
from tradeclaw.execution.risk import BasicRiskEngine, RiskConfig
from tradeclaw.execution.validator import OrderIntentValidator


class ValidatorAndRiskTests(unittest.TestCase):
    def test_validator_rejects_quantity_and_amount_together(self):
        validator = OrderIntentValidator()
        intent = OrderIntent(
            intent_id="intent-1",
            symbol="600000.SH",
            side="buy",
            quantity=100,
            amount=1000.0,
            order_type="market",
            tif="day",
            strategy_tag="test",
            price_reference=10.0,
            rationale="test",
        )

        result = validator.validate(intent)

        self.assertFalse(result.ok)
        self.assertIn("mutually exclusive", result.error)

    def test_risk_engine_vetoes_oversized_order(self):
        engine = BasicRiskEngine(RiskConfig(max_single_order_amount=2000.0, max_position_ratio=0.5))
        account = AccountSnapshot(cash=10000.0, equity=10000.0)
        intent = OrderIntent(
            intent_id="intent-2",
            symbol="600000.SH",
            side="buy",
            quantity=500,
            amount=None,
            order_type="market",
            tif="day",
            strategy_tag="test",
            price_reference=10.0,
            rationale="oversized",
        )

        decisions = engine.evaluate([intent], account_snapshot=account, positions=[])

        self.assertEqual(decisions[0].action, "veto")
        self.assertIn("max_single_order_amount", decisions[0].reason)


if __name__ == "__main__":
    unittest.main()
