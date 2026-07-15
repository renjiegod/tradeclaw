import unittest
from datetime import date

from doyoutrade.data.coverage_ranges import (
    consecutive_trading_day_ranges,
    merge_cached_day_ranges,
    merge_date_ranges,
    trading_day_adjacent,
)


class CoverageRangesTests(unittest.TestCase):
    def test_trading_day_adjacent_merges_across_weekend(self) -> None:
        friday = date(2026, 1, 2)
        monday = date(2026, 1, 5)
        self.assertTrue(trading_day_adjacent(friday, monday))

    def test_trading_day_adjacent_splits_on_weekday_gap(self) -> None:
        thursday = date(2026, 1, 1)
        tuesday = date(2026, 1, 6)
        self.assertFalse(trading_day_adjacent(thursday, tuesday))

    def test_consecutive_trading_day_ranges_collapses_weekly_bars(self) -> None:
        days = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
        self.assertEqual(
            consecutive_trading_day_ranges(days),
            [(date(2026, 1, 2), date(2026, 1, 6))],
        )

    def test_merge_date_ranges_merges_weekend_split_ranges(self) -> None:
        self.assertEqual(
            merge_date_ranges(
                [
                    (date(2026, 1, 2), date(2026, 1, 2)),
                    (date(2026, 1, 5), date(2026, 1, 9)),
                ]
            ),
            [(date(2026, 1, 2), date(2026, 1, 9))],
        )

    def test_merge_cached_day_ranges_uses_trading_day_adjacency(self) -> None:
        self.assertEqual(
            merge_cached_day_ranges(
                [
                    ("2026-01-02", "2026-01-02"),
                    ("2026-01-05", "2026-01-09"),
                ]
            ),
            [("2026-01-02", "2026-01-09")],
        )


if __name__ == "__main__":
    unittest.main()
