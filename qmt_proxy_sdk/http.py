from __future__ import annotations

from typing import Any

import httpx

from qmt_proxy_sdk.exceptions import (
    AuthenticationError,
    ClientError,
    QmtProxyError,
    RequestValidationError,
    ServerError,
    TransportError,
)


class AsyncHttpTransport:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 60.0,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        merged_headers = dict(headers or {})
        if api_key:
            merged_headers.setdefault("Authorization", f"Bearer {api_key}")
        merged_headers.setdefault("Content-Type", "application/json")

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
        try:
            response = await self._client.request(
                method=method,
                url=path,
                params=params,
                json=json,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise TransportError(str(exc)) from exc

        payload = self._decode_response(response)

        if response.is_error:
            raise self._map_error(response.status_code, payload)

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
