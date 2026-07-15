"""``data_research_reports`` operation — fetch brokerage research reports.

Sibling to ``data_news`` but on the analyst-research axis rather than the
media-news axis. Shapes intentionally mirror ``data_news`` so an agent's
mental model carries over:

* **Multi-symbol fan-out** via ``code`` (single), ``symbols`` (CSV / JSON
  array), or ``universe_file`` (one canonical symbol per line) — mutually
  exclusive. Per-symbol failures surface as ``symbols[i].status ==
  "failed"`` with a stable ``error_code`` and never collapse the run.
* **Window resolution** reuses :class:`MarketDataFetcher._resolve_window`
  so ``period`` / ``start_date`` / ``end_date`` parse identically to
  ``data_run`` (and conflicts raise ``conflicting_range_args``). The
  upstream akshare endpoint has no date parameter, so the provider
  filters to the window client-side on the report ``日期`` column.
* **Local-file persistence** — each symbol's reports are written to
  ``research_reports_<code>.csv`` under the assistant artifacts root, and
  a ``data_research_reports_manifest.json`` summarises the run. No
  database table is involved (research reports are a cache-to-disk
  artifact, not run-link state).
* **Distinct failure modes** — a persistent upstream error raises
  ``research_reports_fetch_failed`` (carrying the exception type); a
  genuinely empty window raises ``research_reports_empty``. They are
  never merged into one status.

Debug events (per CLAUDE.md §错误可见性, all key steps observable):

* ``operation_data_research_reports.request`` — input keys
* ``operation_data_research_reports.rejected`` — unknown_arguments (kwargs contract)
* ``operation_data_research_reports.failed`` — global validation failure
* ``operation_data_research_reports.symbol.started`` / ``.validated`` /
  ``.completed`` / ``.failed`` — per-symbol lifecycle with ``code`` + ``error_code``
* ``operation_data_research_reports.created`` — final envelope summary
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd

from doyoutrade.api.operations.data_run import (
    _InvalidDataRunArgument,
    _load_universe_file,
    _parse_csv_symbols,
    _validate_canonical_codes,
)
from doyoutrade.api.operations.market_data import (
    MarketDataFetcher,
    _ConflictingRange,
    _get_artifacts_root,
    _InvalidDate,
    _InvalidPeriod,
    _safe_code,
)
from doyoutrade.debug import emit_debug_event
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._coercion import SchemaCoercion
from doyoutrade.tools._prose import append_json_payload, format_error_text, format_unknown_args

logger = logging.getLogger(__name__)

# Only akshare serves research reports today; ``auto`` resolves to it.
_SUPPORTED_RESEARCH_SOURCES = ("auto", "akshare")

# CSV column order for the per-symbol research-report artifact. Forecast
# dicts are serialised as JSON strings so the CSV stays rectangular.
_RESEARCH_CSV_COLUMNS = (
    "report_date",
    "title",
    "rating",
    "institution",
    "industry",
    "recent_report_count",
    "eps_forecasts",
    "pe_forecasts",
    "pdf_url",
)

_DEFAULT_LIMIT = 50
# Most-recent rows echoed inline in the envelope (full set lives in the CSV).
_PREVIEW_ROWS = 5


class _InvalidDataResearchArgument(ValueError):
    """Structured argument failure carrying a stable ``error_code``."""

    def __init__(
        self,
        error_code: str,
        message: str,
        hint: str | None = None,
        *,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint
        self.error_type = error_type


def _adapt_run_argument(exc: _InvalidDataRunArgument) -> _InvalidDataResearchArgument:
    """Re-wrap a reused ``data_run`` symbol-parsing error as our own type.

    The shared helpers raise ``_InvalidDataRunArgument`` with stable tokens
    (``invalid_symbols`` / ``invalid_symbol`` / ``no_symbols`` / …); we
    preserve the token verbatim so the agent's error vocabulary is
    identical across ``data run`` / ``data news`` / ``data reports``.
    """

    return _InvalidDataResearchArgument(
        exc.error_code, str(exc), exc.hint, error_type=exc.error_type
    )


def _build_research_report_provider(data_source: str):
    """Resolve a :class:`ResearchReportProvider` for the requested source.

    ``auto`` and ``akshare`` both resolve to akshare — the only research
    source available today. Kept as an explicit dispatch so the high-risk
    run-link factory stays untouched.
    """

    if data_source in ("auto", "akshare"):
        from doyoutrade.data.research_report_akshare import (
            AkshareResearchReportProvider,
        )

        return AkshareResearchReportProvider(), "akshare"
    raise _InvalidDataResearchArgument(
        "unknown_data_source",
        f"unknown data_source {data_source!r}",
        f"use one of: {', '.join(_SUPPORTED_RESEARCH_SOURCES)}",
    )


class DataResearchReportsTool(OperationHandler):
    name = "data_research_reports"
    description = (
        "Fetch brokerage research reports (券商个股研报) for one or many "
        "A-share symbols and persist each symbol's reports to a local CSV. "
        "Each report carries the title, Eastmoney rating (东财评级, e.g. "
        "买入/增持), issuing institution (机构), industry, report date, "
        "PDF link, and analyst EPS / PE forecasts by year. Symbols come "
        "from ``code`` (single), ``symbols`` (CSV / JSON list), or "
        "``universe_file`` — exactly one. The requested window (``period`` "
        "or ``start_date``/``end_date``) filters reports by report date; "
        "``limit`` caps the most-recent N per symbol. Per-symbol failures "
        "surface as symbols[i].status == 'failed' with a stable error_code; "
        "they never collapse the run."
    )
    category = "data"
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Single canonical CODE.EXCHANGE symbol."},
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of canonical symbols. CSV string also accepted at CLI layer.",
            },
            "universe_file": {
                "type": "string",
                "description": "Path to a file with one CODE.EXCHANGE per line (# comments ok).",
            },
            "period": {"type": "string", "description": "Relative window, e.g. 7d / 1mo / 1y."},
            "start_date": {"type": "string", "description": "Inclusive YYYY-MM-DD."},
            "end_date": {"type": "string", "description": "Inclusive YYYY-MM-DD."},
            "data_source": {
                "type": "string",
                "enum": list(_SUPPORTED_RESEARCH_SOURCES),
                "default": "auto",
            },
            "limit": {
                "type": "integer",
                "minimum": 0,
                "description": f"Max most-recent reports per symbol (default {_DEFAULT_LIMIT}).",
            },
        },
        "additionalProperties": False,
    }

    coercion_rules = (
        SchemaCoercion(
            field="symbols",
            declared_type="array",
            item_type=str,
            error_code="invalid_symbols",
        ),
    )

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                "operation_data_research_reports.rejected",
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
            await emit_debug_event("operation_data_research_reports.failed", {"tool": self.name, **err})
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
            "operation_data_research_reports.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )

        try:
            normalized = self._normalize_inputs(kwargs)
        except _InvalidDataResearchArgument as exc:
            await emit_debug_event(
                "operation_data_research_reports.failed",
                {
                    "tool": self.name,
                    "error_code": exc.error_code,
                    "error_type": exc.error_type,
                    "message": str(exc),
                    "hint": exc.hint,
                },
            )
            return ToolResult(
                text=format_error_text(exc.error_code, str(exc), exc.hint),
                is_error=True,
            )

        symbols = normalized["codes"]
        results: list[dict[str, Any]] = []
        global_meta: dict[str, Any] = {"requested_start": None, "requested_end": None}

        for code in symbols:
            await emit_debug_event(
                "operation_data_research_reports.symbol.started",
                {"tool": self.name, "code": code},
            )
            try:
                outcome = await self._run_for_symbol(code, normalized)
            except _InvalidDataResearchArgument as exc:
                await emit_debug_event(
                    "operation_data_research_reports.symbol.failed",
                    {
                        "tool": self.name,
                        "code": code,
                        "error_code": exc.error_code,
                        "error_type": exc.error_type,
                        "message": str(exc),
                    },
                )
                results.append(
                    {
                        "code": code,
                        "status": "failed",
                        "error_code": exc.error_code,
                        "error_type": exc.error_type,
                        "message": str(exc),
                        "hint": exc.hint,
                    }
                )
                continue
            except Exception as exc:
                logger.exception("data_research_reports unexpected failure code=%s", code)
                await emit_debug_event(
                    "operation_data_research_reports.symbol.failed",
                    {
                        "tool": self.name,
                        "code": code,
                        "error_code": "data_research_reports_unexpected_failure",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                results.append(
                    {
                        "code": code,
                        "status": "failed",
                        "error_code": "data_research_reports_unexpected_failure",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                continue
            results.append(outcome)
            if global_meta["requested_start"] is None:
                global_meta["requested_start"] = outcome.get("requested_start")
                global_meta["requested_end"] = outcome.get("requested_end")
            await emit_debug_event(
                "operation_data_research_reports.symbol.completed",
                {
                    "tool": self.name,
                    "code": code,
                    "reports_path": outcome.get("reports_path"),
                    "report_count": outcome.get("report_count"),
                },
            )

        successes = [r for r in results if r.get("status") == "ok"]
        failures = [r for r in results if r.get("status") == "failed"]

        manifest_path = self._write_manifest(results=results, normalized=normalized)

        payload: dict[str, Any] = {
            "status": "ok" if not failures else ("partial" if successes else "failed"),
            "requested_start": global_meta["requested_start"],
            "requested_end": global_meta["requested_end"],
            "limit": normalized["limit"],
            "symbols_total": len(symbols),
            "symbols_succeeded": len(successes),
            "symbols_failed": len(failures),
            "manifest_path": manifest_path,
            "symbols": results,
        }

        await emit_debug_event(
            "operation_data_research_reports.created",
            {
                "tool": self.name,
                "symbols_total": len(symbols),
                "symbols_succeeded": len(successes),
                "symbols_failed": len(failures),
                "manifest_path": manifest_path,
            },
        )

        header = self._summary_header(payload)
        return ToolResult(text=append_json_payload(header, payload), is_error=False)

    # ------------------------------------------------------------------
    # Input normalization
    # ------------------------------------------------------------------

    def _normalize_inputs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        code = kwargs.get("code")
        symbols = kwargs.get("symbols")
        universe_file = kwargs.get("universe_file")

        provided_keys = [
            name
            for name, value in (
                ("code", code),
                ("symbols", symbols),
                ("universe_file", universe_file),
            )
            if value is not None
        ]
        if len(provided_keys) == 0:
            raise _InvalidDataResearchArgument(
                "missing_symbol_input",
                "pass exactly one of code / symbols / universe_file",
                "pick a single input mode",
            )
        if len(provided_keys) > 1:
            raise _InvalidDataResearchArgument(
                "conflicting_symbol_args",
                f"got multiple symbol inputs: {provided_keys}",
                "pick exactly one of code / symbols / universe_file",
            )

        try:
            if code is not None:
                codes = _validate_canonical_codes(
                    [code] if not isinstance(code, list) else code
                )
            elif symbols is not None:
                codes = _validate_canonical_codes(_parse_csv_symbols(symbols))
            else:
                codes = _validate_canonical_codes(_load_universe_file(universe_file))
        except _InvalidDataRunArgument as exc:
            raise _adapt_run_argument(exc) from exc

        data_source = kwargs.get("data_source") or "auto"
        if data_source not in _SUPPORTED_RESEARCH_SOURCES:
            raise _InvalidDataResearchArgument(
                "unknown_data_source",
                f"unknown data_source {data_source!r}",
                f"use one of: {', '.join(_SUPPORTED_RESEARCH_SOURCES)}",
            )

        return {
            "codes": codes,
            "period": kwargs.get("period"),
            "start_date": kwargs.get("start_date"),
            "end_date": kwargs.get("end_date"),
            "data_source": data_source,
            "limit": self._resolve_limit(kwargs.get("limit")),
        }

    def _resolve_limit(self, value: Any) -> int:
        if value is None:
            return _DEFAULT_LIMIT
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _InvalidDataResearchArgument(
                "invalid_limit",
                f"limit must be an integer >= 0, got {type(value).__name__}({value!r})",
            )
        intval = int(value)
        if float(value) != intval or intval < 0:
            raise _InvalidDataResearchArgument(
                "invalid_limit",
                f"limit must be an integer >= 0, got {value!r}",
            )
        return intval

    # ------------------------------------------------------------------
    # Per-symbol pipeline
    # ------------------------------------------------------------------

    async def _run_for_symbol(self, code: str, normalized: dict[str, Any]) -> dict[str, Any]:
        market_tool = MarketDataFetcher()
        try:
            requested_start, requested_end, _label = market_tool._resolve_window(
                period=normalized["period"],
                start_date=normalized["start_date"],
                end_date=normalized["end_date"],
            )
        except _ConflictingRange as exc:
            raise _InvalidDataResearchArgument(
                "conflicting_range_args",
                str(exc),
                "Pass either period OR start_date/end_date, not both.",
            ) from exc
        except _InvalidDate as exc:
            raise _InvalidDataResearchArgument(
                "invalid_date",
                str(exc),
                "Use YYYY-MM-DD and ensure start_date <= end_date.",
            ) from exc
        except _InvalidPeriod as exc:
            raise _InvalidDataResearchArgument(
                "invalid_period",
                str(exc),
                "Use <N><unit> with unit in d/w/m/mo/y, e.g. 7d or 1mo.",
            ) from exc

        provider, source_name = _build_research_report_provider(normalized["data_source"])

        await emit_debug_event(
            "operation_data_research_reports.symbol.validated",
            {
                "tool": self.name,
                "code": code,
                "requested_start": requested_start.isoformat(),
                "requested_end": requested_end.isoformat(),
                "data_source": source_name,
                "limit": normalized["limit"],
            },
        )

        try:
            reports = await provider.fetch_research_reports(
                code,
                requested_start.isoformat(),
                requested_end.isoformat(),
                limit=normalized["limit"],
            )
        except Exception as exc:
            logger.warning(
                "data_research_reports fetch failed code=%s data_source=%s err=%s: %s",
                code, source_name, type(exc).__name__, exc,
            )
            raise _InvalidDataResearchArgument(
                "research_reports_fetch_failed",
                f"failed to fetch research reports for {code}: {exc}",
                "check the symbol and data_source",
                error_type=type(exc).__name__,
            ) from exc

        if not reports:
            raise _InvalidDataResearchArgument(
                "research_reports_empty",
                f"no research reports for {code} in window "
                f"{requested_start.isoformat()}..{requested_end.isoformat()}",
                "widen the window (period/start_date/end_date) or try another symbol",
            )

        reports_path = self._persist_reports(code, reports)
        preview = [
            {
                "report_date": r.report_date,
                "title": r.title,
                "rating": r.rating,
                "institution": r.institution,
            }
            for r in reports[:_PREVIEW_ROWS]
        ]
        return {
            "code": code,
            "status": "ok",
            "data_source": source_name,
            "requested_start": requested_start.isoformat(),
            "requested_end": requested_end.isoformat(),
            "report_count": len(reports),
            "reports_path": reports_path,
            "latest": preview,
        }

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    def _persist_reports(self, code: str, reports: list[Any]) -> str:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "report_date": r.report_date,
                "title": r.title,
                "rating": r.rating,
                "institution": r.institution,
                "industry": r.industry,
                "recent_report_count": r.recent_report_count,
                "eps_forecasts": json.dumps(r.eps_forecasts, ensure_ascii=False),
                "pe_forecasts": json.dumps(r.pe_forecasts, ensure_ascii=False),
                "pdf_url": r.pdf_url,
            }
            for r in reports
        ]
        df = pd.DataFrame(rows)[list(_RESEARCH_CSV_COLUMNS)]
        reports_path = root / f"research_reports_{_safe_code(code)}.csv"
        df.to_csv(reports_path, index=False)
        return str(reports_path)

    def _write_manifest(
        self,
        *,
        results: list[dict[str, Any]],
        normalized: dict[str, Any],
    ) -> str:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "kind": "data_research_reports",
            "data_source": normalized["data_source"],
            "limit": normalized["limit"],
            "period": normalized["period"],
            "start_date": normalized["start_date"],
            "end_date": normalized["end_date"],
            "symbols": [
                {
                    "code": r.get("code"),
                    "status": r.get("status"),
                    "report_count": r.get("report_count"),
                    "reports_path": r.get("reports_path"),
                    "error_code": r.get("error_code"),
                }
                for r in results
            ],
        }
        manifest_path = root / "data_research_reports_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(manifest_path)

    def _summary_header(self, payload: dict[str, Any]) -> str:
        return (
            f"data reports: {payload['symbols_succeeded']}/{payload['symbols_total']} "
            f"symbols ok (status={payload['status']}, limit={payload['limit']})"
        )


__all__ = ["DataResearchReportsTool"]
