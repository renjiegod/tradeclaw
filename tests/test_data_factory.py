import asyncio
import unittest

from tradeclaw.config import DataSettings, QmtSettings
from tradeclaw.data.factory import (
    build_trading_data_stack,
    normalize_provider_id,
    register_trading_data_provider,
    resolve_effective_provider,
)
from tradeclaw.data.mock_provider import MockTradingDataProvider, StaticUniverseProvider
from tradeclaw.data.qmt_proxy import QmtLiveDataProvider


def _settings(*, base_url=None, default_provider="auto") -> DataSettings:
    return DataSettings(
        symbols=["600000.SH"],
        qmt=QmtSettings(
            base_url=base_url,
            token=None,
            session_id=None,
            timeout_seconds=5.0,
        ),
        default_provider=default_provider,
    )


class DataFactoryTests(unittest.TestCase):
    def test_auto_selects_mock_without_qmt_url(self):
        dp, up = build_trading_data_stack("auto", _settings(base_url=None))
        self.assertIsInstance(dp, MockTradingDataProvider)
        self.assertIsInstance(up, StaticUniverseProvider)

    def test_auto_selects_qmt_when_base_url_set(self):
        dp, up = build_trading_data_stack("auto", _settings(base_url="http://127.0.0.1:9"))
        self.assertIsInstance(dp, QmtLiveDataProvider)
        self.assertIsInstance(up, StaticUniverseProvider)

    def test_mock_ignores_qmt_url(self):
        dp, _ = build_trading_data_stack("mock", _settings(base_url="http://127.0.0.1:9"))
        self.assertIsInstance(dp, MockTradingDataProvider)

    def test_qmt_requires_base_url(self):
        with self.assertRaises(ValueError):
            build_trading_data_stack("qmt", _settings(base_url=None))

    def test_demo_alias_is_mock(self):
        self.assertEqual(normalize_provider_id("demo"), "mock")

    def test_resolve_effective_prefers_instance_override(self):
        self.assertEqual(resolve_effective_provider("mock", "qmt"), "mock")
        self.assertEqual(resolve_effective_provider(None, "mock"), "mock")

    def test_register_custom_provider(self):
        def _builder(data_cfg: DataSettings, symbols: list[str]):
            return MockTradingDataProvider(cash=42.0), StaticUniverseProvider(symbols)

        register_trading_data_provider("fixture", _builder)
        dp, _ = build_trading_data_stack("fixture", _settings())
        self.assertIsInstance(dp, MockTradingDataProvider)

        async def _cash():
            acct = await dp.get_account_snapshot()
            return acct.cash

        self.assertEqual(asyncio.run(_cash()), 42.0)

    def test_register_reserved_name_raises(self):
        with self.assertRaises(ValueError):
            register_trading_data_provider("qmt", lambda dc, s: (None, None))


if __name__ == "__main__":
    unittest.main()
