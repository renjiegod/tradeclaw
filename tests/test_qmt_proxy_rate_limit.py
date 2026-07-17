"""429 限流韧性：``qmt_proxy_sdk`` HTTP 自动退避重试 + WS 握手 429 重连。

云网关（doyoutrade-cloud）对分钟限流 / 日配额返回 429，带 ``Retry-After`` 头，
响应 JSON 为 ``{"error_code": "rate_limited"|"daily_quota_exceeded", "message": ...}``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import unittest
from unittest import mock

import httpx
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response as WsResponse

import qmt_proxy_sdk.http as qmt_http
import qmt_proxy_sdk.ws as qmt_ws
from qmt_proxy_sdk.exceptions import ClientError, RateLimitedError, TransportError
from qmt_proxy_sdk.http import AsyncHttpTransport
from qmt_proxy_sdk.ws import QuoteStream


def _rate_limited_response(
    *, retry_after: str | None = "1", error_code: str = "rate_limited"
) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(
        429,
        headers=headers,
        json={"error_code": error_code, "message": "too many requests"},
    )


def _ok_envelope(data: dict) -> httpx.Response:
    return httpx.Response(
        200, json={"success": True, "message": "ok", "code": 0, "data": data}
    )


class _SleepRecorder:
    """记录 ``asyncio.sleep`` 的调用参数并立即返回（不真等）。"""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._real_sleep = asyncio.sleep

    async def __call__(self, seconds: float, *args, **kwargs):
        self.delays.append(seconds)
        await self._real_sleep(0)


class RateLimitRetryHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._log = logging.getLogger(qmt_http.__name__)
        self._prev_log_level = self._log.level
        self._log.setLevel(logging.CRITICAL)

    def tearDown(self) -> None:
        self._log.setLevel(self._prev_log_level)

    async def _request(
        self,
        handler,
        *,
        sleep: _SleepRecorder,
        path: str = "quotes/latest",
        **transport_kwargs,
    ):
        client = httpx.AsyncClient(
            base_url="http://cloud.test/",
            transport=httpx.MockTransport(handler),
        )
        try:
            t = AsyncHttpTransport(
                base_url="http://ignored", client=client, **transport_kwargs
            )
            with mock.patch("asyncio.sleep", sleep):
                return await t.request("GET", path)
        finally:
            await client.aclose()

    async def test_429_with_retry_after_recovers(self) -> None:
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            calls["n"] += 1
            if calls["n"] <= 2:
                return _rate_limited_response(retry_after="2")
            return _ok_envelope({"p": 1.0})

        sleep = _SleepRecorder()
        out = await self._request(handler, sleep=sleep)

        self.assertEqual(out, {"p": 1.0})
        self.assertEqual(calls["n"], 3)
        # 两次重试都尊重服务端 Retry-After=2。
        self.assertEqual(sleep.delays, [2.0, 2.0])

    async def test_429_retries_exhausted_raises_rate_limited_error(self) -> None:
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            calls["n"] += 1
            return _rate_limited_response(retry_after="1")

        sleep = _SleepRecorder()
        with self.assertRaises(RateLimitedError) as ctx:
            await self._request(handler, sleep=sleep)

        # 首发 + 3 次重试 = 4 次请求。
        self.assertEqual(calls["n"], 4)
        self.assertEqual(sleep.delays, [1.0, 1.0, 1.0])
        exc = ctx.exception
        self.assertEqual(exc.status_code, 429)
        self.assertEqual(exc.error_code, "rate_limited")
        self.assertEqual(exc.retry_after, 1.0)
        # 兼容性：旧代码 except ClientError 仍能兜住。
        self.assertIsInstance(exc, ClientError)

    async def test_429_retry_after_over_cap_raises_immediately(self) -> None:
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            calls["n"] += 1
            # 日配额：Retry-After = 距上海午夜秒数，远超上限。
            return _rate_limited_response(
                retry_after="86400", error_code="daily_quota_exceeded"
            )

        sleep = _SleepRecorder()
        with self.assertRaises(RateLimitedError) as ctx:
            await self._request(handler, sleep=sleep)

        # 不等待、不重试，直接抛给上层决定。
        self.assertEqual(calls["n"], 1)
        self.assertEqual(sleep.delays, [])
        self.assertEqual(ctx.exception.retry_after, 86400.0)
        self.assertEqual(ctx.exception.error_code, "daily_quota_exceeded")

    async def test_429_retry_after_cap_is_configurable(self) -> None:
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            calls["n"] += 1
            return _rate_limited_response(retry_after="10")

        sleep = _SleepRecorder()
        with self.assertRaises(RateLimitedError):
            await self._request(handler, sleep=sleep, rate_limit_max_wait=5.0)

        self.assertEqual(calls["n"], 1)
        self.assertEqual(sleep.delays, [])

    async def test_429_without_retry_after_uses_exponential_backoff(self) -> None:
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            calls["n"] += 1
            if calls["n"] <= 3:
                return _rate_limited_response(retry_after=None)
            return _ok_envelope({"ok": True})

        sleep = _SleepRecorder()
        out = await self._request(handler, sleep=sleep)

        self.assertEqual(out, {"ok": True})
        self.assertEqual(calls["n"], 4)
        self.assertEqual(sleep.delays, [1.0, 2.0, 4.0])

    async def test_non_429_client_error_is_not_retried(self) -> None:
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            calls["n"] += 1
            return httpx.Response(400, json={"message": "bad", "code": 400})

        sleep = _SleepRecorder()
        with self.assertRaises(ClientError) as ctx:
            await self._request(handler, sleep=sleep)

        self.assertEqual(calls["n"], 1)
        self.assertEqual(sleep.delays, [])
        self.assertNotIsInstance(ctx.exception, RateLimitedError)


# ---------------------------------------------------------------------------
# WebSocket 握手 429 重连
# ---------------------------------------------------------------------------


class _FakeSubscription:
    subscription_id = "sub-429"


class _FakeDataApi:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def create_subscription(self, **kwargs) -> _FakeSubscription:  # noqa: ARG002
        return _FakeSubscription()

    async def delete_subscription(self, *, subscription_id: str) -> None:
        self.deleted.append(subscription_id)


class _FakeWs:
    """最小可用的 ws 连接：异步迭代产出预置消息，send 为 no-op。

    ``park=True`` 时消息耗尽后挂起（可被取消），模拟保持连接不断开。
    """

    def __init__(self, messages: list[str], *, park: bool = False) -> None:
        self._messages = list(messages)
        self._park = park

    async def send(self, data: str) -> None:  # noqa: ARG002
        return None

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield m
        if self._park:
            await asyncio.Event().wait()


class _FakeConnectCM:
    def __init__(self, ws: _FakeWs) -> None:
        self._ws = ws

    async def __aenter__(self) -> _FakeWs:
        return self._ws

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _handshake_429(retry_after: str | None = "7") -> InvalidStatus:
    headers = Headers()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return InvalidStatus(WsResponse(429, "Too Many Requests", headers))


class _WsSleepRecorder:
    """记录重连退避的 sleep；心跳间隔的 sleep 真实挂起（结束时被 cancel）。"""

    def __init__(self, heartbeat_interval: float) -> None:
        self.delays: list[float] = []
        self._heartbeat_interval = heartbeat_interval
        self._real_sleep = asyncio.sleep

    async def __call__(self, seconds: float, *args, **kwargs):
        if seconds == self._heartbeat_interval:
            await self._real_sleep(seconds)
            return
        self.delays.append(seconds)
        await self._real_sleep(0)


class QuoteStreamHandshake429Tests(unittest.IsolatedAsyncioTestCase):
    HEARTBEAT = 3600.0

    def setUp(self) -> None:
        self._log = logging.getLogger(qmt_ws.__name__)
        self._prev_log_level = self._log.level
        self._log.setLevel(logging.CRITICAL)

    def tearDown(self) -> None:
        self._log.setLevel(self._prev_log_level)

    def _stream(self, **kwargs) -> QuoteStream:
        return QuoteStream(
            data_api=_FakeDataApi(),
            ws_base_url="ws://cloud.test",
            symbols=["000001.SZ"],
            heartbeat_interval=self.HEARTBEAT,
            reconnect_delay=1.0,
            **kwargs,
        )

    async def test_handshake_429_reconnects_and_waits_retry_after(self) -> None:
        quote_msg = json.dumps(
            {
                "type": "quote",
                "timestamp": "2026-07-17T09:30:00",
                "data": {"stock_code": "000001.SZ", "last_price": 11.5},
            }
        )
        attempts: list[str] = []

        def fake_connect(url: str, **kwargs):  # noqa: ARG001
            attempts.append(url)
            if len(attempts) == 1:
                raise _handshake_429("7")
            return _FakeConnectCM(_FakeWs([json.dumps({"type": "connected"}), quote_msg]))

        sleep = _WsSleepRecorder(self.HEARTBEAT)
        stream = self._stream()
        with mock.patch.object(qmt_ws, "connect", fake_connect), mock.patch(
            "asyncio.sleep", sleep
        ):
            agen = stream._stream()
            quote = await agen.__anext__()
            await stream.aclose()
            await agen.aclose()

        self.assertEqual(len(attempts), 2)
        self.assertEqual(quote.stock_code, "000001.SZ")
        self.assertEqual(quote.last_price, 11.5)
        # 重连等待 = max(原退避 1.0*1, Retry-After 7) = 7。
        self.assertEqual(sleep.delays, [7.0])

    async def test_handshake_429_without_retry_after_uses_base_backoff(self) -> None:
        attempts: list[str] = []

        def fake_connect(url: str, **kwargs):  # noqa: ARG001
            attempts.append(url)
            if len(attempts) == 1:
                raise _handshake_429(retry_after=None)
            # 第二次连接成功后挂起（模拟保持连接），可被取消。
            return _FakeConnectCM(
                _FakeWs([json.dumps({"type": "connected"})], park=True)
            )

        sleep = _WsSleepRecorder(self.HEARTBEAT)
        stream = self._stream()
        with mock.patch.object(qmt_ws, "connect", fake_connect), mock.patch(
            "asyncio.sleep", sleep
        ):
            agen = stream._stream()
            # 无行情消息可产出：驱动到第二次连接建立即可，随后取消收尾。
            task = asyncio.ensure_future(agen.__anext__())
            while len(attempts) < 2:
                await sleep._real_sleep(0)
            await stream.aclose()
            task.cancel()
            with self.assertRaises((asyncio.CancelledError, StopAsyncIteration)):
                await task
            await agen.aclose()

        self.assertEqual(len(attempts), 2)
        # 无 Retry-After → 原退避 reconnect_delay * attempt = 1.0。
        self.assertEqual(sleep.delays, [1.0])

    async def test_handshake_429_exhausts_reconnects_raises_transport_error(self) -> None:
        attempts: list[str] = []

        def fake_connect(url: str, **kwargs):  # noqa: ARG001
            attempts.append(url)
            raise _handshake_429("1")

        sleep = _WsSleepRecorder(self.HEARTBEAT)
        stream = self._stream(reconnect_attempts=2)
        with mock.patch.object(qmt_ws, "connect", fake_connect), mock.patch(
            "asyncio.sleep", sleep
        ):
            with self.assertRaises(TransportError):
                async for _quote in stream:
                    pass

        # 首连 + 2 次重连 = 3 次握手；InvalidStatus 不再漏出（否则这里
        # 抛的是 websockets.InvalidStatus 而非 TransportError）。
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep.delays, [1.0, 2.0])

    async def test_retry_after_helper_handles_legacy_and_invalid_values(self) -> None:
        # 新式 InvalidStatus。
        self.assertEqual(qmt_ws._handshake_retry_after(_handshake_429("7")), 7.0)
        self.assertIsNone(qmt_ws._handshake_retry_after(_handshake_429(None)))
        self.assertIsNone(qmt_ws._handshake_retry_after(_handshake_429("not-a-number")))
        # 非 429 状态不触发。
        non_429 = InvalidStatus(WsResponse(503, "Unavailable", Headers({"Retry-After": "9"})))
        self.assertIsNone(qmt_ws._handshake_retry_after(non_429))
        # 旧式 InvalidStatusCode 形态（status_code/headers 直接挂在异常上），
        # 用 duck-typing 模拟。

        class _LegacyExc(Exception):
            status_code = 429
            headers = {"Retry-After": "12"}

        self.assertEqual(qmt_ws._handshake_retry_after(_LegacyExc()), 12.0)


if __name__ == "__main__":
    unittest.main()
