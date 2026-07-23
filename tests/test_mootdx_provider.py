"""Offline unit tests for the mootdx data provider.

No network / no mootdx install required: the adjust engine is a pure function and
the provider takes an injected fake client. The adjust tests hand-compute the
expected qfq/hfq series so a future regression in the ratio formula is caught
(this is the cross-check baseline that would otherwise need baostock qfq).
"""

import asyncio
import unittest
from unittest.mock import patch

import pandas as pd

import doyoutrade.data.mootdx_provider as mp
from doyoutrade.data.mootdx_provider import (
    MootdxDataProvider,
    MootdxRealtimeQuoteProvider,
    _ex_ratio,
    _weekday_range,
    compute_adjusted_ohlc,
    symbol_to_tdx_code,
)


def _row(date, close, o=None, h=None, low=None, vol=1000.0, amount=None):
    return {
        "date": date,
        "timestamp": f"{date}T15:00:00",
        "open": o if o is not None else close,
        "high": h if h is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "volume": vol,
        "amount": amount,
    }


class SymbolAndCalendarTests(unittest.TestCase):
    def test_symbol_to_tdx_code(self):
        self.assertEqual(symbol_to_tdx_code("600036.SH"), "600036")
        self.assertEqual(symbol_to_tdx_code(" 000001.SZ "), "000001")
        self.assertEqual(symbol_to_tdx_code("430047.BJ"), "430047")
        self.assertEqual(symbol_to_tdx_code("600036"), "600036")
        self.assertIsNone(symbol_to_tdx_code("nope"))
        self.assertIsNone(symbol_to_tdx_code("60003.SH"))  # 5 digits

    def test_weekday_range_skips_weekends(self):
        # 2024-01-06/07 are Sat/Sun.
        out = _weekday_range("2024-01-05", "2024-01-08")
        self.assertEqual(out, ["2024-01-05", "2024-01-08"])


class ExRatioTests(unittest.TestCase):
    def test_cash_dividend_only(self):
        # per-10-share dividend 5, prev_close=10 -> (100-5)/100 = 0.95
        self.assertAlmostEqual(_ex_ratio(10.0, 5.0, 0.0, 0.0, 0.0), 0.95)

    def test_bonus_shares(self):
        # per-10-share bonus 10 shares -> 10*pc / (20*pc) = 0.5
        self.assertAlmostEqual(_ex_ratio(10.0, 0.0, 10.0, 0.0, 0.0), 0.5)

    def test_rights_issue(self):
        # per-10-share rights 3 @ 5, pc=10 -> (100 + 5*3)/((10+3)*10) = 115/130
        self.assertAlmostEqual(_ex_ratio(10.0, 0.0, 0.0, 3.0, 5.0), 115.0 / 130.0)

    def test_bad_prev_close_returns_none(self):
        self.assertIsNone(_ex_ratio(0.0, 5.0, 0.0, 0.0, 0.0))


class AdjustEngineTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            _row("2024-01-01", 10.0),
            _row("2024-01-02", 11.0),
            _row("2024-01-03", 12.0),
        ]
        # ex-date 2024-01-02, per-10-share dividend 5. prev_close = 10.0 -> ratio 0.95
        self.events = [
            {"ex_date": "2024-01-02", "fenhong": 5.0, "songzhuangu": 0.0, "peigu": 0.0, "peigujia": 0.0}
        ]

    def test_none_is_identity(self):
        out = compute_adjusted_ohlc(self.rows, self.events, "none")
        self.assertEqual([r["close"] for r in out], [10.0, 11.0, 12.0])

    def test_qfq_keeps_latest_scales_history(self):
        out = compute_adjusted_ohlc(self.rows, self.events, "qfq")
        closes = [r["close"] for r in out]
        # 2024-01-01 has a later ex-date -> *0.95 = 9.5; on/after ex-date unchanged.
        self.assertAlmostEqual(closes[0], 9.5)
        self.assertAlmostEqual(closes[1], 11.0)
        self.assertAlmostEqual(closes[2], 12.0)

    def test_qfq_scales_all_ohlc_fields(self):
        rows = [_row("2024-01-01", close=10.0, o=9.0, h=10.5, low=8.5)]
        events = [{"ex_date": "2024-01-02", "fenhong": 5.0, "songzhuangu": 0, "peigu": 0, "peigujia": 0}]
        out = compute_adjusted_ohlc(rows, events, "qfq")
        self.assertAlmostEqual(out[0]["open"], 9.0 * 0.95)
        self.assertAlmostEqual(out[0]["high"], 10.5 * 0.95)
        self.assertAlmostEqual(out[0]["low"], 8.5 * 0.95)
        self.assertAlmostEqual(out[0]["close"], 10.0 * 0.95)

    def test_hfq_keeps_earliest_scales_forward(self):
        out = compute_adjusted_ohlc(self.rows, self.events, "hfq")
        closes = [r["close"] for r in out]
        self.assertAlmostEqual(closes[0], 10.0)              # earliest unchanged
        self.assertAlmostEqual(closes[1], 11.0 / 0.95)       # on/after ex-date scaled up
        self.assertAlmostEqual(closes[2], 12.0 / 0.95)

    def test_volume_untouched_by_adjust(self):
        out = compute_adjusted_ohlc(self.rows, self.events, "qfq")
        self.assertEqual([r["volume"] for r in out], [1000.0, 1000.0, 1000.0])

    def test_event_before_all_bars_has_no_effect(self):
        events = [{"ex_date": "2020-01-01", "fenhong": 5.0, "songzhuangu": 0, "peigu": 0, "peigujia": 0}]
        out = compute_adjusted_ohlc(self.rows, events, "qfq")
        self.assertEqual([r["close"] for r in out], [10.0, 11.0, 12.0])

    def test_event_on_first_bar_is_skipped_not_misadjusted(self):
        # ex-date == earliest bar -> no prev-close anchor -> skipped (unadjusted),
        # never silently mis-scaled.
        events = [{"ex_date": "2024-01-01", "fenhong": 5.0, "songzhuangu": 0, "peigu": 0, "peigujia": 0}]
        with self.assertLogs(mp.logger, level="WARNING") as cm:
            out = compute_adjusted_ohlc(self.rows, events, "qfq")
        self.assertEqual([r["close"] for r in out], [10.0, 11.0, 12.0])
        self.assertTrue(any("cannot resolve prev-close" in m for m in cm.output))


