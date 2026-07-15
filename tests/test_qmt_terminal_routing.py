"""Multi-terminal qmt-proxy routing: X-QMT-Terminal flows account → SDK header.

Covers the doyoutrade side of the multi-QMT-terminal feature:
- ``ResolvedAccount`` carries ``qmt_terminal_id`` and ``market_only()`` keeps it.
- The vendored ``qmt_proxy_sdk`` transport sends ``X-QMT-Terminal`` and sets the
  ``qmt.terminal_id`` span attribute.
- ``QmtProxyRestClient`` and ``create_qmt_proxy_rest_client`` propagate it.
"""

from __future__ import annotations

import unittest

import httpx
import qmt_proxy_sdk.http as qmt_http
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from qmt_proxy_sdk.http import AsyncHttpTransport
from doyoutrade.data.account_resolution import ResolvedAccount, resolved_account_from_record
from doyoutrade.infra.qmt import create_qmt_proxy_rest_client
from doyoutrade.infra.qmt_proxy_client import QmtProxyRestClient


class ResolvedAccountTerminalTests(unittest.TestCase):
    def test_resolved_from_record_carries_terminal_id(self):
        account = resolved_account_from_record(
            {
                "id": "acct-1",
                "name": "东莞实盘",
                "mode": "live",
                "base_url": "http://proxy:8000",
                "qmt_account_id": "100",
                "qmt_terminal_id": "dgzq",
            }
        )
        self.assertEqual(account.qmt_terminal_id, "dgzq")

    def test_market_only_preserves_terminal_id(self):
        account = resolved_account_from_record(
            {"id": "acct-1", "name": "x", "mode": "live",
             "base_url": "http://p:8000", "qmt_terminal_id": "gj"}
        )
        market = account.market_only()
        self.assertEqual(market.qmt_terminal_id, "gj")
        self.assertEqual(market.qmt_account_id, None)  # trading identity cleared
        self.assertEqual(market.mode, "mock")

    def test_terminal_id_defaults_none_when_absent(self):
        account = resolved_account_from_record(
            {"id": "a", "name": "x", "mode": "live", "base_url": "http://p:8000"}
        )
        self.assertIsNone(account.qmt_terminal_id)


class SdkTerminalHeaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self._orig_tracer = qmt_http.tracer
        qmt_http.tracer = provider.get_tracer(qmt_http.__name__)

    def tearDown(self) -> None:
        qmt_http.tracer = self._orig_tracer
        self.exporter.clear()

    async def test_transport_sends_terminal_header_and_span_attribute(self):
        seen_headers: dict[str, str] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen_headers["X-QMT-Terminal"] = request.headers.get("X-QMT-Terminal", "")
            return httpx.Response(200, json={"success": True, "message": "ok", "code": 0, "data": {}})

        transport = AsyncHttpTransport(
            base_url="http://proxy.test",
            api_key="k",
            terminal_id="dgzq",
            transport=httpx.MockTransport(handler),
        )
        try:
            await transport.request("POST", "/api/v1/data/market", json={"x": 1})
        finally:
            await transport.aclose()

        self.assertEqual(seen_headers["X-QMT-Terminal"], "dgzq")
        spans = [s for s in self.exporter.get_finished_spans() if s.name == "qmt.http.request"]
        self.assertTrue(spans)
        self.assertEqual(spans[-1].attributes.get("qmt.terminal_id"), "dgzq")

    async def test_transport_without_terminal_sets_default_attribute_and_no_header(self):
        seen: dict[str, object] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["has_header"] = "X-QMT-Terminal" in request.headers
            return httpx.Response(200, json={"success": True, "message": "ok", "code": 0, "data": {}})

        transport = AsyncHttpTransport(
            base_url="http://proxy.test",
            api_key="k",
            transport=httpx.MockTransport(handler),
        )
        try:
            await transport.request("GET", "/api/v1/health")
        finally:
            await transport.aclose()

        self.assertFalse(seen["has_header"])
        spans = [s for s in self.exporter.get_finished_spans() if s.name == "qmt.http.request"]
        self.assertEqual(spans[-1].attributes.get("qmt.terminal_id"), "default")


class RestClientTerminalTests(unittest.TestCase):
    def test_rest_client_propagates_terminal_to_sdk_header(self):
        client = QmtProxyRestClient(
            base_url="http://proxy:8000", token="t", terminal_id="dgzq", account_id="100"
        )
        self.assertEqual(client.terminal_id, "dgzq")
        headers = client._client._transport._client.headers
        self.assertEqual(headers.get("X-QMT-Terminal"), "dgzq")

    def test_factory_propagates_account_terminal_id(self):
        account = ResolvedAccount(
            account_id="acct-1",
            name="东莞实盘",
            mode="live",
            base_url="http://proxy:8000",
            token="t",
            timeout_seconds=30.0,
            qmt_account_id="100",
            session_id=None,
            mock_cash=0.0,
            mock_equity=0.0,
            qmt_terminal_id="dgzq",
        )
        client = create_qmt_proxy_rest_client(account)
        self.assertEqual(client.terminal_id, "dgzq")
        headers = client._client._transport._client.headers
        self.assertEqual(headers.get("X-QMT-Terminal"), "dgzq")


if __name__ == "__main__":
    unittest.main()
