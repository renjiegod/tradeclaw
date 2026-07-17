import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from qmt_proxy_sdk import AsyncQmtProxyClient
from qmt_proxy_sdk.exceptions import ClientError
from qmt_proxy_sdk.models.data import FullTickResponse, MarketDataResponse, TickData, TradingCalendarResponse
from qmt_proxy_sdk.models.trading import AccountInfo, AccountType, ConnectResponse, PositionInfo

from doyoutrade.data.cloud_profile import (
    CloudPlan,
    CloudProfile,
    CloudQuota,
    CloudRecommendations,
)
from doyoutrade.infra.qmt_proxy_client import QmtProxyRestClient


class _FakeDataApi:
    def __init__(self) -> None:
        # Record get_market_data calls so tests can assert the read-first /
        # download-fallback contract (each entry is the ``disable_download`` flag).
        self.market_calls: list[bool] = []
        self.full_kline_calls: list[dict[str, object]] = []
        # When True, the first (disable_download=True) read returns empty bars to
        # exercise the download-enabled fallback path.
        self.empty_on_fast_read = False
        self.raise_on_full_kline: Exception | None = None

    async def get_market_data(
        self,
        *,
        stock_codes,
        start_date,
        end_date,
        period,
        fields=None,
        adjust_type="none",
        fill_data=True,
        disable_download=False,
    ):
        self.market_calls.append(disable_download)
        rows = (
            []
            if (self.empty_on_fast_read and disable_download)
            else [
                {
                    "time": "20260101",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.9,
                    "close": 10.2,
                    "volume": 1000,
                    "amount": 1_020_000.0,
                }
            ]
        )
        return [
            MarketDataResponse(
                stock_code=stock_codes[0],
                data=rows,
                fields=fields or ["time", "open", "high", "low", "close", "volume", "amount"],
                period=period,
                start_date=start_date,
                end_date=end_date,
            )
        ]

    async def get_full_kline(
        self, *, stock_codes, start_time, end_time, period, fields=None, adjust_type="none"
    ):
        self.full_kline_calls.append(
            {
                "stock_codes": list(stock_codes),
                "start_time": start_time,
                "end_time": end_time,
                "period": period,
                "fields": list(fields or []),
                "adjust_type": adjust_type,
            }
        )
        if self.raise_on_full_kline is not None:
            raise self.raise_on_full_kline
        return [
            MarketDataResponse(
                stock_code=stock_codes[0],
                data=[
                    {
                        "time": "2026-01-01 09:35:00",
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.9,
                        "close": 10.2,
                        "volume": 1000,
                        "amount": 1_020_000.0,
                    }
                ],
                fields=fields or ["time", "open", "high", "low", "close", "volume", "amount"],
                period=period,
                start_date=start_time,
                end_date=end_time,
            )
        ]

    async def get_trading_calendar(self, year: int) -> TradingCalendarResponse:
        return TradingCalendarResponse(
            trading_dates=[f"{year}-01-02"],
            holidays=[],
            year=year,
        )

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
    def __init__(self):
        self.connect_calls = 0

    async def connect(self, *, account_id, password=None, client_id=None):
        self.connect_calls += 1
        return ConnectResponse(
            success=True,
            message="ok",
            session_id=f"session_{account_id}_live",
        )

    async def get_account_info(self, session_id):
        if session_id == "bad-session":
            raise ClientError("账户未连接", payload={"message": "账户未连接"})
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
        if session_id == "bad-session":
            raise ClientError("账户未连接", payload={"message": "账户未连接"})
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


class _ConnectFailsTradingApi:
    async def connect(self, *, account_id, password=None, client_id=None):
        return ConnectResponse(success=False, message="xttrader offline")