class NormalizeDfTests(unittest.TestCase):
    def test_vol_lots_to_shares_and_sort(self):
        df = pd.DataFrame(
            [
                {"open": 11.0, "high": 11.2, "low": 10.8, "close": 11.1, "vol": 200.0, "amount": 2.2e6, "datetime": "2024-01-02 15:00"},
                {"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "vol": 100.0, "amount": 1.0e6, "datetime": "2024-01-01 15:00"},
            ]
        )
        rows = MootdxDataProvider._normalize_df(df)
        # ascending by date
        self.assertEqual([r["date"] for r in rows], ["2024-01-01", "2024-01-02"])
        # vol(lots) * 100 = shares
        self.assertEqual(rows[0]["volume"], 100.0 * 100)
        self.assertEqual(rows[1]["volume"], 200.0 * 100)

    def test_blank_core_row_dropped(self):
        df = pd.DataFrame(
            [
                {"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "vol": 100.0, "amount": 1.0e6, "datetime": "2024-01-01 15:00"},
                {"open": None, "high": None, "low": None, "close": None, "vol": 0.0, "amount": 0.0, "datetime": "2024-01-02 15:00"},
            ]
        )
        rows = MootdxDataProvider._normalize_df(df)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2024-01-01")


class _FakeClient:
    def __init__(self, bars_df, xdxr_df):
        self._bars = bars_df
        self._xdxr = xdxr_df
        self.bars_calls = []

    def bars(self, symbol, frequency, offset):
        self.bars_calls.append((symbol, frequency, offset))
        return self._bars

    def xdxr(self, symbol):
        return self._xdxr


class SyncGetBarsTests(unittest.TestCase):
    def _make(self):
        bars_df = pd.DataFrame(
            [
                {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "vol": 100.0, "amount": 1e6, "datetime": "2024-01-01 15:00"},
                {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "vol": 200.0, "amount": 2e6, "datetime": "2024-01-02 15:00"},
                {"open": 12.0, "high": 12.0, "low": 12.0, "close": 12.0, "vol": 300.0, "amount": 3e6, "datetime": "2024-01-03 15:00"},
            ]
        )
        xdxr_df = pd.DataFrame(
            [
                {"year": 2024, "month": 1, "day": 2, "category": 1, "fenhong": 5.0, "songzhuangu": 0.0, "peigu": 0.0, "peigujia": 0.0},
                # a non-price share-structure category that must be ignored
                {"year": 2024, "month": 1, "day": 2, "category": 5, "fenhong": 0.0, "songzhuangu": 0.0, "peigu": 0.0, "peigujia": 0.0},
            ]
        )
        return MootdxDataProvider(symbols=["600036.SH"], client=_FakeClient(bars_df, xdxr_df))

    def test_qfq_end_to_end_with_clip_and_units(self):
        p = self._make()
        bars = p._sync_get_bars("600036.SH", "2024-01-01", "2024-12-31", "1d", "qfq")
        self.assertEqual([b.timestamp[:10] for b in bars], ["2024-01-01", "2024-01-02", "2024-01-03"])
        # qfq: earliest scaled *0.95, latest unchanged
        self.assertAlmostEqual(bars[0].close, 9.5)
        self.assertAlmostEqual(bars[2].close, 12.0)
        # vol(lots)->shares
        self.assertEqual(bars[0].volume, 100.0 * 100)
        self.assertEqual(bars[0].adjust_type, "qfq")
        self.assertEqual(bars[0].symbol, "600036.SH")

    def test_none_adjust_skips_xdxr(self):
        p = self._make()
        bars = p._sync_get_bars("600036.SH", "2024-01-01", "2024-12-31", "1d", "none")
        self.assertAlmostEqual(bars[0].close, 10.0)  # unadjusted

    def test_date_clip(self):
        p = self._make()
        bars = p._sync_get_bars("600036.SH", "2024-01-02", "2024-01-02", "1d", "none")
        self.assertEqual([b.timestamp[:10] for b in bars], ["2024-01-02"])

    def test_unknown_symbol_returns_empty(self):
        p = self._make()
        self.assertEqual(p._sync_get_bars("bogus", "2024-01-01", "2024-12-31", "1d", "qfq"), [])

    def test_get_trading_dates_weekday(self):
        p = self._make()
        out = asyncio.run(p.get_trading_dates("2024-01-05", "2024-01-08"))
        self.assertEqual(out, ["2024-01-05", "2024-01-08"])


class FactoryWiringTests(unittest.TestCase):
    def test_mootdx_in_provider_list_and_builds(self):
        from doyoutrade.data import factory
        from doyoutrade.config import DataSettings

        self.assertIn("mootdx", factory.list_data_provider_ids())
        dp, universe, reader = factory.build_trading_data_stack(
            "mootdx", DataSettings(default_provider="auto"), ["600036.SH"]
        )
        self.assertIsInstance(dp, MootdxDataProvider)

    def test_mootdx_is_reserved_name(self):
        from doyoutrade.data import factory

        with self.assertRaises(ValueError):
            factory.register_trading_data_provider("mootdx", lambda cfg, syms: (None, None, None))


class StdClientBootstrapTests(unittest.TestCase):
    def test_candidate_std_servers_normalizes_and_dedupes(self):
        out = mp._candidate_std_servers(
            preferred=[("9.9.9.9", 7709)],
            bestip={"HQ": ["1.1.1.1", 7709]},
            configured={
                "HQ": [
                    ("named-dup", "1.1.1.1", 7709),
                    ("named-two", "2.2.2.2", "7710"),
                    ("broken", "", 7709),
                ]
            },
            builtin=[
                ("named-two-dup", "2.2.2.2", 7710),
                ("named-three", "3.3.3.3", 7720),
            ],
        )
        self.assertEqual(out, [("9.9.9.9", 7709), ("1.1.1.1", 7709), ("2.2.2.2", 7710), ("3.3.3.3", 7720)])

    def test_make_std_client_falls_back_to_explicit_servers(self):
        client_a = object()
        client_b = object()

        with (
            patch.object(mp, "_build_default_std_client", side_effect=ValueError("not enough values to unpack")),
            patch.object(
                mp,
                "_candidate_std_servers",
                return_value=[("1.1.1.1", 7709), ("2.2.2.2", 7709)],
            ),
            patch.object(mp, "_build_std_client_with_server", side_effect=[client_a, client_b]) as build_mock,
            patch.object(mp, "_probe_std_client", side_effect=[False, True]),
        ):
            client = mp._make_std_client()

        self.assertIs(client, client_b)
        self.assertEqual(
            [call.args[1] for call in build_mock.call_args_list],
            [("1.1.1.1", 7709), ("2.2.2.2", 7709)],
        )


class _FakeQuotesClient:
    def __init__(self, df, raises=None):
        self._df = df
        self._raises = raises
        self.calls = []

    def quotes(self, symbol):
        self.calls.append(list(symbol))
        if self._raises is not None:
            raise self._raises
        return self._df


class RealtimeQuoteTests(unittest.TestCase):
    def test_fetch_quotes_maps_fields_and_units(self):
        df = pd.DataFrame(
            [
                {"code": "600036", "price": 37.34, "last_close": 36.83, "open": 36.70,
                 "high": 37.48, "low": 36.52, "vol": 100.0, "amount": 2.0e9, "servertime": "15:00:00"},
            ]
        )
        p = MootdxRealtimeQuoteProvider(client=_FakeQuotesClient(df))
        out = asyncio.run(p.fetch_quotes(["600036.SH", "000001.SZ"]))
        q = out["600036.SH"]
        self.assertEqual(q.status, "ok")
        self.assertAlmostEqual(q.price, 37.34)
        self.assertAlmostEqual(q.prev_close, 36.83)
        self.assertAlmostEqual(q.change, 37.34 - 36.83)
        self.assertAlmostEqual(q.change_pct, (37.34 - 36.83) / 36.83 * 100.0)
        self.assertEqual(q.volume, 100.0 * 100)  # 手 -> 股
        # symbol the upstream did not return -> visible no_data placeholder
        self.assertEqual(out["000001.SZ"].status, "no_data")

    def test_fetch_failure_degrades_to_no_data(self):
        p = MootdxRealtimeQuoteProvider(client=_FakeQuotesClient(None, raises=RuntimeError("boom")))
        with self.assertLogs(mp.logger, level="WARNING"):
            out = asyncio.run(p.fetch_quotes(["600036.SH"]))
        self.assertEqual(out["600036.SH"].status, "no_data")

    def test_unknown_symbols_only(self):
        p = MootdxRealtimeQuoteProvider(client=_FakeQuotesClient(pd.DataFrame()))
        out = asyncio.run(p.fetch_quotes(["bogus"]))
        self.assertEqual(out["bogus"].status, "no_data")


if __name__ == "__main__":
    unittest.main()
