"""``data_fundamentals`` operation — valuation / market-cap snapshot.

A sibling of ``data_news`` / ``data_sector`` on the fundamentals axis. Takes
``code`` / ``symbols`` / ``universe_file`` (mutually exclusive, same shapes
as ``data run``), batch-fetches float / total market cap + PE / PB via the
:class:`doyoutrade.data.protocols.FundamentalsProvider` (``--data-source auto``
= akshare snapshot → qmt), and writes a ``fundamentals.csv`` artifact.

Market-cap values are in 元 (100亿 = ``1e10``) — the same scale
``stock screen --min-float-mv`` consumes.

Failure-mode discipline (per CLAUDE.md §错误可见性): a provider error
surfaces ``fundamentals_fetch_failed``; symbols the source can't serve are
reported in ``missing`` (not silently dropped). Debug events:
``operation_data_fundamentals.request`` / ``.rejected`` / ``.failed`` /
``.created``.
"""

from __future__ import annotations

import csv
import logging
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

_SUPPORTED_SOURCES = ("auto", "akshare", "qmt")
_CSV_COLUMNS = ("code", "float_mv", "total_mv", "pe", "pb", "price")
_PREVIEW_ROWS = 10


class _InvalidArg(ValueError):
    def __init__(self, error_code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint


class DataFundamentalsTool(OperationHandler):
    name = "data_fundamentals"
    description = (
        "Fetch float / total market cap + PE / PB for one or many A-share "
        "symbols and write a fundamentals CSV. --data-source auto walks "
        "akshare (whole-market snapshot, carries PE/PB) → qmt (float-cap only)."
    )
    category = "data"
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "symbols": {"type": "array", "items": {"type": "string"}},
            "universe_file": {"type": "string"},
            "data_source": {"type": "string", "enum": list(_SUPPORTED_SOURCES), "default": "auto"},
            "output_path": {"type": "string"},
        },
        "additionalProperties": False,
    }
    coercion_rules = (
        SchemaCoercion(field="symbols", declared_type="array", item_type=str, error_code="invalid_symbols"),
    )

    def __init__(self, *, fundamentals_provider_factory=None) -> None:
        self._fundamentals_provider_factory = fundamentals_provider_factory

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                "operation_data_fundamentals.rejected",
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
            await emit_debug_event("operation_data_fundamentals.failed", {"tool": self.name, **err})
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
            "operation_data_fundamentals.request",
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
        except _InvalidArg as exc:
            await emit_debug_event(
                "operation_data_fundamentals.failed",
                {"tool": self.name, "error_code": exc.error_code, "message": str(exc)},
            )
            return ToolResult(text=format_error_text(exc.error_code, str(exc), exc.hint), is_error=True)

        try:
            fundamentals = await self._fetch(data_source, symbols)
        except _InvalidArg as exc:
            await emit_debug_event(
                "operation_data_fundamentals.failed",
                {"tool": self.name, "error_code": exc.error_code, "message": str(exc)},
            )
            return ToolResult(text=format_error_text(exc.error_code, str(exc), exc.hint), is_error=True)
        except Exception as exc:
            logger.exception("data_fundamentals fetch failed source=%s", data_source)
            await emit_debug_event(
                "operation_data_fundamentals.failed",
                {
                    "tool": self.name,
                    "error_code": "fundamentals_fetch_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "fundamentals_fetch_failed",
                    f"failed to fetch fundamentals via data_source={data_source!r}: {exc}",
                    "check the source (akshare network / qmt base_url); try --data-source explicitly",
                ),
                is_error=True,
            )

        rows = [
            {
                "code": code,
                "float_mv": f.float_mv,
                "total_mv": f.total_mv,
                "pe": f.pe,
                "pb": f.pb,
                "price": f.price,
            }
            for code, f in ((s, fundamentals.get(s)) for s in symbols)
            if f is not None
        ]
        missing = [s for s in symbols if s not in fundamentals]
        csv_path = self._persist(rows, kwargs.get("output_path"))

        payload = {
            "status": "ok" if not missing else ("partial" if rows else "failed"),
            "data_source": data_source,
            "symbols_total": len(symbols),
            "symbols_matched": len(rows),
            "missing": missing,
            "fundamentals_path": str(csv_path),
            "preview": rows[:_PREVIEW_ROWS],
        }
        await emit_debug_event(
            "operation_data_fundamentals.created",
            {
                "tool": self.name,
                "symbols_total": len(symbols),
                "symbols_matched": len(rows),
                "fundamentals_path": str(csv_path),
            },
        )
        header = (
            f"Fetched fundamentals for {len(rows)}/{len(symbols)} symbols via "
            f"data_source={data_source} → {csv_path}."
        )
        return ToolResult(text=append_json_payload(header, payload), is_error=False)

    # ------------------------------------------------------------------

    def _resolve_symbols(self, kwargs: dict[str, Any]) -> list[str]:
        code = kwargs.get("code")
        symbols = kwargs.get("symbols")
        universe_file = kwargs.get("universe_file")
        provided = [x for x in (code, symbols, universe_file) if x not in (None, "", [])]
        if len(provided) > 1:
            raise _InvalidArg(
                "conflicting_symbol_args",
                "pass exactly one of code / symbols / universe_file",
            )
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
        raise _InvalidArg(
            "missing_symbol_input",
            "no symbols given; pass code / symbols / universe_file",
        )

    async def _fetch(self, data_source: str, symbols: list[str]) -> dict[str, Any]:
        if self._fundamentals_provider_factory is not None:
            provider = self._fundamentals_provider_factory(data_source)
        else:
            from doyoutrade.config import get_config
            from doyoutrade.data.account_resolution import resolve_default_market_account
            from doyoutrade.data.factory import build_fundamentals_provider

            account = await resolve_default_market_account()
            try:
                provider = build_fundamentals_provider(data_source, get_config().data, account)
            except ValueError as exc:
                raise _InvalidArg("data_source_unavailable", str(exc),
                                  "use --data-source akshare or set a default account with base_url") from exc
        try:
            return await provider.get_fundamentals_batch(list(symbols))
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("data_fundamentals: provider.aclose() raised: %s", exc)

    def _persist(self, rows: list[dict[str, Any]], output_path: Any) -> Path:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        path = Path(str(output_path)).expanduser() if output_path else root / "fundamentals.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(_CSV_COLUMNS))
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        return path


__all__ = ["DataFundamentalsTool"]
