"""HTTP client mode for ``doyoutrade-cli`` business paths.

Business commands run against the API server so the server process remains
the single owner of runtime state, schedulers, repositories, channels, and
other integrations. The CLI stays a command-line argument and envelope
adapter.

This module is the thin HTTP layer those commands use. It:

* Resolves the API base URL (env ``DOYOUTRADE_API_URL`` →
  ``cfg.api.base_url`` → derived from ``cfg.server``).
* Issues the HTTP call with ``httpx.AsyncClient``.
* Translates the response (or transport failure) into the same envelope /
  exit-code contract the in-process tool path uses, so skill docs /
  shell pipelines see one shape regardless of mode.

The envelope contract is single-sourced from ``_envelope.py``:

* Success body → ``success_envelope(data=body, summary="")``
* HTTP 204 / empty body → ``success_envelope(data=None, summary="")``
* HTTP 400 / 422 → ``error_code="validation_error"`` (EXIT_VALIDATION)
* HTTP 404 → callable-supplied ``not_found_error_code`` (default
  ``"not_found"``, which ``exit_code_for_error`` routes to EXIT_NOT_FOUND)
* HTTP 5xx → ``error_code="server_error"`` (EXIT_FAILURE)
* Connection / timeout failure → ``error_code="api_unavailable"`` with a
  hint pointing at how to start the server. Operators must see this as a
  distinct failure mode rather than a generic "tool error".
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from doyoutrade.cli._envelope import (
    EXIT_OK,
    Meta,
    error_envelope,
    exit_code_for_error,
    success_envelope,
)


_DEFAULT_TIMEOUT_SECONDS = 15.0


def resolve_api_base_url() -> str:
    """Return the API base URL the CLI should talk to.

    Order: ``DOYOUTRADE_API_URL`` → ``cfg.api.base_url`` →
    ``http://<server.host>:<server.port>`` (with ``0.0.0.0`` rewritten to
    ``127.0.0.1`` since you cannot dial the wildcard). Always returns a
    URL without a trailing slash so callers can compose paths with
    ``f"{base}/...".
    """

    env = os.environ.get("DOYOUTRADE_API_URL")
    if env:
        return env.rstrip("/")
    env = os.environ.get("DOYOUTRADE_API_BASE_URL")
    if env:
        return env.rstrip("/")

    from doyoutrade.config import get_config

    cfg = get_config()
    api_url = getattr(cfg.api, "base_url", None)
    if api_url:
        return api_url.rstrip("/")

    host = cfg.server.host or "127.0.0.1"
    # 0.0.0.0 is a valid bind address but not dialable from a client. Same
    # for the IPv6 wildcard.
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{cfg.server.port}"


def build_context_headers(meta: Meta) -> dict[str, str]:
    """Build headers that preserve agent/session/debug/trace context over HTTP."""

    headers: dict[str, str] = {}
    if meta.agent_id:
        headers["X-DOYOUTRADE-Agent-Id"] = meta.agent_id
        headers["X-DOYOUTRADE-Calling-Agent-Id"] = meta.agent_id
    if meta.session_id:
        headers["X-DOYOUTRADE-Session-Id"] = meta.session_id
        headers["X-DOYOUTRADE-Calling-Session-Id"] = meta.session_id
    if meta.debug_session_id:
        headers["X-DOYOUTRADE-Debug-Session-Id"] = meta.debug_session_id
    if meta.run_id:
        headers["X-DOYOUTRADE-Run-Id"] = meta.run_id

    traceparent = os.environ.get("TRACEPARENT") or os.environ.get("traceparent")
    tracestate = os.environ.get("TRACESTATE") or os.environ.get("tracestate")
    if traceparent:
        headers["traceparent"] = traceparent
    if tracestate:
        headers["tracestate"] = tracestate
    return headers


def _extract_error_message(response: httpx.Response) -> str:
    """Pull a human-readable error message out of a FastAPI error body.

    FastAPI's ``HTTPException`` renders as ``{"detail": "..."}``. Some
    routes also emit list/object detail bodies (e.g. validation errors
    from pydantic). We keep the message terse here — the full body is
    re-attached to ``error.extra.body`` for debugging.
    """

    try:
        body = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text or f"HTTP {response.status_code}"

    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(detail, list) and detail:
            # pydantic validation error shape: list of {loc, msg, type}
            first = detail[0]
            if isinstance(first, dict) and isinstance(first.get("msg"), str):
                return first["msg"]
        if isinstance(body.get("message"), str):
            return body["message"]
    text = (response.text or "").strip()
    return text or f"HTTP {response.status_code}"


def _extract_structured_detail(response: httpx.Response) -> dict[str, Any] | None:
    """Return dict-shaped FastAPI ``detail`` payloads when present."""

    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    if not isinstance(detail, dict):
        return None
    if not isinstance(detail.get("error_code"), str):
        return None
    return detail


def _response_body(response: httpx.Response) -> dict[str, Any] | None:
    """Decode a 2xx JSON body into the envelope's ``data`` dict.

    Non-object bodies (a bare list, a string) are wrapped under
    ``{"items": ...}`` / ``{"value": ...}`` so the success envelope
    always carries a dict — matches the in-process tool path's habit of
    returning dicts.
    """

    if response.status_code == 204 or not response.content:
        return None
    try:
        body = response.json()
    except ValueError:
        return {"_raw": response.text}
    if isinstance(body, dict):
        return body
    if isinstance(body, list):
        return {"items": body, "total": len(body)}
    return {"value": body}


async def invoke_api(
    method: str,
    path: str,
    *,
    json: Any = None,
    params: dict[str, Any] | None = None,
    meta: Meta | None = None,
    not_found_error_code: str = "not_found",
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], int]:
    """Issue one HTTP call and return ``(envelope, exit_code)``.

    ``path`` should start with ``/``. ``json`` is the request body for
    POST/PUT/PATCH; ``params`` is the query-string mapping.
    ``not_found_error_code`` lets callers ship a domain-specific token
    (``"cron_job_not_found"``) so the envelope stays as actionable as the
    in-process tool errors — ``exit_code_for_error`` already routes any
    ``*_not_found`` code to ``EXIT_NOT_FOUND``.
    """

    base = resolve_api_base_url()
    url = f"{base}{path}"
    if meta is None:
        meta = Meta()

    # Forward calling-session context so the API can enforce flags
    # the in-process tools enforce out of the box (e.g. block a cron-
    # fired session from creating more crons via the CLI; see
    # ``create_agent_cron_job`` in doyoutrade/api/app.py).
    headers = build_context_headers(meta)

    try:
        # The CLI talks to a configured Doyoutrade API endpoint, usually
        # localhost. Do not inherit shell/system proxy settings here: a
        # refused local connection must surface as api_unavailable, not as a
        # proxy-generated HTTP 5xx.
        async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
            response = await client.request(
                method, url, json=json, params=params, headers=headers,
            )
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        envelope = error_envelope(
            error_code="api_unavailable",
            error_type=type(exc).__name__,
            message=(
                f"Cannot reach doyoutrade API at {base} ({exc}). "
                f"Start the server (uvicorn doyoutrade.api.app:app) or set "
                f"DOYOUTRADE_API_URL / api.base_url to a reachable host."
            ),
            hint="Start the doyoutrade API server or set DOYOUTRADE_API_URL / api.base_url to a reachable host.",
            meta=meta,
        )
        return envelope, exit_code_for_error("api_unavailable")
    except httpx.TimeoutException as exc:
        envelope = error_envelope(
            error_code="api_timeout",
            error_type=type(exc).__name__,
            message=f"Timed out after {timeout_seconds}s calling {method} {url}: {exc}",
            meta=meta,
        )
        return envelope, exit_code_for_error("api_timeout")
    except httpx.HTTPError as exc:
        # Catch-all for other transport errors (TLS, invalid URL, …) so the
        # CLI never prints a bare Python traceback.
        envelope = error_envelope(
            error_code="api_transport_error",
            error_type=type(exc).__name__,
            message=f"HTTP transport error calling {method} {url}: {exc}",
            meta=meta,
        )
        return envelope, exit_code_for_error("api_transport_error")

    status = response.status_code

    if 200 <= status < 300:
        envelope = success_envelope(_response_body(response), "", meta=meta)
        return envelope, EXIT_OK

    detail = _extract_structured_detail(response)
    message = _extract_error_message(response)
    hint: str | None = None
    repair_hints: list[str] | None = None
    if detail is not None:
        message = str(detail.get("message") or message)
        hint_value = detail.get("hint")
        if isinstance(hint_value, str) and hint_value:
            hint = hint_value
        repair_hints_value = detail.get("repair_hints")
        if isinstance(repair_hints_value, list):
            repair_hints = [
                str(item) for item in repair_hints_value if isinstance(item, str) and item
            ] or None

    if status in (400, 422):
        code = (
            str(detail.get("error_code"))
            if detail is not None
            else "validation_error"
        )
    elif status == 404:
        code = not_found_error_code
    elif status == 409:
        code = "conflict"
    elif status == 503:
        code = "server_unavailable"
    elif status >= 500:
        code = "server_error"
    else:
        code = f"http_{status}"

    envelope = error_envelope(
        error_code=code,
        error_type=f"HTTP{status}",
        message=message,
        hint=hint,
        repair_hints=repair_hints,
        meta=meta,
        extra={"http_status": status, "url": url},
    )
    return envelope, exit_code_for_error(code)


__all__ = ["build_context_headers", "invoke_api", "resolve_api_base_url"]
