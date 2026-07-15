from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import httpx
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from qmt_proxy_sdk.exceptions import (
    AuthenticationError,
    ClientError,
    QmtProxyError,
    RequestValidationError,
    ServerError,
    TransportError,
)


logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# Same key as ``doyoutrade.observability.debug_span_export.ATTR_EVENT_PAYLOAD_JSON`` so exported
# debug spans deserialize event payloads in the UI.
_EVENT_PAYLOAD_JSON = "doyoutrade.event.payload_json"
_MAX_BODY_CHARS = 32_000


def _json_default(obj: Any) -> Any:
    """Match doyoutrade ``json_default_with_decimals`` for HTTP/debug previews (SDK has no doyoutrade dep)."""
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            return "0"
        s = format(obj, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s if s else "0"
    return str(obj)


def _truncate_text(text: str, limit: int = _MAX_BODY_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 15)] + "...[truncated]"


def _json_body_preview(body: Any) -> str | None:
    if body is None:
        return None
    raw = json.dumps(body, default=_json_default, ensure_ascii=False)
    return _truncate_text(raw)


def _redact_headers(headers: dict[str, str] | None) -> dict[str, str] | None:
    if not headers:
        return None
    out = {k: ("[REDACTED]" if k.lower() == "authorization" else v) for k, v in headers.items()}
    return out


def _span_json_event(span: Span, name: str, payload: dict[str, Any]) -> None:
    if not span.is_recording():
        return
    span.add_event(
        name,
        {_EVENT_PAYLOAD_JSON: json.dumps(payload, default=_json_default, ensure_ascii=False)},
    )


def _request_full_url(client: httpx.AsyncClient, path: str) -> str:
    try:
        return str(client.base_url.join(path))
    except Exception:
        return path


class AsyncHttpTransport:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 60.0,
        headers: dict[str, str] | None = None,
        terminal_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        merged_headers = dict(headers or {})
        if api_key:
            merged_headers.setdefault("Authorization", f"Bearer {api_key}")
        # 选择目标 QMT 终端（多终端 qmt-proxy 部署）；缺省由服务端走默认终端。
        if terminal_id:
            merged_headers.setdefault("X-QMT-Terminal", terminal_id)
        merged_headers.setdefault("Content-Type", "application/json")
        self._terminal_id = terminal_id

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                base_url=base_url,
                headers=merged_headers,
                timeout=timeout,
                transport=transport,
            )
            self._owns_client = True

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        with tracer.start_as_current_span("qmt.http.request") as span:
            span.set_attribute("http.method", method.upper())
            span.set_attribute("url.path", path)
            # 哪个 QMT 终端承接了这次调用（多终端 qmt-proxy 路由可见性）；
            # 缺省 "default" 表示走服务端默认终端。
            span.set_attribute("qmt.terminal_id", self._terminal_id or "default")
            full_url = _request_full_url(self._client, path)
            _span_json_event(
                span,
                "qmt.http.request_sent",
                {
                    "method": method.upper(),
                    "path": path,
                    "url": full_url,
                    "params": params or {},
                    "request_body": _json_body_preview(json),
                    "request_headers": _redact_headers(headers),
                },
            )
            logger.info("qmt http request started method=%s path=%s", method.upper(), path)
            try:
                response = await self._client.request(
                    method=method,
                    url=path,
                    params=params,
                    json=json,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                # ``httpx`` timeout exceptions (e.g. ReadTimeout) carry an empty
                # ``str(exc)`` — fall back to the class name so the error surfaces
                # as ``TransportError: ReadTimeout`` instead of an opaque
                # ``TransportError('')`` in traces / debug events.
                detail = str(exc).strip() or type(exc).__name__
                _span_json_event(
                    span,
                    "qmt.http.transport_error",
                    {"error_type": type(exc).__name__, "message": detail, "url": full_url},
                )
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, detail))
                logger.exception(
                    "qmt http transport failed method=%s path=%s error_type=%s detail=%s",
                    method.upper(),
                    path,
                    type(exc).__name__,
                    detail,
                )
                raise TransportError(detail) from exc

            span.set_attribute("http.response.status_code", response.status_code)
            try:
                raw_response_text = response.text
            except Exception as decode_exc:  # pragma: no cover - defensive
                raw_response_text = f"<response body decode error: {decode_exc}>"

            _span_json_event(
                span,
                "qmt.http.response_received",
                {
                    "status_code": response.status_code,
                    "content_type": response.headers.get("Content-Type") or "",
                    "response_body": _truncate_text(raw_response_text),
                },
            )

            payload = self._decode_response(response)

            if response.is_error:
                error = self._map_error(response.status_code, payload)
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, str(error)))
                logger.warning(
                    "qmt http request failed method=%s path=%s status_code=%s",
                    method.upper(),
                    path,
                    response.status_code,
                )
                raise error

            span.set_status(Status(StatusCode.OK))
            logger.info(
                "qmt http request completed method=%s path=%s status_code=%s",
                method.upper(),
                path,
                response.status_code,
            )
            if isinstance(payload, dict) and self._looks_like_envelope(payload):
                return payload.get("data")

            return payload

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _decode_response(self, response: httpx.Response) -> Any:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type.lower():
            return response.json()

        try:
            return response.json()
        except ValueError:
            return response.text

    def _looks_like_envelope(self, payload: dict[str, Any]) -> bool:
        return "success" in payload and "message" in payload and "code" in payload

    def _map_error(self, status_code: int, payload: Any) -> QmtProxyError:
        message = self._extract_message(payload)
        kwargs = {
            "status_code": status_code,
            "payload": payload,
            "code": self._extract_code(payload),
        }

        if status_code in (401, 403):
            return AuthenticationError(message, **kwargs)
        if status_code == 422:
            return RequestValidationError(message, **kwargs)
        if 400 <= status_code < 500:
            return ClientError(message, **kwargs)
        return ServerError(message, **kwargs)

    def _extract_message(self, payload: Any) -> str:
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, dict):
                return str(detail.get("message") or detail)
            if detail:
                return str(detail)
            if payload.get("message"):
                return str(payload["message"])
        if payload:
            return str(payload)
        return "Request failed"

    def _extract_code(self, payload: Any) -> int | None:
        if not isinstance(payload, dict):
            return None

        code = payload.get("code")
        if code is None:
            return None

        try:
            return int(code)
        except (TypeError, ValueError):
            return None
