import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from doyoutrade.core.post_cycle_account import build_post_cycle_account
from doyoutrade.core.models import AccountSnapshot, PositionSnapshot
from doyoutrade.money.decimal_helpers import (
    decimal_from_number,
    decimal_to_json_str,
    json_default_with_decimals,
)


class DecimalJsonStrTests(unittest.TestCase):
    def test_decimal_from_number_float_avoids_repr_binary_tail(self) -> None:
        """``repr(89999.12)`` expands IEEE noise; ledger ingest should use human decimal."""
        d = decimal_from_number(89999.12)
        self.assertEqual(decimal_to_json_str(d), "89999.12")

    def test_strips_spurious_trailing_zeros(self) -> None:
        """``format(f)`` on Decimal can echo operand exponent width; JSON should not."""
        self.assertEqual(decimal_to_json_str(Decimal("100000.0000000000000000")), "100000")
        self.assertEqual(decimal_to_json_str(Decimal("24.76")), "24.76")

    def test_json_dumps_default_matches_canonical_strings(self) -> None:
        raw = json.dumps({"x": Decimal("100000.0000000000000000")}, default=json_default_with_decimals)
        self.assertEqual(json.loads(raw), {"x": "100000"})


class PostCycleAccountTests(unittest.TestCase):
    def test_ledger_missing_available_defaults_to_zero_for_t1(self) -> None:
        """Ledger without ``available`` is treated as 0 sellable (T+1), not full quantity."""
        acct = AccountSnapshot(cash=50_000.0, equity=100_000.0)
        pos = [
            PositionSnapshot(symbol="600000.SH", quantity=100.0, cost_price=9.0),
        ]
        out = build_post_cycle_account(
            account=acct,
            positions=pos,
            source="ledger",
            symbol_to_price={"600000.SH": 10.0},
            captured_at=datetime(2026, 4, 18, 8, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(out["source"], "ledger")
        self.assertEqual(out["positions"][0]["available"], 0)
        self.assertEqual(out["positions"][0]["quantity"], 100)
        self.assertEqual(out["positions"][0]["last_price"], decimal_to_json_str(decimal_from_number(10.0)))
        self.assertEqual(
            out["positions"][0]["market_value"],
            decimal_to_json_str(Decimal(100) * decimal_from_number(10.0)),
        )
        self.assertEqual(
            out["total_market_value"],
            decimal_to_json_str(Decimal(100) * decimal_from_number(10.0)),
        )

    def test_broker_leaves_available_none_when_not_set(self) -> None:
        acct = AccountSnapshot(cash=0.0, equity=0.0)
        pos = [PositionSnapshot(symbol="600000.SH", quantity=100.0, cost_price=9.0)]
        out = build_post_cycle_account(account=acct, positions=pos, source="broker")
        self.assertIsNone(out["positions"][0]["available"])

    def test_zero_quantity_positions_omitted(self) -> None:
        acct = AccountSnapshot(cash=100_000.0, equity=100_000.0)
        pos = [
            PositionSnapshot(symbol="600000.SH", quantity=0.0, cost_price=0.0),
            PositionSnapshot(symbol="000592.SZ", quantity=10.0, cost_price=5.0),
        ]
        out = build_post_cycle_account(
            account=acct,
            positions=pos,
            source="ledger",
            symbol_to_price={"000592.SZ": 6.0},
        )
        self.assertEqual(len(out["positions"]), 1)
        self.assertEqual(out["positions"][0]["symbol"], "000592.SZ")
        self.assertEqual(out["positions"][0]["quantity"], 10)
        self.assertEqual(
            out["positions"][0]["market_value"],
            decimal_to_json_str(Decimal(10) * decimal_from_number(6.0)),
        )

    def test_market_value_is_quantity_times_last_price_after_floor(self) -> None:
        acct = AccountSnapshot(cash=0.0, equity=100_000.0)
        pos = [PositionSnapshot(symbol="000592.SZ", quantity=9000.900090009001, cost_price=11.11)]
        out = build_post_cycle_account(
            account=acct,
            positions=pos,
            source="ledger",
            symbol_to_price={"000592.SZ": 11.11},
        )
        self.assertEqual(out["positions"][0]["quantity"], 9000)
        self.assertEqual(
            out["positions"][0]["market_value"],
            decimal_to_json_str(Decimal(9000) * decimal_from_number(11.11)),
        )


if __name__ == "__main__":
    unittest.main()
