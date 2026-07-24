import asyncio
import unittest

from doyoutrade.account import QmtAccountReader, StoreBackedAccountReader
from doyoutrade.config import DataSettings, TushareSettings
from doyoutrade.data.account_resolution import QmtMockPositionSettings, ResolvedAccount
from doyoutrade.data.factory import (
    build_trading_data_stack,
    list_data_provider_ids,
    normalize_provider_id,
    register_trading_data_provider,
    resolve_effective_provider,
)
from doyoutrade.data.akshare_provider import AkshareDataProvider
from doyoutrade.data.baostock_provider import BaostockDataProvider
from doyoutrade.data.fallback_provider import FallbackHistoricalDataProvider
from doyoutrade.data.mock_provider import MockTradingDataProvider, StaticUniverseProvider
from doyoutrade.data.mootdx_provider import MootdxDataProvider
from doyoutrade.data.qmt_proxy import QmtLiveDataProvider
from doyoutrade.data.tushare_provider import TushareDataProvider


def _settings(*, default_provider="auto") -> DataSettings:
    return DataSettings(default_provider=default_provider)


def _account(*, base_url="http://127.0.0.1:9", mode="live", qmt_account_id="10000001") -> ResolvedAccount:
    """A resolved account fixture (replaces the old config.data.qmt block)."""
    return ResolvedAccount(
        account_id="acct-test000001",
        name="test",
        mode=mode,
        base_url=base_url,
        token=None,
        timeout_seconds=5.0,
        qmt_account_id=qmt_account_id,
        session_id=None,
        mock_cash=100_000.0,
        mock_equity=100_000.0,
        mock_positions=(QmtMockPositionSettings(symbol="600000.SH", quantity=0.0, cost_price=0.0),),
    )


class DataFactoryTests(unittest.TestCase):
    def test_auto_chain_without_qmt_starts_with_baostock(self):
        """Without a connected account, auto skips QMT: baostock → mootdx → akshare."""
        dp, up, ar = build_trading_data_stack("auto", _settings(), ["600000.SH"], account=None)
        self.assertIsInstance(dp, FallbackHistoricalDataProvider)
        # First non-auth provider after the QMT skip is baostock, then mootdx
        # (socket minute/intraday + qfq), then akshare.
        self.assertIsInstance(dp.providers[0], BaostockDataProvider)
        self.assertIsInstance(dp.providers[1], MootdxDataProvider)
        self.assertIsInstance(dp.providers[2], AkshareDataProvider)
        self.assertIsInstance(up, StaticUniverseProvider)
        self.assertIsInstance(ar, StoreBackedAccountReader)

    def test_auto_chain_with_qmt_starts_with_qmt(self):
        """QMT is the auto-chain primary when the account supplies a base_url."""
        dp, up, ar = build_trading_data_stack(
            "auto", _settings(), ["600000.SH"], account=_account()
        )
        self.assertIsInstance(dp, FallbackHistoricalDataProvider)
        self.assertIsInstance(dp.providers[0], QmtLiveDataProvider)
        provider_types = [type(p).__name__ for p in dp.providers]
        self.assertNotIn("TushareDataProvider", provider_types)
        self.assertEqual(provider_types[1], "BaostockDataProvider")
        self.assertEqual(provider_types[2], "MootdxDataProvider")
        self.assertEqual(provider_types[3], "AkshareDataProvider")
        self.assertIsInstance(up, StaticUniverseProvider)
        self.assertIsNotNone(ar)

    def test_mock_ignores_account(self):
        dp, _, ar = build_trading_data_stack("mock", _settings(), ["600000.SH"], account=_account())
        self.assertIsInstance(dp, MockTradingDataProvider)
        self.assertIsInstance(ar, StoreBackedAccountReader)

    def test_qmt_requires_account_connection(self):
        with self.assertRaises(ValueError):
            build_trading_data_stack("qmt", _settings(), ["600000.SH"], account=None)

    def test_qmt_mock_mode_skips_proxy_account_id_on_client(self):
        dp, _, ar = build_trading_data_stack(
            "qmt", _settings(), ["600000.SH"], account=_account(mode="mock")
        )
        self.assertIsInstance(dp, QmtLiveDataProvider)
        # mock-mode account → no live trading-terminal session.
        self.assertEqual(dp.client.account_id, None)
        self.assertIsInstance(ar, StoreBackedAccountReader)

    def test_qmt_live_mode_uses_qmt_account_reader(self):
        dp, _, ar = build_trading_data_stack(
            "qmt", _settings(), ["600000.SH"], account=_account(mode="live")
        )
        self.assertIsInstance(dp, QmtLiveDataProvider)
        self.assertEqual(dp.client.account_id, "10000001")
        self.assertIsInstance(ar, QmtAccountReader)

    def test_demo_alias_is_mock(self):
        self.assertEqual(normalize_provider_id("demo"), "mock")

    def test_list_data_provider_ids_core_order(self):
        ids = list_data_provider_ids()
        self.assertEqual(
            ids[:6], ["auto", "mock", "qmt", "akshare", "tushare", "baostock"]
        )
        self.assertTrue(all(isinstance(x, str) and x for x in ids))

    def test_baostock_stack(self):
        dp, up, ar = build_trading_data_stack("baostock", _settings(), ["600000.SH"])
        self.assertIsInstance(dp, BaostockDataProvider)
        self.assertIsInstance(up, StaticUniverseProvider)
        self.assertIsInstance(ar, StoreBackedAccountReader)

    def test_tushare_stack_passes_configured_url_to_provider(self):
        settings = DataSettings(
            default_provider="tushare",
            tushare=TushareSettings(token="tok", url="http://proxy.example.com"),
        )
        dp, up, ar = build_trading_data_stack("tushare", settings, ["600000.SH"])
        self.assertIsInstance(dp, TushareDataProvider)
        self.assertEqual(dp._token, "tok")
        self.assertEqual(dp._url, "http://proxy.example.com")
        self.assertIsInstance(up, StaticUniverseProvider)

    def test_tushare_stack_url_none_when_unconfigured(self):
        settings = DataSettings(
            default_provider="tushare", tushare=TushareSettings(token="tok")
        )
        dp, _, _ = build_trading_data_stack("tushare", settings, ["600000.SH"])
        self.assertIsInstance(dp, TushareDataProvider)
        self.assertIsNone(dp._url)

    def test_resolve_effective_prefers_instance_override(self):
        self.assertEqual(resolve_effective_provider("mock", "qmt"), "mock")
        self.assertEqual(resolve_effective_provider(None, "mock"), "mock")

    def test_register_custom_provider(self):
        def _builder(data_cfg: DataSettings, symbols: list[str]):
            store = MockTradingDataProvider(cash=42.0)
            return store, StaticUniverseProvider(symbols), StoreBackedAccountReader(store)

        register_trading_data_provider("fixture", _builder)
        dp, _, ar = build_trading_data_stack("fixture", _settings(), ["600000.SH"])
        self.assertIsInstance(dp, MockTradingDataProvider)

        async def _cash():
            acct = await ar.get_account_snapshot()
            return acct.cash

        self.assertEqual(asyncio.run(_cash()), 42.0)

    def test_register_reserved_name_raises(self):
        with self.assertRaises(ValueError):
            register_trading_data_provider("qmt", lambda dc, s: (None, None, None))


if __name__ == "__main__":
    unittest.main()
