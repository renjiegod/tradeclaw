import unittest
from unittest.mock import MagicMock, patch

import doyoutrade.data.baostock_provider as bp


class _FakeTradeDateResult:
    error_code = "0"

    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows
        self._i = -1

    def next(self) -> bool:
        self._i += 1
        return self._i < len(self._rows)

    def get_row_data(self) -> list[str]:
        return self._rows[self._i]


class _FakeKDataResult:
    def __init__(self, rows: list[list[str]], error_code: str = "0", error_msg: str = "success") -> None:
        self._rows = rows
        self._i = -1
        self.error_code = error_code
        self.error_msg = error_msg

    def next(self) -> bool:
        self._i += 1
        return self._i < len(self._rows)

    def get_row_data(self) -> list[str]:
        return self._rows[self._i]


class BaostockProviderUnitTests(unittest.TestCase):
    def test_symbol_to_baostock_maps_sh_sz(self) -> None:
        self.assertEqual(bp.symbol_to_baostock("600000.SH"), "sh.600000")
        self.assertEqual(bp.symbol_to_baostock(" 000001.SZ "), "sz.000001")
        self.assertIsNone(bp.symbol_to_baostock("430047.BJ"))
        self.assertIsNone(bp.symbol_to_baostock("nope"))

    def tearDown(self) -> None:
        bp._bs_logged_in = False

    def test_sync_get_trading_dates_filters_open_days(self) -> None:
        prev = bp._bs_logged_in
        bp._bs_logged_in = True
        try:
            fake = _FakeTradeDateResult(
                [
                    ["2024-01-02", "1"],
                    ["2024-01-03", "0"],
                    ["2024-01-04", "1"],
                ]
            )
            with patch.object(bp.bs, "query_trade_dates", return_value=fake):
                p = bp.BaostockDataProvider(symbols=["600000.SH"])
                out = p._sync_get_trading_dates("2024-01-01", "2024-01-31")
            self.assertEqual(out, ["2024-01-02", "2024-01-04"])
        finally:
            bp._bs_logged_in = prev

    def test_sync_is_trading_day(self) -> None:
        prev = bp._bs_logged_in
        bp._bs_logged_in = True
        try:
            fake = _FakeTradeDateResult([["2024-02-10", "0"]])
            with patch.object(bp.bs, "query_trade_dates", return_value=fake):
                p = bp.BaostockDataProvider(symbols=["600000.SH"])
                self.assertFalse(p._sync_is_trading_day("2024-02-10"))
        finally:
            bp._bs_logged_in = prev

    def test_to_baostock_date_normalizes_formats(self) -> None:
        self.assertEqual(bp._to_baostock_date("2024-12-02"), "2024-12-02")
        self.assertEqual(bp._to_baostock_date("20241202"), "2024-12-02")
        self.assertEqual(bp._to_baostock_date("2024-12-02T09:35:00"), "2024-12-02")

    def test_freq_for_interval_maps_weekly_monthly_and_minutes(self) -> None:
        self.assertEqual(bp._freq_for_interval("1d"), "d")
        self.assertEqual(bp._freq_for_interval("1w"), "w")
        self.assertEqual(bp._freq_for_interval("1mo"), "m")
        self.assertEqual(bp._freq_for_interval("5m"), "5")

    def test_freq_for_interval_rejects_unsupported_1m(self) -> None:
        # baostock's smallest aggregate is 5 minutes; 1m must not silently
        # downgrade to 5m (regression for the old "1m" -> "5" remap).
        with self.assertRaises(ValueError):
            bp._freq_for_interval("1m")

    def test_sync_get_bars_passes_dashed_dates(self) -> None:
        # Regression: dates must reach baostock as YYYY-MM-DD, not YYYYMMDD
        # (the compact form makes baostock return None -> "日期格式不正确").
        bp._bs_logged_in = True
        fake = _FakeKDataResult([["2024-12-02", "10.0", "11.0", "9.0", "10.5", "100", "1000.0"]])
        mock_q = MagicMock(return_value=fake)
        with patch.object(bp.bs, "query_history_k_data_plus", mock_q):
            bars, suspended = bp.BaostockHistoricalProvider()._sync_get_bars(
                "600519.SH", "20241202", "20241206", "1d", "qfq"
            )
        self.assertEqual(mock_q.call_args.kwargs["start_date"], "2024-12-02")
        self.assertEqual(mock_q.call_args.kwargs["end_date"], "2024-12-06")
        self.assertEqual(mock_q.call_args.kwargs["frequency"], "d")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, 10.5)
        self.assertEqual(suspended, set())

    def test_sync_get_bars_maps_adjust_flags_correctly(self) -> None:
        bp._bs_logged_in = True
        fake = _FakeKDataResult([["2024-12-02", "10.0", "11.0", "9.0", "10.5", "100", "1000.0"]])
        mock_q = MagicMock(return_value=fake)
        with patch.object(bp.bs, "query_history_k_data_plus", mock_q):
            provider = bp.BaostockHistoricalProvider()
            provider._sync_get_bars("600519.SH", "2024-12-02", "2024-12-06", "1d", "qfq")
            self.assertEqual(mock_q.call_args.kwargs["adjustflag"], "2")
            provider._sync_get_bars("600519.SH", "2024-12-02", "2024-12-06", "1d", "hfq")
            self.assertEqual(mock_q.call_args.kwargs["adjustflag"], "1")
            provider._sync_get_bars("600519.SH", "2024-12-02", "2024-12-06", "1d", "none")
            self.assertEqual(mock_q.call_args.kwargs["adjustflag"], "3")

    def test_sync_get_bars_raises_on_none_result(self) -> None:
        # Regression: a None result (baostock's reply to a bad request) must
        # surface a descriptive RuntimeError, not the misleading
        # "'NoneType' object has no attribute 'error_code'".
        bp._bs_logged_in = True
        with patch.object(bp.bs, "query_history_k_data_plus", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                bp.BaostockHistoricalProvider()._sync_get_bars(
                    "600519.SH", "2024-12-02", "2024-12-06", "5m", "qfq"
                )
        self.assertIn("YYYY-MM-DD", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, AttributeError)

    def test_sync_get_bars_skips_suspended_days(self) -> None:
        # 停牌日：tradestatus=0，量额为空串，OHLC 用前收填充。必须跳过（不填 0 造假 bar），
        # 且不能让 float('') 崩掉整次抓取。保留正常交易日。
        bp._bs_logged_in = True
        fake = _FakeKDataResult(
            [
                ["2025-11-17", "10.0", "11.0", "9.0", "10.5", "100", "1000.0", "1"],
                ["2025-11-18", "10.5", "10.5", "10.5", "10.5", "", "", "0"],
                ["2025-11-19", "10.5", "10.5", "10.5", "10.5", "", "", "0"],
                ["2025-11-20", "10.6", "11.2", "10.4", "11.0", "200", "2200.0", "1"],
            ]
        )
        with patch.object(bp.bs, "query_history_k_data_plus", return_value=fake):
            bars, suspended = bp.BaostockHistoricalProvider()._sync_get_bars(
                "603122.SH", "2025-11-17", "2025-11-20", "1d", "qfq"
            )
        self.assertEqual(len(bars), 2)
        self.assertEqual([b.timestamp[:10] for b in bars], ["2025-11-17", "2025-11-20"])
        self.assertEqual(bars[1].close, 11.0)
        # The two tradestatus=0 days are surfaced as suspensions (not dropped)
        # so the write-time continuity check can exclude them from the calendar.
        self.assertEqual(suspended, {"2025-11-18", "2025-11-19"})

    def test_sync_get_bars_raises_on_blank_ohlc_for_trading_day(self) -> None:
        # 交易日（tradestatus=1）核心价格为空 = 数据损坏，必须暴露而不是静默跳过。
        bp._bs_logged_in = True
        fake = _FakeKDataResult(
            [["2025-11-17", "", "11.0", "9.0", "10.5", "100", "1000.0", "1"]]
        )
        with patch.object(bp.bs, "query_history_k_data_plus", return_value=fake):
            with self.assertRaises(ValueError) as ctx:
                bp.BaostockHistoricalProvider()._sync_get_bars(
                    "603122.SH", "2025-11-17", "2025-11-17", "1d", "qfq"
                )
        self.assertIn("blank OHLC", str(ctx.exception))

    def test_sync_get_bars_surfaces_backend_error_code(self) -> None:
        # A non-"0" error_code is a real failure and must be raised (visible),
        # not swallowed into an empty list.
        bp._bs_logged_in = True
        fake = _FakeKDataResult([], error_code="10001", error_msg="boom")
        with patch.object(bp.bs, "query_history_k_data_plus", return_value=fake):
            with self.assertRaises(RuntimeError) as ctx:
                bp.BaostockHistoricalProvider()._sync_get_bars(
                    "600519.SH", "2024-12-02", "2024-12-06", "1d", "qfq"
                )
        self.assertIn("10001", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))


