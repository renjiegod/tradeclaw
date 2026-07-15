"""Tests for the portfolio protection engine (max-drawdown circuit breaker)."""

from __future__ import annotations

import unittest
from decimal import Decimal

from doyoutrade.execution.protection import (
    ProtectionEngine,
    protection_config_from_config,
    protection_engine_from_config,
)


class ProtectionConfigTests(unittest.TestCase):
    def test_off_when_absent_empty_or_disabled(self) -> None:
        self.assertIsNone(protection_config_from_config(None))
        self.assertIsNone(protection_config_from_config({}))
        self.assertIsNone(protection_config_from_config("nope"))
        self.assertIsNone(protection_config_from_config({"enabled": False, "max_drawdown_pct": 0.2}))

    def test_off_when_no_usable_guard(self) -> None:
        # a dict with only enabled:true but no threshold → nothing to enforce → off
        self.assertIsNone(protection_config_from_config({"enabled": True}))

    def test_valid_threshold(self) -> None:
        cfg = protection_config_from_config({"max_drawdown_pct": 0.2})
        assert cfg is not None
        self.assertEqual(cfg.max_drawdown_pct, 0.2)

    def test_out_of_range_rejected(self) -> None:
        for bad in (0.0, 1.0, 1.5, -0.1):
            with self.assertRaises(ValueError):
                protection_config_from_config({"max_drawdown_pct": bad})


class ProtectionEngineTests(unittest.TestCase):
    def _engine(self, mdd: float = 0.2) -> ProtectionEngine:
        eng = protection_engine_from_config({"max_drawdown_pct": mdd})
        assert eng is not None
        return eng

    def test_tracks_peak_and_halts_on_drawdown(self) -> None:
        eng = self._engine(0.2)
        # rise to a peak
        self.assertFalse(eng.evaluate(100000).halted)
        self.assertFalse(eng.evaluate(120000).halted)  # new peak 120000
        # 10% down from peak → within 20% → no halt
        d = eng.evaluate(108000)
        self.assertFalse(d.halted)
        self.assertAlmostEqual(d.drawdown_pct, 0.1, places=4)
        # 25% down from peak 120000 → breach
        d2 = eng.evaluate(90000)
        self.assertTrue(d2.halted)
        self.assertEqual(d2.reason, "max_drawdown_exceeded")
        self.assertEqual(d2.peak_equity, Decimal("120000"))
        self.assertAlmostEqual(d2.drawdown_pct, 0.25, places=4)

    def test_peak_does_not_reset_on_recovery_then_redrawdown(self) -> None:
        eng = self._engine(0.15)
        eng.evaluate(100000)  # peak 100000
        self.assertFalse(eng.evaluate(95000).halted)  # 5% dd
        # breach at 16% down
        self.assertTrue(eng.evaluate(84000).halted)

    def test_no_halt_at_exact_threshold(self) -> None:
        # halt is strictly > threshold; exactly 20% does not halt
        eng = self._engine(0.2)
        eng.evaluate(100000)
        self.assertFalse(eng.evaluate(80000).halted)


if __name__ == "__main__":
    unittest.main()
