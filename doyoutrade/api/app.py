from __future__ import annotations

from datetime import date, datetime, timezone
import json
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace as trace_api
from starlette.exceptions import HTTPException as StarletteHTTPException

from doyoutrade.assistant import attachments as _attachments
from doyoutrade.assistant.cron_manager import AgentCronManager
from doyoutrade.assistant.repository import normalize_tool_configs
from doyoutrade.assistant.session_export import build_assistant_session_export
from doyoutrade.assistant.prompt_templates import get_prompt_template, list_prompt_templates
from doyoutrade.core.models import signal_context_from_intent_json
from doyoutrade.data.instrument_catalog.validation import (
    CatalogError,
    CatalogNotTradableError,
    CatalogValidationError,
)
from doyoutrade.debug import emit_debug_event
from doyoutrade.observability import get_logger, get_tracer
from doyoutrade.persistence.errors import (
    AgentInUseError,
    BuiltinAgentImmutableError,
    PersistenceError,
    RecordNotFoundError,
    StateConflictError,
)
from doyoutrade.runtime.cycle_task import validate_api_task_settings, validate_optional_task_settings
from doyoutrade.runtime.triggers import (
    TriggerValidationError,
    compute_next_fire,
    validate_trigger_input,
)
from doyoutrade.monitoring.conditions import MonitorConditionError, validate_condition_tree
from doyoutrade.runtime.trigger_delivery import deliver_trigger_result, render_trigger_digest
from doyoutrade.strategy_registry import StrategyDefinitionCreate


logger = get_logger(__name__)
tracer = get_tracer(__name__)


def _parse_structured_value_error(exc: ValueError) -> dict[str, Any] | None:
    raw = str(exc).strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("error_code"), str):
        return None
    return payload


def _cloud_forced_max_turns() -> int | None:
    """In a cloud deployment, the agent tool-call-round cap (max_turns) is set
    by the operator in the dytc admin console, not by end users. The copilot
    spawner injects the resolved value as DOYOUTRADE_CLOUD_AGENT_MAX_TURNS; when
    present (and valid) the agent CRUD endpoints clamp max_turns to it so a user
    cannot raise it via the UI, a direct API call, or a custom agent.

    Returns the forced value, or None when not in cloud mode / unset / invalid
    (local single-machine deployments keep the user-controlled behavior)."""
    if (os.environ.get("DOYOUTRADE_DEPLOYMENT_MODE") or "local").strip().lower() != "cloud":
        return None
    raw = os.environ.get("DOYOUTRADE_CLOUD_AGENT_MAX_TURNS")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "ignoring invalid DOYOUTRADE_CLOUD_AGENT_MAX_TURNS=%r (not an int)", raw
        )
        return None
    if value < 1:
        logger.warning(
            "ignoring out-of-range DOYOUTRADE_CLOUD_AGENT_MAX_TURNS=%r (must be >= 1)", raw
        )
        return None
    return value


def _normalize_optional_string(value, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    return normalized or None


def _normalize_required_string(value, *, field_name: str) -> str:
    normalized = _normalize_optional_string(value, field_name=field_name)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return normalized


def _local_market_sync_error_response(
    *,
    status_code: int,
    error_code: str,
    error_type: str,
    error_message: str,
    hint: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_build_error_payload(
            status_code=status_code,
            detail={
                "error_code": error_code,
                "error_type": error_type,
                "error_message": error_message,
                "hint": hint,
            },
        ),
    )


def _current_error_trace_id() -> str | None:
    ctx = trace_api.get_current_span().get_span_context()
    if not getattr(ctx, "is_valid", False) or not getattr(ctx, "trace_id", 0):
        return None
    return format(ctx.trace_id, "032x")


def _error_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _stringify_error_detail(detail: Any) -> str | None:
    if detail is None:
        return None
    if isinstance(detail, str):
        normalized = detail.strip()
        return normalized or None
    if isinstance(detail, dict):
        for key in ("message", "error_message", "detail"):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        try:
            return json.dumps(detail, ensure_ascii=False, indent=2)
        except TypeError:
            return str(detail)
    if isinstance(detail, list):
        try:
            return json.dumps(detail, ensure_ascii=False, indent=2)
        except TypeError:
            return str(detail)
    return str(detail)


def _build_error_payload(*, status_code: int, detail: Any, error_type: str | None = None) -> dict[str, Any]:
    error_code = None
    hint = None
    message = _stringify_error_detail(detail) or f"HTTP {status_code}"
    derived_error_type = error_type
    if isinstance(detail, dict):
        raw_code = detail.get("error_code")
        if isinstance(raw_code, str) and raw_code.strip():
            error_code = raw_code.strip()
        raw_type = detail.get("error_type")
        if isinstance(raw_type, str) and raw_type.strip():
            derived_error_type = raw_type.strip()
        raw_hint = detail.get("hint")
        if isinstance(raw_hint, str) and raw_hint.strip():
            hint = raw_hint.strip()
    return {
        "detail": detail,
        "status_code": status_code,
        "error_code": error_code,
        "error_type": derived_error_type,
        "error_message": message,
        "hint": hint,
        "trace_id": _current_error_trace_id(),
        "timestamp": _error_timestamp(),
    }


def _catalog_error_detail(exc: CatalogError) -> dict[str, Any]:
    # Structured 400 detail for instrument-catalog validation failures.
    # missing_symbols  -> symbol absent from catalog (CatalogValidationError)
    # non_tradable_symbols -> symbol present but is_tradable=False, e.g. an
    #   index submitted as a task / backtest universe member
    detail: dict[str, Any] = {"message": str(exc)}
    if isinstance(exc, CatalogValidationError):
        detail["missing_symbols"] = exc.missing_symbols
        detail["hint"] = exc.hint
    if isinstance(exc, CatalogNotTradableError):
        detail["non_tradable_symbols"] = exc.non_tradable_symbols
    return detail


def _normalize_symbol_list(value, *, field_name: str) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError(
            f"{field_name} must be a list of strings or a comma-separated string",
        )

    normalized = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} must contain only strings")
        symbol = item.strip()
        if symbol:
            normalized.append(symbol)
    return normalized


def _strip_equity_curve_from_task(row):
    """Return a shallow copy of a task payload with ``backtest_summary.equity_curve`` removed.

    The list endpoint trims the per-bar equity series to keep ``GET /tasks`` cheap; the
    detail endpoint serves the full curve. ``equity_curve_meta`` (downsampled flag +
    ``raw_length``) is always retained so the frontend can display a breadcrumb.
    """
    if not isinstance(row, dict):
        return row
    summary = row.get("backtest_summary")
    if not isinstance(summary, dict):
        return row
    if "equity_curve" not in summary:
        return row
    cleaned = dict(row)
    summary_copy = dict(summary)
    summary_copy.pop("equity_curve", None)
    cleaned["backtest_summary"] = summary_copy
    return cleaned


def _normalize_settings(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("settings must be an object or null")
    return dict(value)


def _normalize_object(value, *, field_name: str):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object or null")
    return dict(value)


def _normalize_bool(value, *, field_name: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field_name} must be a boolean")


def _normalize_int(value, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _normalize_context_compaction(value):
    payload = _normalize_object(value, field_name="context_compaction")
    if payload is None:
        return None

    string_fields = (
        "mode",
        "trigger_strategy",
    )
    bool_fields = (
        "enabled",
        "micro_compaction_enabled",
        "full_compaction_enabled",
        "allow_slash_compact",
    )
    int_fields = (
        "auto_threshold_tokens",
        "warning_threshold_tokens",
        "preserve_recent_messages",
        "preserve_recent_tool_pairs",
        "tool_result_max_chars",
    )
    allowed_fields = set(string_fields) | set(bool_fields) | set(int_fields)
    allowed_fields.add("summary_model_route_name")
    normalized = {}

    for field_name in payload:
        if field_name not in allowed_fields:
            raise ValueError(
                f"context_compaction contains unsupported field: {field_name}"
            )

    for field_name in string_fields:
        if field_name in payload:
            normalized[field_name] = _normalize_required_string(
                payload.get(field_name),
                field_name=f"context_compaction.{field_name}",
            )
    if "summary_model_route_name" in payload:
        normalized["summary_model_route_name"] = (
            _normalize_optional_string(
                payload.get("summary_model_route_name"),
                field_name="context_compaction.summary_model_route_name",
            )
            or ""
        )
    for field_name in bool_fields:
        if field_name in payload:
            normalized[field_name] = _normalize_bool(
                payload.get(field_name),
                field_name=f"context_compaction.{field_name}",
            )
    for field_name in int_fields:
        if field_name in payload:
            normalized[field_name] = _normalize_int(
                payload.get(field_name),
                field_name=f"context_compaction.{field_name}",
            )
    return normalized


def _normalize_system_prompt_template_id(value, *, field_name: str = "system_prompt_template_id") -> str | None:
    normalized = _normalize_optional_string(value, field_name=field_name)
    if normalized is None:
        return None
    if get_prompt_template(normalized) is None:
        raise ValueError(f"unknown {field_name}: {normalized}")
    return normalized


def _normalize_tool_configs(value, *, fallback_tool_names=None):
    try:
        return normalize_tool_configs(value, fallback_tool_names=fallback_tool_names)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _serialize_strategy_definition_summary(snapshot) -> dict:
    return {
        "definition_id": snapshot.definition_id,
        "name": snapshot.name,
        "current_version": snapshot.current_version,
        "api_version": snapshot.api_version,
        "parameter_schema": snapshot.parameter_schema_json,
        "default_parameters": snapshot.default_parameters_json,
        "capabilities": snapshot.capabilities_json,
        "provenance": snapshot.provenance_json,
        "code_hash": snapshot.code_hash,
        "status": snapshot.status,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
        "updated_at": snapshot.updated_at.isoformat() if snapshot.updated_at else None,
    }


_MAX_FILE_BYTES = 200 * 1024  # 200 KB


def _read_strategy_files(storage, definition_id: str, version: str | None) -> list[dict]:
    """Return a list of {path, content, skipped_reason?, size_bytes?} dicts.

    Returns [] when version is None (no finalized version yet).
    """
    if storage is None or version is None:
        return []
    from doyoutrade.persistence.strategy_storage import VersionNotFound
    try:
        vdir = storage.version_dir(definition_id, version)
    except VersionNotFound:
        return []
    files = []
    for rel_path in storage.list_files(vdir):
        abs_path = vdir / rel_path
        try:
            size = abs_path.stat().st_size
        except OSError as exc:
            logger.warning(
                "strategy_file_stat_failed definition=%s version=%s path=%s exc=%s",
                definition_id,
                version,
                rel_path,
                exc,
            )
            continue
        if size > _MAX_FILE_BYTES:
            files.append(
                {
                    "path": rel_path,
                    "content": None,
                    "skipped_reason": "too_large",
                    "size_bytes": size,
                }
            )
        else:
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = None
            files.append({"path": rel_path, "content": content})
    return files


def _serialize_strategy_definition_detail(snapshot, storage=None) -> dict:
    payload = _serialize_strategy_definition_summary(snapshot)
    payload.update(
        {
            "input_contract": snapshot.input_contract_json,
            "generation_prompt": snapshot.generation_prompt,
            "generation_model": snapshot.generation_model,
            "generation_metadata": snapshot.generation_metadata_json,
            "files": _read_strategy_files(storage, snapshot.definition_id, snapshot.current_version),
        }
    )
    return payload


async def _refresh_quote_stream(app) -> None:
    """Re-resolve the default account on the quote stream service after an
    account mutation (create / update / set-default / delete) so live quotes
    pick up the new connection without a server restart. Best-effort: a
    missing service (isolated tests) or a refresh failure is logged but never
    propagates — the account mutation itself already succeeded."""
    qss = getattr(app.state, "quote_stream_service", None)
    if qss is None:
        return
    refresh = getattr(qss, "refresh", None)
    if refresh is None:
        return
    try:
        await refresh()
    except Exception as exc:  # noqa: BLE001 — visible, non-fatal
        import logging

        logging.getLogger(__name__).warning(
            "quote stream refresh after account mutation failed (%s): %s",
            type(exc).__name__,
            exc,
        )


class _QmtProxyForwardError(Exception):
    """A forwarded qmt-proxy request failed.

    ``status_code`` / ``body`` are set only for a non-2xx *client* (4xx) upstream
    response so the caller can propagate the upstream validation envelope with
    its original status instead of collapsing it to a 502. Connection failures
    and 5xx upstream responses leave them ``None`` (caller maps to 502).
    """

    def __init__(
        self, message: str, *, status_code: int | None = None, body: dict | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _resolve_default_account(accounts: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Pick the enabled, ``is_default`` account from a ``list_accounts()`` result.

    Mirrors ``SqlAlchemyAccountRepository.get_default_account`` but works off the
    already-serialized account dicts the service exposes, so it stays mockable.
    """
    for account in accounts or []:
        if not isinstance(account, dict):
            continue
        if account.get("is_default") and account.get("enabled", True):
            return account
    return None


async def _forward_to_qmt_proxy(
    *, method: str, base_url: str, token: str, payload: dict | None = None
) -> dict:
    """Forward a config request to ``{base_url}/api/v1/config`` (Bearer auth).

    Returns the parsed JSON envelope. Raises :class:`_QmtProxyForwardError` on
    connection failure or a non-2xx upstream status — the caller maps that to a
    502. Kept module-level (not a closure) so tests can patch it directly.
    """
    import httpx

    url = base_url.rstrip("/") + "/api/v1/config"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(method, url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise _QmtProxyForwardError(
            f"qmt-proxy request failed ({type(exc).__name__}: {exc}) url={url}"
        ) from exc
    if response.status_code // 100 != 2:
        body_json: dict | None = None
        try:
            parsed = response.json()
            body_json = parsed if isinstance(parsed, dict) else None
            detail = (
                (body_json.get("message") or body_json.get("error_message") or str(body_json))
                if body_json is not None
                else str(parsed)
            )
        except ValueError:
            detail = (response.text or "")[:500]
        # 4xx = the caller's config input is invalid → propagate the upstream
        # validation envelope with its original status so the UI treats it as a
        # fixable field error; 5xx = upstream fault → caller maps to 502.
        if 400 <= response.status_code < 500:
            raise _QmtProxyForwardError(
                f"qmt-proxy returned HTTP {response.status_code}: {detail}",
                status_code=response.status_code,
                body=body_json,
            )
        raise _QmtProxyForwardError(
            f"qmt-proxy returned HTTP {response.status_code}: {detail}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise _QmtProxyForwardError(
            f"qmt-proxy returned non-JSON body: {(response.text or '')[:200]!r}"
        ) from exc


async def _qmt_proxy_config_forward(
    service, *, method: str, payload: dict | None = None
):
    """Resolve the default account and forward a config request to qmt-proxy.

    400 ``qmt_proxy_unreachable`` when there is no usable default account
    (missing / no base_url / no token); 502 ``qmt_proxy_error`` on upstream
    failure. On success returns the upstream ``data`` payload verbatim.
    """
    accounts = await service.list_accounts()
    default = _resolve_default_account(accounts)
    base_url = str((default or {}).get("base_url") or "").strip()
    token = str((default or {}).get("token") or "").strip()
    if default is None or not base_url or not token:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "qmt_proxy_unreachable",
                "error_type": "validation_error",
                "message": (
                    "qmt-proxy 不可达：请先配置默认账户的 base_url 与 token"
                    "（POST /accounts/<id>/set-default，并填写 base_url/token）。"
                ),
            },
        )
    try:
        body = await _forward_to_qmt_proxy(
            method=method, base_url=base_url, token=token, payload=payload
        )
    except _QmtProxyForwardError as exc:
        # Upstream 4xx (validation) → propagate the qmt-proxy error envelope with
        # its original status so the UI shows a fixable field error, not a
        # connectivity problem. Connection failure / 5xx → 502 qmt_proxy_error.
        if exc.status_code is not None and 400 <= exc.status_code < 500:
            upstream = exc.body or {}
            raise HTTPException(
                status_code=exc.status_code,
                detail={
                    "error_code": upstream.get("error_code", "invalid_config"),
                    "error_type": upstream.get("error_type", "validation_error"),
                    "message": (
                        upstream.get("message")
                        or upstream.get("error_message")
                        or str(exc)
                    ),
                    "field": upstream.get("field"),
                },
            ) from exc
        raise HTTPException(
            status_code=502,
            detail={
                "error_code": "qmt_proxy_error",
                "error_type": "upstream_error",
                "message": str(exc),
            },
        ) from exc
    if method.upper() != "GET":
        logger.info(
            "qmt-proxy config %s forwarded base_url=%s fields=%s",
            method.upper(),
            base_url,
            sorted((payload or {}).keys()),
        )
    if isinstance(body, dict):
        return body.get("data")
    return body


def create_app(
    service,
    approval_gate,
    model_invocation_repository=None,
    strategy_registry_service=None,
    strategy_definition_repository=None,
    assistant_service=None,
    channel_manager=None,
    channel_repository=None,
    cron_manager=None,
    cron_run_repo=None,
    strategy_storage=None,
    compiler=None,
    capability_registry=None,
    runtime_control_plane=None,
    quote_stream_service=None,
    update_service=None,
    knowledge_graph_repository=None,
):
    try:
        import asyncio
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import StreamingResponse
        from starlette.requests import Request
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise RuntimeError("FastAPI is not installed. Install fastapi and uvicorn.") from exc

    from doyoutrade.capabilities import load_builtin_capabilities

    capability_registry = capability_registry or load_builtin_capabilities()

    app = FastAPI(title="Doyoutrade API")
    app.state.service = service
    app.state.channel_manager = channel_manager
    app.state.channel_repository = channel_repository
    app.state.cron_manager = cron_manager
    app.state.cron_run_repo = cron_run_repo
    app.state.cycle_run_repository = getattr(service, "cycle_run_repository", None)
    app.state.capability_registry = capability_registry
    app.state.quote_stream_service = quote_stream_service
    app.state.update_service = update_service
    # Exposed for the recursive-cron guard in create_agent_cron_job:
    # the handler looks up the calling session's config to decide
    # whether to refuse the request. We attach the same service
    # reference the rest of the app already has.
    app.state.assistant_service = assistant_service

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        payload = _build_error_payload(
            status_code=exc.status_code,
            detail=exc.detail,
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=payload,
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _starlette_http_exception_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        payload = _build_error_payload(
            status_code=exc.status_code,
            detail=exc.detail,
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=payload,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        payload = _build_error_payload(
            status_code=422,
            detail=exc.errors(),
            error_type=type(exc).__name__,
        )
        payload["error_code"] = "request_validation_error"
        payload["error_message"] = "Request validation failed"
        return JSONResponse(status_code=422, content=payload)

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("api request failed path=%s", request.url.path)
        payload = _build_error_payload(
            status_code=500,
            detail=str(exc) or "Internal server error",
            error_type=type(exc).__name__,
        )
        return JSONResponse(status_code=500, content=payload)

    # Upload constants
    _BLOCKED_UPLOAD_EXT = {
        ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".app", ".dmg",
        ".so", ".dll", ".dylib",
        ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    }
    _MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
    _CHUNK_SIZE = 1024 * 1024  # 1 MB
    # uploads/ dir + the file_id contract live in the attachments module (single
    # source of truth shared with the assistant service).
    _UPLOADS_DIR = _attachments.UPLOADS_DIR

    if assistant_service is None:
        from doyoutrade.assistant import AssistantService

        assistant_service = AssistantService(
            platform_service=service,
            strategy_registry_service=strategy_registry_service,
            strategy_definition_repository=strategy_definition_repository,
        )
    from doyoutrade.api.cli_tools import build_cli_tool_registry

    cli_tool_registry = build_cli_tool_registry(
        service=service,
        strategy_registry_service=strategy_registry_service,
        strategy_definition_repository=strategy_definition_repository,
        cron_manager=cron_manager,
        cron_run_repo=cron_run_repo,
        strategy_storage=strategy_storage,
        compiler=compiler,
    )
    app.state.cli_tool_registry = cli_tool_registry

    async def _execute_cli_tool_payload(tool_name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a structured operation behind a resource endpoint and return JSON data.

        The public CLI boundary stays on OpenAPI paths. Some operations still
        share implementation with assistant-callable adapters until those
        adapters are split into standalone application services.
        """

        from doyoutrade.cli._envelope import parse_tool_result
        from doyoutrade.tools import ToolResult, adapt_sync_dict_to_tool_result

        tool = cli_tool_registry.get(tool_name)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"operation not available: {tool_name}")
        raw = tool.execute(**dict(args or {}))
        if hasattr(raw, "__await__"):
            raw = await raw
        if isinstance(raw, dict):
            raw = adapt_sync_dict_to_tool_result(raw)
        if isinstance(raw, ToolResult):
            text = raw.text
            is_error = raw.is_error
        else:
            text = raw if isinstance(raw, str) else str(raw)
            is_error = bool(getattr(raw, "is_error", False))
        data, summary, error_info = parse_tool_result(text, is_error=is_error)
        if is_error:
            code = (error_info or {}).get("error_code") or "operation_failed"
            status = 404 if str(code).endswith("_not_found") else 400
            raise HTTPException(status_code=status, detail=(error_info or {}).get("message") or summary or code)
        if isinstance(data, dict):
            return data
        return {"text": text}
    if channel_repository is None:
        channel_repository = getattr(assistant_service, "channel_repo", None)
        app.state.channel_repository = channel_repository
    if runtime_control_plane is None:
        from doyoutrade.runtime.control_plane import RuntimeControlPlane

        runtime_control_plane = RuntimeControlPlane(
            service=service,
            assistant_service=assistant_service,
            capability_registry=capability_registry,
            channel_manager=channel_manager,
            channel_repository=channel_repository,
            cron_manager=cron_manager,
            cron_run_repo=cron_run_repo,
            model_invocation_repository=model_invocation_repository,
        )
    app.state.runtime_control_plane = runtime_control_plane
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from doyoutrade.api.skills import build_skills_router
    from doyoutrade.skills.loader import default_skills_root

    app.include_router(build_skills_router(default_skills_root))

    # Read-only access to the private KB's 复盘 journals (journal/ partition
    # only) so the frontend 复盘 tab can render them. Writes stay agent-gated;
    # the knowledge-graph endpoints (query + deterministic re-projection) ride
    # the same router and mutate only the DB-derived graph, never KB files.
    from doyoutrade.api.knowledge_base import build_knowledge_router
    from doyoutrade.tools._sandbox import knowledge_root

    app.include_router(
        build_knowledge_router(
            knowledge_root,
            knowledge_graph_repository=knowledge_graph_repository,
        )
    )

    # 交割单 CSV 导入（multipart 预览 / 提交 + 券商目录建议）——写入知识库
    # trades/ 分区（无 DB 表；`import_trades_csv` 是唯一写路径）。
    from doyoutrade.api.portfolio_import_routes import build_portfolio_import_router

    app.include_router(build_portfolio_import_router())

    # Swarm 多智能体编排：复用 assistant_service 作为 worker 引擎，持久化复用其
    # agent_repo 的 session_factory（SqlAlchemy）。只有当依赖齐备时才挂载。
    try:
        from doyoutrade.api.swarm_routes import build_swarm_router
        from doyoutrade.swarm.orchestrator import SwarmOrchestrator
        from doyoutrade.swarm.store import SwarmStore

        _swarm_agent_repo = getattr(assistant_service, "agent_repo", None)
        _swarm_session_factory = getattr(_swarm_agent_repo, "session_factory", None)
        if _swarm_session_factory is not None:
            _swarm_store = SwarmStore(_swarm_session_factory)
            _swarm_orchestrator = SwarmOrchestrator(
                _swarm_store, assistant_service, _swarm_agent_repo
            )
            app.state.swarm_orchestrator = _swarm_orchestrator
            app.state.swarm_store = _swarm_store
            app.include_router(build_swarm_router(_swarm_orchestrator, _swarm_store))
    except Exception:  # pragma: no cover - 缺失依赖时不阻断主 app 启动
        import logging as _logging

        _logging.getLogger(__name__).warning("swarm 路由挂载失败", exc_info=True)

    @app.post("/upload", status_code=201)
    async def upload_file(file: UploadFile = File(...)):
        """Accept a file upload, validate extension and size, save to uploads/."""
        # Create uploads directory if it doesn't exist
        _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

        # Validate extension
        filename = file.filename or "unknown"
        ext = Path(filename).suffix.lower()
        if ext in _BLOCKED_UPLOAD_EXT:
            raise HTTPException(
                status_code=400,
                detail=f"File type {ext} is not allowed",
            )

        # Stream file in chunks, enforce size limit
        storage_name = uuid.uuid4().hex + ext
        storage_path = _UPLOADS_DIR / storage_name
        bytes_read = 0
        try:
            with open(storage_path, "wb") as dest:
                while True:
                    chunk = await file.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if bytes_read > _MAX_UPLOAD_BYTES:
                        # Over limit: abort and clean up
                        dest.close()
                        os.unlink(storage_path)
                        raise HTTPException(
                            status_code=413,
                            detail="File exceeds 50 MB limit",
                        )
                    dest.write(chunk)
        except HTTPException:
            raise
        except Exception as exc:
            # Clean up partial file on any other error
            if storage_path.exists():
                os.unlink(storage_path)
            logger.exception("upload_file failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        logger.info("upload_file saved filename=%s size=%d path=%s", filename, bytes_read, storage_path)
        # Return an opaque file_id (the on-disk storage name) + display metadata.
        # The server's absolute path is intentionally NOT exposed to the client;
        # it is re-derived server-side from file_id when a message references it.
        mime_type = file.content_type or mimetypes.guess_type(filename)[0]
        return {
            "status": "ok",
            "file_id": storage_name,
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": bytes_read,
        }

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/version")
    async def get_version():
        from doyoutrade import __version__, engine_version
        from doyoutrade.version_info import get_git_version_info

        git_info = get_git_version_info()
        return {
            "package_version": __version__,
            "engine_version": engine_version(),
            "git_tag": git_info["tag"],
            "git_commit": git_info["commit"],
            "git_commit_short": git_info["commit_short"],
            "git_dirty": git_info["dirty"],
        }

    @app.get("/runtime/health")
    async def runtime_health():
        return await runtime_control_plane.health()

    @app.get("/runtime/status")
    async def runtime_status():
        return await runtime_control_plane.status()

    @app.get("/runtime/capabilities")
    async def runtime_capabilities(kind: str | None = Query(default=None)):
        items = capability_registry.summary(kind=kind)
        return {
            "items": items,
            "total": len(items),
            "kinds": capability_registry.kinds(),
        }

    @app.get("/data-providers")
    async def list_data_providers():
        """Ids accepted by ``build_trading_data_stack`` plus public manifests."""
        from doyoutrade.data.factory import list_data_provider_ids

        providers = list_data_provider_ids()
        items_by_provider = {
            item.get("provider_id"): item
            for item in capability_registry.summary(kind="data_provider")
            if isinstance(item.get("provider_id"), str)
        }
        items = [items_by_provider[provider] for provider in providers if provider in items_by_provider]
        return {"providers": providers, "items": items}

    @app.get("/market/bars")
    async def get_local_market_bars(
        symbol: str = Query(..., max_length=64),
        interval: str = Query(default="1d", max_length=8),
        start: str | None = Query(default=None, max_length=64),
        end: str | None = Query(default=None, max_length=64),
        provider: str | None = Query(default=None, max_length=32),
        adjust: str | None = Query(default=None, max_length=16),
        backfill: bool = Query(default=True),
    ):
        try:
            return await service.get_local_market_bars(
                symbol=_normalize_required_string(symbol, field_name="symbol"),
                interval=_normalize_required_string(interval, field_name="interval"),
                start=_normalize_optional_string(start, field_name="start"),
                end=_normalize_optional_string(end, field_name="end"),
                provider=_normalize_optional_string(provider, field_name="provider"),
                adjust=_normalize_optional_string(adjust, field_name="adjust"),
                backfill=backfill,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/market/bars/sync-range")
    async def sync_local_market_bars_range(payload: dict, response: Response):
        try:
            result = await service.sync_local_market_bars_range(
                symbol=_normalize_required_string(payload.get("symbol"), field_name="symbol"),
                interval=_normalize_required_string(payload.get("interval"), field_name="interval"),
                start=_normalize_required_string(payload.get("start"), field_name="start"),
                end=_normalize_required_string(payload.get("end"), field_name="end"),
                provider=_normalize_optional_string(payload.get("provider"), field_name="provider"),
                adjust=_normalize_optional_string(payload.get("adjust"), field_name="adjust"),
                mode=_normalize_required_string(payload.get("mode"), field_name="mode"),
            )
        except ValueError as exc:
            return _local_market_sync_error_response(
                status_code=400,
                error_code="local_market_sync_invalid_request",
                error_type=type(exc).__name__,
                error_message=str(exc),
                hint="check symbol, interval, start, end, provider, adjust, and mode",
            )
        except RuntimeError as exc:
            return _local_market_sync_error_response(
                status_code=503,
                error_code="local_market_sync_unavailable",
                error_type=type(exc).__name__,
                error_message=str(exc),
                hint="configure the local market repository and upstream market data runtime",
            )
        if result.get("status") == "accepted":
            response.status_code = 202
        return result

    @app.get("/market/bars/sync-jobs/{job_id}")
    async def get_local_market_sync_job(job_id: str):
        try:
            return await service.get_local_market_sync_job(job_id)
        except RecordNotFoundError as exc:
            return _local_market_sync_error_response(
                status_code=404,
                error_code="local_market_sync_job_not_found",
                error_type=type(exc).__name__,
                error_message=str(exc),
                hint="check the async sync job id and retry after creating a new sync request if needed",
            )
        except ValueError as exc:
            return _local_market_sync_error_response(
                status_code=400,
                error_code="local_market_sync_job_invalid_request",
                error_type=type(exc).__name__,
                error_message=str(exc),
                hint="check the sync job identifier format",
            )
        except RuntimeError as exc:
            return _local_market_sync_error_response(
                status_code=503,
                error_code="local_market_sync_job_unavailable",
                error_type=type(exc).__name__,
                error_message=str(exc),
                hint="check local market sync job storage availability",
            )

    @app.get("/market/bars/overlays")
    async def get_local_market_overlays(
        symbol: str = Query(..., max_length=64),
        interval: str = Query(default="1d", max_length=8),
        start: str = Query(..., max_length=64),
        end: str = Query(..., max_length=64),
        overlay_kind: str = Query(..., max_length=32),
        run_id: str | None = Query(default=None, max_length=64),
        task_id: str | None = Query(default=None, max_length=64),
        signal_source_id: str | None = Query(default=None, max_length=64),
    ):
        try:
            return await service.get_local_market_overlays(
                symbol=_normalize_required_string(symbol, field_name="symbol"),
                interval=_normalize_required_string(interval, field_name="interval"),
                start=_normalize_required_string(start, field_name="start"),
                end=_normalize_required_string(end, field_name="end"),
                overlay_kind=_normalize_required_string(overlay_kind, field_name="overlay_kind"),
                run_id=_normalize_optional_string(run_id, field_name="run_id"),
                task_id=_normalize_optional_string(task_id, field_name="task_id"),
                signal_source_id=_normalize_optional_string(
                    signal_source_id,
                    field_name="signal_source_id",
                ),
            )
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/instruments/catalog")
    async def list_instrument_catalog(
        q: str | None = Query(default=None, description="Filter by symbol prefix or name substring"),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        try:
            return await service.list_instrument_catalog(q=q, limit=limit, offset=offset)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/instruments/catalog/item")
    async def get_instrument_catalog_item(
        symbol: str = Query(..., description="Canonical symbol e.g. 600000.SH"),
    ):
        row = await service.get_instrument_catalog_item(symbol)
        if row is None:
            raise HTTPException(status_code=404, detail="symbol not in catalog")
        return row

    @app.post("/instruments/catalog/sync")
    async def sync_instrument_catalog_route(payload: dict):
        try:
            source = _normalize_required_string(payload.get("source"), field_name="source")
            mode = _normalize_required_string(payload.get("mode"), field_name="mode")
            raw_syms = payload.get("symbols")
            symbols: list[str] | None
            if raw_syms is None:
                symbols = None
            elif isinstance(raw_syms, list):
                symbols = []
                for i, x in enumerate(raw_syms):
                    if not isinstance(x, str):
                        raise ValueError(f"symbols[{i}] must be a string")
                    t = x.strip()
                    if t:
                        symbols.append(t)
            else:
                raise ValueError("symbols must be an array of strings or null")
            return await service.sync_instrument_catalog(source=source, mode=mode, symbols=symbols)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/instruments/catalog/delete")
    async def delete_instrument_catalog_symbols_route(payload: dict):
        try:
            raw = payload.get("symbols")
            if not isinstance(raw, list) or not raw:
                raise ValueError("symbols must be a non-empty array of strings")
            symbols: list[str] = []
            for i, x in enumerate(raw):
                if not isinstance(x, str):
                    raise ValueError(f"symbols[{i}] must be a string")
                t = x.strip()
                if t:
                    symbols.append(t)
            if not symbols:
                raise ValueError("symbols must contain at least one non-empty string")
            return await service.delete_instrument_catalog_symbols(symbols)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/instruments/catalog/clear")
    async def clear_instrument_catalog_route(payload: dict):
        try:
            confirm = payload.get("confirm")
            if not isinstance(confirm, str):
                raise ValueError("confirm must be a string")
            return await service.clear_instrument_catalog(confirm=confirm)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/instrument-universe/search")
    async def instrument_universe_search(
        source: str = Query(..., description="Registered listing source (e.g. akshare_a, local_catalog)"),
        q: str | None = Query(default=None, description="Search text; empty returns no items"),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        from doyoutrade.data.instrument_universe import search_instrument_universe

        try:
            return await search_instrument_universe(
                source=source,
                q=q or "",
                limit=limit,
                instrument_catalog_repository=getattr(
                    service, "instrument_catalog_repository", None
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("instrument_universe_search failed source=%s", source)
            msg = str(exc).strip() or type(exc).__name__
            if len(msg) > 280:
                msg = msg[:277] + "..."
            raise HTTPException(
                status_code=503,
                detail=f"instrument listing source unavailable: {msg}",
            ) from exc

    @app.post("/data/run")
    async def run_data_probe(payload: dict):
        return await _execute_cli_tool_payload("data_run", payload)

    @app.post("/data/news")
    async def run_data_news(payload: dict):
        return await _execute_cli_tool_payload("data_news", payload)

    @app.post("/data/reports")
    async def run_data_research_reports(payload: dict):
        return await _execute_cli_tool_payload("data_research_reports", payload)

    @app.post("/data/breadth")
    async def run_data_market_breadth(payload: dict):
        return await _execute_cli_tool_payload("data_market_breadth", payload)

    @app.post("/data/lhb")
    async def run_data_lhb(payload: dict):
        return await _execute_cli_tool_payload("data_lhb", payload)

    @app.post("/data/chips")
    async def run_data_chips(payload: dict):
        return await _execute_cli_tool_payload("data_chips", payload)

    @app.post("/data/fund-flow")
    async def run_data_fund_flow(payload: dict):
        return await _execute_cli_tool_payload("data_fund_flow", payload)

    @app.post("/data/earnings")
    async def run_data_earnings(payload: dict):
        return await _execute_cli_tool_payload("data_earnings", payload)

    @app.post("/data/sector")
    async def run_data_sector(payload: dict):
        return await _execute_cli_tool_payload("data_sector", payload)

    @app.post("/data/sector-heat")
    async def run_data_sector_heat(payload: dict):
        return await _execute_cli_tool_payload("data_sector_heat", payload)

    @app.post("/data/fundamentals")
    async def run_data_fundamentals(payload: dict):
        return await _execute_cli_tool_payload("data_fundamentals", payload)

    @app.post("/data/events")
    async def run_data_events(payload: dict):
        return await _execute_cli_tool_payload("data_events", payload)

    @app.get("/sdk/dp-methods")
    async def list_sdk_dp_methods():
        return await _execute_cli_tool_payload("list_dp_methods")

    @app.get("/sdk/indicators")
    async def list_sdk_indicators():
        return await _execute_cli_tool_payload("list_indicators")

    @app.get("/sdk/data-requests")
    async def list_sdk_data_requests():
        return await _execute_cli_tool_payload("list_data_requests")

    @app.post("/sdk/validate-recursive")
    async def run_validate_recursive(payload: dict):
        return await _execute_cli_tool_payload("validate_recursive", payload)

    @app.post("/backtest/walk-forward")
    async def run_walk_forward(payload: dict):
        return await _execute_cli_tool_payload("walk_forward_backtest", payload)

    @app.post("/analysis/pattern")
    async def run_pattern_analysis(payload: dict):
        return await _execute_cli_tool_payload("pattern_recognition", payload)

    @app.post("/analysis/indicators")
    async def run_indicator_analysis(payload: dict):
        return await _execute_cli_tool_payload("compute_indicators", payload)

    @app.post("/analysis/factor")
    async def run_factor_analysis(payload: dict):
        return await _execute_cli_tool_payload("factor_analysis", payload)

    @app.post("/stock/screen")
    async def run_stock_screen(payload: dict):
        return await _execute_cli_tool_payload("stock_screen", payload)

    @app.get("/model-invocations")
    async def list_model_invocations(
        limit: int = Query(default=10, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        trace_id: str | None = Query(default=None, description="Exact match on stored trace_id"),
        span_id: str | None = Query(default=None, description="Exact match on stored span_id"),
        run_id: str | None = Query(default=None, description="Exact match on stored run_id"),
    ):
        if model_invocation_repository is None:
            return {"items": [], "total": 0}
        t_filter = _normalize_optional_string(trace_id, field_name="trace_id")
        s_filter = _normalize_optional_string(span_id, field_name="span_id")
        r_filter = _normalize_optional_string(run_id, field_name="run_id")
        items, total = await model_invocation_repository.list_invocations(
            limit=limit,
            offset=offset,
            trace_id=t_filter,
            span_id=s_filter,
            run_id=r_filter,
        )
        return {"items": items, "total": total}

    @app.get("/model-invocations/by-span/{span_id}")
    async def get_model_invocation_by_span(span_id: str):
        if model_invocation_repository is None:
            raise HTTPException(status_code=404, detail="repository not available")
        result = await model_invocation_repository.get_invocation_by_span_id(span_id)
        if result is None:
            raise HTTPException(status_code=404, detail="model invocation not found")
        return result

    # ── Assistant Agent ───────────────────────────────────────────────────────

    @app.get("/assistant/tools")
    async def list_assistant_tools():
        return {"tools": assistant_service.list_tools()}

    @app.post("/assistant/tools/{tool_name}/execute")
    async def execute_assistant_tool(tool_name: str, payload: dict, request: Request):
        """Execute an assistant tool inside the API server runtime.

        This endpoint is the CLI/server boundary: ``doyoutrade-cli`` remains
        responsible for command parsing and envelope rendering, while the API
        process owns tool execution and runtime state.
        """

        tool = cli_tool_registry.get(tool_name)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"tool not found: {tool_name}")

        args = payload.get("args", {})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise HTTPException(status_code=400, detail="args must be an object")

        calling_agent_id = (
            request.headers.get("X-DOYOUTRADE-Agent-Id")
            or request.headers.get("X-DOYOUTRADE-Calling-Agent-Id")
        )
        session_id = (
            request.headers.get("X-DOYOUTRADE-Session-Id")
            or request.headers.get("X-DOYOUTRADE-Calling-Session-Id")
        )
        debug_session_id = request.headers.get("X-DOYOUTRADE-Debug-Session-Id")
        run_id = request.headers.get("X-DOYOUTRADE-Run-Id")
        call_args = dict(args)
        if session_id and getattr(tool, "requires_session_id", False):
            call_args.setdefault("session_id", session_id)
        if (
            calling_agent_id
            and getattr(tool, "requires_calling_agent_id", False)
            and not call_args.get("agent_id")
        ):
            call_args["agent_id"] = calling_agent_id
        if (
            session_id
            and getattr(tool, "requires_calling_session_id", False)
            and not call_args.get("target_session_id")
        ):
            call_args["target_session_id"] = session_id

        import inspect

        from doyoutrade.tools import ToolResult, adapt_sync_dict_to_tool_result
        from doyoutrade.tools._prose import format_error_text

        from contextlib import nullcontext
        from opentelemetry import propagate, trace as trace_api

        if debug_session_id:
            from doyoutrade.observability.debug_span_export import debug_span_export_for_session

            export_cm = debug_span_export_for_session(debug_session_id, span_source="cli")
        else:
            export_cm = nullcontext()

        parent_ctx = propagate.extract(request.headers)
        tracer = trace_api.get_tracer("doyoutrade.api.cli_tools")
        span_name = f"cli_api.{tool_name}"
        with export_cm:
            with tracer.start_as_current_span(span_name, context=parent_ctx) as span:
                if calling_agent_id:
                    span.set_attribute("doyoutrade.agent_id", calling_agent_id)
                if session_id:
                    span.set_attribute("doyoutrade.session_id", session_id)
                if run_id:
                    span.set_attribute("doyoutrade.run_id", run_id)
                if debug_session_id:
                    span.set_attribute("doyoutrade.debug_session_id", debug_session_id)
                span.set_attribute("doyoutrade.cli.tool_name", tool_name)
                try:
                    raw = tool.execute(**call_args)
                    if inspect.isawaitable(raw):
                        raw = await raw
                except TypeError as exc:
                    text = format_error_text(
                        "validation_error",
                        str(exc) or "tool rejected kwargs",
                    )
                    is_error = True
                except Exception as exc:
                    text = format_error_text(
                        "internal_error",
                        str(exc) or f"{type(exc).__name__} (no message)",
                    )
                    is_error = True
                else:
                    if isinstance(raw, dict):
                        raw = adapt_sync_dict_to_tool_result(raw)
                        span.set_attribute("doyoutrade.cli.tool_kind", "sync_dict")
                    elif isinstance(raw, ToolResult):
                        span.set_attribute("doyoutrade.cli.tool_kind", "tool_result")
                    else:
                        span.set_attribute("doyoutrade.cli.tool_kind", "raw")
                    if isinstance(raw, ToolResult):
                        text = raw.text
                        is_error = raw.is_error
                    else:
                        text = raw if isinstance(raw, str) else str(raw)
                        is_error = bool(getattr(raw, "is_error", False))

        return {
            "tool_name": tool_name,
            "is_error": is_error,
            "text": text,
        }

    @app.get("/assistant/channels")
    async def list_assistant_channels():
        """List persisted assistant channels without plaintext secrets."""
        repo = channel_repository
        if repo is None:
            return {"items": [], "total": 0}
        channels = await repo.list_channels()
        return {"items": channels, "total": len(channels)}

    @app.get("/assistant/feishu/chats")
    async def list_feishu_chats():
        """List the Feishu groups each running feishu bot belongs to.

        Powers the trigger delivery channel picker: a channel target needs both a
        bot (registered channel record id) and a group (``oc_…`` chat_id), so this
        flattens (bot × its groups) into selectable rows. Live Feishu call per bot;
        a bot that errors (missing scope / offline) is logged and skipped, never
        failing the whole list (CLAUDE.md §错误可见性).
        """
        manager = channel_manager
        if manager is None or not hasattr(manager, "channel_ids"):
            return {"items": []}
        names: dict[str, str] = {}
        if channel_repository is not None:
            try:
                for row in await channel_repository.list_channels():
                    names[row["id"]] = row.get("name") or row["id"]
            except Exception:
                logger.exception("feishu chats: channel name lookup failed")
        items: list[dict[str, str]] = []
        # ChannelManager.channel_ids is a @property (returns a list), not a method.
        for cid in manager.channel_ids:
            channel = manager.get(cid)
            if getattr(channel, "channel_type", "") != "feishu":
                continue
            try:
                chats = await channel.list_chats()
            except Exception as exc:
                logger.exception("feishu chats: list_chats failed channel_id=%s", cid)
                items.append(
                    {
                        "channel_id": cid,
                        "channel_name": names.get(cid, cid),
                        "chat_id": "",
                        "name": "",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            for chat in chats:
                items.append(
                    {
                        "channel_id": cid,
                        "channel_name": names.get(cid, cid),
                        "chat_id": chat["chat_id"],
                        "name": chat.get("name") or chat["chat_id"],
                    }
                )
        return {"items": items}

    async def _ensure_channel_agent(agent_id: str) -> None:
        agent_repo = getattr(assistant_service, "agent_repo", None)
        if agent_repo is None:
            return
        agent = await agent_repo.get_agent(agent_id)
        if agent is None:
            raise ValueError(f"agent not found: {agent_id}")

    def _normalize_channel_payload(payload: dict, *, partial: bool = False) -> dict:
        data = {}
        if not partial or "name" in payload:
            data["name"] = _normalize_required_string(payload.get("name"), field_name="name")
        if not partial or "type" in payload:
            channel_type = _normalize_required_string(payload.get("type"), field_name="type")
            allowed = capability_registry.channel_types()
            if channel_type not in allowed:
                raise ValueError(f"type must be one of: {', '.join(allowed)}")
            data["type"] = channel_type
        if "enabled" in payload:
            data["enabled"] = bool(_normalize_bool(payload.get("enabled"), field_name="enabled"))
        elif not partial:
            data["enabled"] = False
        if not partial or "agent_id" in payload:
            data["agent_id"] = _normalize_required_string(payload.get("agent_id"), field_name="agent_id")
        if "config" in payload or not partial:
            data["config"] = _normalize_object(payload.get("config") or {}, field_name="config") or {}
        if "secrets" in payload or not partial:
            data["secrets"] = _normalize_object(payload.get("secrets") or {}, field_name="secrets") or {}
        return data

    @app.post("/assistant/channels", status_code=201)
    async def create_assistant_channel(payload: dict):
        repo = channel_repository
        if repo is None:
            raise HTTPException(status_code=404, detail="channel repository not available")
        try:
            data = _normalize_channel_payload(payload)
            await _ensure_channel_agent(data["agent_id"])
            return await repo.create_channel(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/assistant/channels/{channel_id}")
    async def get_assistant_channel(channel_id: str):
        repo = channel_repository
        if repo is None:
            raise HTTPException(status_code=404, detail="channel repository not available")
        channel = await repo.get_channel(channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail=f"channel not found: {channel_id}")
        return channel

    @app.put("/assistant/channels/{channel_id}")
    async def update_assistant_channel(channel_id: str, payload: dict):
        repo = channel_repository
        if repo is None:
            raise HTTPException(status_code=404, detail="channel repository not available")
        try:
            updates = _normalize_channel_payload(payload, partial=True)

            # 获取当前 channel，确认存在
            current = await repo.get_channel(channel_id)
            if current is None:
                raise HTTPException(status_code=404, detail=f"channel not found: {channel_id}")

            if "agent_id" in updates:
                await _ensure_channel_agent(updates["agent_id"])

            # 如果 enabled 字段发生变化，联动 start/stop
            enabled_changed = (
                "enabled" in updates
                and bool(updates["enabled"]) != bool(current.get("enabled"))
            )
            if enabled_changed:
                mgr = channel_manager
                if mgr is not None and mgr.get(channel_id) is not None:
                    if updates["enabled"]:
                        exc = await mgr.start(channel_id)
                        new_status = "running" if exc is None else "stopped"
                        new_error = str(exc) if exc else ""
                    else:
                        exc = await mgr.stop(channel_id)
                        new_status = "stopped"
                        new_error = ""
                    updates["status"] = new_status
                    if new_error:
                        updates["last_error"] = new_error

            return await repo.update_channel(channel_id, updates)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/assistant/channels/{channel_id}", status_code=204)
    async def delete_assistant_channel(channel_id: str):
        repo = channel_repository
        if repo is None:
            raise HTTPException(status_code=404, detail="channel repository not available")
        try:
            await repo.delete_channel(channel_id)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return None

    @app.post("/assistant/channels/{channel_id}/secrets/{secret_key}/copy")
    async def copy_assistant_channel_secret(channel_id: str, secret_key: str):
        repo = channel_repository
        if repo is None:
            raise HTTPException(status_code=404, detail="channel repository not available")
        try:
            value = await repo.copy_secret(channel_id, secret_key)
            return {"secret_key": secret_key, "value": value}
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/assistant/channels/{channel_id}/start")
    async def start_assistant_channel(channel_id: str):
        mgr = channel_manager
        if mgr is None:
            raise HTTPException(status_code=404, detail="channel manager not available")
        channel = mgr.get(channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail=f"channel not found: {channel_id}")
        try:
            await mgr.start(channel_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        repo = channel_repository
        if repo is not None:
            await repo.update_status(channel_id, status="running")
        return await repo.get_channel(channel_id) if repo else {"id": channel_id}

    @app.post("/assistant/channels/{channel_id}/stop")
    async def stop_assistant_channel(channel_id: str):
        mgr = channel_manager
        if mgr is None:
            raise HTTPException(status_code=404, detail="channel manager not available")
        channel = mgr.get(channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail=f"channel not found: {channel_id}")
        try:
            await mgr.stop(channel_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        repo = channel_repository
        if repo is not None:
            await repo.update_status(channel_id, status="stopped")
        return await repo.get_channel(channel_id) if repo else {"id": channel_id}

    @app.post("/assistant/sessions", status_code=201)
    async def create_assistant_session(payload: dict):
        title = _normalize_optional_string(payload.get("title", ""), field_name="title") or ""
        agent_id = _normalize_required_string(payload.get("agent_id"), field_name="agent_id")
        try:
            model_route_name = _normalize_optional_string(
                payload.get("model_route_name"),
                field_name="model_route_name",
            )
            if model_route_name:
                ensure_route = getattr(service, "ensure_model_route_exists", None)
                if ensure_route is not None:
                    await ensure_route(model_route_name)
            return await assistant_service.create_session(agent_id=agent_id, title=title)
        except (RecordNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/assistant/sessions")
    async def list_assistant_sessions(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        channel_id: str | None = Query(default=None),
        source: str | None = Query(default=None, description='Filter by source: "web" or "channel"'),
    ):
        normalized_source = str(source or "").strip().lower() or None
        normalized_channel_id = str(channel_id or "").strip() or None
        if normalized_source is not None and normalized_source not in {"web", "channel"}:
            raise HTTPException(status_code=400, detail='source must be "web" or "channel"')
        if normalized_channel_id and normalized_source:
            raise HTTPException(status_code=400, detail="channel_id and source are mutually exclusive")
        return await assistant_service.list_sessions(
            limit=limit,
            offset=offset,
            channel_id=normalized_channel_id,
            source=normalized_source,
        )

    @app.get("/assistant/sessions/{session_id}")
    async def get_assistant_session(session_id: str):
        row = await assistant_service.get_session(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"assistant session not found: {session_id}")
        return row

    @app.post("/assistant/sessions/{session_id}/messages")
    async def send_assistant_message(session_id: str, payload: dict):
        # content is optional when structured attachments are present (e.g. the
        # user uploads a file without typing anything); the service enforces
        # "at least one of content/attachments".
        content = _normalize_optional_string(payload.get("content"), field_name="content") or ""
        try:
            attachments = _attachments.normalize_attachments(payload.get("attachments"))
        except _attachments.AttachmentError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error_code": exc.error_code, "message": str(exc)},
            ) from exc
        try:
            return await assistant_service.send_message(
                session_id=session_id, content=content, attachments=attachments
            )
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/assistant/approvals/{approval_id}/resolve")
    async def resolve_assistant_approval(approval_id: str, payload: dict):
        """Resolve a pending blocking tool-call approval (web counterpart of
        the Feishu approval card buttons)."""
        action = str(payload.get("action") or "")
        if action not in (
            "approve_once",
            "approve_always",
            "approve_persist",
            "reject",
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "action must be approve_once | approve_always | "
                    "approve_persist | reject"
                ),
            )
        broker = getattr(assistant_service, "approval_broker", None)
        if broker is None:
            raise HTTPException(status_code=503, detail="approval broker unavailable")
        accepted = broker.resolve(
            approval_id,
            action=action,
            source="web",
            resolver_id=str(payload.get("resolver_id") or ""),
            reason=str(payload.get("reason") or ""),
            command_prefix=str(payload.get("command_prefix") or ""),
        )
        if not accepted:
            # Already resolved on another surface, timed out, or unknown —
            # the clicker must see this instead of assuming their decision won.
            raise HTTPException(
                status_code=409,
                detail=f"approval {approval_id} is not pending (resolved elsewhere or expired)",
            )
        return {"status": "resolved", "approval_id": approval_id, "action": action}

    @app.get("/assistant/sessions/{session_id}/approvals/pending")
    async def list_pending_assistant_approvals(session_id: str):
        """Pending approvals for one session — lets the web UI recover the
        banner after a page refresh (SSE events are otherwise the source)."""
        broker = getattr(assistant_service, "approval_broker", None)
        if broker is None:
            return {"items": []}
        return {"items": broker.list_pending(session_id)}

    @app.post("/assistant/sessions/{session_id}/questions/{question_id}/answer")
    async def answer_assistant_question(session_id: str, question_id: str, payload: dict):
        """Answer a pending ask_user_question (web counterpart of clicking an
        option / typing into the card). Resolves the suspended tool wait so the
        SAME run continues — no synthetic user message. ``selected`` are the
        chosen option labels; ``custom`` is free-form text (either or both)."""
        raw_selected = payload.get("selected")
        if raw_selected is None:
            selected: list[str] = []
        elif isinstance(raw_selected, str):
            selected = [raw_selected]
        elif isinstance(raw_selected, list):
            selected = [str(item) for item in raw_selected]
        else:
            raise HTTPException(
                status_code=400, detail="selected must be a string or a list of strings"
            )
        custom = str(payload.get("custom") or "").strip()
        if not selected and not custom:
            raise HTTPException(
                status_code=400, detail="an answer requires selected options and/or custom text"
            )
        broker = getattr(assistant_service, "question_broker", None)
        if broker is None:
            raise HTTPException(status_code=503, detail="question broker unavailable")
        request = broker.get(question_id)
        if request is not None and request.session_id != session_id:
            raise HTTPException(
                status_code=404,
                detail=f"question {question_id} does not belong to session {session_id}",
            )
        accepted = broker.resolve(
            question_id,
            selected=selected,
            custom=custom,
            source="option_click",
            resolver_id=str(payload.get("resolver_id") or ""),
        )
        if not accepted:
            # Already answered on another surface, timed out, or unknown — the
            # clicker must see this instead of assuming their answer won.
            raise HTTPException(
                status_code=409,
                detail=f"question {question_id} is not pending (answered elsewhere or expired)",
            )
        return {"status": "answered", "question_id": question_id}

    @app.get("/assistant/sessions/{session_id}/questions/pending")
    async def list_pending_assistant_questions(session_id: str):
        """Pending ask_user_question waits for one session — lets the web UI
        recover the card after a page refresh (SSE events are otherwise the
        source)."""
        broker = getattr(assistant_service, "question_broker", None)
        if broker is None:
            return {"items": []}
        return {"items": broker.list_pending(session_id)}

    @app.post("/assistant/sessions/{session_id}/stop")
    async def stop_assistant_session(session_id: str):
        session = await assistant_service.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        result = await assistant_service.stop_attempt(session_id)
        return result

    @app.get("/assistant/sessions/{session_id}/messages")
    async def list_assistant_messages(
        session_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ):
        session = await assistant_service.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"assistant session not found: {session_id}")
        return await assistant_service.list_messages(session_id, limit=limit, offset=offset)

    @app.get("/assistant/sessions/{session_id}/events")
    async def list_assistant_events(
        session_id: str,
        after_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        # When set (and after_id is omitted), returns the most recent `limit`
        # events in chronological order instead of the oldest `limit` — the
        # frontend's "reconstruct current live state on page load" call needs
        # the tail, not the head, of a long session's event log.
        tail: bool = Query(default=False),
    ):
        session = await assistant_service.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"assistant session not found: {session_id}")
        return await assistant_service.list_events(session_id, after_id=after_id, limit=limit, tail=tail)

    @app.get("/assistant/sessions/{session_id}/events/stream")
    async def stream_assistant_events(
        session_id: str,
        last_event_id: str | None = Query(default=None),
    ):
        session = await assistant_service.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"assistant session not found: {session_id}")

        async def _events():
            marker = last_event_id
            idle_ticks = 0
            while idle_ticks < 300:
                rows = await assistant_service.list_events(session_id, after_id=marker, limit=50)
                if rows:
                    idle_ticks = 0
                    for row in rows:
                        marker = row["event_id"]
                        data = json.dumps(row["payload"], ensure_ascii=False)
                        yield (
                            f"id: {row['event_id']}\n"
                            f"event: {row['event_type']}\n"
                            f"data: {data}\n\n"
                        )
                else:
                    idle_ticks += 1
                    yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(_events(), media_type="text/event-stream")

    @app.get("/assistant/sessions/{session_id}/traces")
    async def list_assistant_traces(
        session_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        session = await assistant_service.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"assistant session not found: {session_id}")
        return await assistant_service.list_traces(session_id, limit=limit, offset=offset)

    @app.get("/assistant/sessions/{session_id}/traces/{trace_id}")
    async def get_assistant_trace_detail(session_id: str, trace_id: str):
        session = await assistant_service.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"assistant session not found: {session_id}")
        detail = await assistant_service.get_trace_detail(trace_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"trace not found: {trace_id}")
        return detail

    @app.get("/assistant/sessions/{session_id}/export")
    async def export_assistant_session(
        session_id: str,
        format_: str = Query(default="markdown", alias="format", pattern="^(json|markdown)$"),
        include_traces: bool = Query(default=True),
    ):
        session = await assistant_service.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"assistant session not found: {session_id}")

        warnings: list[str] = []
        agent = None
        agent_repo = getattr(assistant_service, "agent_repo", None)
        if agent_repo is not None and session.get("agent_id"):
            try:
                agent = await agent_repo.get_agent(session["agent_id"])
            except Exception as exc:
                warnings.append(f"agent metadata unavailable: {exc}")

        message_limit = 1000
        event_limit = 1000
        trace_limit = 200
        messages = await assistant_service.list_messages(session_id, limit=message_limit, offset=0)
        events = await assistant_service.list_events(session_id, after_id=None, limit=event_limit)
        if len(messages) >= message_limit:
            warnings.append(f"messages may be truncated at {message_limit} rows")
        if len(events) >= event_limit:
            warnings.append(f"events may be truncated at {event_limit} rows")
        traces = {"items": [], "total": 0, "limit": 0, "offset": 0}
        trace_details: list[dict[str, Any]] = []
        if include_traces:
            try:
                traces = await assistant_service.list_traces(session_id, limit=trace_limit, offset=0)
                trace_items = list(traces.get("items") or [])
                trace_total = int(traces.get("total") or len(trace_items))
                if trace_total > len(trace_items):
                    warnings.append(f"trace details truncated: fetched {len(trace_items)} of {trace_total} traces")
                for item in trace_items:
                    trace_id = item.get("trace_id")
                    if not trace_id:
                        continue
                    try:
                        detail = await assistant_service.get_trace_detail(trace_id)
                    except Exception as exc:
                        warnings.append(f"trace detail unavailable for {trace_id}: {exc}")
                        continue
                    if detail is not None:
                        trace_details.append(detail)
                    else:
                        warnings.append(f"trace detail unavailable for {trace_id}: not found")
            except Exception as exc:
                warnings.append(f"trace aggregation unavailable: {exc}")
                traces = {"items": [], "total": 0, "limit": 0, "offset": 0}

        return build_assistant_session_export(
            session=session,
            agent=agent,
            messages=messages,
            events=events,
            traces=traces,
            trace_details=trace_details,
            fmt=format_,
            include_traces=include_traces,
            warnings=warnings,
        )

    # ── Agent Management ───────────────────────────────────────────────────────

    @app.get("/assistant/agents", response_model=dict)
    async def list_assistant_agents(
        include_inactive: bool = Query(default=False),
    ):
        if not hasattr(assistant_service, "agent_repo") or assistant_service.agent_repo is None:
            raise HTTPException(status_code=404, detail="agent repository not available")
        agents = await assistant_service.agent_repo.list_agents(include_inactive=include_inactive)
        return {"items": agents, "total": len(agents)}

    @app.post("/assistant/agents", status_code=201, response_model=dict)
    async def create_assistant_agent(payload: dict):
        if not hasattr(assistant_service, "agent_repo") or assistant_service.agent_repo is None:
            raise HTTPException(status_code=404, detail="agent repository not available")
        try:
            name = _normalize_required_string(payload.get("name"), field_name="name")
            system_prompt_template_id = _normalize_system_prompt_template_id(
                payload.get("prompt_template_id", payload.get("system_prompt_template_id")),
                field_name=(
                    "prompt_template_id"
                    if "prompt_template_id" in payload
                    else "system_prompt_template_id"
                ),
            )
            if system_prompt_template_id is None:
                system_prompt = _normalize_required_string(payload.get("system_prompt"), field_name="system_prompt")
            else:
                system_prompt = _normalize_optional_string(payload.get("system_prompt"), field_name="system_prompt") or ""
            agent_data = {
                "name": name,
                "status": _normalize_optional_string(payload.get("status"), field_name="status") or "active",
                "system_prompt": system_prompt,
                "system_prompt_template_id": system_prompt_template_id,
                "prompt_template_id": system_prompt_template_id,
                "model_route_name": _normalize_optional_string(payload.get("model_route_name"), field_name="model_route_name") or "",
                "tool_configs": _normalize_tool_configs(
                    payload.get("tool_configs"),
                    fallback_tool_names=payload.get("tool_names") or [],
                ),
                "skill_names": payload.get("skill_names") or [],
                "max_turns": int(payload.get("max_turns") or 6),
                "context_compaction": _normalize_context_compaction(
                    payload.get("context_compaction")
                ),
                "is_default": False,
            }
            forced_max_turns = _cloud_forced_max_turns()
            if forced_max_turns is not None and agent_data["max_turns"] != forced_max_turns:
                logger.info(
                    "cloud clamp: create agent name=%s max_turns %s -> %s (admin-configured)",
                    name, agent_data["max_turns"], forced_max_turns,
                )
                agent_data["max_turns"] = forced_max_turns
            return await assistant_service.agent_repo.create_agent(agent_data)
        except Exception as exc:
            logger.exception("create_assistant_agent failed")
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/assistant/agents/tools", response_model=dict)
    async def list_assistant_agent_tools():
        return {"tools": assistant_service.list_tools()}

    @app.get("/assistant/agents/skills", response_model=dict)
    async def list_assistant_agent_skills():
        from doyoutrade.skills import load_skills
        skills = load_skills()
        return {
            "items": [
                {"name": s.name, "description": s.description}
                for s in skills
            ]
        }

    @app.get("/assistant/agents/prompt-templates", response_model=dict)
    async def list_assistant_agent_prompt_templates():
        items = list_prompt_templates()
        return {"items": items, "total": len(items)}

    @app.get("/assistant/agents/{agent_id}", response_model=dict)
    async def get_assistant_agent(agent_id: str):
        if not hasattr(assistant_service, "agent_repo") or assistant_service.agent_repo is None:
            raise HTTPException(status_code=404, detail="agent repository not available")
        agent = await assistant_service.agent_repo.get_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")
        return agent

    @app.put("/assistant/agents/{agent_id}", response_model=dict)
    async def update_assistant_agent(agent_id: str, payload: dict):
        if not hasattr(assistant_service, "agent_repo") or assistant_service.agent_repo is None:
            raise HTTPException(status_code=404, detail="agent repository not available")
        try:
            updates = {}
            for key in ("name", "status", "system_prompt", "system_prompt_template_id", "prompt_template_id", "model_route_name",
                        "tool_names", "tool_configs", "skill_names", "max_turns", "context_compaction"):
                if key not in payload:
                    continue
                value = payload.get(key)
                if key == "name":
                    updates[key] = _normalize_required_string(value, field_name=key)
                elif key == "status":
                    updates[key] = _normalize_optional_string(value, field_name=key) or "active"
                elif key == "system_prompt":
                    if value is None:
                        updates[key] = ""
                    else:
                        updates[key] = _normalize_optional_string(value, field_name=key) or ""
                elif key in {"system_prompt_template_id", "prompt_template_id"}:
                    normalized = _normalize_system_prompt_template_id(value, field_name=key)
                    updates["system_prompt_template_id"] = normalized
                    updates["prompt_template_id"] = normalized
                elif key == "context_compaction":
                    updates[key] = _normalize_context_compaction(value)
                elif key == "tool_configs":
                    updates[key] = _normalize_tool_configs(
                        value,
                        fallback_tool_names=payload.get("tool_names") if "tool_names" in payload else None,
                    )
                elif value is not None:
                    updates[key] = value
            forced_max_turns = _cloud_forced_max_turns()
            if forced_max_turns is not None and updates.get("max_turns") != forced_max_turns:
                logger.info(
                    "cloud clamp: update agent id=%s max_turns %s -> %s (admin-configured)",
                    agent_id, updates.get("max_turns", "<unchanged>"), forced_max_turns,
                )
                updates["max_turns"] = forced_max_turns
            return await assistant_service.agent_repo.update_agent(agent_id, updates)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except BuiltinAgentImmutableError as exc:
            raise HTTPException(
                status_code=403,
                detail={"error_code": "agent_builtin_immutable", "message": str(exc)},
            ) from exc
        except Exception as exc:
            logger.exception("update_assistant_agent failed")
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/assistant/agents/{agent_id}", status_code=204)
    async def delete_assistant_agent(agent_id: str, force: bool = False):
        if not hasattr(assistant_service, "agent_repo") or assistant_service.agent_repo is None:
            raise HTTPException(status_code=404, detail="agent repository not available")
        try:
            await assistant_service.agent_repo.delete_agent(agent_id, force=force)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except BuiltinAgentImmutableError as exc:
            raise HTTPException(
                status_code=403,
                detail={"error_code": "agent_builtin_immutable", "message": str(exc)},
            ) from exc
        except AgentInUseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/assistant/agents/{agent_id}/clone", status_code=201, response_model=dict)
    async def clone_assistant_agent(agent_id: str, payload: dict):
        if not hasattr(assistant_service, "agent_repo") or assistant_service.agent_repo is None:
            raise HTTPException(status_code=404, detail="agent repository not available")
        new_name = _normalize_required_string(payload.get("name"), field_name="name")
        try:
            return await assistant_service.agent_repo.clone_agent(agent_id, new_name)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("clone_assistant_agent failed")
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ── Agent Cron Jobs ─────────────────────────────────────────────────────────

    def _normalize_cron_string(value: str | None, field_name: str) -> str:
        if value is None:
            raise HTTPException(400, f"{field_name} is required")
        v = value.strip()
        if not v:
            raise HTTPException(400, f"{field_name} cannot be empty")
        return v

    def _normalize_cron_task_payload(payload: dict) -> dict:
        """Accept both public task shapes.

        The frontend submits ``task: {kind, params}``; existing API/CLI callers
        may still send the flattened storage shape
        ``task_kind`` / ``task_params_json``.
        """
        if "task" not in payload:
            return payload
        if "task_kind" in payload or "task_params_json" in payload:
            raise HTTPException(
                400,
                "task cannot be combined with task_kind or task_params_json",
            )
        normalized = dict(payload)
        task = normalized.pop("task")
        if task is None:
            normalized["task_kind"] = None
            normalized["task_params_json"] = None
            return normalized
        if not isinstance(task, dict):
            raise HTTPException(
                400,
                "task must be an object with a string 'kind' or null",
            )
        task_kind = task.get("kind")
        if not isinstance(task_kind, str) or not task_kind.strip():
            raise HTTPException(400, "task.kind must be a non-empty string")
        task_params = task.get("params")
        if task_params is None:
            task_params = {}
        if not isinstance(task_params, dict):
            raise HTTPException(400, "task.params must be an object")
        normalized["task_kind"] = task_kind.strip()
        normalized["task_params_json"] = task_params
        return normalized

    @app.get("/assistant/agents/{agent_id}/cron/jobs", response_model=dict)
    async def list_agent_cron_jobs(
        agent_id: str,
        request: Request,
    ):
        mgr: AgentCronManager = request.app.state.cron_manager
        jobs = await mgr.list_jobs(agent_id=agent_id)
        return {"items": jobs, "total": len(jobs)}

    @app.post("/assistant/agents/{agent_id}/cron/jobs", status_code=201, response_model=dict)
    async def create_agent_cron_job(agent_id: str, payload: dict, request: Request):
        payload = _normalize_cron_task_payload(payload)
        mgr: AgentCronManager = request.app.state.cron_manager
        # Recursive-cron guard: if the calling session was itself fired
        # by a cron job, refuse to create another one. The CLI's
        # ``invoke_api`` forwards the calling session id as
        # ``X-DOYOUTRADE-Calling-Session-Id``; in-process callers can
        # bypass by setting ``acknowledge_cron_recursion: true`` in the
        # payload (operator override; LLMs do not have this fact in
        # their context).
        calling_session_id = request.headers.get(
            "X-DOYOUTRADE-Calling-Session-Id"
        )
        acknowledge_recursion = bool(
            payload.get("acknowledge_cron_recursion", False),
        )
        if calling_session_id and not acknowledge_recursion:
            svc = getattr(request.app.state, "assistant_service", None)
            calling_session = None
            if svc is not None:
                try:
                    calling_session = await svc.get_session(calling_session_id)
                except Exception:
                    calling_session = None
            if calling_session and (calling_session.get("config") or {}).get(
                "cron_origin"
            ):
                origin_job = (calling_session["config"] or {}).get(
                    "cron_origin_job_id"
                )
                raise HTTPException(
                    403,
                    "Recursive cron creation blocked: the calling "
                    f"session {calling_session_id!r} was itself fired "
                    f"by cron job {origin_job!r}. If a cron-driven "
                    "agent really needs to schedule another job (rare "
                    "— usually a planning mistake), POST with "
                    "acknowledge_cron_recursion=true.",
                )
        pre_action = payload.get("pre_action")
        if pre_action is not None:
            if not isinstance(pre_action, dict) or not isinstance(pre_action.get("kind"), str):
                raise HTTPException(400, "pre_action must be an object with a string 'kind'")
        task_kind = payload.get("task_kind")
        task_params_json = payload.get("task_params_json")
        if task_kind is not None:
            if not isinstance(task_kind, str) or not task_kind.strip():
                raise HTTPException(400, "task_kind must be a non-empty string")
            if task_params_json is None:
                task_params_json = {}
            if not isinstance(task_params_json, dict):
                raise HTTPException(400, "task_params_json must be an object")
            if pre_action is not None:
                raise HTTPException(400, "task_kind and pre_action are mutually exclusive")
            input_template = payload.get("input_template")
            if input_template is not None and str(input_template).strip():
                raise HTTPException(400, "task_kind and input_template are mutually exclusive")
            # Autofill ``target_session_id`` for delivery-bound task kinds.
            # When an LLM tool / CLI caller creates a cron from an existing
            # assistant session, omitting ``target_session_id`` historically
            # produced a job that would later fire with ``delivery_status=
            # 'skipped'`` (silent drop). Surface the calling session id from
            # the request header (the same header used by the recursive-cron
            # guard above) so the cron actually pushes back to the user's
            # session by default.
            #
            # Key absence is the trigger; an explicit ``null`` from the
            # caller is respected so diagnostic fires can still be created
            # without a push target.
            _delivery_kinds = {"agent_chat_reply", "daily_review"}
            if (
                task_kind.strip() in _delivery_kinds
                and "target_session_id" not in task_params_json
                and calling_session_id
            ):
                task_params_json["target_session_id"] = calling_session_id
                logger.info(
                    "cron-create: autofilled target_session_id=%s from caller "
                    "session for task_kind=%s agent_id=%s",
                    calling_session_id, task_kind.strip(), agent_id,
                )
        else:
            input_template = _normalize_cron_string(payload.get("input_template"), "input_template")
        data: dict[str, Any] = {
            "agent_id": agent_id,
            "name": _normalize_cron_string(payload.get("name"), "name"),
            "input_template": None if task_kind is not None else input_template,
            "max_concurrency": int(payload.get("max_concurrency") or 1),
            "timeout_seconds": int(payload.get("timeout_seconds") or 120),
            "enabled": bool(payload.get("enabled", True)),
            "pre_action": pre_action,
            "task_kind": task_kind.strip() if isinstance(task_kind, str) else None,
            "task_params_json": task_params_json if task_kind is not None else None,
        }
        # Tagged-union schedule: 'cron' (recurring) or 'at' (one-shot).
        # ``schedule_kind`` is optional for back-compat — when missing
        # we fall back to the legacy ``cron_expression``-only shape.
        kind = (payload.get("schedule_kind") or "").strip() or None
        if kind is None and payload.get("cron_expression") is not None:
            kind = "cron"
        if kind not in ("cron", "at"):
            raise HTTPException(
                400,
                f"schedule_kind must be 'cron' or 'at', got {kind!r}. "
                "For one-shot 'fire in N seconds' use schedule_kind='at' "
                "with at_iso=<ISO-8601+offset> or in_duration='60s'.",
            )
        data["schedule_kind"] = kind
        if kind == "cron":
            data["cron_expression"] = _normalize_cron_string(
                payload.get("cron_expression"), "cron_expression",
            )
            data["timezone"] = payload.get("timezone") or "UTC"
        else:
            # 'at' kind. Accept either an explicit ISO timestamp or a
            # relative duration string (``in_duration``). The manager
            # resolves duration → at_iso against the server clock.
            at_iso_raw = payload.get("at_iso")
            in_duration = payload.get("in_duration")
            if at_iso_raw and in_duration:
                raise HTTPException(
                    400,
                    "schedule_kind='at' accepts at_iso OR in_duration, "
                    "not both.",
                )
            if not at_iso_raw and not in_duration:
                raise HTTPException(
                    400,
                    "schedule_kind='at' requires at_iso (ISO-8601 with "
                    "offset) or in_duration ('60s' / '5m' / '2h' / '1d').",
                )
            if at_iso_raw:
                data["at_iso"] = _normalize_cron_string(at_iso_raw, "at_iso")
            if in_duration:
                data["in_duration"] = _normalize_cron_string(
                    in_duration, "in_duration",
                )
        # Explicit caller value wins; otherwise default at→true,
        # cron→false. The default is computed by the manager to keep
        # the policy in one place.
        if "delete_after_run" in payload:
            data["delete_after_run"] = bool(payload["delete_after_run"])
        # Opt-out for intentional far-future schedules (default off blocks
        # the LLM footgun where bad field order / timezone confusion ends up
        # scheduled 1 year out instead of in 30 seconds). The CLI does not
        # surface this flag yet; direct API callers can pass it.
        acknowledge_distant = bool(payload.get("acknowledge_distant_schedule", False))
        try:
            job = await mgr.create_job(
                data, acknowledge_distant_schedule=acknowledge_distant,
            )
        except ValueError as exc:
            # AgentCronManager raises ValueError for caller-input problems:
            # bad/missing cron_expression, unknown agent_id, missing required
            # fields, next-fire-too-far. Surface as 400 so the CLI envelope
            # carries the message back to the assistant instead of a 500.
            detail = _parse_structured_value_error(exc)
            raise HTTPException(400, detail if detail is not None else str(exc)) from exc
        return job

    @app.get("/assistant/agents/{agent_id}/cron/jobs/{job_id}", response_model=dict)
    async def get_agent_cron_job(agent_id: str, job_id: str, request: Request):
        mgr: AgentCronManager = request.app.state.cron_manager
        job = await mgr.get_job(job_id)
        if not job or job["agent_id"] != agent_id:
            raise HTTPException(404, f"cron job not found: {job_id}")
        return job

    @app.put("/assistant/agents/{agent_id}/cron/jobs/{job_id}", response_model=dict)
    async def update_agent_cron_job(agent_id: str, job_id: str, payload: dict, request: Request):
        payload = _normalize_cron_task_payload(payload)
        mgr: AgentCronManager = request.app.state.cron_manager
        job = await mgr.get_job(job_id)
        if not job or job["agent_id"] != agent_id:
            raise HTTPException(404, f"cron job not found: {job_id}")
        updates: dict[str, Any] = {}
        for key in (
            "name", "cron_expression", "timezone",
            "schedule_kind", "at_iso", "delete_after_run",
            "input_template", "max_concurrency", "timeout_seconds", "enabled",
            "task_kind", "task_params_json",
        ):
            if key in payload:
                updates[key] = payload[key]
        if "in_duration" in payload:
            updates["in_duration"] = payload["in_duration"]
        if "pre_action" in payload:
            pre_action = payload["pre_action"]
            if pre_action is not None:
                if not isinstance(pre_action, dict) or not isinstance(pre_action.get("kind"), str):
                    raise HTTPException(400, "pre_action must be an object with a string 'kind' or null")
            updates["pre_action"] = pre_action  # explicit None clears the field
        if "task_kind" in updates:
            task_kind = updates["task_kind"]
            if task_kind is not None and (
                not isinstance(task_kind, str) or not task_kind.strip()
            ):
                raise HTTPException(400, "task_kind must be a non-empty string or null")
            if isinstance(task_kind, str):
                updates["task_kind"] = task_kind.strip()
                params = updates.get("task_params_json")
                if params is None:
                    params = {}
                    updates["task_params_json"] = params
                if not isinstance(params, dict):
                    raise HTTPException(400, "task_params_json must be an object")
                if updates.get("pre_action") is not None:
                    raise HTTPException(400, "task_kind and pre_action are mutually exclusive")
                if "input_template" in updates and updates.get("input_template"):
                    raise HTTPException(400, "task_kind and input_template are mutually exclusive")
        elif "task_params_json" in updates and updates["task_params_json"] is not None:
            if not isinstance(updates["task_params_json"], dict):
                raise HTTPException(400, "task_params_json must be an object")
        acknowledge_distant = bool(payload.get("acknowledge_distant_schedule", False))
        try:
            updated = await mgr.update_job(
                job_id, updates,
                acknowledge_distant_schedule=acknowledge_distant,
            )
        except ValueError as exc:
            # Same translation as create_agent_cron_job: caller-input
            # validation (bad cron_expression / next-fire-too-far on
            # schedule changes) should surface as 400, not 500.
            detail = _parse_structured_value_error(exc)
            raise HTTPException(400, detail if detail is not None else str(exc)) from exc
        return updated

    @app.delete("/assistant/agents/{agent_id}/cron/jobs/{job_id}", status_code=204)
    async def delete_agent_cron_job(agent_id: str, job_id: str, request: Request):
        mgr: AgentCronManager = request.app.state.cron_manager
        job = await mgr.get_job(job_id)
        if not job or job["agent_id"] != agent_id:
            raise HTTPException(404, f"cron job not found: {job_id}")
        try:
            await mgr.delete_job(job_id)
        except ValueError as exc:
            # Race: row was deleted between the get_job check above and
            # the mgr.delete_job call (or the manager itself raises for
            # another caller-input issue). Map to 404 so callers retry
            # rather than treating it as a server bug.
            raise HTTPException(404, str(exc)) from exc

    @app.post("/assistant/agents/{agent_id}/cron/jobs/{job_id}/pause", response_model=dict)
    async def pause_agent_cron_job(agent_id: str, job_id: str, request: Request):
        mgr: AgentCronManager = request.app.state.cron_manager
        job = await mgr.get_job(job_id)
        if not job or job["agent_id"] != agent_id:
            raise HTTPException(404, f"cron job not found: {job_id}")
        try:
            return await mgr.pause_job(job_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/assistant/agents/{agent_id}/cron/jobs/{job_id}/resume", response_model=dict)
    async def resume_agent_cron_job(agent_id: str, job_id: str, request: Request):
        mgr: AgentCronManager = request.app.state.cron_manager
        job = await mgr.get_job(job_id)
        if not job or job["agent_id"] != agent_id:
            raise HTTPException(404, f"cron job not found: {job_id}")
        try:
            return await mgr.resume_job(job_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/assistant/agents/{agent_id}/cron/jobs/{job_id}/run", response_model=dict)
    async def trigger_agent_cron_job(agent_id: str, job_id: str, request: Request):
        mgr: AgentCronManager = request.app.state.cron_manager
        job = await mgr.get_job(job_id)
        if not job or job["agent_id"] != agent_id:
            raise HTTPException(404, f"cron job not found: {job_id}")
        try:
            run_id = await mgr.trigger_job(job_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"cron_job_run_id": run_id}

    @app.get("/assistant/cron-jobs/{job_id}/runs", response_model=dict)
    async def list_cron_job_runs_route(job_id: str, request: Request, limit: int = 20):
        if limit < 1:
            raise HTTPException(400, "limit must be >= 1")
        limit = min(limit, 200)
        repo = request.app.state.cron_run_repo
        if repo is None:
            raise HTTPException(503, "cron run repository not configured")
        items = await repo.list_for_job(job_id, limit=limit)
        return {"items": items}

    @app.get("/assistant/cron-job-runs", response_model=dict)
    async def list_cron_job_runs_by_trace_route(
        request: Request,
        trace_id: str = Query(..., description="OpenTelemetry trace_id of the cron.job.fire span"),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        """Reverse-resolve cron firings by the trace_id stored on each run.

        Lets an operator who only has a trace_id (from a log / span) find
        which cron firing produced it, then drill into ``.../trace`` for the
        aggregated spans + model_invocations.
        """
        repo = request.app.state.cron_run_repo
        if repo is None:
            raise HTTPException(503, "cron run repository not configured")
        t_filter = _normalize_optional_string(trace_id, field_name="trace_id")
        if not t_filter:
            raise HTTPException(400, "trace_id must not be empty")
        items = await repo.list_by_trace_id(t_filter, limit=limit)
        return {"items": items, "trace_id": t_filter}

    @app.get("/assistant/cron-job-runs/{run_id}", response_model=dict)
    async def get_cron_job_run_route(run_id: str, request: Request):
        repo = request.app.state.cron_run_repo
        if repo is None:
            raise HTTPException(503, "cron run repository not configured")
        row = await repo.get_run(run_id)
        if row is None:
            raise HTTPException(404, f"cron job run not found: {run_id}")
        return row

    @app.get("/assistant/cron-job-runs/{run_id}/trace", response_model=dict)
    async def get_cron_job_run_trace_route(run_id: str, request: Request):
        """Spans + model_invocations associated with one cron fire.

        Aggregates across every session the fire touched: the agent's LLM
        composition session (``agent_session_id``), the pre-action's debug
        session (``pre_debug_session_id``) for legacy strategy_cycle rows,
        and any per-instance ``cycle_runs`` sessions stamped into
        ``pre_result_json.pre_data.instances[*].run_id`` /
        ``debug_session_id`` by ``strategy_signal_alert``. The frontend
        feeds the resulting ``spans`` array straight into ``TraceViewer``.
        """

        repo = request.app.state.cron_run_repo
        if repo is None:
            raise HTTPException(503, "cron run repository not configured")
        row = await repo.get_run(run_id)
        if row is None:
            raise HTTPException(404, f"cron job run not found: {run_id}")
        if assistant_service is None:
            raise HTTPException(503, "assistant service not configured")

        # Collect every session_id we have a path to. Order is irrelevant —
        # spans across sessions belong to different traces and the viewer
        # groups by parent_span_id within each trace.
        session_ids: list[str] = []
        agent_session_id = row.get("agent_session_id")
        if isinstance(agent_session_id, str) and agent_session_id.strip():
            session_ids.append(agent_session_id.strip())
        pre_debug_session_id = row.get("pre_debug_session_id")
        if isinstance(pre_debug_session_id, str) and pre_debug_session_id.strip():
            session_ids.append(pre_debug_session_id.strip())

        # strategy_signal_alert writes per-instance run_ids into the
        # pre_result_json blob — see StrategySignalAlertExecutor.run.
        related: list[dict[str, Any]] = []
        pre_result_json = row.get("pre_result_json") or {}
        if isinstance(pre_result_json, dict):
            pre_data = pre_result_json.get("pre_data")
            if isinstance(pre_data, dict):
                instances = pre_data.get("instances")
                if isinstance(instances, list):
                    for inst in instances:
                        if not isinstance(inst, dict):
                            continue
                        related.append({
                            "task_id": inst.get("task_id"),
                            "run_id": inst.get("run_id"),
                            "status": inst.get("status"),
                        })
        cycle_repo = getattr(request.app.state, "cycle_run_repository", None)
        if cycle_repo is None:
            svc_obj = getattr(request.app.state, "service", None)
            cycle_repo = getattr(svc_obj, "cycle_run_repository", None)
        for rel in related:
            run_id_rel = rel.get("run_id")
            if not isinstance(run_id_rel, str) or not run_id_rel.strip():
                continue
            if cycle_repo is None:
                continue
            try:
                cycle_row = await cycle_repo.get_by_run_id(run_id_rel.strip())
            except Exception:
                continue
            session_id = (
                cycle_row.get("session_id")
                if isinstance(cycle_row, dict)
                else getattr(cycle_row, "session_id", None)
            )
            if isinstance(session_id, str) and session_id.strip():
                session_ids.append(session_id.strip())

        deduped_session_ids = list(dict.fromkeys(session_ids))
        trace_payload = await assistant_service.get_spans_for_sessions(deduped_session_ids)
        return {
            **trace_payload,
            "run_id": run_id,
            "session_ids": deduped_session_ids,
            "related": related,
        }

    @app.get("/strategy-definitions")
    async def list_strategy_definitions():
        if strategy_definition_repository is None:
            raise HTTPException(status_code=404, detail="strategy definitions not available")
        items = await strategy_definition_repository.list_definitions()
        return {"items": [_serialize_strategy_definition_summary(item) for item in items]}

    @app.post("/strategy-definitions", status_code=201)
    async def create_strategy_definition(payload: dict):
        """Create a strategy definition (metadata only).

        ``class_name`` and ``source_code`` fields in the request body are
        accepted but ignored — source code now lives on disk under the
        authoring lifecycle path, not in the database.
        """
        if strategy_registry_service is None:
            raise HTTPException(status_code=404, detail="strategy definitions not available")
        try:
            snapshot = await strategy_registry_service.create_definition(
                StrategyDefinitionCreate(
                    definition_id=_normalize_required_string(payload.get("definition_id"), field_name="definition_id"),
                    name=_normalize_required_string(payload.get("name"), field_name="name"),
                    api_version=_normalize_required_string(payload.get("api_version") or "v1", field_name="api_version"),
                    input_contract=_normalize_object(payload.get("input_contract"), field_name="input_contract"),
                    parameter_schema=_normalize_object(payload.get("parameter_schema"), field_name="parameter_schema"),
                    default_parameters=_normalize_object(
                        payload.get("default_parameters"),
                        field_name="default_parameters",
                    ),
                    capabilities=_normalize_object(payload.get("capabilities"), field_name="capabilities"),
                    provenance=_normalize_object(payload.get("provenance"), field_name="provenance"),
                    generation_prompt=str(payload.get("generation_prompt") or ""),
                    generation_model=str(payload.get("generation_model") or ""),
                    generation_metadata=_normalize_object(
                        payload.get("generation_metadata"),
                        field_name="generation_metadata",
                    ),
                    status=str(payload.get("status") or "active"),
                )
            )
            return _serialize_strategy_definition_detail(snapshot, storage=strategy_storage)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/strategy-definitions/{definition_id}")
    async def get_strategy_definition(definition_id: str):
        if strategy_definition_repository is None:
            raise HTTPException(status_code=404, detail="strategy definitions not available")
        try:
            snapshot = await strategy_definition_repository.get_definition(definition_id)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _serialize_strategy_definition_detail(snapshot, storage=strategy_storage)

    @app.patch("/strategy-definitions/{definition_id}")
    async def update_strategy_definition(definition_id: str, payload: dict):
        if strategy_registry_service is None:
            raise HTTPException(status_code=404, detail="strategy definitions not available")
        try:
            snapshot = await strategy_registry_service.update_definition(
                definition_id,
                name=_normalize_optional_string(payload.get("name"), field_name="name"),
                api_version=_normalize_optional_string(payload.get("api_version"), field_name="api_version"),
                input_contract=_normalize_object(payload.get("input_contract"), field_name="input_contract"),
                parameter_schema=_normalize_object(payload.get("parameter_schema"), field_name="parameter_schema"),
                default_parameters=_normalize_object(
                    payload.get("default_parameters"),
                    field_name="default_parameters",
                ),
                capabilities=_normalize_object(payload.get("capabilities"), field_name="capabilities"),
                provenance=_normalize_object(payload.get("provenance"), field_name="provenance"),
                generation_prompt=_normalize_optional_string(
                    payload.get("generation_prompt"),
                    field_name="generation_prompt",
                ),
                generation_model=_normalize_optional_string(
                    payload.get("generation_model"),
                    field_name="generation_model",
                ),
                generation_metadata=_normalize_object(
                    payload.get("generation_metadata"),
                    field_name="generation_metadata",
                ),
                status=_normalize_optional_string(payload.get("status"), field_name="status"),
            )
            return _serialize_strategy_definition_detail(snapshot, storage=strategy_storage)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/strategy-definitions/{definition_id}", status_code=204)
    async def delete_strategy_definition(definition_id: str):
        if strategy_registry_service is None:
            raise HTTPException(status_code=404, detail="strategy definitions not available")
        try:
            await strategy_registry_service.delete_definition(definition_id)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/strategy-definitions", status_code=204)
    async def delete_strategy_definitions(payload: dict):
        if strategy_registry_service is None:
            raise HTTPException(status_code=404, detail="strategy definitions not available")
        raw_ids = payload.get("definition_ids")
        if not isinstance(raw_ids, list):
            raise HTTPException(status_code=400, detail="definition_ids must be a list of strings")
        try:
            definition_ids = [
                _normalize_required_string(definition_id, field_name="definition_ids[]")
                for definition_id in raw_ids
            ]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not definition_ids:
            raise HTTPException(status_code=400, detail="definition_ids must not be empty")
        try:
            await strategy_registry_service.delete_definitions(definition_ids)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/strategy-definitions/{definition_id}/compile")
    async def compile_strategy_definition(definition_id: str):
        """Deprecated: source code no longer lives in the database.

        This endpoint returns 410 Gone.  Use the authoring lifecycle
        (``compile_strategy_draft``) to compile a strategy definition.
        """
        raise HTTPException(
            status_code=410,
            detail={
                "error_code": "endpoint_removed",
                "message": (
                    "The /compile endpoint was removed in the strategy-as-files refactor. "
                    "Use the in-process compile_strategy_draft tool (or "
                    "`doyoutrade-cli sdk validate`)."
                ),
                "replacement": "compile_strategy_draft",
            },
        )

    @app.post("/strategy-authoring/sessions", status_code=201)
    async def open_strategy_authoring_session(payload: dict):
        return await _execute_cli_tool_payload("open_strategy_authoring", payload)

    @app.delete("/strategy-authoring/sessions/{session_id}")
    async def cancel_strategy_authoring_session(session_id: str):
        return await _execute_cli_tool_payload("cancel_strategy_authoring", {"session_id": session_id})

    @app.post("/strategy-authoring/sessions/{session_id}/compile")
    async def compile_strategy_authoring_session(session_id: str):
        return await _execute_cli_tool_payload("compile_strategy_draft", {"session_id": session_id})

    @app.post("/strategy-authoring/sessions/{session_id}/finalize")
    async def finalize_strategy_authoring_session(session_id: str):
        return await _execute_cli_tool_payload("finalize_strategy_authoring", {"session_id": session_id})

    # --- Accounts (QMT connection + identity, replaces config.data.qmt) ------

    @app.get("/accounts")
    async def list_accounts():
        return {"items": await service.list_accounts()}

    @app.get("/accounts/statement")
    async def get_account_statement(
        account_id: str | None = Query(default=None),
        asof: date | None = Query(default=None),
    ):
        try:
            return await service.get_account_statement(account_id, asof=asof)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/accounts/{account_id}")
    async def get_account(account_id: str):
        try:
            return await service.get_account(account_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/accounts", status_code=201)
    async def create_account(payload: dict):
        try:
            result = await service.create_account(payload)
        except (KeyError,) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _refresh_quote_stream(app)
        return result

    @app.put("/accounts/{account_id}")
    async def update_account(account_id: str, payload: dict):
        try:
            result = await service.update_account(account_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _refresh_quote_stream(app)
        return result

    @app.post("/accounts/{account_id}/set-default")
    async def set_default_account(account_id: str):
        try:
            result = await service.set_default_account(account_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        await _refresh_quote_stream(app)
        return result

    @app.delete("/accounts/{account_id}", status_code=204)
    async def delete_account(account_id: str):
        try:
            await service.delete_account(account_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            # account_in_use → 409 conflict
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await _refresh_quote_stream(app)

    # --- Config (static / low-frequency YAML settings, editable in the UI) ----

    @app.get("/config")
    async def get_config_endpoint():
        from doyoutrade import config_store

        return config_store.read_config_masked()

    @app.put("/config")
    async def put_config_endpoint(payload: dict):
        from doyoutrade import config_store

        try:
            return config_store.write_config(payload)
        except ValueError as exc:
            # ConfigValidationError (a ValueError subclass) carries .field;
            # a plain ValueError falls back to field=None.
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "invalid_config",
                    "error_type": "validation_error",
                    "message": str(exc),
                    "field": getattr(exc, "field", None),
                },
            ) from exc

    # --- Self-update (release-based 自动更新; see doyoutrade/infra/updater.py) --

    def _require_update_service():
        svc = getattr(app.state, "update_service", None)
        if svc is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "error_code": "updater_unavailable",
                    "error_type": "service_unavailable",
                    "message": "update service is not wired into this server "
                    "(test / embedded deployment)",
                },
            )
        return svc

    @app.get("/update/status")
    async def get_update_status():
        return _require_update_service().status()

    @app.post("/update/check")
    async def post_update_check():
        return await _require_update_service().check_now()

    @app.post("/update/apply")
    async def post_update_apply():
        from doyoutrade.infra.updater import UpdateError

        try:
            return await _require_update_service().apply()
        except UpdateError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_code": exc.error_code,
                    "error_type": "update_error",
                    "message": str(exc),
                    "hint": exc.hint,
                },
            ) from exc

    @app.get("/qmt-proxy/config")
    async def get_qmt_proxy_config():
        return await _qmt_proxy_config_forward(service, method="GET")

    @app.put("/qmt-proxy/config")
    async def put_qmt_proxy_config(payload: dict):
        return await _qmt_proxy_config_forward(service, method="PUT", payload=payload)

    # --- Watchlist (自选股) CRUD + tags -----------------------------------------
    @app.get("/watchlist")
    async def list_watchlist(tag: str | None = Query(default=None, max_length=128)):
        return {
            "items": await service.list_watchlist(
                _normalize_optional_string(tag, field_name="tag")
            )
        }

    @app.get("/watchlist/tags")
    async def list_watchlist_tags():
        # Declared before /watchlist/{entry_id} so "tags" is not captured as an id.
        return {"items": await service.list_watchlist_tags()}

    @app.get("/watchlist/{entry_id}")
    async def get_watchlist_entry(entry_id: str):
        try:
            return await service.get_watchlist_entry(entry_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/watchlist", status_code=201)
    async def add_watchlist_entry(payload: dict):
        try:
            return await service.add_watchlist_entry(payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except StateConflictError as exc:
            # duplicate_watchlist_symbol → 409 conflict
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/watchlist/{entry_id}")
    async def update_watchlist_entry(entry_id: str, payload: dict):
        try:
            return await service.update_watchlist_entry(entry_id, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/watchlist/{entry_id}", status_code=204)
    async def delete_watchlist_entry(entry_id: str):
        try:
            await service.delete_watchlist_entry(entry_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # --- Realtime quotes: one-shot REST + WebSocket stream (qmt-proxy only) -----
    @app.get("/market/quotes")
    async def get_market_quotes(symbol: list[str] = Query(default_factory=list)):
        from doyoutrade.core.models import QuoteSnapshot

        symbols = [str(s).strip() for s in symbol if str(s).strip()]
        if not symbols:
            return {"items": []}
        qss = app.state.quote_stream_service
        if qss is None:
            # Service not wired (e.g. isolated tests) → visible disconnected
            # placeholders so the frontend renders "—" rather than 500ing.
            return {
                "items": [
                    QuoteSnapshot(symbol=s, status="qmt_disconnected").to_dict()
                    for s in symbols
                ]
            }
        quotes = await qss.fetch_once(symbols)
        items = []
        for s in symbols:
            q = quotes.get(s)
            items.append(
                q.to_dict()
                if q is not None
                else QuoteSnapshot(symbol=s, status="no_data").to_dict()
            )
        return {"items": items}

    @app.websocket("/ws/market/quotes")
    async def ws_market_quotes(websocket: WebSocket):
        await websocket.accept()
        qss = websocket.app.state.quote_stream_service
        if qss is None:
            await websocket.send_json({"type": "status", "status": "qmt_disconnected"})
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                return

        async def _send(frame: dict) -> None:
            await websocket.send_json(frame)

        handle = None
        try:
            while True:
                msg = await websocket.receive_json()
                if not isinstance(msg, dict):
                    continue
                if msg.get("action") == "subscribe":
                    symbols = [
                        str(s).strip()
                        for s in (msg.get("symbols") or [])
                        if str(s).strip()
                    ]
                    if handle is None:
                        handle = await qss.register(_send, symbols)
                    else:
                        await qss.update_subscription(handle, symbols)
        except WebSocketDisconnect:
            pass
        finally:
            if handle is not None:
                await qss.unregister(handle)

    @app.get("/tasks")
    async def list_tasks():
        rows = await service.list_tasks()
        return [_strip_equity_curve_from_task(row) for row in rows]

    @app.get("/tasks/page")
    async def list_tasks_page(
        q: str | None = Query(default=None, description="Search by task_id or name"),
        status: str | None = Query(default=None, max_length=32),
        mode: str | None = Query(default=None, max_length=32),
        modes: str | None = Query(
            default=None,
            max_length=128,
            description=(
                "Comma-separated set of modes (e.g. 'paper,live,signal_only'). "
                "Takes precedence over the single 'mode' filter; used by the UI to "
                "group the trading vs backtest tabs in one query."
            ),
        ),
        definition_id: str | None = Query(
            default=None,
            max_length=64,
            description="Exact strategy definition id (sd-...) filter.",
        ),
        limit: int = Query(default=20, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        modes_list = (
            [segment.strip() for segment in modes.split(",") if segment.strip()]
            if modes
            else None
        )
        result = await service.list_tasks_page(
            q=_normalize_optional_string(q, field_name="q"),
            status=_normalize_optional_string(status, field_name="status"),
            mode=_normalize_optional_string(mode, field_name="mode"),
            modes=modes_list,
            limit=limit,
            offset=offset,
            definition_id=_normalize_optional_string(definition_id, field_name="definition_id"),
        )
        return {
            "items": [_strip_equity_curve_from_task(row) for row in result["items"]],
            "total": result["total"],
            "limit": result["limit"],
            "offset": result["offset"],
        }

    @app.get("/tasks/{task_id}")
    async def get_task_detail(task_id: str):
        try:
            return await service.get_task_status(task_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/tasks/{task_id}/duplicate-preset")
    async def get_task_duplicate_preset(task_id: str):
        try:
            return await service.build_task_duplicate_preset(task_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/templates")
    async def list_templates():
        return service.list_templates()

    @app.get("/model-routes")
    async def list_model_routes():
        try:
            return {"items": await service.list_model_routes_api()}
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/model-routes")
    async def create_model_route(payload: dict):
        try:
            rn = _normalize_required_string(payload.get("route_name"), field_name="route_name")
            kind = _normalize_required_string(payload.get("provider_kind"), field_name="provider_kind")
            allowed_kinds = capability_registry.model_provider_kinds()
            if kind not in allowed_kinds:
                raise ValueError(f"provider_kind must be one of: {', '.join(allowed_kinds)}")
            body = {
                "route_name": rn,
                "provider_kind": kind,
                "api_key": str(payload.get("api_key") or ""),
                "base_url": _normalize_optional_string(payload.get("base_url"), field_name="base_url"),
                "target_model": _normalize_optional_string(payload.get("target_model"), field_name="target_model"),
                "settings": _normalize_object(payload.get("settings"), field_name="settings"),
            }
            return await service.create_model_route_api(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/model-routes/lookup")
    async def get_model_route_by_name(route_name: str = Query(..., description="model_route.route_name")):
        try:
            return await service.get_model_route_api(route_name)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.patch("/model-routes/{route_id}")
    async def patch_model_route(route_id: str, payload: dict):
        try:
            if "provider_kind" in payload:
                kind = _normalize_required_string(
                    payload.get("provider_kind"), field_name="provider_kind"
                )
                allowed_kinds = capability_registry.model_provider_kinds()
                if kind not in allowed_kinds:
                    raise ValueError(f"provider_kind must be one of: {', '.join(allowed_kinds)}")
            return await service.update_model_route_api(route_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.delete("/model-routes/{route_id}", status_code=204)
    async def delete_model_route(route_id: str):
        try:
            await service.delete_model_route_api(route_id)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/model-routes/{route_id}/api-key")
    async def reveal_model_route_api_key(route_id: str):
        try:
            return await service.reveal_model_route_api_key(route_id)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/model-routes/{route_id}/test")
    async def test_model_route(route_id: str, payload: dict | None = None):
        prompt = _normalize_optional_string((payload or {}).get("prompt"), field_name="prompt")
        if not prompt:
            prompt = "你好，请用一句话介绍一下你自己，用于测试这个模型配置是否可用。"
        try:
            adapter, route_name = await service.prepare_model_route_test(route_id)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        async def _sse():
            async for chunk in service.stream_model_route_test(adapter, route_name, prompt):
                event = chunk["type"]
                data = json.dumps({k: v for k, v in chunk.items() if k != "type"}, ensure_ascii=False)
                yield f"event: {event}\ndata: {data}\n\n"

        return StreamingResponse(_sse(), media_type="text/event-stream")

    # ------------------------------------------------------------------
    # Web first-run setup wizard (SetupWizard.tsx). Backs the same "is the
    # default agent configured?" question the terminal onboarding wizard asks
    # (doyoutrade/onboarding.py::_run) — imported from there, not
    # reimplemented, so a machine configured from either surface reads as
    # configured from the other. See DOYOUTRADE_WEB_SETUP in
    # doyoutrade/api/server.py::_serve_doyoutrade for how the terminal wizard
    # defers to this overlay on a double-click launch.
    # ------------------------------------------------------------------

    @app.get("/setup/status")
    async def get_setup_status():
        from doyoutrade.onboarding import agent_route_usable

        # Deployment mode is an env-level flag (not user YAML): a hosted/cloud
        # deployment sets DOYOUTRADE_DEPLOYMENT_MODE=cloud so the SAME frontend
        # build can conditionally render cloud-only chrome (user menu, plan,
        # logout). Unknown/unset → "local" (unchanged single-machine behavior).
        deployment_mode = (os.environ.get("DOYOUTRADE_DEPLOYMENT_MODE") or "local").strip().lower()
        if deployment_mode not in ("local", "cloud"):
            deployment_mode = "local"
        try:
            agent_repo = getattr(assistant_service, "agent_repo", None)
            if agent_repo is None:
                raise RuntimeError("assistant agent repository is not configured")
            route_repository = getattr(service, "model_route_repository", None)
            if route_repository is None:
                raise RuntimeError("model routes are not configured")
            configured = await agent_route_usable(route_repository, agent_repo)
            return {"configured": configured, "deployment_mode": deployment_mode}
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/setup/providers")
    async def get_setup_providers():
        from doyoutrade.onboarding import serialize_presets

        return {"items": serialize_presets()}

    @app.post("/setup/complete")
    async def complete_setup(payload: dict):
        from doyoutrade.onboarding import DEFAULT_ROUTE_NAME, create_route_and_bind_agent

        try:
            route_name = (
                _normalize_optional_string(payload.get("route_name"), field_name="route_name")
                or DEFAULT_ROUTE_NAME
            )
            kind = _normalize_required_string(payload.get("provider_kind"), field_name="provider_kind")
            allowed_kinds = capability_registry.model_provider_kinds()
            if kind not in allowed_kinds:
                raise ValueError(f"provider_kind must be one of: {', '.join(allowed_kinds)}")
            base_url = _normalize_optional_string(payload.get("base_url"), field_name="base_url")
            target_model = _normalize_optional_string(payload.get("target_model"), field_name="target_model")
            api_key = str(payload.get("api_key") or "")

            agent_repo = getattr(assistant_service, "agent_repo", None)
            if agent_repo is None:
                raise RuntimeError("assistant agent repository is not configured")
            route_repository = getattr(service, "model_route_repository", None)
            if route_repository is None:
                raise RuntimeError("model routes are not configured")

            resolved_name = await create_route_and_bind_agent(
                route_repository,
                agent_repo,
                route_name=route_name,
                provider_kind=kind,
                api_key=api_key,
                base_url=base_url,
                target_model=target_model,
            )
            return await service.get_model_route_api(resolved_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/system/state")
    async def get_system_state():
        return await service.get_system_state()

    @app.post("/system/kill-switch")
    async def set_kill_switch(payload: dict):
        enabled = bool(payload.get("enabled", True))
        await service.set_kill_switch(enabled)
        logger.warning("api kill switch updated enabled=%s", enabled)
        return await service.get_system_state()

    @app.post("/system/tick")
    async def tick_once():
        executed = await service.tick_once(source="manual")
        expired = await approval_gate.expire_pending() if hasattr(approval_gate, "expire_pending") else []
        logger.info(
            "api system tick handled executed=%s expired_count=%s",
            executed,
            len(expired),
        )
        return {"executed": executed, "expired_count": len(expired)}

    @app.post("/tasks")
    async def create_task(payload: dict):
        task_name = ""
        try:
            raw_settings = payload.get("settings")
            if raw_settings is None:
                raise ValueError("settings is required")
            normalized_settings = _normalize_settings(raw_settings)
            if normalized_settings is None:
                raise ValueError("settings is required")
            task_name = _normalize_required_string(payload.get("name"), field_name="name")
            if "universe" in normalized_settings:
                u = _normalize_symbol_list(normalized_settings.get("universe"), field_name="settings.universe")
                normalized_settings["universe"] = [] if u is None else u
            validate_api_task_settings(normalized_settings)
            instance = await service.create_task(
                name=task_name,
                mode=_normalize_optional_string(payload.get("mode"), field_name="mode"),
                description=_normalize_optional_string(
                    payload.get("description", ""),
                    field_name="description",
                )
                or "",
                data_provider=_normalize_optional_string(
                    payload.get("data_provider"),
                    field_name="data_provider",
                ),
                settings=normalized_settings,
            )
        except CatalogError as exc:
            raise HTTPException(
                status_code=400,
                detail=_catalog_error_detail(exc),
            ) from exc
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PersistenceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (KeyError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        logger.info(
            "api task created task_id=%s mode=%s",
            instance.task_id,
            instance.config.mode,
        )
        return await service.get_task_status(instance.task_id)

    @app.post("/tasks/{task_id}/start")
    async def start_task(task_id: str):
        try:
            await service.start_task(task_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        logger.info("api task started task_id=%s", task_id)
        return await service.get_task_status(task_id)

    @app.post("/tasks/{task_id}/pause")
    async def pause_task(task_id: str):
        try:
            await service.pause_task(task_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        logger.info("api task paused task_id=%s", task_id)
        return await service.get_task_status(task_id)

    @app.post("/tasks/{task_id}/stop")
    async def stop_task(task_id: str):
        try:
            await service.stop_task(task_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        logger.info("api task stopped task_id=%s", task_id)
        return await service.get_task_status(task_id)

    @app.delete("/tasks/{task_id}", status_code=204)
    async def delete_task(task_id: str):
        try:
            await service.delete_task(task_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        logger.info("api task deleted task_id=%s", task_id)

    @app.delete("/tasks", status_code=204)
    async def delete_tasks(payload: dict):
        raw_task_ids = payload.get("task_ids")
        if not isinstance(raw_task_ids, list):
            raise HTTPException(status_code=400, detail="task_ids must be a non-empty list")
        task_ids = [str(item).strip() for item in raw_task_ids if str(item).strip()]
        if not task_ids:
            raise HTTPException(status_code=400, detail="task_ids must be a non-empty list")
        try:
            await service.delete_tasks(task_ids)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        logger.info("api tasks deleted count=%s", len(task_ids))

    @app.put("/tasks/{task_id}")
    async def update_task(task_id: str, payload: dict):
        try:
            normalized_settings = _normalize_settings(payload.get("settings"))
            if normalized_settings is not None and "universe" in normalized_settings:
                u = _normalize_symbol_list(normalized_settings.get("universe"), field_name="settings.universe")
                normalized_settings["universe"] = [] if u is None else u

            if normalized_settings is not None:
                validate_optional_task_settings(normalized_settings)

            instance = await service.update_task(
                task_id,
                name=_normalize_optional_string(payload.get("name"), field_name="name"),
                mode=_normalize_optional_string(payload.get("mode"), field_name="mode"),
                description=_normalize_optional_string(payload.get("description"), field_name="description"),
                data_provider=_normalize_optional_string(payload.get("data_provider"), field_name="data_provider"),
                settings=normalized_settings,
            )
        except CatalogError as exc:
            raise HTTPException(
                status_code=400,
                detail=_catalog_error_detail(exc),
            ) from exc
        except (KeyError, ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        logger.info("api task updated task_id=%s", task_id)
        return instance

    # --- Task Triggers (Task-owned schedule + execution intent + delivery) -------
    def _trigger_repo():
        repo = getattr(service, "task_trigger_repository", None)
        if repo is None:
            raise HTTPException(status_code=503, detail="task_trigger_repository not configured")
        return repo

    async def _require_task(task_id: str):
        try:
            return await service.task_repository.get_task(task_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def _require_trading_task(task_id: str, *, feature_label: str):
        try:
            task = await service.get_task_status(task_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if str(task.get("mode") or "").strip() == "backtest":
            raise HTTPException(
                status_code=409,
                detail=f"backtest tasks do not support {feature_label}",
            )
        return task

    def _naive_utcnow():
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _trigger_schedule_fields(fields: dict) -> dict:
        """Map validated fields onto repo columns (kind-irrelevant keys -> None)."""
        return {
            "schedule_kind": fields["schedule_kind"],
            "interval_seconds": fields.get("interval_seconds"),
            "cron_expression": fields.get("cron_expression"),
            "timezone": fields.get("timezone", "UTC"),
            "at_iso": fields.get("at_iso"),
            "range_start": fields.get("range_start"),
            "range_end": fields.get("range_end"),
            "bar_interval": fields.get("bar_interval"),
            "trading_session": fields.get("trading_session"),
            "delete_after_run": fields.get("delete_after_run", False),
            "execution_intent": fields["execution_intent"],
        }

    @app.get("/tasks/{task_id}/triggers")
    async def list_task_triggers(task_id: str):
        await _require_trading_task(task_id, feature_label="task triggers")
        triggers = await _trigger_repo().list_for_task(task_id)
        return {"triggers": triggers}

    @app.get("/tasks/{task_id}/triggers/{trigger_id}")
    async def get_task_trigger(task_id: str, trigger_id: str):
        await _require_trading_task(task_id, feature_label="task triggers")
        try:
            return await _trigger_repo().get_trigger(trigger_id)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/tasks/{task_id}/triggers", status_code=201)
    async def create_task_trigger(task_id: str, payload: dict, request: Request):
        await _require_trading_task(task_id, feature_label="task triggers")
        try:
            fields = validate_trigger_input(payload)
        except TriggerValidationError as exc:
            raise HTTPException(status_code=400, detail=exc.to_payload()) from exc
        # Auto-fill the originating chat session for delivery target kind=session+origin
        # (parity with today's "push to the session I created it from" UX).
        delivery = fields.get("delivery_json")
        if isinstance(delivery, dict) and isinstance(delivery.get("target"), dict):
            tgt = delivery["target"]
            origin_sid = request.headers.get("X-DOYOUTRADE-Calling-Session-Id")
            if tgt.get("kind") == "session" and tgt.get("origin") and not tgt.get("session_id") and origin_sid:
                tgt["session_id"] = origin_sid
        now = _naive_utcnow()
        sched = _trigger_schedule_fields(fields)
        next_fire = compute_next_fire(
            schedule_kind=sched["schedule_kind"],
            interval_seconds=sched["interval_seconds"],
            cron_expression=sched["cron_expression"],
            timezone_str=sched["timezone"],
            at_iso=sched["at_iso"],
            last_fired_at=None,
            now=now,
        )
        snap = await _trigger_repo().create_trigger(
            task_id=task_id,
            name=str(payload.get("name") or ""),
            delivery_json=delivery,
            next_fire_at=next_fire,
            **sched,
        )
        logger.info("api task trigger created task_id=%s trigger_id=%s", task_id, snap.id)
        return snap

    @app.put("/tasks/{task_id}/triggers/{trigger_id}")
    async def update_task_trigger(task_id: str, trigger_id: str, payload: dict):
        await _require_trading_task(task_id, feature_label="task triggers")
        repo = _trigger_repo()
        try:
            existing = await repo.get_trigger(trigger_id)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        merged = {
            "schedule_kind": payload.get("schedule_kind", existing.schedule_kind),
            "interval_seconds": payload.get("interval_seconds", existing.interval_seconds),
            "cron_expression": payload.get("cron_expression", existing.cron_expression),
            "timezone": payload.get("timezone", existing.timezone),
            "at_iso": payload.get("at_iso", existing.at_iso),
            "range_start": payload.get("range_start", existing.range_start),
            "range_end": payload.get("range_end", existing.range_end),
            "bar_interval": payload.get("bar_interval", existing.bar_interval),
            "trading_session": payload.get("trading_session", existing.trading_session),
            "delete_after_run": payload.get("delete_after_run", existing.delete_after_run),
            "execution_intent": payload.get("execution_intent", existing.execution_intent),
        }
        if "delivery_json" in payload:
            merged["delivery_json"] = payload["delivery_json"]
        try:
            fields = validate_trigger_input(merged)
        except TriggerValidationError as exc:
            raise HTTPException(status_code=400, detail=exc.to_payload()) from exc
        sched = _trigger_schedule_fields(fields)
        now = _naive_utcnow()
        update_kwargs: dict = dict(sched)
        update_kwargs["next_fire_at"] = compute_next_fire(
            schedule_kind=sched["schedule_kind"],
            interval_seconds=sched["interval_seconds"],
            cron_expression=sched["cron_expression"],
            timezone_str=sched["timezone"],
            at_iso=sched["at_iso"],
            last_fired_at=existing.last_fired_at,
            now=now,
        )
        if "delivery_json" in fields:
            update_kwargs["delivery_json"] = fields["delivery_json"]
        if "name" in payload:
            update_kwargs["name"] = str(payload["name"] or "")
        if "enabled" in payload:
            update_kwargs["enabled"] = bool(payload["enabled"])
        snap = await repo.update_trigger(trigger_id, **update_kwargs)
        logger.info("api task trigger updated task_id=%s trigger_id=%s", task_id, trigger_id)
        return snap

    @app.post("/tasks/{task_id}/triggers/{trigger_id}/pause")
    async def pause_task_trigger(task_id: str, trigger_id: str):
        await _require_trading_task(task_id, feature_label="task triggers")
        try:
            return await _trigger_repo().update_trigger(trigger_id, status="paused")
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/tasks/{task_id}/triggers/{trigger_id}/resume")
    async def resume_task_trigger(task_id: str, trigger_id: str):
        await _require_trading_task(task_id, feature_label="task triggers")
        try:
            return await _trigger_repo().update_trigger(trigger_id, status="active", last_error="")
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/tasks/{task_id}/triggers/{trigger_id}/run")
    async def run_task_trigger(task_id: str, trigger_id: str):
        await _require_trading_task(task_id, feature_label="task triggers")
        repo = _trigger_repo()
        try:
            trigger = await repo.get_trigger(trigger_id)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # Manual fire is out-of-band and ungated by trading_session (matches `cron trigger`).
        run_id = await service.run_trigger(trigger)
        if run_id is not None:
            await repo.record_fire(
                trigger_id,
                last_fired_at=_naive_utcnow(),
                next_fire_at=trigger.next_fire_at,
                last_run_id=run_id,
                status=None,
                last_error="",
            )
            # Phase 2: best-effort post-cycle delivery per trigger.delivery_json.
            try:
                await deliver_trigger_result(
                    assistant_service,
                    trigger=trigger,
                    run_id=run_id,
                    cycle_run_repository=getattr(service, "cycle_run_repository", None),
                    instrument_catalog_repository=getattr(
                        service, "instrument_catalog_repository", None
                    ),
                    task_repository=getattr(service, "task_repository", None),
                )
            except Exception:
                logger.exception("manual trigger run delivery raised trigger_id=%s", trigger_id)
        logger.info(
            "api task trigger manual run task_id=%s trigger_id=%s run_id=%s",
            task_id,
            trigger_id,
            run_id,
        )
        return {"run_id": run_id}

    @app.delete("/tasks/{task_id}/triggers/{trigger_id}")
    async def delete_task_trigger(task_id: str, trigger_id: str):
        await _require_trading_task(task_id, feature_label="task triggers")
        await _trigger_repo().delete_trigger(trigger_id)
        logger.info("api task trigger deleted task_id=%s trigger_id=%s", task_id, trigger_id)
        return {"status": "deleted"}

    # --- Monitor rules (盯盘: standalone realtime condition-tree monitoring) ------
    def _monitor_repo():
        repo = getattr(service, "monitor_rule_repository", None)
        if repo is None:
            raise HTTPException(status_code=503, detail="monitor_rule_repository not configured")
        return repo

    def _monitor_alert_repo():
        repo = getattr(service, "monitor_alert_repository", None)
        if repo is None:
            raise HTTPException(status_code=503, detail="monitor_alert_repository not configured")
        return repo

    async def _reload_monitor_daemon():
        """Best-effort: tell the running daemon to re-pin symbols + rebuild its index."""
        daemon = getattr(app.state, "monitor_daemon", None)
        if daemon is None:
            return
        try:
            await daemon.reload_rules()
        except Exception as exc:  # noqa: BLE001 — visible, never blocks the CRUD response
            logger.warning("monitor daemon reload failed after rule change: %s", exc)

    def _validate_monitor_scope(scope_kind, scope_json) -> dict:
        if scope_kind not in ("watchlist_tag", "symbols"):
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "monitor_scope_kind_unknown",
                    "message": f"scope_kind must be 'watchlist_tag' or 'symbols', got {scope_kind!r}",
                },
            )
        if not isinstance(scope_json, dict):
            raise HTTPException(
                status_code=400,
                detail={"error_code": "monitor_scope_invalid", "message": "scope must be an object"},
            )
        if scope_kind == "symbols":
            symbols = scope_json.get("symbols")
            if not isinstance(symbols, list) or not symbols:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error_code": "monitor_scope_empty",
                        "message": "scope.symbols must be a non-empty list for scope_kind='symbols'",
                    },
                )
            return {"symbols": [str(s) for s in symbols]}
        tag = scope_json.get("tag")
        return {"tag": str(tag)} if tag not in (None, "") else {}

    def _normalize_monitor_delivery(payload: dict) -> dict | None:
        """Accept a canonical ``delivery_json`` or a convenience channel_id+chat_id."""
        delivery = payload.get("delivery_json")
        if isinstance(delivery, dict):
            return delivery
        channel_id = payload.get("channel_id")
        chat_id = payload.get("chat_id")
        if channel_id and chat_id:
            return {
                "mode": "card",
                "target": {"kind": "channel", "channel_id": str(channel_id), "chat_id": str(chat_id)},
            }
        session_id = payload.get("session_id")
        if session_id:
            return {"mode": "card", "target": {"kind": "session", "session_id": str(session_id)}}
        return None

    async def _resolve_monitor_symbols(rule) -> tuple[list[str], dict[str, str | None]]:
        scope = rule.scope_json or {}
        if rule.scope_kind == "symbols":
            return [str(s) for s in (scope.get("symbols") or [])], {}
        wl = getattr(service, "watchlist_repository", None)
        if wl is None:
            return [], {}
        tag = scope.get("tag")
        tag_arg = None if tag in (None, "", "*") else str(tag)
        entries = await wl.list_entries(tag_arg)
        symbols: list[str] = []
        names: dict[str, str | None] = {}
        for entry in entries:
            sym = entry.get("symbol")
            if sym:
                symbols.append(str(sym))
                names[str(sym)] = entry.get("display_name")
        return symbols, names

    @app.get("/monitors")
    async def list_monitors():
        rules = await _monitor_repo().list_rules()
        return {"items": rules, "total": len(rules)}

    @app.get("/monitors/{monitor_id}")
    async def get_monitor(monitor_id: str):
        try:
            return await _monitor_repo().get_rule(monitor_id)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail={"error_code": "monitor_not_found", "message": str(exc)}) from exc

    @app.post("/monitors", status_code=201)
    async def create_monitor(payload: dict):
        scope_kind = payload.get("scope_kind")
        scope_json = payload.get("scope") if isinstance(payload.get("scope"), dict) else payload.get("scope_json")
        scope_json = _validate_monitor_scope(scope_kind, scope_json if isinstance(scope_json, dict) else {})
        try:
            condition_json = validate_condition_tree(payload.get("condition_json"))
        except MonitorConditionError as exc:
            raise HTTPException(status_code=400, detail=exc.to_payload()) from exc
        delivery_json = _normalize_monitor_delivery(payload)
        cooldown = payload.get("cooldown_seconds")
        cooldown_seconds = int(cooldown) if isinstance(cooldown, int) and not isinstance(cooldown, bool) else 300
        try:
            snap = await _monitor_repo().create_rule(
                name=str(payload.get("name") or ""),
                enabled=bool(payload.get("enabled", True)),
                status="active",
                scope_kind=scope_kind,
                scope_json=scope_json,
                condition_json=condition_json,
                delivery_json=delivery_json,
                cooldown_seconds=cooldown_seconds,
            )
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await _reload_monitor_daemon()
        logger.info("api monitor created monitor_id=%s scope_kind=%s", snap.id, scope_kind)
        return snap

    @app.put("/monitors/{monitor_id}")
    async def update_monitor(monitor_id: str, payload: dict):
        repo = _monitor_repo()
        try:
            existing = await repo.get_rule(monitor_id)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail={"error_code": "monitor_not_found", "message": str(exc)}) from exc
        fields: dict = {}
        if "name" in payload:
            fields["name"] = str(payload.get("name") or "")
        if "enabled" in payload:
            fields["enabled"] = bool(payload.get("enabled"))
        if "status" in payload and payload.get("status") in ("active", "paused", "error"):
            fields["status"] = payload["status"]
        if "cooldown_seconds" in payload and isinstance(payload["cooldown_seconds"], int) and not isinstance(payload["cooldown_seconds"], bool):
            fields["cooldown_seconds"] = int(payload["cooldown_seconds"])
        if "scope_kind" in payload or "scope" in payload or "scope_json" in payload:
            scope_kind = payload.get("scope_kind", existing.scope_kind)
            raw_scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else payload.get("scope_json")
            fields["scope_kind"] = scope_kind
            fields["scope_json"] = _validate_monitor_scope(scope_kind, raw_scope if isinstance(raw_scope, dict) else existing.scope_json)
        if "condition_json" in payload:
            try:
                fields["condition_json"] = validate_condition_tree(payload.get("condition_json"))
            except MonitorConditionError as exc:
                raise HTTPException(status_code=400, detail=exc.to_payload()) from exc
        if "delivery_json" in payload or "channel_id" in payload or "session_id" in payload:
            fields["delivery_json"] = _normalize_monitor_delivery(payload)
        snap = await repo.update_rule(monitor_id, **fields)
        await _reload_monitor_daemon()
        logger.info("api monitor updated monitor_id=%s", monitor_id)
        return snap

    @app.delete("/monitors/{monitor_id}")
    async def delete_monitor(monitor_id: str):
        await _monitor_repo().delete_rule(monitor_id)
        await _reload_monitor_daemon()
        logger.info("api monitor deleted monitor_id=%s", monitor_id)
        return {"status": "deleted"}

    @app.get("/monitors/{monitor_id}/alerts")
    async def list_monitor_alerts(monitor_id: str, symbol: str | None = None, limit: int = 100):
        # Ensure the rule exists (404 otherwise) so a typo'd id is visible.
        try:
            await _monitor_repo().get_rule(monitor_id)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail={"error_code": "monitor_not_found", "message": str(exc)}) from exc
        alerts = await _monitor_alert_repo().list_for_rule(
            monitor_id, symbol=symbol, limit=max(1, min(int(limit), 500))
        )
        return {"items": alerts, "total": len(alerts)}

    @app.post("/monitors/{monitor_id}/run-once")
    async def run_monitor_once(monitor_id: str):
        """Evaluate the rule against current one-shot snapshots WITHOUT persisting alerts.

        A dry-run for testing a rule's condition tree. Returns per-symbol match
        results. When qmt is disconnected, returns ``qmt_disconnected`` placeholders
        rather than failing (parity with /market/quotes), so the call is never a 500.
        """
        try:
            rule = await _monitor_repo().get_rule(monitor_id)
        except RecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail={"error_code": "monitor_not_found", "message": str(exc)}) from exc

        from datetime import datetime, timezone
        from doyoutrade.monitoring.evaluator import EvalContext, MonitorEvalError, evaluate_tree
        from doyoutrade.monitoring.state import SymbolIntradayState, trading_day_for

        symbols, _names = await _resolve_monitor_symbols(rule)
        now = datetime.now(timezone.utc)
        trading_day = trading_day_for(now)
        with tracer.start_as_current_span("monitor.run_once") as span:
            span.set_attribute("monitor.rule_id", monitor_id)
            span.set_attribute("monitor.symbol_count", len(symbols))
            results: list[dict] = []
            matched_count = 0
            quotes: dict = {}
            if symbols and quote_stream_service is not None:
                quotes = await quote_stream_service.fetch_once(symbols)
            for sym in symbols:
                snap = quotes.get(sym)
                if snap is None:
                    results.append({"symbol": sym, "matched": False, "status": "qmt_disconnected" if quote_stream_service is None else "no_data"})
                    continue
                state = SymbolIntradayState(symbol=sym, trading_day=trading_day)
                state.fold_snapshot(snap)  # seed peak so a sealed snapshot has a baseline
                ctx = EvalContext(snapshot=snap, state=state, now=now)
                try:
                    triggered, leaves = evaluate_tree(rule.condition_json, ctx)
                except MonitorEvalError as exc:
                    results.append({"symbol": sym, "matched": False, "error": exc.reason})
                    continue
                if triggered:
                    matched_count += 1
                results.append({
                    "symbol": sym,
                    "matched": triggered,
                    "status": getattr(snap, "status", "ok"),
                    "matched_leaves": [l.label for l in leaves if l.triggered],
                    "quote": snap.to_dict(),
                })
            span.set_attribute("monitor.matched_count", matched_count)
            await emit_debug_event(
                "monitor_run_once",
                {
                    "monitor_rule_id": monitor_id,
                    "symbol_count": len(symbols),
                    "matched_count": matched_count,
                    "hint": "dry-run evaluation of a monitor rule against current snapshots (no alerts persisted)",
                },
            )
        return {
            "monitor_id": monitor_id,
            "evaluated_at": now.replace(tzinfo=None).isoformat(),
            "symbols": results,
            "matched_count": matched_count,
        }

    # --- Decision signals (功能 5: 决策信号落库 → 回测验证闭环) --------------
    def _decision_signal_unavailable(exc: RuntimeError) -> HTTPException:
        return HTTPException(
            status_code=503,
            detail={"error_code": "decision_signal_unwired", "message": str(exc)},
        )

    @app.get("/decision-signals")
    async def list_decision_signals(
        task_id: str | None = None,
        run_id: str | None = None,
        symbol: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        try:
            return await service.list_decision_signals(
                task_id=task_id,
                run_id=run_id,
                symbol=symbol,
                status=status,
                limit=max(1, min(int(limit), 500)),
                offset=max(0, int(offset)),
            )
        except RuntimeError as exc:
            raise _decision_signal_unavailable(exc) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error_code": "validation_error", "message": str(exc)},
            ) from exc

    @app.get("/decision-signals/{signal_id}")
    async def get_decision_signal(signal_id: str):
        try:
            return await service.get_decision_signal(signal_id)
        except RuntimeError as exc:
            raise _decision_signal_unavailable(exc) from exc
        except RecordNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"error_code": "decision_signal_not_found", "message": str(exc)},
            ) from exc

    @app.post("/decision-signals/{signal_id}/evaluate")
    async def evaluate_decision_signal(signal_id: str, payload: dict | None = None):
        body = payload if isinstance(payload, dict) else {}
        horizon = body.get("horizon")
        provider = body.get("provider")
        try:
            result = await service.evaluate_decision_signal(
                signal_id,
                horizon=str(horizon) if horizon is not None else None,
                provider=str(provider) if provider is not None else None,
            )
        except RuntimeError as exc:
            raise _decision_signal_unavailable(exc) from exc
        except RecordNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"error_code": "decision_signal_not_found", "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error_code": "validation_error", "message": str(exc)},
            ) from exc
        logger.info(
            "api decision signal evaluate signal_id=%s status=%s",
            signal_id,
            result.get("status"),
        )
        return result

    async def _compose_backtest_task_name(
        definition_id: str,
        universe: list[str],
        range_start: str,
        range_end: str,
    ) -> str:
        """Build a human-readable name for an auto-created backtest task.

        Format: ``{strategy_name} · {stock_label} · {start}~{end}``. Naming is
        best-effort: a strategy / stock lookup that fails falls back to the raw
        definition_id / symbol code and logs a warning — resolving a display
        name must never block task creation.
        """
        strategy_label = definition_id
        if strategy_definition_repository is not None:
            try:
                snapshot = await strategy_definition_repository.get_definition(definition_id)
                resolved = getattr(snapshot, "name", None)
                if isinstance(resolved, str) and resolved.strip():
                    strategy_label = resolved.strip()
            except RecordNotFoundError:
                logger.info(
                    "backtest auto-name: strategy definition %s not found, using id",
                    definition_id,
                )
            except Exception as exc:  # best-effort naming; never block task creation
                logger.warning(
                    "backtest auto-name strategy lookup failed definition_id=%s error_type=%s msg=%s",
                    definition_id,
                    type(exc).__name__,
                    exc,
                )

        first_symbol = universe[0]
        stock_label = first_symbol
        try:
            item = await service.get_instrument_catalog_item(first_symbol)
            display = item.get("display_name") if isinstance(item, dict) else None
            if isinstance(display, str) and display.strip():
                stock_label = display.strip()
        except Exception as exc:  # best-effort naming; fall back to symbol code
            logger.warning(
                "backtest auto-name stock lookup failed symbol=%s error_type=%s msg=%s",
                first_symbol,
                type(exc).__name__,
                exc,
            )
        if len(universe) > 1:
            stock_label = f"{stock_label}等{len(universe)}只"

        return f"{strategy_label} · {stock_label} · {range_start}~{range_end}"

    @app.post("/backtest-runs", status_code=201)
    async def create_backtest_run(payload: dict):
        from doyoutrade.tools import TERMINAL_BACKTEST_STATUSES

        try:
            range_start = _normalize_required_string(payload.get("range_start"), field_name="range_start")
            range_end = _normalize_required_string(payload.get("range_end"), field_name="range_end")
            task_id = _normalize_optional_string(payload.get("task_id"), field_name="task_id")
            definition_id = _normalize_optional_string(
                payload.get("definition_id"),
                field_name="definition_id",
            )
            entry_modes = [task_id, definition_id]
            if sum(1 for mode in entry_modes if mode) != 1:
                raise ValueError("pass exactly one of task_id or definition_id")

            auto_created_task_id: str | None = None
            if definition_id is not None:
                universe = _normalize_symbol_list(payload.get("universe"), field_name="universe")
                if not universe:
                    raise ValueError("definition mode requires a non-empty universe")
                name = _normalize_optional_string(payload.get("name"), field_name="name")
                if name is None:
                    name = await _compose_backtest_task_name(
                        definition_id, universe, range_start, range_end
                    )
                strategy_block: dict[str, Any] = {"definition_id": definition_id}
                parameters = _normalize_object(payload.get("parameters"), field_name="parameters")
                if isinstance(parameters, dict) and parameters:
                    strategy_block["parameter_overrides"] = parameters
                auto_settings: dict[str, Any] = {
                    "strategy": strategy_block,
                    "universe": universe,
                }
                validate_api_task_settings(auto_settings)
                created = await service.create_task(
                    name=name,
                    mode="backtest",
                    description="",
                    data_provider=_normalize_optional_string(
                        payload.get("data_provider") or "auto",
                        field_name="data_provider",
                    ),
                    settings=auto_settings,
                )
                task_id = getattr(created, "task_id", None)
                if not isinstance(task_id, str) or not task_id:
                    raise RuntimeError("platform did not return a task_id for the auto-created backtest task")
                auto_created_task_id = task_id

            assert task_id is not None
            # Defaults to full debug observability (preserve historical behavior);
            # explicit False runs in fast mode (no trace persistence).
            debug_enabled_raw = _normalize_bool(payload.get("debug_enabled"), field_name="debug_enabled")
            debug_enabled = True if debug_enabled_raw is None else bool(debug_enabled_raw)
            row = await service.start_backtest_job(
                task_id,
                range_start=range_start,
                range_end=range_end,
                market_profile=_normalize_optional_string(
                    payload.get("market_profile"),
                    field_name="market_profile",
                ),
                bar_interval=_normalize_optional_string(payload.get("bar_interval"), field_name="bar_interval"),
                config_overrides=_normalize_object(payload.get("config_overrides"), field_name="config_overrides"),
                model_route_name=_normalize_optional_string(
                    payload.get("model_route_name"),
                    field_name="model_route_name",
                ),
                debug_enabled=debug_enabled,
            )

            run_id = row.get("run_id") if isinstance(row, dict) else None
            if not isinstance(run_id, str) or not run_id:
                raise RuntimeError("backtest job did not return a valid run_id")

            timeout_seconds = float(payload.get("timeout_seconds", 120.0) or 0.0)
            poll_interval_seconds = max(float(payload.get("poll_interval_seconds", 0.2) or 0.0), 0.0)
            run = dict(row)
            if timeout_seconds > 0:
                deadline = asyncio.get_running_loop().time() + timeout_seconds
                while True:
                    run = await service.get_backtest_job(task_id, run_id)
                    status = str(run.get("status") or "").strip().lower()
                    if status in TERMINAL_BACKTEST_STATUSES:
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        break
                    await asyncio.sleep(poll_interval_seconds)

            result: dict[str, Any] = {
                "status": run.get("status") or row.get("status"),
                "run_id": run_id,
                "task_id": task_id,
            }
            if auto_created_task_id is not None:
                result["auto_created_task_id"] = auto_created_task_id
            summary_getter = getattr(service, "get_backtest_summary", None)
            if callable(summary_getter):
                summary_result = await summary_getter(run_id)
                if isinstance(summary_result, dict) and summary_result.get("summary_state") == "ok":
                    summary = summary_result.get("summary")
                    if isinstance(summary, dict):
                        result["summary"] = summary
            return result
        except CatalogError as exc:
            raise HTTPException(
                status_code=400,
                detail=_catalog_error_detail(exc),
            ) from exc
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/backtest-runs/{run_id}/summary")
    async def get_backtest_run_summary(run_id: str, format: str = Query(default="json")):
        getter = getattr(service, "get_backtest_summary", None)
        if not callable(getter):
            raise HTTPException(status_code=404, detail="backtest summary not available")
        try:
            result = await getter(run_id)
            if not isinstance(result, dict):
                raise HTTPException(status_code=404, detail="backtest summary not found")
            if format == "markdown":
                from doyoutrade.backtest.summary import render_summary_markdown

                summary = result.get("summary")
                if not isinstance(summary, dict):
                    raise HTTPException(status_code=404, detail="backtest summary not found")
                return {"format": "markdown", "markdown": render_summary_markdown(summary)}
            return result
        except HTTPException:
            raise
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/backtest-runs/{run_id}/suggest-iteration")
    async def suggest_backtest_iteration(run_id: str):
        return await _execute_cli_tool_payload("suggest_strategy_iteration", {"run_id": run_id})

    @app.post("/tasks/{task_id}/debug-sessions", status_code=202)
    async def create_debug_session(task_id: str, payload: dict):
        await _require_trading_task(task_id, feature_label="debug sessions")
        try:
            return await service.start_debug_session(
                task_id,
                input_overrides=_normalize_object(
                    payload.get("input_overrides"),
                    field_name="input_overrides",
                ),
            )
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/tasks/{task_id}/debug-sessions")
    async def list_debug_sessions(task_id: str):
        try:
            return await service.list_debug_sessions(task_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/tasks/{task_id}/debug-sessions/{session_id}")
    async def get_debug_session(task_id: str, session_id: str):
        try:
            return await service.get_debug_session(task_id, session_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))


    @app.post("/tasks/{task_id}/runs", status_code=202)
    async def create_backtest_job(task_id: str, payload: dict):
        try:
            range_start = _normalize_required_string(payload.get("range_start"), field_name="range_start")
            range_end = _normalize_required_string(payload.get("range_end"), field_name="range_end")
            market_profile = _normalize_optional_string(payload.get("market_profile"), field_name="market_profile")
            bar_interval = _normalize_optional_string(payload.get("bar_interval"), field_name="bar_interval")
            config_overrides = _normalize_object(
                payload.get("config_overrides"),
                field_name="config_overrides",
            )
            model_route_name = _normalize_optional_string(
                payload.get("model_route_name"), field_name="model_route_name"
            )
            # Defaults to full debug observability; explicit false runs fast mode.
            debug_enabled_raw = _normalize_bool(payload.get("debug_enabled"), field_name="debug_enabled")
            debug_enabled = True if debug_enabled_raw is None else bool(debug_enabled_raw)
            return await service.start_backtest_job(
                task_id,
                range_start=range_start,
                range_end=range_end,
                market_profile=market_profile,
                bar_interval=bar_interval,
                config_overrides=config_overrides,
                model_route_name=model_route_name,
                debug_enabled=debug_enabled,
            )
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CatalogError as exc:
            raise HTTPException(
                status_code=400,
                detail=_catalog_error_detail(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/tasks/{task_id}/runs")
    async def list_backtest_jobs(
        task_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        try:
            return await service.list_backtest_jobs(task_id, limit=limit, offset=offset)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/tasks/{task_id}/runs/{run_id}")
    async def get_backtest_job(task_id: str, run_id: str):
        try:
            return await service.get_backtest_job(task_id, run_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/tasks/{task_id}/runs/{run_id}/chart")
    async def get_backtest_chart(
        task_id: str,
        run_id: str,
        symbol: str | None = Query(default=None, max_length=64),
    ):
        try:
            return await service.get_backtest_chart(
                task_id,
                run_id,
                symbol=_normalize_optional_string(symbol, field_name="symbol"),
            )
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/tasks/{task_id}/runs/{run_id}/pause")
    async def pause_backtest_job(task_id: str, run_id: str):
        try:
            return await service.pause_backtest_job(task_id, run_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/tasks/{task_id}/runs/{run_id}/resume")
    async def resume_backtest_job(task_id: str, run_id: str):
        try:
            return await service.resume_backtest_job(task_id, run_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/tasks/{task_id}/runs/{run_id}/stop")
    async def stop_backtest_job(task_id: str, run_id: str):
        try:
            return await service.stop_backtest_job(task_id, run_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/tasks/{task_id}/cycle-runs")
    async def list_cycle_runs(
        task_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        q: str | None = Query(
            default=None,
            max_length=80,
            description="Substring match on run_id",
        ),
        status: str | None = Query(default=None, max_length=32),
        run_kind: str | None = Query(default=None, max_length=16),
        run_mode: str | None = Query(default=None, max_length=32),
        exclude_run_kind: str | None = Query(
            default=None,
            max_length=16,
            description="Omit cycles whose run_kind equals this value (e.g. debug).",
        ),
        started_after: str | None = Query(
            default=None,
            description="ISO-8601 lower bound for wall_started_at (inclusive)",
        ),
        started_before: str | None = Query(
            default=None,
            description="ISO-8601 upper bound for wall_started_at (inclusive)",
        ),
        parent_run_id: str | None = Query(
            default=None,
            max_length=64,
            alias="run_id",
            description="When set, only cycles whose session_id matches this run's session_id",
        ),
    ):
        try:
            return await service.list_cycle_runs(
                task_id,
                limit=limit,
                offset=offset,
                run_id_contains=q,
                status=status,
                run_kind=run_kind,
                run_mode=run_mode,
                exclude_run_kind=exclude_run_kind,
                started_after=started_after,
                started_before=started_before,
                run_id=parent_run_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/cycle-runs/{run_id}")
    async def get_cycle_run(run_id: str):
        try:
            return await service.get_cycle_run(run_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/cycle-runs/{run_id}/debug-view")
    async def get_run_debug_view(run_id: str):
        try:
            payload = await service.get_run_debug_view(run_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        # Enrich with the user-facing push surface (cards / approvals / agent /
        # session). Isolated so an enrichment fault never breaks the core debug
        # view — it surfaces an explicit reason instead (§错误可见性).
        try:
            payload["push_detail"] = await _build_push_detail(payload)
        except Exception:
            logger.warning("push_detail assembly failed run_id=%s", run_id, exc_info=True)
            payload["push_detail"] = {
                "resolved_from_kind": "",
                "strategy": {"name": None, "task_id": None, "reason": "push_detail_unavailable"},
                "composer_agent": {"agent": None, "agent_id": None, "compose_mode": None, "reason": "push_detail_unavailable"},
                "assistant_session": {"session": None, "reason": "push_detail_unavailable"},
                "pushed_messages": {"items": [], "reason": "push_detail_unavailable"},
                "approvals": {"items": [], "total": 0, "reason": "push_detail_unavailable"},
            }
        return payload

    @app.get("/traces/{trace_id}/debug-view")
    async def get_trace_debug_view(trace_id: str):
        try:
            return await service.get_trace_debug_view(trace_id)
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    def _serialize_pending_approval(item, snapshot: dict | None = None) -> dict:
        # Enriched view so the Feishu card / web Approvals page can show WHAT is
        # being approved (symbol / 买卖 / notional) and link back to the task,
        # without re-parsing intent_payload. notional is a decimal string
        # (§金额十进制). Legacy rows (pre-resume migration) return null for the
        # new fields — the frontend types mark them optional.
        return {
            "approval_id": item.approval_id,
            "intent_id": item.intent_id,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            "status": getattr(item, "status", None),
            "mode": getattr(item, "mode", None),
            "task_id": getattr(item, "task_id", None),
            "run_id": getattr(item, "run_id", None),
            "account_id": getattr(item, "account_id", None),
            "symbol": getattr(item, "symbol", None),
            "action": getattr(item, "action", None),
            "notional": getattr(item, "notional", None),
            "resolver_id": getattr(item, "resolver_id", None),
            "decision_source": getattr(item, "decision_source", None),
            "dispatched_at": (
                item.dispatched_at.isoformat()
                if getattr(item, "dispatched_at", None)
                else None
            ),
            # Dispatch receipt: did the approved order actually go out / fail?
            # ``dispatch_error`` set (no fill) ⇒ 失败; ``dispatched_at`` set + no
            # error ⇒ 已派发/成交; status rejected/expired ⇒ 放弃. The matched
            # broker fill (by intent_id) is attached separately by the cycle-run
            # debug view (it lands in a later resume cycle, not this run).
            "dispatch_error": getattr(item, "dispatch_error", None) or None,
            "dispatch_attempts": getattr(item, "dispatch_attempts", None),
            # History-view fields: who/why/when a decision landed. Harmless on
            # the pending list (always null/empty there); required for the full
            # Approvals table to show resolved rows.
            "reason": getattr(item, "reason", None) or None,
            "decided_at": (
                item.decided_at.isoformat()
                if getattr(item, "decided_at", None)
                else None
            ),
            "resolved_at": (
                item.resolved_at.isoformat()
                if getattr(item, "resolved_at", None)
                else None
            ),
            # Signal + order context (理由 / signal_tag / 策略 / 限价 / 订单类型 /
            # 有效期 / 平仓原因) parsed from the persisted intent so the web/Chat
            # card matches the Feishu card. Display-only; empty when absent.
            **signal_context_from_intent_json(getattr(item, "intent_payload", None)),
            # Signal-time 行情/判断 (现价 / 涨跌幅 / 方向) from the order's cycle
            # digest — the same data the pure signal digest carried. Empty when the
            # digest is unavailable (e.g. legacy / synthetic rows).
            "last_price": (snapshot or {}).get("last_price") or None,
            "pct_change": (snapshot or {}).get("pct_change") or None,
            "direction": (snapshot or {}).get("direction") or None,
            # Instrument display name (工商银行) so the web/Chat card names the
            # stock, not just the opaque symbol.
            "symbol_name": (snapshot or {}).get("symbol_name") or None,
        }

    async def _approval_snapshots(items: list) -> dict:
        """Map approval_id → {symbol_name, 行情/判断} for a page of approvals.

        Best-effort + deduped (one digest fetch per distinct run_id, one name
        lookup per distinct symbol), so the Approvals table / pending poll
        enriches without N redundant lookups. ``symbol_name`` resolves for every
        row (it needs only the symbol); the 行情/判断 fields need the order's cycle
        digest (run_id). Any missing repo / lookup failure leaves those fields
        empty — the card still renders its intent-derived facts.
        """
        cycle_repo = getattr(service, "cycle_run_repository", None)
        catalog_repo = getattr(service, "instrument_catalog_repository", None)
        result: dict = {}
        digest_cache: dict = {}
        name_cache: dict = {}
        for item in items:
            approval_id = getattr(item, "approval_id", None)
            run_id = getattr(item, "run_id", None)
            symbol = getattr(item, "symbol", None)
            entry: dict = {
                "symbol_name": None,
                "last_price": None,
                "pct_change": None,
                "direction": None,
            }
            # Display name (by symbol, deduped) — resolves regardless of run_id.
            if symbol and catalog_repo is not None:
                if symbol not in name_cache:
                    try:
                        row = await catalog_repo.get(symbol)
                        name_cache[symbol] = (
                            str(row.get("display_name") or "") if isinstance(row, dict) else ""
                        )
                    except Exception:
                        logger.warning(
                            "approval snapshot: symbol name lookup failed symbol=%s",
                            symbol,
                            exc_info=True,
                        )
                        name_cache[symbol] = ""
                entry["symbol_name"] = name_cache[symbol] or None
            # Signal-time 行情/判断 (by run_id + symbol).
            if run_id and symbol and cycle_repo is not None:
                if run_id not in digest_cache:
                    try:
                        digest_cache[run_id] = await cycle_repo.get_by_run_id(run_id)
                    except Exception:
                        logger.warning(
                            "approval snapshot: cycle digest lookup failed run_id=%s",
                            run_id,
                            exc_info=True,
                        )
                        digest_cache[run_id] = None
                digest = digest_cache[run_id]
                if isinstance(digest, dict):
                    details = digest.get("details") or {}
                    market = (details.get("market_snapshot") or {}).get(symbol) or {}
                    diag = (details.get("signal_diagnostics") or {}).get(symbol) or {}
                    last_price = market.get("last_price") if isinstance(market, dict) else None
                    pct = market.get("pct_change") if isinstance(market, dict) else None
                    direction = diag.get("direction") if isinstance(diag, dict) else None
                    try:
                        pct_f = None if pct is None else float(pct)
                    except (TypeError, ValueError):
                        pct_f = None
                    sign = "+" if isinstance(pct_f, float) and pct_f > 0 else ""
                    entry["last_price"] = None if last_price is None else str(last_price)
                    entry["pct_change"] = None if pct_f is None else f"{sign}{pct_f:.2f}%"
                    entry["direction"] = str(direction) if direction else None
            result[approval_id] = entry
        return result

    async def _resolve_agent_dict(agent_id):
        """Serialize an assistant agent by id (already main-agent-overridden).

        Tries the dedicated agent repo, then the assistant repository. Returns
        ``(agent_dict | None, reason | None)`` — an absent agent yields an
        explicit ``composer_agent_not_found`` reason, never a silent null.
        """
        aid = str(agent_id or "").strip()
        if not aid:
            return None, "composer_agent_unspecified"
        for holder in ("agent_repo", "repository"):
            repo = getattr(assistant_service, holder, None) if assistant_service else None
            getter = getattr(repo, "get_agent", None) if repo is not None else None
            if getter is None:
                continue
            try:
                agent = await getter(aid)
            except (KeyError, RecordNotFoundError):
                agent = None
            except Exception:
                logger.warning("push_detail: agent lookup failed agent_id=%s", aid, exc_info=True)
                agent = None
            if agent:
                return agent, None
        return None, "composer_agent_not_found"

    async def _build_push_detail(payload: dict) -> dict:
        """Aggregate the user-facing push surface for a cycle run.

        Reconstructs, from persisted state and matching the REAL push: the
        actual pushed card messages (assistant_messages), the approvals tied to
        this run (with dispatch receipt + matched fill), the composer/cron
        assistant agent, the strategy/task name and the landing assistant
        session. Pure read-side; every empty subsection carries an explicit
        ``reason`` (§错误可见性 — no silent empties).
        """
        rf = str(((payload.get("resolved_from") or {}).get("identifier_type")) or "")
        cycle_run = payload.get("cycle_run") or {}
        run_id = str(cycle_run.get("run_id") or "").strip()
        run_kind = str(cycle_run.get("run_kind") or "").strip()
        task_id = cycle_run.get("task_id")
        trace_id = str(cycle_run.get("trace_id") or "").strip()
        agent_name = cycle_run.get("agent_name") or None
        strategy = {
            "name": agent_name,
            "task_id": task_id,
            "reason": None if agent_name else "cycle_run_no_strategy_name",
        }

        # Non cycle-run carriers (backtest job, debug session) never push.
        if rf != "cycle_run" or not run_id:
            suffix = "backtest" if rf == "backtest_job" else "debug_session"
            return {
                "resolved_from_kind": run_kind or rf,
                "strategy": strategy,
                "composer_agent": {"agent": None, "agent_id": None, "compose_mode": None, "reason": f"{suffix}_no_composer_agent"},
                "assistant_session": {"session": None, "reason": f"{suffix}_no_delivery"},
                "pushed_messages": {"items": [], "reason": f"{suffix}_no_delivery"},
                "approvals": {"items": [], "total": 0, "reason": f"{suffix}_no_approvals"},
            }

        composer_agent = {"agent": None, "agent_id": None, "compose_mode": None, "reason": None}
        assistant_session = {"session": None, "reason": None}
        pushed_messages = {"items": [], "reason": None}
        target_session_id = None
        msg_filter = None          # callable(metadata) -> bool
        delivery_status = None     # cron-only push outcome
        explicit_agent_id = None   # composer agent declared by the delivery binding
        reconstruct_trigger = None         # trigger obj → deterministic card fallback
        reconstruct_no_signal = "brief"
        reconstruct_compose_mode = None
        reconstruct_channel_target = None

        if run_kind == "trigger":
            trigger = None
            trigger_repo = getattr(service, "task_trigger_repository", None)
            trigger_id = cycle_run.get("trigger_id")
            if trigger_repo is not None and trigger_id:
                try:
                    trigger = await trigger_repo.get_trigger(trigger_id)
                except (KeyError, RecordNotFoundError):
                    trigger = None
                except Exception:
                    logger.warning("push_detail: trigger lookup failed trigger_id=%s", trigger_id, exc_info=True)
                    trigger = None
            if trigger is None:
                composer_agent["reason"] = "trigger_not_found"
                assistant_session["reason"] = "trigger_not_found"
                pushed_messages["reason"] = "trigger_not_found"
            else:
                delivery = getattr(trigger, "delivery_json", None) or {}
                mode = str(delivery.get("mode") or "").strip() or None
                composer_agent["compose_mode"] = mode
                if mode == "prose":
                    explicit_agent_id = delivery.get("composer_agent_id")
                else:
                    composer_agent["reason"] = "deterministic_card_no_composer_agent"
                target = delivery.get("target") or {}
                kind = str(target.get("kind") or "").strip()
                if kind == "session":
                    target_session_id = target.get("session_id")
                    msg_filter = lambda md: md.get("run_id") == run_id or md.get("cron_job_run_id") == run_id  # noqa: E731
                elif kind == "channel":
                    # Card went to a Feishu group (not an assistant session). The
                    # exact pushed card is read below from details.delivered_cards
                    # (recorded at delivery); older runs fall back to a
                    # deterministic reconstruction so the content is still shown.
                    assistant_session["reason"] = "channel_target_no_assistant_session"
                    reconstruct_trigger = trigger
                    reconstruct_no_signal = str(delivery.get("no_signal_mode") or "brief")
                    reconstruct_compose_mode = mode
                    reconstruct_channel_target = target.get("chat_name") or target.get("chat_id")
                else:
                    assistant_session["reason"] = "manual_run_no_delivery"
                    pushed_messages["reason"] = "manual_run_no_delivery"
        elif run_kind == "cron":
            cron_run = None
            if cron_run_repo is not None and trace_id:
                try:
                    runs = await cron_run_repo.list_by_trace_id(trace_id)
                except Exception:
                    logger.warning("push_detail: cron run lookup failed trace_id=%s", trace_id, exc_info=True)
                    runs = []
                cron_run = next((r for r in runs if r.get("pre_run_id") == run_id), None) or (runs[0] if runs else None)
            if cron_run is None:
                composer_agent["reason"] = "cron_run_not_found"
                assistant_session["reason"] = "cron_run_not_found"
                pushed_messages["reason"] = "cron_run_not_found"
            else:
                delivery_status = cron_run.get("delivery_status")
                cron_repo = getattr(cron_manager, "_repo", None) if cron_manager else None
                job = None
                if cron_repo is not None and cron_run.get("job_id"):
                    try:
                        job = await cron_repo.get_job(cron_run["job_id"])
                    except Exception:
                        logger.warning("push_detail: cron job lookup failed job_id=%s", cron_run.get("job_id"), exc_info=True)
                        job = None
                if job is None:
                    composer_agent["reason"] = "cron_job_not_found"
                else:
                    explicit_agent_id = job.get("agent_id")
                    target_session_id = (job.get("task_params_json") or {}).get("target_session_id")
                cron_run_id = cron_run.get("id")
                if target_session_id:
                    msg_filter = lambda md: md.get("cron_job_run_id") == cron_run_id  # noqa: E731
                else:
                    assistant_session["reason"] = "cron_no_target_session"
                    pushed_messages["reason"] = "cron_no_target_session"
        else:
            # manual / scheduled / debug / backtest_bar: the cycle carries no
            # delivery binding, so nothing was composed or pushed.
            composer_agent["reason"] = "manual_run_no_composer_agent"
            assistant_session["reason"] = "manual_run_no_delivery"
            pushed_messages["reason"] = "manual_run_no_delivery"

        # (1) Faithful source: the delivery orchestrator recorded the EXACT card
        # it pushed (Feishu channel pushes leave no assistant_messages row). This
        # wins over everything below — it IS what the user received.
        details = cycle_run.get("details") or {}
        delivered = details.get("delivered_cards")
        if isinstance(delivered, list) and delivered:
            items = []
            for i, card in enumerate(delivered):
                if not isinstance(card, dict):
                    continue
                items.append({
                    "message_id": f"delivered-{i}",
                    "session_id": None,
                    "role": "assistant",
                    "content": card.get("content"),
                    "created_at": card.get("delivered_at"),
                    "source": "trigger",
                    "channel_target": card.get("chat_name") or card.get("chat_id"),
                    "delivery_status": card.get("status"),
                    "run_id": run_id,
                    "cron_job_run_id": None,
                    "reconstructed": False,
                    "note": None,
                })
            pushed_messages["items"] = items
            pushed_messages["reason"] = None
            target_session_id = None  # don't also dredge session messages (avoid dup)

        # Fetch the landing session + its messages once we have a target.
        repo = getattr(assistant_service, "repository", None) if assistant_service else None
        if target_session_id and repo is not None:
            sess = None
            try:
                sess = await repo.get_session(target_session_id)
            except (KeyError, RecordNotFoundError):
                sess = None
            except Exception:
                logger.warning("push_detail: session lookup failed session_id=%s", target_session_id, exc_info=True)
                sess = None
            if sess:
                assistant_session["session"] = {
                    "session_id": sess.get("session_id"),
                    "title": sess.get("title"),
                    "status": sess.get("status"),
                    "agent_id": sess.get("agent_id"),
                }
            elif assistant_session["reason"] is None:
                assistant_session["reason"] = "assistant_session_not_found"

            msgs = []
            try:
                msgs = await repo.list_messages(target_session_id, limit=200, offset=0)
            except Exception:
                logger.warning("push_detail: list_messages failed session_id=%s", target_session_id, exc_info=True)
                msgs = []
            items = []
            for m in msgs:
                md = m.get("metadata") or {}
                if msg_filter is not None and not msg_filter(md):
                    continue
                items.append({
                    "message_id": m.get("message_id"),
                    "session_id": m.get("session_id"),
                    "role": m.get("role"),
                    "content": m.get("content"),
                    "created_at": m.get("created_at"),
                    "source": md.get("source"),
                    "channel_target": md.get("channel_id") or md.get("chat_id"),
                    "delivery_status": delivery_status,
                    "run_id": md.get("run_id"),
                    "cron_job_run_id": md.get("cron_job_run_id"),
                    "reconstructed": False,
                    "note": None,
                })
            pushed_messages["items"] = items
            if not items and pushed_messages["reason"] is None:
                pushed_messages["reason"] = "no_persisted_message_for_run"

        # (2) Fallback for Feishu channel pushes predating delivered_cards: the
        # card itself was never persisted, but for a DETERMINISTIC card it is
        # exactly reproducible from the same (trigger, digest) inputs the push
        # used; for prose the AI text is gone, so we show the deterministic card
        # with an explicit note (better than a misleading "未推送卡片").
        if not pushed_messages["items"] and reconstruct_trigger is not None:
            rendered = None
            try:
                # Resolve the same operator-facing names (任务名 / 股票名称) the
                # live push now carries, so a reconstructed card matches the real
                # push instead of degrading back to opaque ids / codes.
                from doyoutrade.runtime.trigger_delivery import (
                    _collect_digest_symbols,
                    _resolve_symbol_names,
                    _resolve_task_name,
                )

                recon_task_name = await _resolve_task_name(
                    getattr(service, "task_repository", None),
                    getattr(reconstruct_trigger, "task_id", None),
                )
                recon_symbol_names = await _resolve_symbol_names(
                    getattr(service, "instrument_catalog_repository", None),
                    _collect_digest_symbols(cycle_run),
                )
                rendered = render_trigger_digest(
                    reconstruct_trigger, cycle_run, no_signal_mode=reconstruct_no_signal,
                    task_name=recon_task_name, symbol_names=recon_symbol_names,
                )
            except Exception:
                logger.warning("push_detail: digest reconstruction failed run_id=%s", run_id, exc_info=True)
                rendered = None
            if rendered:
                note = (
                    "实际推送为 AI 合成文案，原文未单独留存；此处为同一周期数据的确定性卡片重建"
                    if reconstruct_compose_mode == "prose"
                    else "确定性卡片重建（与实际推送内容一致）"
                )
                pushed_messages["items"] = [{
                    "message_id": f"reconstructed-{run_id}",
                    "session_id": None,
                    "role": "assistant",
                    "content": rendered,
                    "created_at": None,
                    "source": "trigger",
                    "channel_target": reconstruct_channel_target,
                    "delivery_status": None,
                    "run_id": run_id,
                    "cron_job_run_id": None,
                    "reconstructed": True,
                    "note": note,
                }]
                pushed_messages["reason"] = None
            elif pushed_messages["reason"] is None:
                pushed_messages["reason"] = "channel_only_no_persisted_message"

        # Composer agent: explicit binding wins; else the landing session's agent.
        if composer_agent["agent"] is None and composer_agent["reason"] in (None, "composer_agent_unspecified"):
            agent_id = explicit_agent_id or (assistant_session["session"] or {}).get("agent_id")
            composer_agent["agent_id"] = agent_id
            composer_agent["agent"], composer_agent["reason"] = await _resolve_agent_dict(agent_id)

        # Approvals for this run + dispatch receipt + matched fill (by intent_id).
        approvals_block = {"items": [], "total": 0, "reason": None}
        try:
            appr_items, appr_total = await approval_gate.list_approvals(run_id=run_id, limit=500, offset=0)
        except Exception:
            logger.warning("push_detail: approvals lookup failed run_id=%s", run_id, exc_info=True)
            appr_items, appr_total = [], 0
            approvals_block["reason"] = "approvals_unavailable"
        if appr_items:
            snapshots = await _approval_snapshots(appr_items)
            fill_repo = getattr(service, "trade_fill_repository", None)
            serialized = []
            for item in appr_items:
                row = _serialize_pending_approval(item, snapshots.get(getattr(item, "approval_id", None)))
                matched_fill = None
                intent_id = getattr(item, "intent_id", None)
                if fill_repo is not None and task_id and intent_id:
                    fill = None
                    try:
                        fill = await fill_repo.get_by_intent_id(task_id=task_id, intent_id=intent_id)
                    except Exception:
                        logger.warning("push_detail: matched fill lookup failed intent_id=%s", intent_id, exc_info=True)
                        fill = None
                    if fill:
                        matched_fill = {
                            "quantity": fill.get("quantity"),
                            "price": fill.get("price"),
                            "amount": fill.get("amount"),
                            "filled_at": fill.get("filled_at"),
                        }
                row["matched_fill"] = matched_fill
                serialized.append(row)
            approvals_block["items"] = serialized
            approvals_block["total"] = appr_total
        elif approvals_block["reason"] is None:
            approvals_block["reason"] = "no_approvals_for_run"

        return {
            "resolved_from_kind": run_kind,
            "strategy": strategy,
            "composer_agent": composer_agent,
            "assistant_session": assistant_session,
            "pushed_messages": pushed_messages,
            "approvals": approvals_block,
        }

    # Status values the history filter accepts; an unknown value is a malformed
    # query, not an empty result (§错误可见性 — surface it, do not swallow).
    _APPROVAL_QUERY_STATUSES = frozenset({"pending", "approved", "rejected", "expired"})

    def _parse_approval_ts(value: str, field: str):
        from datetime import datetime, timezone

        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": f"invalid_{field}",
                    "message": f"{field} must be an ISO-8601 datetime, got {value!r}",
                },
            ) from exc
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @app.get("/approvals")
    async def list_approvals(
        status: list[str] | None = Query(default=None),
        symbol: str | None = Query(default=None, max_length=32),
        task_id: str | None = Query(default=None, max_length=64),
        account_id: str | None = Query(default=None, max_length=64),
        decision_source: str | None = Query(default=None, max_length=16),
        q: str | None = Query(default=None, max_length=128),
        created_after: str | None = Query(default=None, max_length=64),
        created_before: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        # ``status`` accepts both repeated (?status=pending&status=approved) and
        # comma-joined (?status=pending,approved) forms.
        statuses: list[str] = []
        for raw in status or []:
            statuses.extend(part.strip() for part in raw.split(",") if part.strip())
        invalid = sorted({s for s in statuses if s not in _APPROVAL_QUERY_STATUSES})
        if invalid:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "invalid_status",
                    "message": f"unknown approval status filter(s): {invalid}",
                    "valid": sorted(_APPROVAL_QUERY_STATUSES),
                },
            )
        after = _parse_approval_ts(created_after, "created_after") if created_after else None
        before = _parse_approval_ts(created_before, "created_before") if created_before else None
        items, total = await approval_gate.list_approvals(
            statuses=statuses or None,
            symbol=symbol or None,
            task_id=task_id or None,
            account_id=account_id or None,
            decision_source=decision_source or None,
            search=q,
            created_after=after,
            created_before=before,
            limit=limit,
            offset=offset,
        )
        snapshots = await _approval_snapshots(items)
        return {
            "items": [
                _serialize_pending_approval(item, snapshots.get(getattr(item, "approval_id", None)))
                for item in items
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def _resolver_args_from_request(request: Request) -> tuple[str | None, str | None]:
        """Parse optional ``{resolver_id, reason}`` from the request body.

        approve/reject may be called with no body (legacy frontend) — tolerate an
        empty / non-JSON body and fall back to no resolver. The resolver_id is
        recorded for audit only (no hard auth this iteration, per design).
        """
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return None, None
        resolver_id = body.get("resolver_id")
        reason = body.get("reason")
        return (
            str(resolver_id) if resolver_id not in (None, "") else None,
            str(reason) if reason not in (None, "") else None,
        )

    @app.get("/approvals/pending")
    async def pending_approvals():
        items = await approval_gate.list_pending()
        snapshots = await _approval_snapshots(items)
        return [
            _serialize_pending_approval(item, snapshots.get(getattr(item, "approval_id", None)))
            for item in items
        ]

    @app.post("/approvals/{approval_id}/approve")
    async def approve(approval_id: str, request: Request):
        resolver_id, _reason = await _resolver_args_from_request(request)
        try:
            result = await approval_gate.approve(
                approval_id, resolver_id=resolver_id, decision_source="web"
            )
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        logger.info(
            "api approval approved approval_id=%s intent_id=%s resolver_id=%s",
            approval_id, result.intent_id, resolver_id,
        )
        return {
            "status": result.status,
            "intent_id": result.intent_id,
            "approval_id": result.approval_id,
        }

    @app.post("/approvals/{approval_id}/reject")
    async def reject(approval_id: str, request: Request):
        resolver_id, reason = await _resolver_args_from_request(request)
        try:
            result = await approval_gate.reject(
                approval_id,
                reason=reason or "api reject",
                resolver_id=resolver_id,
                decision_source="web",
            )
        except (KeyError, RecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except StateConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        logger.info(
            "api approval rejected approval_id=%s intent_id=%s resolver_id=%s",
            approval_id, result.intent_id, resolver_id,
        )
        return {
            "status": result.status,
            "intent_id": result.intent_id,
            "approval_id": result.approval_id,
        }

    return app
