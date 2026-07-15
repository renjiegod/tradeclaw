import unittest

from doyoutrade.data.bar_timestamp import normalize_bar_timestamp


class BarTimestampTests(unittest.TestCase):
    def test_compact_yyyymmdd_to_tradingagents_date(self):
        self.assertEqual(normalize_bar_timestamp("20260102"), "2026-01-02")

    def test_midnight_iso_to_date_only(self):
        self.assertEqual(normalize_bar_timestamp("2026-01-02T00:00:00"), "2026-01-02")

    def test_intraday_keeps_t_separator(self):
        self.assertEqual(
            normalize_bar_timestamp("2026-01-01T09:31:00"),
            "2026-01-01T09:31:00",
        )

    def test_z_suffix_utc_to_naive_wall(self):
        self.assertEqual(
            normalize_bar_timestamp("2026-01-01T08:30:00Z"),
            "2026-01-01T08:30:00",
        )

    def test_empty_returns_empty(self):
        self.assertEqual(normalize_bar_timestamp(""), "")
        self.assertEqual(normalize_bar_timestamp(None), "")


if __name__ == "__main__":
    unittest.main()
