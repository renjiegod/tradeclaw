from __future__ import annotations

import unittest

from doyoutrade.data.continuity import (
    CONTINUITY_VIOLATION_ERROR_CODE,
    ContinuityError,
    covered_days_from_payloads,
    validate_continuity,
)

_CAL = {
    "2026-01-05",
    "2026-01-06",
    "2026-01-07",
    "2026-01-08",
    "2026-01-09",
}


def _bars(days: list[str]) -> list[dict]:
    return [
        {
            "symbol": "600000.SH",
            "timestamp": d,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
        }
        for d in days
    ]


class CoveredDaysTests(unittest.TestCase):
    def test_extracts_distinct_days(self) -> None:
        covered = covered_days_from_payloads(_bars(["2026-01-05", "2026-01-06"]), interval="1d")
        self.assertEqual({d.isoformat() for d in covered}, {"2026-01-05", "2026-01-06"})

    def test_empty_timestamp_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "continuity_bar_timestamp_invalid"):
            covered_days_from_payloads([{"timestamp": ""}], interval="1d")


class ValidateContinuityTests(unittest.TestCase):
    def _run(self, days, *, cal=_CAL, suspended=None, authoritative=True, susp_avail=True):
        return validate_continuity(
            bars=_bars(days),
            interval="1d",
            expected_trading_days=cal,
            suspended_days=suspended or set(),
            authoritative=authoritative,
            suspension_source_available=susp_avail,
            max_internal_gap_days=90,
        )

    def test_complete_is_ok(self) -> None:
        r = self._run(sorted(_CAL))
        self.assertEqual(r.classification, "ok")
        self.assertTrue(r.ok)

    def test_suspension_excluded_is_ok(self) -> None:
        r = self._run(sorted(_CAL - {"2026-01-07"}), suspended={"2026-01-07"})
        self.assertEqual(r.classification, "ok")

    def test_confirmed_defect_is_hard_violation(self) -> None:
        r = self._run(sorted(_CAL - {"2026-01-07"}))
        self.assertEqual(r.classification, "calendar_violation")
        self.assertTrue(r.is_hard_violation)
        self.assertEqual(r.missing_days, ["2026-01-07"])

    def test_unverifiable_when_no_suspension_source(self) -> None:
        r = self._run(sorted(_CAL - {"2026-01-07"}), susp_avail=False)
        self.assertEqual(r.classification, "calendar_unverifiable")
        self.assertFalse(r.is_hard_violation)

    def test_non_authoritative_small_gap_degrades_ok(self) -> None:
        r = self._run(sorted(_CAL - {"2026-01-07"}), authoritative=False, cal=None)
        self.assertEqual(r.classification, "degraded_ok")
        self.assertTrue(r.ok)

    def test_non_authoritative_huge_gap_is_violation(self) -> None:
        r = validate_continuity(
            bars=_bars(["2026-01-05", "2026-06-01"]),
            interval="1d",
            expected_trading_days=None,
            suspended_days=set(),
            authoritative=False,
            suspension_source_available=False,
            max_internal_gap_days=90,
        )
        self.assertEqual(r.classification, "internal_gap_violation")
        self.assertTrue(r.is_hard_violation)
        self.assertGreater(r.largest_internal_gap_days, 90)

    def test_listing_boundary_not_flagged(self) -> None:
        # Symbol only traded the last 3 days of the window (IPO mid-window): the
        # earlier calendar days are a listing boundary, NOT an internal gap.
        r = self._run(["2026-01-07", "2026-01-08", "2026-01-09"])
        self.assertEqual(r.classification, "ok")

    def test_error_carries_stable_code(self) -> None:
        self.assertEqual(ContinuityError.error_code, CONTINUITY_VIOLATION_ERROR_CODE)


if __name__ == "__main__":
    unittest.main()
