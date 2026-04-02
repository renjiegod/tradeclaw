from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener


class QmtProxyRestClient:
    """Thin REST client for qmt-proxy endpoints."""

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        timeout_seconds: float = 5.0,
        opener=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = float(timeout_seconds)
        self.opener = opener or build_opener()

    def fetch_history(self, symbol: str, start_time: str, end_time: str, interval: str = "1m"):
        payload = self._request_json(
            method="GET",
            path="/history",
            params={
                "symbol": symbol,
                "start_time": start_time,
                "end_time": end_time,
                "interval": interval,
            },
        )
        return _extract_data(payload)

    def fetch_account(self):
        payload = self._request_json(method="GET", path="/account")
        return _extract_data(payload)

    def fetch_positions(self):
        payload = self._request_json(method="GET", path="/positions")
        return _extract_data(payload)

    def fetch_latest_quotes(self, symbols):
        payload = self._request_json(
            method="GET",
            path="/quotes/latest",
            params={"symbols": ",".join(symbols)},
        )
        return _extract_data(payload)

    def _request_json(self, method: str, path: str, params=None, payload=None):
        params = params or {}
        query = urlencode(params)
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request_data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            request_data = json.dumps(payload).encode("utf-8")

        request = Request(url=url, data=request_data, headers=headers, method=method)

        try:
            response = self.opener.open(request, timeout=self.timeout_seconds)
        except (HTTPError, URLError) as exc:
            raise RuntimeError(f"qmt-proxy request failed: {exc}") from exc

        body = response.read()
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))


@dataclass
class QuoteSubscriptionSession:
    app: object
    thread: threading.Thread
    started: threading.Event

    def wait_started(self, timeout: float = 2.0) -> bool:
        return self.started.wait(timeout=timeout)

    def stop(self):
        if hasattr(self.app, "close"):
            self.app.close()
        self.thread.join(timeout=1.0)


class QmtProxyWsClient:
    """WebSocket client wrapper for quote subscription."""

    def __init__(self, url: str, token: Optional[str] = None, websocket_app_factory=None):
        self.url = url
        self.token = token
        self.websocket_app_factory = websocket_app_factory

    def subscribe_quotes(self, symbols, on_quote: Callable[[dict], None], on_error: Optional[Callable] = None):
        app_factory = self.websocket_app_factory
        if app_factory is None:
            try:
                import websocket
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("websocket-client is not installed") from exc
            app_factory = websocket.WebSocketApp

        started = threading.Event()
        headers = [f"Authorization: Bearer {self.token}"] if self.token else None

        def _on_open(ws):
            message = json.dumps({"action": "subscribe", "symbols": list(symbols)})
            ws.send(message)
            started.set()

        def _on_message(_ws, message):
            payload = json.loads(message)
            on_quote(payload)

        def _on_error(_ws, error):
            if on_error is not None:
                on_error(error)

        app = app_factory(
            self.url,
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
            on_close=None,
            header=headers,
        )
        thread = threading.Thread(target=app.run_forever, name="qmt-ws", daemon=True)
        thread.start()
        return QuoteSubscriptionSession(app=app, thread=thread, started=started)


def _extract_data(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload
