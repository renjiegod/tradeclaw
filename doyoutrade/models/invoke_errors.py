"""Normalize LLM invoke failures for logs, persistence, and OTel."""

from __future__ import annotations

import traceback
from typing import Any

_TRACEBACK_MAX = 8000
_OTEL_STATUS_MAX = 512
_BODY_PREVIEW_MAX = 2000
# Larger cap when persisting model_invocations.response_payload on failures (HTTP error pages, JSON errors).
_MODEL_INVOCATION_BODY_PREVIEW_MAX = 65536


def _try_request_url_from_exception(exc: BaseException) -> str | None:
    try:
        req = exc.request
    except Exception:
        return None
    if req is None:
        return None
    url = getattr(req, "url", None)
    if url is None:
        return None
    return str(url)


def _request_url_from_exception_chain(exc: BaseException) -> str | None:
    """Best-effort HTTP request URL from httpx/httpcore (and similar) exception chains."""
    seen: set[int] = set()

    def visit(e: BaseException | None) -> str | None:
        if e is None or id(e) in seen:
            return None
        seen.add(id(e))
        u = _try_request_url_from_exception(e)
        if u:
            return u
        c = e.__cause__
        if c is not None:
            got = visit(c)
            if got:
                return got
        ctx = e.__context__
        if ctx is not None and ctx is not c:
            return visit(ctx)
        return None

    return visit(exc)


