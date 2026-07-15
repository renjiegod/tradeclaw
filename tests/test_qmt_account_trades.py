"""QMT account 交割单/资产 wrappers + reader (daily-review data source).

Covers the additive ``fetch_asset`` / ``fetch_trades`` wrappers on
``QmtProxyRestClient`` (dict shape, session reconnect) and
``QmtAccountReader.get_asset_snapshot`` / ``get_trades`` (domain snapshots +
``asof`` date filter). Mirrors the fake-SDK pattern in ``test_qmt_proxy_client``.
"""

import unittest
from datetime import date, datetime
from decimal import Decimal

from qmt_proxy_sdk.exceptions import ClientError
from qmt_proxy_sdk.models.trading import AssetInfo, ConnectResponse, TradeInfo

from doyoutrade.account.qmt_reader import QmtAccountReader
from doyoutrade.core.models import AssetSnapshot, TradeSnapshot
from doyoutrade.infra.qmt_proxy_client import QmtProxyRestClient


class _FakeTrading:
    def __init__(self):
        self.connect_calls = 0

    async def connect(self, *, account_id, password=None, client_id=None):
        self.connect_calls += 1
        return ConnectResponse(
            success=True, message="ok", session_id=f"session_{account_id}_live"
        )

    async def get_asset(self, session_id):
        if session_id == "bad-session":
            raise ClientError("账户未连接", payload={"message": "账户未连接"})
        return AssetInfo(
            total_asset=120000.0,
            market_value=20000.0,
            cash=100000.0,
            frozen_cash=5000.0,
            available_cash=95000.0,
            profit_loss=1234.5,
            profit_loss_ratio=0.0123,
        )

    async def get_trades(self, session_id):
        if session_id == "bad-session":
            raise ClientError("账户未连接", payload={"message": "账户未连接"})
        return [
            TradeInfo(
                trade_id="tr1",
                order_id="o1",
                stock_code="600000.SH",
                side="BUY",
                volume=100,
                price=10.5,
                amount=1050.0,
                trade_time=datetime(2026, 6, 17, 10, 30, 0),
                commission=0.5,
            ),
            TradeInfo(
                trade_id="tr2",
                order_id="o2",
                stock_code="000001.SZ",
                side="SELL",
                volume=200,
                price=12.0,
                amount=2400.0,
                trade_time=datetime(2026, 6, 16, 14, 0, 0),  # prior day
                commission=1.0,
            ),
        ]


class _FakeSdk:
    def __init__(self):
        self.data = object()
        self.trading = _FakeTrading()
        self.closed = False

    async def aclose(self):
        self.closed = True


class QmtAccountTradesTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_asset_returns_full_breakdown_dict(self):
        sdk = _FakeSdk()
        client = QmtProxyRestClient(
            base_url="http://localhost:9000", session_id="session-001", sdk_client=sdk
        )
        asset = await client.fetch_asset()
        await client.aclose()
        # The richer breakdown fetch_account drops:
        self.assertEqual(asset["total_asset"], 120000.0)
        self.assertEqual(asset["frozen_cash"], 5000.0)
        self.assertEqual(asset["available_cash"], 95000.0)
        self.assertEqual(asset["profit_loss"], 1234.5)
        self.assertAlmostEqual(asset["profit_loss_ratio"], 0.0123)

    async def test_fetch_trades_returns_plain_dicts(self):
        sdk = _FakeSdk()
        client = QmtProxyRestClient(
            base_url="http://localhost:9000", session_id="session-001", sdk_client=sdk
        )
        trades = await client.fetch_trades()
        await client.aclose()
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["symbol"], "600000.SH")
        self.assertEqual(trades[0]["quantity"], 100)
        self.assertEqual(trades[0]["trade_time"], "2026-06-17T10:30:00")
        # never leaks the pydantic model
        self.assertIsInstance(trades[0], dict)

    async def test_fetch_asset_reconnects_on_stale_session(self):
        sdk = _FakeSdk()
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            session_id="bad-session",
            account_id="20002",
            sdk_client=sdk,
        )
        asset = await client.fetch_asset()
        self.assertEqual(sdk.trading.connect_calls, 1)
        self.assertEqual(client.session_id, "session_20002_live")
        self.assertEqual(asset["total_asset"], 120000.0)

    async def test_reader_get_asset_snapshot_maps_to_decimals(self):
        sdk = _FakeSdk()
        client = QmtProxyRestClient(
            base_url="http://localhost:9000", session_id="session-001", sdk_client=sdk
        )
        reader = QmtAccountReader(client)
        snap = await reader.get_asset_snapshot()
        self.assertIsInstance(snap, AssetSnapshot)
        self.assertEqual(snap.total_asset, Decimal("120000"))
        self.assertEqual(snap.frozen_cash, Decimal("5000"))
        self.assertIsInstance(snap.total_asset, Decimal)

    async def test_reader_get_trades_filters_by_asof(self):
        sdk = _FakeSdk()
        client = QmtProxyRestClient(
            base_url="http://localhost:9000", session_id="session-001", sdk_client=sdk
        )
        reader = QmtAccountReader(client)
        # asof filter keeps only the 06-17 trade
        only_today = await reader.get_trades(date(2026, 6, 17))
        self.assertEqual(len(only_today), 1)
        self.assertIsInstance(only_today[0], TradeSnapshot)
        self.assertEqual(only_today[0].symbol, "600000.SH")
        self.assertEqual(str(only_today[0].price), "10.5")
        # no asof → all trades
        all_trades = await reader.get_trades()
        self.assertEqual(len(all_trades), 2)


if __name__ == "__main__":
    unittest.main()