class BaostockIndexMinuteCapabilityTests(unittest.TestCase):
    """baostock has no minute-bar history for 指数 — capabilities must say so.

    Regression coverage for the "指数 + 60m → not enough values to unpack"
    failure mode: baostock's minute endpoint only covers 股票/ETF, so an
    index + minute-interval request must be rejected by
    ``supports_interval_for_symbol`` before it ever reaches the upstream SDK.
    """

    def test_index_symbol_rejects_minute_intervals(self) -> None:
        from doyoutrade.data.protocols import supports_interval_for_symbol

        caps = bp.BaostockDataProvider.capabilities
        for interval in ("5m", "15m", "30m", "60m"):
            self.assertFalse(
                supports_interval_for_symbol(caps, interval, "000001.SH"),
                f"baostock should reject {interval} for 上证指数 000001.SH",
            )
        # 399001.SZ (深证成指) is also an index — same carve-out applies.
        self.assertFalse(supports_interval_for_symbol(caps, "60m", "399001.SZ"))

    def test_stock_symbol_keeps_minute_support(self) -> None:
        from doyoutrade.data.protocols import supports_interval_for_symbol

        caps = bp.BaostockDataProvider.capabilities
        for interval in ("5m", "15m", "30m", "60m"):
            self.assertTrue(
                supports_interval_for_symbol(caps, interval, "600519.SH"),
                f"baostock should still serve {interval} for a real stock",
            )

    def test_daily_interval_unaffected_for_index(self) -> None:
        from doyoutrade.data.protocols import supports_interval_for_symbol

        caps = bp.BaostockDataProvider.capabilities
        self.assertTrue(supports_interval_for_symbol(caps, "1d", "000001.SH"))

class BaostockIndexMinuteGetBarsGuardTests(unittest.IsolatedAsyncioTestCase):
    """get_bars must refuse index + minute before any baostock SDK call."""

    async def test_get_bars_rejects_index_minute_before_sdk_call(self) -> None:
        from doyoutrade.data.protocols import ProviderIntervalUnsupportedError

        provider = bp.BaostockDataProvider(symbols=["000001.SH"])
        with patch.object(bp.bs, "query_history_k_data_plus") as mock_q:
            with self.assertRaises(ProviderIntervalUnsupportedError) as ctx:
                await provider.get_bars(
                    "000001.SH", "2026-06-25", "2026-07-25", interval="60m"
                )
        mock_q.assert_not_called()
        self.assertIn("000001.SH", str(ctx.exception))
        self.assertIn("60m", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