class _SdkConnectFails:
    def __init__(self):
        self.data = _FakeDataApi()
        self.trading = _ConnectFailsTradingApi()
        self.closed = False

    async def aclose(self):
        self.closed = True


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

    async def test_rest_client_fetches_today_1d_bar_from_tick(self):
        """When interval=1d and end date is today, get_full_tick is used instead of get_market_data."""
        sdk_client = _FakeSdkClient()
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            session_id="session-001",
            sdk_client=sdk_client,
        )
        # Use today's date so _range_includes_today returns True
        today = date.today().isoformat()
        bars = await client.fetch_bars(
            symbol="600000.SH",
            start_time=f"{today}T00:00:00",
            end_time=f"{today}T23:59:59",
            interval="1d",
        )
        await client.aclose()
        # The fake get_full_tick returns last_price=10.2, open=10.0, high=10.5, low=9.9
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["symbol"], "600000.SH")
        self.assertEqual(bars[0]["close"], 10.2)
        self.assertEqual(bars[0]["open"], 10.0)
        self.assertEqual(bars[0]["high"], 10.5)
        self.assertEqual(bars[0]["low"], 9.9)
        self.assertEqual(bars[0]["volume"], 1000)

    async def test_rest_client_maps_market_and_trading_data_from_sdk(self):
        sdk_client = _FakeSdkClient()
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            session_id="session-001",
            sdk_client=sdk_client,
        )

        quotes = await client.fetch_latest_quotes(["600000.SH"])
        history = await client.fetch_bars(
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
        self.assertEqual(
            quotes[0]["tick"],
            {
                "time": "2026-01-01T09:31:00",
                "last_price": 10.2,
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "volume": 1000,
            },
        )
        self.assertEqual(history[0]["symbol"], "600000.SH")
        self.assertEqual(history[0]["close"], 10.2)
        self.assertEqual(history[0]["amount"], 1_020_000.0)
        self.assertEqual(account["cash"], 90000.0)
        self.assertEqual(account["equity"], 120000.0)
        self.assertEqual(positions[0]["symbol"], "600000.SH")
        self.assertEqual(sdk_client.trading.connect_calls, 0)
        self.assertTrue(sdk_client.closed)

    async def test_intraday_history_after_close_uses_market_data_not_full_kline(self):
        sdk_client = _FakeSdkClient()
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            session_id="session-001",
            sdk_client=sdk_client,
        )
        after_close = datetime(2026, 6, 15, 22, 40, tzinfo=ZoneInfo("Asia/Shanghai"))
        with unittest.mock.patch(
            "doyoutrade.infra.qmt_proxy_client._now_market_tz",
            return_value=after_close,
        ):
            bars = await client.fetch_bars(
                symbol="600000.SH",
                start_time="2026-05-15T09:30:00",
                end_time="2026-06-15T15:00:00",
                interval="5m",
            )
        await client.aclose()
        self.assertEqual(sdk_client.data.market_calls, [True])
        self.assertEqual(len(sdk_client.data.full_kline_calls), 0)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["close"], 10.2)

    async def test_intraday_live_session_uses_full_kline(self):
        sdk_client = _FakeSdkClient()
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            session_id="session-001",
            sdk_client=sdk_client,
        )
        in_session = datetime(2026, 6, 15, 10, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        today = in_session.date().isoformat()
        with unittest.mock.patch(
            "doyoutrade.infra.qmt_proxy_client._now_market_tz",
            return_value=in_session,
        ):
            bars = await client.fetch_bars(
                symbol="600000.SH",
                start_time=f"{today}T09:30:00",
                end_time=f"{today}T10:00:00",
                interval="5m",
            )
        await client.aclose()
        self.assertEqual(sdk_client.data.market_calls, [])
        self.assertEqual(len(sdk_client.data.full_kline_calls), 1)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["close"], 10.2)

    async def test_intraday_live_session_unsupported_full_kline_raises_clear_error(self):
        sdk_client = _FakeSdkClient()
        sdk_client.data.raise_on_full_kline = ClientError(
            "获取真实全推K线失败: 当前客户端未支持此功能，请更新客户端或升级投研版",
            code=300000,
            payload={
                "detail": {
                    "message": (
                        "获取真实全推K线失败: 当前客户端未支持此功能，请更新客户端或升级投研版 "
                        "func:commonControl"
                    )
                }
            },
        )
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            session_id="session-001",
            sdk_client=sdk_client,
        )
        in_session = datetime(2026, 6, 15, 10, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        today = in_session.date().isoformat()
        with unittest.mock.patch(
            "doyoutrade.infra.qmt_proxy_client._now_market_tz",
            return_value=in_session,
        ):
            with self.assertRaisesRegex(RuntimeError, "get_full_kline is unsupported"):
                await client.fetch_bars(
                    symbol="600000.SH",
                    start_time=f"{today}T09:30:00",
                    end_time=f"{today}T10:00:00",
                    interval="5m",
                )
        await client.aclose()
        self.assertEqual(sdk_client.data.market_calls, [])
        self.assertEqual(len(sdk_client.data.full_kline_calls), 1)

    async def test_history_fetch_uses_fast_read_when_local_has_bars(self):
        """Historical bars take the fast disable_download=True read; no download retry."""
        sdk_client = _FakeSdkClient()
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            session_id="session-001",
            sdk_client=sdk_client,
        )
        bars = await client.fetch_bars(
            symbol="600000.SH",
            start_time="2026-01-01",
            end_time="2026-02-01",
            interval="1d",
        )
        await client.aclose()
        # Single call, fast path only (disable_download=True), no fallback.
        self.assertEqual(sdk_client.data.market_calls, [True])
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["close"], 10.2)

    async def test_history_fetch_falls_back_to_download_when_local_empty(self):
        """When the fast read returns no bars, retry once with download enabled."""
        sdk_client = _FakeSdkClient()
        sdk_client.data.empty_on_fast_read = True
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            session_id="session-001",
            sdk_client=sdk_client,
        )
        bars = await client.fetch_bars(
            symbol="600000.SH",
            start_time="2026-01-01",
            end_time="2026-02-01",
            interval="1d",
        )
        await client.aclose()
        # Fast read (True) returned empty → download-enabled retry (False).
        self.assertEqual(sdk_client.data.market_calls, [True, False])
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["close"], 10.2)

    @staticmethod
    def _cloud_profile(*, disable_download: bool = True) -> CloudProfile:
        return CloudProfile(
            service="doyoutrade-cloud",
            protocol_version=1,
            plan=CloudPlan(plan_name="free"),
            quota=CloudQuota(),
            capabilities=("rate_limit_headers",),
            recommendations=CloudRecommendations(disable_download=disable_download),
        )

    async def test_cloud_disable_download_skips_fallback_and_emits_event(self):
        """Cloud mode + rec.disable_download: no download-enabled retry."""
        sdk_client = _FakeSdkClient()
        sdk_client.data.empty_on_fast_read = True

        async def _profile() -> CloudProfile:
            return self._cloud_profile(disable_download=True)

        client = QmtProxyRestClient(
            base_url="http://cloud.example",
            session_id="session-001",
            sdk_client=sdk_client,
            cloud_profile_provider=_profile,
        )
        with unittest.mock.patch(
            "doyoutrade.debug.emit_debug_event",
            new_callable=unittest.mock.AsyncMock,
        ) as emit:
            bars = await client.fetch_bars(
                symbol="600000.SH",
                start_time="2026-01-01",
                end_time="2026-02-01",
                interval="1d",
            )
        await client.aclose()
        # Fast read only; the download-enabled retry was intentionally skipped.
        self.assertEqual(sdk_client.data.market_calls, [True])
        self.assertEqual(bars, [])
        skip_calls = [
            call
            for call in emit.await_args_list
            if call.args[0] == "qmt_market_download_fallback_skipped"
        ]
        self.assertEqual(len(skip_calls), 1)
        payload = skip_calls[0].args[1]
        self.assertEqual(payload["reason"], "cloud_disable_download")
        self.assertEqual(payload["plan_name"], "free")
        self.assertEqual(payload["symbol"], "600000.SH")
        self.assertIn("hint", payload)

    async def test_cloud_profile_without_disable_download_keeps_fallback(self):
        """Cloud mode with disable_download=false keeps the classic retry."""
        sdk_client = _FakeSdkClient()
        sdk_client.data.empty_on_fast_read = True

        async def _profile() -> CloudProfile:
            return self._cloud_profile(disable_download=False)

        client = QmtProxyRestClient(
            base_url="http://cloud.example",
            session_id="session-001",
            sdk_client=sdk_client,
            cloud_profile_provider=_profile,
        )
        bars = await client.fetch_bars(
            symbol="600000.SH",
            start_time="2026-01-01",
            end_time="2026-02-01",
            interval="1d",
        )
        await client.aclose()
        self.assertEqual(sdk_client.data.market_calls, [True, False])
        self.assertEqual(len(bars), 1)

    async def test_classic_probe_none_keeps_fallback(self):
        """Provider resolving to None (classic qmt-proxy) keeps the retry."""
        sdk_client = _FakeSdkClient()
        sdk_client.data.empty_on_fast_read = True

        async def _profile() -> None:
            return None

        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            session_id="session-001",
            sdk_client=sdk_client,
            cloud_profile_provider=_profile,
        )
        bars = await client.fetch_bars(
            symbol="600000.SH",
            start_time="2026-01-01",
            end_time="2026-02-01",
            interval="1d",
        )
        await client.aclose()
        self.assertEqual(sdk_client.data.market_calls, [True, False])
        self.assertEqual(len(bars), 1)

    async def test_cloud_probe_failure_keeps_fallback_and_is_logged(self):
        """A raising cloud-profile provider degrades to classic behaviour."""
        sdk_client = _FakeSdkClient()
        sdk_client.data.empty_on_fast_read = True

        async def _profile() -> None:
            raise RuntimeError("hello probe blew up")

        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            session_id="session-001",
            sdk_client=sdk_client,
            cloud_profile_provider=_profile,
        )
        with self.assertLogs("doyoutrade.infra.qmt_proxy_client", level="WARNING") as logs:
            bars = await client.fetch_bars(
                symbol="600000.SH",
                start_time="2026-01-01",
                end_time="2026-02-01",
                interval="1d",
            )
        await client.aclose()
        self.assertEqual(sdk_client.data.market_calls, [True, False])
        self.assertEqual(len(bars), 1)
        self.assertTrue(
            any("cloud profile probe failed" in line for line in logs.output)
        )

    async def test_fast_read_hit_never_probes_cloud_profile(self):
        """A non-empty fast read must not trigger the cloud probe at all."""
        sdk_client = _FakeSdkClient()
        probe_calls = 0

        async def _profile() -> CloudProfile:
            nonlocal probe_calls
            probe_calls += 1
            return self._cloud_profile(disable_download=True)

        client = QmtProxyRestClient(
            base_url="http://cloud.example",
            session_id="session-001",
            sdk_client=sdk_client,
            cloud_profile_provider=_profile,
        )
        bars = await client.fetch_bars(
            symbol="600000.SH",
            start_time="2026-01-01",
            end_time="2026-02-01",
            interval="1d",
        )
        await client.aclose()
        self.assertEqual(sdk_client.data.market_calls, [True])
        self.assertEqual(len(bars), 1)
        self.assertEqual(probe_calls, 0)

    async def test_rest_client_forwards_get_trading_calendar(self):
        sdk_client = _FakeSdkClient()
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            session_id="session-001",
            sdk_client=sdk_client,
        )
        cal = await client.get_trading_calendar(2026)
        await client.aclose()
        self.assertEqual(cal.year, 2026)
        self.assertIn("2026-01-02", cal.trading_dates)

    async def test_rest_client_requires_session_id_for_trading_reads(self):
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            sdk_client=_FakeSdkClient(),
        )

        with self.assertRaises(RuntimeError):
            await client.fetch_account()

    async def test_connect_failure_message_includes_proxy_hint(self):
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            token="secret-api-key",
            session_id=None,
            account_id="20002",
            sdk_client=_SdkConnectFails(),
        )
        with self.assertRaises(RuntimeError) as ctx:
            await client.fetch_account()
        msg = str(ctx.exception)
        self.assertIn("qmt-proxy connect failed", msg)
        self.assertIn("xttrader offline", msg)
        self.assertIn("MOCK", msg)
        self.assertIn("account_id='20002'", msg)
        self.assertIn("secret-api-key", msg)

    async def test_rest_client_connects_and_persists_session_id(self):
        # session_id refresh now writes back to the accounts row via the
        # session_persist callback (not config.yaml).
        persisted: list[tuple[str, str]] = []

        async def _persist(account_pk: str, session_id: str) -> None:
            persisted.append((account_pk, session_id))

        sdk_client = _FakeSdkClient()
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            session_id=None,
            account_id="20002",
            account_pk="acct-abc",
            session_persist=_persist,
            sdk_client=sdk_client,
        )
        account = await client.fetch_account()
        await client.aclose()

        self.assertEqual(sdk_client.trading.connect_calls, 1)
        self.assertEqual(account["cash"], 90000.0)
        self.assertEqual(client.session_id, "session_20002_live")
        self.assertEqual(persisted, [("acct-abc", "session_20002_live")])

    async def test_rest_client_reconnects_on_stale_session(self):
        sdk_client = _FakeSdkClient()
        client = QmtProxyRestClient(
            base_url="http://localhost:9000",
            session_id="bad-session",
            account_id="20002",
            sdk_client=sdk_client,
        )
        account = await client.fetch_account()
        await client.aclose()

        self.assertEqual(sdk_client.trading.connect_calls, 1)
        self.assertEqual(client.session_id, "session_20002_live")
        self.assertEqual(account["account_id"], "session_20002_live")


if __name__ == "__main__":
    unittest.main()
