import unittest

from doyoutrade.data.instrument_universe.akshare_a import (
    clear_akshare_a_spot_cache,
    filter_akshare_rows,
    normalize_ak_a_symbol,
)


class TestNormalizeAkASymbol(unittest.TestCase):
    def test_shanghai_main(self):
        self.assertEqual(normalize_ak_a_symbol("600000"), "600000.SH")

    def test_star_board(self):
        self.assertEqual(normalize_ak_a_symbol("688981"), "688981.SH")

    def test_shenzhen(self):
        self.assertEqual(normalize_ak_a_symbol("000001"), "000001.SZ")
        self.assertEqual(normalize_ak_a_symbol("300750"), "300750.SZ")

    def test_beijing(self):
        self.assertEqual(normalize_ak_a_symbol("430047"), "430047.BJ")

    def test_passthrough_with_suffix(self):
        self.assertEqual(normalize_ak_a_symbol("600000.sh"), "600000.SH")


class TestFilterAkshareRows(unittest.TestCase):
    def setUp(self):
        clear_akshare_a_spot_cache()

    def test_matches_name_substring(self):
        rows = [
            {"symbol": "600000.SH", "name": "浦发银行", "market": "CN"},
            {"symbol": "000001.SZ", "name": "平安银行", "market": "CN"},
        ]
        out = filter_akshare_rows(rows, "浦发", 10)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["symbol"], "600000.SH")

    def test_matches_code_prefix(self):
        rows = [{"symbol": "600000.SH", "name": "浦发银行", "market": "CN"}]
        out = filter_akshare_rows(rows, "6000", 10)
        self.assertEqual(len(out), 1)

    def test_empty_query_returns_empty(self):
        rows = [{"symbol": "600000.SH", "name": "浦发银行", "market": "CN"}]
        self.assertEqual(filter_akshare_rows(rows, "", 10), [])
        self.assertEqual(filter_akshare_rows(rows, "   ", 10), [])

    def test_respects_limit(self):
        rows = [
            {"symbol": "600000.SH", "name": "浦发银行", "market": "CN"},
            {"symbol": "600004.SH", "name": "白云机场", "market": "CN"},
        ]
        out = filter_akshare_rows(rows, "600", 1)
        self.assertEqual(len(out), 1)

    def test_matches_pinyin_and_initials(self):
        rows = [
            {"symbol": "600000.SH", "name": "浦发银行", "market": "CN"},
            {"symbol": "600519.SH", "name": "贵州茅台", "market": "CN"},
        ]
        self.assertEqual(filter_akshare_rows(rows, "pufa", 10)[0]["symbol"], "600000.SH")
        self.assertEqual(filter_akshare_rows(rows, "pfyh", 10)[0]["symbol"], "600000.SH")
        self.assertEqual(filter_akshare_rows(rows, "mt", 10)[0]["symbol"], "600519.SH")
