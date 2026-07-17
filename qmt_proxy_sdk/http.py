from __future__ import annotations

import asyncio
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
    RateLimitedError,
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

# 429 限流自动重试的缺省参数（云网关 doyoutrade-cloud 分钟限流 / 日配额）。
DEFAULT_RATE_LIMIT_RETRIES = 3
DEFAULT_RATE_LIMIT_MAX_WAIT = 120.0


def _parse_retry_after(value: str | None) -> float | None:
    """把 ``Retry-After`` 头解析为非负秒数；无法解析时返回 ``None``。"""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return None
    return max(0.0, seconds)


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
        rate_limit_retries: int = DEFAULT_RATE_LIMIT_RETRIES,
        rate_limit_max_wait: float = DEFAULT_RATE_LIMIT_MAX_WAIT,
    ) -> None:
        # 429 限流韧性：最多自动重试 ``rate_limit_retries`` 次；``Retry-After``
        # 超过 ``rate_limit_max_wait`` 秒时不等待、直接抛 RateLimitedError 让上层决定。
        self._rate_limit_retries = rate_limit_retries
        self._rate_limit_max_wait = rate_limit_max_wait
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
        """发起请求；对 429 限流自动退避重试。

        这些端点均为数据读取（POST 也是幂等查询），统一重试是安全的。
        重试节奏：优先服务端 ``Retry-After``，缺失时指数退避 1/2/4s；
        ``Retry-After`` 超过 ``rate_limit_max_wait`` 时不等待、直接抛出。
        """
        attempt = 0
        while True:
            try:
                return await self._request_once(
                    method, path, params=params, json=json, headers=headers
                )
            except RateLimitedError as exc:
                retry_after = exc.retry_after
                if retry_after is not None and retry_after > self._rate_limit_max_wait:
                    logger.warning(
                        "qmt http rate limited, Retry-After=%.0fs exceeds max wait %.0fs, "
                        "not retrying method=%s path=%s error_code=%s",
                        retry_after,
                        self._rate_limit_max_wait,
                        method.upper(),
                        path,
                        exc.error_code,
                    )
                    raise
                if attempt >= self._rate_limit_retries:
                    logger.warning(
                        "qmt http rate limited, retries exhausted after %d attempts "
                        "method=%s path=%s error_code=%s",
                        attempt,
                        method.upper(),
                        path,
                        exc.error_code,
                    )
                    raise
                attempt += 1
                delay = retry_after if retry_after is not None else float(2 ** (attempt - 1))
                logger.warning(
                    "qmt http rate limited (429), retrying in %.1fs (attempt %d/%d) "
                    "method=%s path=%s error_code=%s",
                    delay,
                    attempt,
                    self._rate_limit_retries,
                    method.upper(),
                    path,
                    exc.error_code,
                )
                logger.debug("qmt http rate limit payload=%r", exc.payload)
                await asyncio.sleep(delay)

    async def _request_once(
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
                error = self._map_error(
                    response.status_code,
                    payload,
                    retry_after=_parse_retry_after(response.headers.get("Retry-After")),
                )
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

    def _map_error(
        self, status_code: int, payload: Any, *, retry_after: float | None = None
    ) -> QmtProxyError:
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
        if status_code == 429:
            return RateLimitedError(
                message,
                retry_after=retry_after,
                error_code=self._extract_error_code(payload),
                **kwargs,
            )
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

    def _extract_error_code(self, payload: Any) -> str | None:
        """云网关 429 响应 JSON 的 ``error_code``（如 rate_limited / daily_quota_exceeded）。"""
        if not isinstance(payload, dict):
            return None
        error_code = payload.get("error_code")
        if isinstance(error_code, str) and error_code:
            return error_code
        detail = payload.get("detail")
        if isinstance(detail, dict):
            nested = detail.get("error_code")
            if isinstance(nested, str) and nested:
                return nested
        return None

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