def _truncate_body_preview(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _body_preview_from_response_obj(resp: Any, *, max_len: int) -> str | None:
    if resp is None:
        return None
    text = getattr(resp, "text", None)
    if isinstance(text, str) and text.strip():
        return _truncate_body_preview(text, max_len)
    content = getattr(resp, "content", None)
    if isinstance(content, (bytes, bytearray)) and content:
        decoded = bytes(content)[: max_len + 64].decode("utf-8", errors="replace")
        return _truncate_body_preview(decoded, max_len)
    return None


def _body_preview_from_single_exception(exc: BaseException, *, max_len: int) -> str | None:
    raw_body = getattr(exc, "body", None)
    if isinstance(raw_body, (bytes, bytearray)):
        decoded = bytes(raw_body)[: max_len + 64].decode("utf-8", errors="replace")
        return _truncate_body_preview(decoded, max_len)
    if isinstance(raw_body, str) and raw_body.strip():
        return _truncate_body_preview(raw_body, max_len)
    resp = getattr(exc, "response", None)
    preview = _body_preview_from_response_obj(resp, max_len=max_len)
    if preview:
        return preview
    return None


def _http_status_from_exception_chain(exc: BaseException) -> int | None:
    seen: set[int] = set()

    def visit(e: BaseException | None) -> int | None:
        if e is None or id(e) in seen:
            return None
        seen.add(id(e))
        sc = getattr(e, "status_code", None)
        if sc is not None:
            try:
                return int(sc)
            except (TypeError, ValueError):
                pass
        resp = getattr(e, "response", None)
        if resp is not None:
            sc2 = getattr(resp, "status_code", None)
            if sc2 is not None:
                try:
                    return int(sc2)
                except (TypeError, ValueError):
                    pass
        c = e.__cause__
        if c is not None:
            inner = visit(c)
            if inner is not None:
                return inner
        ctx = e.__context__
        if ctx is not None and ctx is not c:
            return visit(ctx)
        return None

    return visit(exc)


def _body_preview_from_exception_chain(exc: BaseException, *, max_len: int) -> str | None:
    """Best-effort HTTP (or SDK) response body text from an exception and its cause/context chain."""
    seen: set[int] = set()

    def visit(e: BaseException | None) -> str | None:
        if e is None or id(e) in seen:
            return None
        seen.add(id(e))
        got = _body_preview_from_single_exception(e, max_len=max_len)
        if got:
            return got
        c = e.__cause__
        if c is not None:
            inner = visit(c)
            if inner:
                return inner
        ctx = e.__context__
        if ctx is not None and ctx is not c:
            return visit(ctx)
        return None

    return visit(exc)


def adapter_invoke_endpoint_url(adapter: Any) -> str | None:
    """Resolve a display URL for the model HTTP endpoint (LM Studio, OpenAI-compatible, Anthropic, etc.)."""
    from doyoutrade.models.recording import RecordingModelAdapter

    cur: Any = adapter
    for _ in range(16):
        if cur is None:
            return None
        for attr in ("api_host", "base_url"):
            val = getattr(cur, attr, None)
            if isinstance(val, str):
                s = val.strip()
                if s:
                    if attr == "api_host" and "://" not in s:
                        return f"http://{s}"
                    return s
        for client_attr in ("sync_client", "async_client", "client"):
            cli = getattr(cur, client_attr, None)
            if cli is None:
                continue
            bu = getattr(cli, "base_url", None)
            if bu is not None:
                s = str(bu).strip()
                if s:
                    return s
        if isinstance(cur, RecordingModelAdapter):
            cur = cur._inner
            continue
        break
    return None


def exception_to_invoke_error(
    exc: BaseException,
    *,
    code: str = "chat_ainvoke_failed",
    adapter: Any | None = None,
    body_preview_max: int | None = None,
) -> dict[str, Any]:
    """Build a JSON-friendly error dict from an exception (for strategies, worker, API).

    Intended to be called from an ``except`` block so ``traceback.format_exc()`` matches the failure.

    ``body_preview_max`` overrides the default cap for ``body_preview`` (e.g. larger when persisting
    model invocation rows). When the top-level exception has no body, the cause/context chain is
    searched (e.g. ``ValueError`` wrapping ``httpx.HTTPStatusError``).
    """
    tb = traceback.format_exc()
    if len(tb) > _TRACEBACK_MAX:
        tb = tb[-_TRACEBACK_MAX:]

    msg = str(exc).strip()
    if not msg:
        msg = repr(exc)

    err: dict[str, Any] = {
        "code": code,
        "type": type(exc).__name__,
        "message": msg,
        "traceback": tb,
    }

    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        resp = getattr(exc, "response", None)
        if resp is not None:
            status_code = getattr(resp, "status_code", None)
    if status_code is None:
        status_code = _http_status_from_exception_chain(exc)
    if status_code is not None:
        try:
            err["http_status"] = int(status_code)
        except (TypeError, ValueError):
            pass

    if body_preview_max is None:
        cap = _BODY_PREVIEW_MAX
    else:
        cap = int(body_preview_max)
    body_preview: str | None = None
    if cap > 0:
        body_preview = _body_preview_from_single_exception(exc, max_len=cap)
        if body_preview is None:
            body_preview = _body_preview_from_exception_chain(exc, max_len=cap)

    if body_preview:
        err["body_preview"] = body_preview

    request_id = getattr(exc, "request_id", None)
    if isinstance(request_id, str) and request_id.strip():
        err["request_id"] = request_id.strip()

    url = _request_url_from_exception_chain(exc)
    if not url and adapter is not None:
        url = adapter_invoke_endpoint_url(adapter)
    if url:
        err["url"] = url

    return err


def model_invocation_failure_response_payload(
    exc: BaseException,
    *,
    adapter: Any | None = None,
) -> dict[str, Any]:
    """Persisted as ``model_invocations.response_payload`` when ``ok`` is false.

    Wraps :func:`exception_to_invoke_error` with a larger body cap so gateway/SDK error JSON is kept.
    """
    return {
        "error": exception_to_invoke_error(
            exc,
            code="model_invocation_failed",
            adapter=adapter,
            body_preview_max=_MODEL_INVOCATION_BODY_PREVIEW_MAX,
        ),
    }


def failure_message_from_error(err: dict[str, Any]) -> str:
    """One-line summary for ``failure_message``, alerts, and OTel span status."""
    if not err:
        return "unknown_error"
    typ = str(err.get("type") or "").strip()
    msg = str(err.get("message") or "").strip()
    code = str(err.get("code") or "").strip()
    if not msg:
        msg = code or "unknown_error"
    line = f"{typ}: {msg}" if typ else msg
    hs = err.get("http_status")
    if hs is not None:
        line = f"{line} (http {hs})"
    url = str(err.get("url") or "").strip()
    if url:
        line = f"{line} @ {url}"
    return line


def otel_status_description_from_error(err: dict[str, Any]) -> str:
    """Bounded description for ``Span.set_status`` (backends may truncate)."""
    s = failure_message_from_error(err)
    if len(s) <= _OTEL_STATUS_MAX:
        return s
    return s[: _OTEL_STATUS_MAX - 1] + "…"
