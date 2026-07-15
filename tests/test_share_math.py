import math
import unittest

from doyoutrade.core.share_math import (
    floor_fraction_shares,
    floor_partial_sell_shares,
    floor_to_lot,
    floor_whole_share_count,
    max_whole_shares_affordable,
)


class ShareMathTests(unittest.TestCase):
    def test_floor_whole_share_count_nextbelow_integer(self):
        x = math.nextafter(3356.0, 0.0)
        self.assertLess(x, 3356.0)
        self.assertEqual(floor_whole_share_count(x), 3356)

    def test_floor_whole_share_count_rejects_negative(self):
        self.assertEqual(floor_whole_share_count(-1.0), 0)

    def test_max_whole_shares_affordable_matches_floor_when_clean(self):
        # Aligns with math.floor(100_000/11.11) == 9000; 9000*11.11 = 99_990 <= cap.
        self.assertEqual(max_whole_shares_affordable(100_000.0, 11.11), 9000)

    def test_floor_partial_sell_shares_partial_cap(self):
        # T binds: same as test_sell_partial_when_t_smaller_than_sellable (T=20000, ref=50, qty=1000 -> 400)
        self.assertEqual(floor_partial_sell_shares(1000.0, 20_000.0, 50.0), 400)

    def test_floor_partial_sell_qty_binds(self):
        self.assertEqual(floor_partial_sell_shares(50.0, 1_000_000.0, 10.0), 50)

    def test_floor_fraction_shares_full_is_identity(self):
        # fraction 1.0 must return qty unchanged (byte-identical full exit).
        self.assertEqual(floor_fraction_shares(100, 1.0), 100)
        self.assertEqual(floor_fraction_shares(137, 1.0), 137)

    def test_floor_fraction_shares_scales_and_floors(self):
        self.assertEqual(floor_fraction_shares(100, 0.5), 50)
        self.assertEqual(floor_fraction_shares(101, 0.5), 50)  # floor(50.5)
        self.assertEqual(floor_fraction_shares(2, 0.4), 0)     # floor(0.8) → zero-share

    def test_floor_fraction_shares_guards(self):
        self.assertEqual(floor_fraction_shares(0, 0.5), 0)
        self.assertEqual(floor_fraction_shares(100, 0.0), 0)

    def test_floor_to_lot_aligns_down(self):
        self.assertEqual(floor_to_lot(137, 100), 100)
        self.assertEqual(floor_to_lot(250, 100), 200)
        self.assertEqual(floor_to_lot(300, 100), 300)

    def test_floor_to_lot_below_one_lot_is_zero(self):
        # A positive count below one lot floors to 0 — callers surface a skip.
        self.assertEqual(floor_to_lot(99, 100), 0)

    def test_floor_to_lot_size_one_is_identity(self):
        # lot_size 1 (and 0) = whole-share trading, unchanged.
        self.assertEqual(floor_to_lot(137, 1), 137)
        self.assertEqual(floor_to_lot(137, 0), 137)

    def test_floor_to_lot_non_positive(self):
        self.assertEqual(floor_to_lot(0, 100), 0)
        self.assertEqual(floor_to_lot(-50, 100), 0)


if __name__ == "__main__":
    unittest.main()
