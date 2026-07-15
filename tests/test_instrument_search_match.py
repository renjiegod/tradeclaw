import unittest

from doyoutrade.data.instrument_catalog.search_match import (
    display_name_search_keys,
    is_pinyin_style_query,
    matches_instrument_query,
)


class InstrumentSearchMatchTests(unittest.TestCase):
    def test_display_name_search_keys(self):
        full, initials = display_name_search_keys("浦发银行")
        self.assertEqual(full, "pufayinhang")
        self.assertEqual(initials, "PFYH")

    def test_is_pinyin_style_query(self):
        self.assertTrue(is_pinyin_style_query("pufa"))
        self.assertTrue(is_pinyin_style_query("PFYH"))
        self.assertFalse(is_pinyin_style_query("600519"))
        self.assertFalse(is_pinyin_style_query("浦发"))
        self.assertFalse(is_pinyin_style_query(""))

    def test_matches_name_substring(self):
        self.assertTrue(
            matches_instrument_query(
                "浦发",
                symbol="600000.SH",
                display_name="浦发银行",
            )
        )

    def test_matches_symbol_prefix(self):
        self.assertTrue(
            matches_instrument_query(
                "600000",
                symbol="600000.SH",
                display_name="浦发银行",
            )
        )

    def test_matches_pinyin_substring(self):
        self.assertTrue(
            matches_instrument_query(
                "pufa",
                symbol="600000.SH",
                display_name="浦发银行",
            )
        )

    def test_matches_initials_substring(self):
        self.assertTrue(
            matches_instrument_query(
                "pfyh",
                symbol="600000.SH",
                display_name="浦发银行",
            )
        )
        self.assertTrue(
            matches_instrument_query(
                "mt",
                symbol="600519.SH",
                display_name="贵州茅台",
            )
        )


if __name__ == "__main__":
    unittest.main()
