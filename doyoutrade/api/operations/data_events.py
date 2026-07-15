"""``data_events`` operation — calendar / status events (停牌雷).

A sibling of ``data_fundamentals`` on the event axis. Takes ``code`` /
``symbols`` / ``universe_file`` (mutually exclusive), batch-fetches events
via the :class:`doyoutrade.data.protocols.EventProvider` (akshare suspension
snapshot today), and writes an ``events.csv`` artifact. The standalone
inspector behind ``stock screen --exclude-suspended``.

Failure-mode discipline (per CLAUDE.md §错误可见性): a provider error
surfaces ``events_fetch_failed``; symbols with no events are simply absent
from the per-symbol counts (not an error). Debug events:
``operation_data_events.request`` / ``.rejected`` / ``.failed`` / ``.created``.
"""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path
from typing import Any

from doyoutrade.api.operations.data_run import (
    _InvalidDataRunArgument,
    _load_universe_file,
    _parse_csv_symbols,
    _validate_canonical_codes,
)
from doyoutrade.api.operations.market_data import _get_artifacts_root
from doyoutrade.debug import emit_debug_event
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._coercion import SchemaCoercion
from doyoutrade.tools._prose import append_json_payload, format_error_text, format_unknown_args

logger = logging.getLogger(__name__)

_SUPPORTED_SOURCES = ("auto", "akshare")
_CSV_COLUMNS = ("code", "event_type", "event_date", "detail")
_PREVIEW_ROWS = 20


