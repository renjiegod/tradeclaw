"""Tests for AkshareRealtimeProvider's em -> sina -> tencent quote cascade.

``stock_zh_a_spot_em()`` is a full-market snapshot that can fail independently
of the single-symbol sina/tencent HTTP endpoints (observed in practice: em's
upstream reset the connection while tencent stayed reachable). These tests
verify: (1) the em snapshot answers every requested symbol in one call
(the batch fix — looping ``fetch_latest_price`` used to trigger one
full-market scan per symbol), (2) symbols em couldn't answer fall through to
sina then tencent, (3) a symbol left unanswered by all three sources comes
back as missing rather than silently defaulting to a fake price, and (4)
北交所 symbols (unsupported by sina/tencent) skip the HTTP cascade instead of
issuing dead requests.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
import pandas as pd

from doyoutrade.data.akshare_provider import (
    AkshareDataProvider,
    AkshareRealtimeProvider,
    AkshareRealtimeQuoteProvider,
    _to_market_prefixed_symbol,
)

_EM_DF = pd.DataFrame(
    [
        {"代码": "000636", "名称": "风华高科", "最新价": 59.41},
        {"代码": "600519", "名称": "贵州茅台", "最新价": 1866.0},
        {"代码": "000002", "名称": "万科A", "最新价": float("nan")},
    ]
)

_EM_DF_FULL = pd.DataFrame(
    [
        {
            "代码": "000636",
            "名称": "风华高科",
            "最新价": 59.41,
            "涨跌幅": 2.5,
            "涨跌额": 1.45,
            "成交量": 12345.0,
            "成交额": 987654.0,
            "最高": 60.0,
            "最低": 57.0,
            "今开": 58.0,
            "昨收": 57.96,
        },
        {
            "代码": "000002",
            "名称": "万科A",
            "最新价": float("nan"),
            "涨跌幅": float("nan"),
            "涨跌额": float("nan"),
            "成交量": float("nan"),
            "成交额": float("nan"),
            "最高": float("nan"),
            "最低": float("nan"),
            "今开": float("nan"),
            "昨收": float("nan"),
        },
    ]
)


def _mock_client(handler):
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return factory


def _gbk_response(status: int, text: str) -> httpx.Response:
    """Build a response with real GBK bytes — sina/tencent both serve GBK.

    ``httpx.Response(text=...)`` encodes as UTF-8; naively reassigning
    ``resp.encoding = "gbk"`` on that would mis-decode the UTF-8 bytes as GBK
    (byte-misaligned garbage), which is a MockTransport test artifact rather
    than anything the real GBK-speaking upstream would produce.
    """
    return httpx.Response(status, content=text.encode("gbk"))


class ToMarketPrefixedSymbolTests(unittest.TestCase):
    def test_sh_and_sz_prefixed(self) -> None:
        self.assertEqual(_to_market_prefixed_symbol("600519.SH"), "sh600519")
        self.assertEqual(_to_market_prefixed_symbol("000636.SZ"), "sz000636")

    def test_bj_unsupported(self) -> None:
        self.assertIsNone(_to_market_prefixed_symbol("430047.BJ"))


class FetchQuotesEmSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_em_call_answers_multiple_symbols(self) -> None:
        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_spot_em",
            return_value=_EM_DF.copy(),
        ) as em_mock:
            provider = AkshareRealtimeProvider()
            quotes = await provider.fetch_quotes(["000636.SZ", "600519.SH"])

        em_mock.assert_called_once()
        self.assertEqual(quotes, {"000636.SZ": 59.41, "600519.SH": 1866.0})

    async def test_nan_price_excluded_from_em_result(self) -> None:
        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_spot_em",
            return_value=_EM_DF.copy(),
        ):
            provider = AkshareRealtimeProvider()
            em_prices, em_error = provider._sync_fetch_em_snapshot(["000002.SZ"])

        # NaN must not surface as a "successful" em price (would otherwise
        # short-circuit the sina/tencent fallback with a garbage value).
        self.assertIsNone(em_error)
        self.assertNotIn("000002.SZ", em_prices)


class FetchQuotesCascadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_em_failure_falls_through_to_sina(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "hq.sinajs.cn" in str(request.url):
                return _gbk_response(
                    200,
                    'var hq_str_sz000636="风华高科,52.12,54.01,59.41,59.41,50.23,0,0,0,0";\n',
                )
            raise AssertionError(f"tencent should not be called: {request.url}")

        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_spot_em",
            side_effect=httpx.ConnectError("em down"),
        ), patch(
            "doyoutrade.data.akshare_provider._make_realtime_http_client",
            _mock_client(handler),
        ):
            provider = AkshareRealtimeProvider()
            quotes = await provider.fetch_quotes(["000636.SZ"])

        self.assertEqual(quotes, {"000636.SZ": 59.41})

    async def test_sina_empty_falls_through_to_tencent(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "hq.sinajs.cn" in str(request.url):
                return _gbk_response(200, 'var hq_str_sz000636="";\n')
            if "qt.gtimg.cn" in str(request.url):
                return _gbk_response(
                    200,
                    'v_sz000636="51~风华高科~000636~59.41~54.01~52.12~1330266~745314~584952";\n',
                )
            raise AssertionError(f"unexpected host: {request.url}")

        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_spot_em",
            side_effect=RuntimeError("em down"),
        ), patch(
            "doyoutrade.data.akshare_provider._make_realtime_http_client",
            _mock_client(handler),
        ):
            provider = AkshareRealtimeProvider()
            price = await provider.fetch_latest_price("000636.SZ")

        self.assertEqual(price, 59.41)

    async def test_all_sources_exhausted_symbol_is_missing_not_faked(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _gbk_response(200, 'var hq_str_sz000636="";\n')

        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_spot_em",
            side_effect=RuntimeError("em down"),
        ), patch(
            "doyoutrade.data.akshare_provider._make_realtime_http_client",
            _mock_client(handler),
        ):
            provider = AkshareRealtimeProvider()
            quotes = await provider.fetch_quotes(["000636.SZ"])
            price = await provider.fetch_latest_price("000636.SZ")

        self.assertEqual(quotes, {})
        self.assertIsNone(price)

    async def test_bj_symbol_skips_http_cascade(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"BJ symbol must not hit sina/tencent: {request.url}")

        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_spot_em",
            side_effect=RuntimeError("em down"),
        ), patch(
            "doyoutrade.data.akshare_provider._make_realtime_http_client",
            _mock_client(handler),
        ):
            provider = AkshareRealtimeProvider()
            quotes = await provider.fetch_quotes(["430047.BJ"])

        self.assertEqual(quotes, {})


class GetMarketContextBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_market_context_makes_a_single_em_call_for_many_symbols(self) -> None:
        symbols = ["000636.SZ", "600519.SH", "000002.SZ"]

        def handler(request: httpx.Request) -> httpx.Response:
            # 000002.SZ's em price is NaN (excluded), so it cascades here;
            # simulate sina/tencent also having nothing for it.
            return _gbk_response(200, 'var hq_str_sz000002="";\n')

        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_spot_em",
            return_value=_EM_DF.copy(),
        ) as em_mock, patch(
            "doyoutrade.data.akshare_provider._make_realtime_http_client",
            _mock_client(handler),
        ):
            provider = AkshareDataProvider(symbols)
            ctx = await provider.get_market_context()

        em_mock.assert_called_once()
        self.assertEqual(ctx.symbol_to_price["000636.SZ"], 59.41)
        self.assertEqual(ctx.symbol_to_price["600519.SH"], 1866.0)
        # NaN in the em snapshot, then unanswered by sina/tencent too, degrades
        # to the documented 0.0 sentinel rather than silently propagating NaN.
        self.assertEqual(ctx.symbol_to_price["000002.SZ"], 0.0)


class AkshareRealtimeQuoteProviderTests(unittest.IsolatedAsyncioTestCase):
    """Tests for AkshareRealtimeQuoteProvider — the full-QuoteSnapshot sibling
    of AkshareRealtimeProvider, used as a non-QMT watchlist realtime source.
    """

    async def test_em_snapshot_builds_full_quote_snapshot(self) -> None:
        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_spot_em",
            return_value=_EM_DF_FULL.copy(),
        ):
            provider = AkshareRealtimeQuoteProvider()
            quotes = await provider.fetch_quotes(["000636.SZ"])

        q = quotes["000636.SZ"]
        self.assertEqual(q.status, "ok")
        self.assertAlmostEqual(q.price, 59.41)
        self.assertAlmostEqual(q.prev_close, 57.96)
        self.assertAlmostEqual(q.change, 1.45)
        self.assertAlmostEqual(q.change_pct, 2.5)
        self.assertAlmostEqual(q.open, 58.0)
        self.assertAlmostEqual(q.high, 60.0)
        self.assertAlmostEqual(q.low, 57.0)
        self.assertAlmostEqual(q.volume, 1234500.0)  # 手 -> 股
        self.assertAlmostEqual(q.amount, 987654.0)

    async def test_nan_em_row_falls_through_to_sina(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "hq.sinajs.cn" in str(request.url):
                return _gbk_response(
                    200,
                    'var hq_str_sz000002="万科A,7.10,7.05,7.20,7.25,7.00,0,0,0,0";\n',
                )
            raise AssertionError(f"tencent should not be called: {request.url}")

        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_spot_em",
            return_value=_EM_DF_FULL.copy(),
        ), patch(
            "doyoutrade.data.akshare_provider._make_realtime_http_client",
            _mock_client(handler),
        ):
            provider = AkshareRealtimeQuoteProvider()
            quotes = await provider.fetch_quotes(["000002.SZ"])

        q = quotes["000002.SZ"]
        self.assertEqual(q.status, "ok")
        self.assertAlmostEqual(q.price, 7.20)
        self.assertAlmostEqual(q.prev_close, 7.05)
        self.assertAlmostEqual(q.open, 7.10)
        self.assertAlmostEqual(q.high, 7.25)
        self.assertAlmostEqual(q.low, 7.00)
        # sina has no volume/amount in this cascade — must not be fabricated.
        self.assertIsNone(q.volume)
        self.assertIsNone(q.amount)

    async def test_sina_empty_falls_through_to_tencent(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "hq.sinajs.cn" in str(request.url):
                return _gbk_response(200, 'var hq_str_sz000636="";\n')
            if "qt.gtimg.cn" in str(request.url):
                return _gbk_response(
                    200,
                    'v_sz000636="51~风华高科~000636~59.41~57.96~58.0~1330266~745314~584952";\n',
                )
            raise AssertionError(f"unexpected host: {request.url}")

        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_spot_em",
            side_effect=RuntimeError("em down"),
        ), patch(
            "doyoutrade.data.akshare_provider._make_realtime_http_client",
            _mock_client(handler),
        ):
            provider = AkshareRealtimeQuoteProvider()
            quotes = await provider.fetch_quotes(["000636.SZ"])

        q = quotes["000636.SZ"]
        self.assertEqual(q.status, "ok")
        self.assertAlmostEqual(q.price, 59.41)
        self.assertAlmostEqual(q.prev_close, 57.96)

    async def test_all_sources_exhausted_returns_no_data_placeholder(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _gbk_response(200, 'var hq_str_sz000636="";\n')

        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_spot_em",
            side_effect=RuntimeError("em down"),
        ), patch(
            "doyoutrade.data.akshare_provider._make_realtime_http_client",
            _mock_client(handler),
        ):
            provider = AkshareRealtimeQuoteProvider()
            quotes = await provider.fetch_quotes(["000636.SZ"])

        self.assertEqual(quotes["000636.SZ"].status, "no_data")

    async def test_bj_symbol_skips_http_cascade_stays_no_data(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"BJ symbol must not hit sina/tencent: {request.url}")

        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_spot_em",
            side_effect=RuntimeError("em down"),
        ), patch(
            "doyoutrade.data.akshare_provider._make_realtime_http_client",
            _mock_client(handler),
        ):
            provider = AkshareRealtimeQuoteProvider()
            quotes = await provider.fetch_quotes(["430047.BJ"])

        self.assertEqual(quotes["430047.BJ"].status, "no_data")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
