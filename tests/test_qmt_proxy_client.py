import unittest

from qmt_proxy_sdk import AsyncQmtProxyClient
from qmt_proxy_sdk.models.data import FullTickResponse, MarketDataResponse, TickData
from qmt_proxy_sdk.models.trading import AccountInfo, AccountType, PositionInfo

from tradeclaw.data.qmt_proxy_client import QmtProxyRestClient


class _FakeDataApi:
    async def get_market_data(self, *, stock_codes, start_date, end_date, period, fields=None):
        return [
            MarketDataResponse(
                stock_code=stock_codes[0],
                data=[
                    {
                        "time": "20260101",
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.9,
                        "close": 10.2,
                        "volume": 1000,
                    }
                ],
                fields=fields or ["time", "open", "high", "low", "close", "volume"],
                period=period,
                start_date=start_date,
                end_date=end_date,
            )
        ]

    async def get_full_tick(self, *, stock_codes, start_time="", end_time=""):
        return FullTickResponse(
            ticks={
                stock_codes[0]: [
                    TickData(
                        time="2026-01-01T09:31:00",
                        last_price=10.2,
                        open=10.0,
                        high=10.5,
                        low=9.9,
                        volume=1000,
                    )
                ]
            }
        )


class _FakeTradingApi:
    async def get_account_info(self, session_id):
        return AccountInfo(
            account_id=session_id,
            account_type=AccountType.SECURITY,
            account_name="demo",
            status="connected",
            balance=100000.0,
            available_balance=90000.0,
            frozen_balance=10000.0,
            market_value=20000.0,
            total_asset=120000.0,
        )

    async def get_positions(self, session_id):
        return [
            PositionInfo(
                stock_code="600000.SH",
                stock_name="PF Bank",
                volume=100,
                available_volume=100,
                frozen_volume=0,
                cost_price=9.8,
                market_price=10.2,
                market_value=1020.0,
                profit_loss=40.0,
                profit_loss_ratio=0.04,
            )
        ]


class _FakeSdkClient:
    def __init__(self):
        self.data = _FakeDataApi()
        self.trading = _FakeTradingApi()
        self.closed = False

    async def aclose(self):
        self.closed = True


class QmtProxyClientTests(unittest.IsolatedAsyncioTestCase):
    def test_vendored_sdk_exports_async_client(self):
        self.assertIsNotNone(AsyncQmtProxyClient)

    async def test_rest_client_maps_market_and_trading_data_from_sdk(self):
        sdk_client = _FakeSdkClient()
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            session_id="session-001",
            sdk_client=sdk_client,
        )

        quotes = await client.fetch_latest_quotes(["600000.SH"])
        history = await client.fetch_history(
            symbol="600000.SH",
            start_time="2026-01-01T09:30:00",
            end_time="2026-01-01T10:00:00",
            interval="1m",
        )
        account = await client.fetch_account()
        positions = await client.fetch_positions()
        await client.aclose()

        self.assertEqual(quotes[0]["symbol"], "600000.SH")
        self.assertEqual(quotes[0]["price"], 10.2)
        self.assertEqual(history[0]["symbol"], "600000.SH")
        self.assertEqual(history[0]["close"], 10.2)
        self.assertEqual(account["cash"], 90000.0)
        self.assertEqual(account["equity"], 120000.0)
        self.assertEqual(positions[0]["symbol"], "600000.SH")
        self.assertTrue(sdk_client.closed)

    async def test_rest_client_requires_session_id_for_trading_reads(self):
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            sdk_client=_FakeSdkClient(),
        )

        with self.assertRaises(RuntimeError):
            await client.fetch_account()


if __name__ == "__main__":
    unittest.main()
