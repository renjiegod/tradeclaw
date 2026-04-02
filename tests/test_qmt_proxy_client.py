import json
import unittest
from urllib.error import HTTPError

from tradeclaw.data.qmt_proxy_client import QmtProxyRestClient, QmtProxyWsClient


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class _RecordingOpener:
    def __init__(self, payload: dict):
        self.payload = payload
        self.requests = []

    def open(self, request, timeout=5):
        self.requests.append((request.full_url, request.get_method(), dict(request.header_items())))
        return _FakeResponse(self.payload)


class QmtProxyClientTests(unittest.TestCase):
    def test_rest_client_calls_expected_endpoint_with_token(self):
        opener = _RecordingOpener(payload={"data": [{"symbol": "600000.SH", "price": 10.2}]})
        client = QmtProxyRestClient(base_url="http://localhost:9000", token="abc", opener=opener)

        result = client.fetch_latest_quotes(["600000.SH"])

        self.assertEqual(len(result), 1)
        self.assertIn("/quotes/latest?symbols=600000.SH", opener.requests[0][0])
        self.assertEqual(opener.requests[0][1], "GET")
        self.assertEqual(opener.requests[0][2].get("Authorization"), "Bearer abc")

    def test_rest_client_raises_runtime_error_on_http_error(self):
        class _BrokenOpener:
            def open(self, request, timeout=5):
                raise HTTPError(request.full_url, 500, "boom", hdrs=None, fp=None)

        client = QmtProxyRestClient(base_url="http://localhost:9000", opener=_BrokenOpener())

        with self.assertRaises(RuntimeError):
            client.fetch_account()

    def test_ws_client_builds_subscription_message(self):
        sent_messages = []

        class _FakeApp:
            def __init__(self, url, on_open=None, on_message=None, on_error=None, on_close=None, header=None):
                self.on_open = on_open
                self.url = url

            def send(self, message):
                sent_messages.append(json.loads(message))

            def run_forever(self):
                if self.on_open:
                    self.on_open(self)

            def close(self):
                return None

        client = QmtProxyWsClient(url="ws://localhost:9001/ws", websocket_app_factory=_FakeApp)

        session = client.subscribe_quotes(["600000.SH", "601318.SH"], on_quote=lambda payload: payload)
        session.wait_started(timeout=1.0)
        session.stop()

        self.assertEqual(sent_messages[0]["action"], "subscribe")
        self.assertEqual(sent_messages[0]["symbols"], ["600000.SH", "601318.SH"])


if __name__ == "__main__":
    unittest.main()
