"""Tracing events on ``qmt_proxy_sdk.http.AsyncHttpTransport``."""

from __future__ import annotations

import json
import logging
import unittest

import httpx
import qmt_proxy_sdk.http as qmt_http
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from qmt_proxy_sdk.exceptions import ClientError, TransportError
from qmt_proxy_sdk.http import AsyncHttpTransport

_EVENT_PAYLOAD_JSON = "doyoutrade.event.payload_json"


def _qmt_http_events(ro) -> list:
    return [e for e in ro.events if e.name.startswith("qmt.http")]


def _event_payload(event) -> dict:
    attrs = dict(event.attributes) if event.attributes else {}
    raw = attrs.get(_EVENT_PAYLOAD_JSON)
    assert isinstance(raw, str)
    return json.loads(raw)


class QmtProxyHttpTransportTracingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._log = logging.getLogger(qmt_http.__name__)
        self._prev_log_level = self._log.level
        self._log.setLevel(logging.CRITICAL)
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self._orig_tracer = qmt_http.tracer
        qmt_http.tracer = provider.get_tracer(qmt_http.__name__)

    def tearDown(self) -> None:
        qmt_http.tracer = self._orig_tracer
        self._log.setLevel(self._prev_log_level)
        self.exporter.clear()

    async def test_request_emits_sent_and_response_events_with_bodies(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            body = json.loads(request.content.decode())
            self.assertEqual(body, {"sym": "600000.SH"})
            return httpx.Response(
                200,
                json={"success": True, "message": "ok", "code": 0, "data": {"p": 1.0}},
            )

        transport_layer = httpx.MockTransport(handler)
        client = httpx.AsyncClient(base_url="http://proxy.test/api/", transport=transport_layer)
        try:
            t = AsyncHttpTransport(base_url="http://ignored", client=client)
            out = await t.request("POST", "quotes/latest", json={"sym": "600000.SH"})
        finally:
            await client.aclose()

        self.assertEqual(out, {"p": 1.0})

        spans = self.exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        ro = spans[0]
        self.assertEqual(ro.name, "qmt.http.request")
        attrs = dict(ro.attributes) if ro.attributes else {}
        self.assertEqual(attrs.get("http.method"), "POST")
        self.assertEqual(attrs.get("http.response.status_code"), 200)

        qev = _qmt_http_events(ro)
        names = [e.name for e in qev]
        self.assertEqual(
            names,
            ["qmt.http.request_sent", "qmt.http.response_received"],
        )

        sent = _event_payload(qev[0])
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["path"], "quotes/latest")
        self.assertIn("http://proxy.test/api/quotes/latest", sent["url"])
        self.assertEqual(sent["params"], {})
        self.assertEqual(json.loads(sent["request_body"]), {"sym": "600000.SH"})

        recv = _event_payload(qev[1])
        self.assertEqual(recv["status_code"], 200)
        self.assertIn("application/json", recv.get("content_type", ""))
        self.assertIn('"p":1.0', recv["response_body"])

    async def test_transport_error_emits_transport_event(self) -> None:
        async def boom(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            raise httpx.ConnectError("refused")

        client = httpx.AsyncClient(
            base_url="http://proxy.test/",
            transport=httpx.MockTransport(boom),
        )
        try:
            t = AsyncHttpTransport(base_url="http://ignored", client=client)
            with self.assertRaises(TransportError):
                await t.request("GET", "ping")
        finally:
            await client.aclose()

        spans = self.exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        ro = spans[0]
        qev = _qmt_http_events(ro)
        names = [e.name for e in qev]
        self.assertEqual(names, ["qmt.http.request_sent", "qmt.http.transport_error"])
        err = _event_payload(qev[1])
        self.assertEqual(err["error_type"], "ConnectError")

    async def test_error_status_still_records_response_body_event(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            return httpx.Response(
                400,
                json={"success": False, "message": "bad", "code": 400, "data": None},
            )

        client = httpx.AsyncClient(
            base_url="http://proxy.test/",
            transport=httpx.MockTransport(handler),
        )
        try:
            t = AsyncHttpTransport(base_url="http://ignored", client=client)
            with self.assertRaises(ClientError):
                await t.request("GET", "x")
        finally:
            await client.aclose()

        spans = self.exporter.get_finished_spans()
        ro = spans[0]
        qev = _qmt_http_events(ro)
        names = [e.name for e in qev]
        self.assertEqual(
            names,
            ["qmt.http.request_sent", "qmt.http.response_received"],
        )
        recv = _event_payload(qev[1])
        self.assertEqual(recv["status_code"], 400)
        self.assertIn("bad", recv["response_body"])

    async def test_authorization_header_redacted_in_request_sent(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            return httpx.Response(
                200,
                json={"success": True, "message": "ok", "code": 0, "data": {}},
            )

        client = httpx.AsyncClient(
            base_url="http://proxy.test/",
            transport=httpx.MockTransport(handler),
        )
        try:
            t = AsyncHttpTransport(base_url="http://ignored", client=client)
            await t.request(
                "GET",
                "ok",
                headers={"Authorization": "Bearer secret", "X-Test": "1"},
            )
        finally:
            await client.aclose()

        spans = self.exporter.get_finished_spans()
        sent = _event_payload(_qmt_http_events(spans[0])[0])
        rh = sent.get("request_headers") or {}
        self.assertEqual(rh.get("Authorization"), "[REDACTED]")
        self.assertEqual(rh.get("X-Test"), "1")


if __name__ == "__main__":
    unittest.main()