class _InvalidArg(ValueError):
    def __init__(self, error_code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint


class DataEventsTool(OperationHandler):
    name = "data_events"
    description = (
        "Fetch calendar / status events (suspension 停牌) for one or many "
        "A-share symbols and write an events CSV. The inspector behind "
        "stock screen --exclude-suspended."
    )
    category = "data"
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "symbols": {"type": "array", "items": {"type": "string"}},
            "universe_file": {"type": "string"},
            "asof": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
            "data_source": {"type": "string", "enum": list(_SUPPORTED_SOURCES), "default": "auto"},
            "output_path": {"type": "string"},
        },
        "additionalProperties": False,
    }
    coercion_rules = (
        SchemaCoercion(field="symbols", declared_type="array", item_type=str, error_code="invalid_symbols"),
    )

    def __init__(self, *, event_provider_factory=None) -> None:
        self._event_provider_factory = event_provider_factory

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                "operation_data_events.rejected",
                {"tool": self.name, "input_keys": sorted(kwargs.keys()), "error": contract.error},
            )
            return ToolResult(
                text=format_unknown_args(
                    list(contract.error.get("unknown", [])),
                    sorted(self._allowed_top_level_kwargs()),
                    dict(contract.error.get("suggested_path") or {}),
                ),
                is_error=True,
            )
        kwargs = dict(contract.kwargs)

        symbols_raw = kwargs.get("symbols")
        symbols_is_string = isinstance(symbols_raw, str)
        if symbols_is_string:
            kwargs.pop("symbols", None)
        coercion = self._apply_schema_coercion(kwargs)
        if coercion.error is not None:
            err = coercion.error
            await emit_debug_event("operation_data_events.failed", {"tool": self.name, **err})
            return ToolResult(
                text=format_error_text(
                    str(err.get("error_code") or "validation_error"),
                    str(err.get("error") or "invalid input"),
                    err.get("hint") if isinstance(err.get("hint"), str) else None,
                ),
                is_error=True,
            )
        kwargs = dict(coercion.kwargs)
        if symbols_is_string:
            kwargs["symbols"] = symbols_raw

        await emit_debug_event(
            "operation_data_events.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )

        try:
            symbols = self._resolve_symbols(kwargs)
            data_source = str(kwargs.get("data_source") or "auto").strip().lower()
            if data_source not in _SUPPORTED_SOURCES:
                raise _InvalidArg(
                    "unknown_data_source",
                    f"unknown data_source {data_source!r}",
                    f"use one of: {', '.join(_SUPPORTED_SOURCES)}",
                )
            asof = kwargs.get("asof")
            if asof is not None:
                date.fromisoformat(str(asof))  # validate shape
        except _InvalidArg as exc:
            await emit_debug_event(
                "operation_data_events.failed",
                {"tool": self.name, "error_code": exc.error_code, "message": str(exc)},
            )
            return ToolResult(text=format_error_text(exc.error_code, str(exc), exc.hint), is_error=True)
        except ValueError:
            await emit_debug_event(
                "operation_data_events.failed",
                {"tool": self.name, "error_code": "invalid_date", "message": f"asof={kwargs.get('asof')!r}"},
            )
            return ToolResult(
                text=format_error_text("invalid_date", f"asof={kwargs.get('asof')!r} is not YYYY-MM-DD"),
                is_error=True,
            )

        try:
            events = await self._fetch(data_source, symbols, asof)
        except Exception as exc:
            logger.exception("data_events fetch failed source=%s", data_source)
            await emit_debug_event(
                "operation_data_events.failed",
                {
                    "tool": self.name,
                    "error_code": "events_fetch_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "events_fetch_failed",
                    f"failed to fetch events via data_source={data_source!r}: {exc}",
                    "check the source (akshare network); try --data-source explicitly",
                ),
                is_error=True,
            )

        rows: list[dict[str, Any]] = []
        for code in symbols:
            for ev in events.get(code, []) or []:
                rows.append({
                    "code": code,
                    "event_type": ev.event_type,
                    "event_date": ev.event_date,
                    "detail": ev.detail,
                })
        with_events = sorted({r["code"] for r in rows})
        csv_path = self._persist(rows, kwargs.get("output_path"))

        payload = {
            "status": "ok",
            "data_source": data_source,
            "asof": asof,
            "symbols_total": len(symbols),
            "symbols_with_events": len(with_events),
            "event_count": len(rows),
            "events_path": str(csv_path),
            "preview": rows[:_PREVIEW_ROWS],
        }
        await emit_debug_event(
            "operation_data_events.created",
            {
                "tool": self.name,
                "symbols_total": len(symbols),
                "symbols_with_events": len(with_events),
                "event_count": len(rows),
                "events_path": str(csv_path),
            },
        )
        header = (
            f"Fetched events for {len(symbols)} symbols via data_source={data_source}: "
            f"{len(rows)} events across {len(with_events)} symbols → {csv_path}."
        )
        return ToolResult(text=append_json_payload(header, payload), is_error=False)

    # ------------------------------------------------------------------

    def _resolve_symbols(self, kwargs: dict[str, Any]) -> list[str]:
        code = kwargs.get("code")
        symbols = kwargs.get("symbols")
        universe_file = kwargs.get("universe_file")
        provided = [x for x in (code, symbols, universe_file) if x not in (None, "", [])]
        if len(provided) > 1:
            raise _InvalidArg("conflicting_symbol_args", "pass exactly one of code / symbols / universe_file")
        try:
            if code is not None:
                return _validate_canonical_codes([str(code)])
            if symbols is not None:
                parsed = _parse_csv_symbols(symbols) if isinstance(symbols, str) else list(symbols)
                return _validate_canonical_codes(parsed)
            if universe_file is not None:
                return _validate_canonical_codes(_load_universe_file(str(universe_file)))
        except _InvalidDataRunArgument as exc:
            raise _InvalidArg(exc.error_code, str(exc), exc.hint) from exc
        raise _InvalidArg("missing_symbol_input", "no symbols given; pass code / symbols / universe_file")

    async def _fetch(self, data_source: str, symbols: list[str], asof: Any) -> dict[str, Any]:
        if self._event_provider_factory is not None:
            provider = self._event_provider_factory(data_source)
        else:
            from doyoutrade.config import get_config
            from doyoutrade.data.factory import build_event_provider

            try:
                provider = build_event_provider(data_source, get_config().data)
            except ValueError as exc:
                raise _InvalidArg("data_source_unavailable", str(exc), "use --data-source akshare") from exc
        try:
            return await provider.get_events_batch(list(symbols), asof=asof)
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("data_events: provider.aclose() raised: %s", exc)

    def _persist(self, rows: list[dict[str, Any]], output_path: Any) -> Path:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        path = Path(str(output_path)).expanduser() if output_path else root / "events.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(_CSV_COLUMNS))
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        return path


__all__ = ["DataEventsTool"]
