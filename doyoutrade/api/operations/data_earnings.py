"""``data_earnings`` operation — fetch earnings preannouncements / express reports.

Sibling to ``data_news`` / ``data_research_reports`` but on the **earnings**
axis (业绩预告 / 业绩快报), and **batch / period-scoped** rather than
symbol-scoped:

* The upstream (akshare ``stock_yjyg_em`` / ``stock_yjkb_em``) serves a
  *full-market* snapshot per fiscal quarter-end (report period), so the
  provider pulls each period once and filters to the requested symbols in
  memory — see :class:`doyoutrade.data.protocols.EarningsProvider`.
* **Report-period resolution**: the requested window (``period`` or
  ``start_date``/``end_date``) is interpreted as a *report-period* window —
  every quarter-end (03-31 / 06-30 / 09-30 / 12-31) that falls inside
  ``[start, end]`` becomes one ``YYYYMMDD`` report-period token. The default
  1y window therefore covers the trailing four quarters. ``announce_date``
  is returned as a field but is NOT used for window filtering (a quarter's
  preannouncement is filed after quarter-end).
* **``--kind forecast|express|both``** (default ``both``): one call can
  surface both 业绩预告 and 业绩快报. A symbol is ``ok`` if it has *any* row
  across the requested kinds; it is ``earnings_empty`` only when every kind
  × period returned nothing for it.
* **Local-file persistence** — each (symbol, kind) pair writes
  ``earnings_<kind>_<code>.csv`` under the assistant artifacts root; a
  ``data_earnings_manifest.json`` summarises the run. No database table is
  involved.
* **Distinct failure modes** — a per-period upstream error is recorded by
  the provider (``failed_periods``) and surfaced as
  ``earnings_fetch_failed`` on the symbols that ended up empty because of
  it; a genuinely empty result (no data for any symbol in any period) is
  ``earnings_empty``. They are never merged.

Debug events (per CLAUDE.md §错误可见性, all key steps observable):

* ``operation_data_earnings.request`` — input keys
* ``operation_data_earnings.rejected`` — unknown_arguments (kwargs contract)
* ``operation_data_earnings.failed`` — global validation failure
* ``operation_data_earnings.symbol.started`` / ``.validated`` /
  ``.completed`` / ``.failed`` — per-symbol lifecycle
* ``operation_data_earnings.created`` — final envelope summary
"""

from __future__ import annotations

import json
import logging
from datetime import date
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

_SUPPORTED_EARNINGS_SOURCES = ("auto", "akshare")
_SUPPORTED_KINDS = ("forecast", "express", "both")

# Quarter-end (month, day) pairs that anchor a report period.
_QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))

# CSV column orders for the per-symbol artifacts.
_FORECAST_CSV_COLUMNS = (
    "report_period",
    "announce_date",
    "preannounce_type",
    "forecast_indicator",
    "forecast_value",
    "change_pct",
    "prev_year_value",
    "change_description",
    "reason",
)
_EXPRESS_CSV_COLUMNS = (
    "report_period",
    "announce_date",
    "eps",
    "revenue",
    "revenue_prev_yoy",
    "revenue_qoq",
    "net_profit",
    "net_profit_prev_yoy",
    "net_profit_qoq",
    "navs_per_share",
    "roe",
    "industry",
)


class _InvalidDataEarningsArgument(ValueError):
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


def _adapt_run_argument(exc: _InvalidDataRunArgument) -> _InvalidDataEarningsArgument:
    """Re-wrap a reused ``data_run`` symbol-parsing error as our own type."""
    return _InvalidDataEarningsArgument(
        exc.error_code, str(exc), exc.hint, error_type=exc.error_type
    )


def _build_earnings_provider(data_source: str):
    """Resolve an :class:`EarningsProvider` for the requested source."""
    if data_source in ("auto", "akshare"):
        from doyoutrade.data.earnings_akshare import AkshareEarningsProvider

        return AkshareEarningsProvider(), "akshare"
    raise _InvalidDataEarningsArgument(
        "unknown_data_source",
        f"unknown data_source {data_source!r}",
        f"use one of: {', '.join(_SUPPORTED_EARNINGS_SOURCES)}",
    )


def _resolve_report_periods(start: date, end: date) -> list[str]:
    """Quarter-end report-period tokens (``YYYYMMDD``) inside ``[start, end]``.

    A report period is anchored on a fiscal quarter-end (03-31 / 06-30 /
    09-30 / 12-31). The window is a *report-period* window: a quarter-end
    date must fall inside ``[start, end]`` to be included. Tokens are
    returned chronologically (oldest first).
    """
    periods: list[str] = []
    for year in range(start.year, end.year + 1):
        for month, day in _QUARTER_ENDS:
            qd = date(year, month, day)
            if start <= qd <= end:
                periods.append(qd.strftime("%Y%m%d"))
    return periods


class DataEarningsTool(OperationHandler):
    name = "data_earnings"
    description = (
        "Fetch earnings preannouncements (业绩预告) and/or express reports "
        "(业绩快报) for one or many A-share symbols and persist each "
        "(symbol, kind) pair to a local CSV. Earnings data is served "
        "full-market per fiscal quarter-end (report period), so the window "
        "(period or start_date/end_date) selects which quarter-ends to "
        "cover. ``kind`` picks forecast / express / both (default both). "
        "Symbols come from ``code`` (single), ``symbols`` (CSV / JSON list), "
        "or ``universe_file`` — exactly one. Per-symbol failures surface as "
        "symbols[i].status == 'failed' with a stable error_code; they never "
        "collapse the run."
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
            "period": {
                "type": "string",
                "description": "Relative report-period window, e.g. 1y (trailing 4 quarters).",
            },
            "start_date": {"type": "string", "description": "Inclusive YYYY-MM-DD (report-period window)."},
            "end_date": {"type": "string", "description": "Inclusive YYYY-MM-DD (report-period window)."},
            "kind": {
                "type": "string",
                "enum": list(_SUPPORTED_KINDS),
                "default": "both",
                "description": "forecast (业绩预告) / express (业绩快报) / both.",
            },
            "data_source": {
                "type": "string",
                "enum": list(_SUPPORTED_EARNINGS_SOURCES),
                "default": "auto",
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
                "operation_data_earnings.rejected",
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
            await emit_debug_event("operation_data_earnings.failed", {"tool": self.name, **err})
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
            "operation_data_earnings.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )

        try:
            normalized = self._normalize_inputs(kwargs)
        except _InvalidDataEarningsArgument as exc:
            await emit_debug_event(
                "operation_data_earnings.failed",
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
        report_periods = normalized["report_periods"]
        want_forecast = normalized["kind"] in ("forecast", "both")
        want_express = normalized["kind"] in ("express", "both")

        await emit_debug_event(
            "operation_data_earnings.symbol.validated",
            {
                "tool": self.name,
                "symbols_total": len(symbols),
                "report_periods": report_periods,
                "kind": normalized["kind"],
                "data_source": normalized["data_source"],
            },
        )

        forecast_map: dict[str, list[Any]] = {}
        express_map: dict[str, list[Any]] = {}
        fetch_failed_periods: list[Any] = []
        try:
            provider, source_name = _build_earnings_provider(normalized["data_source"])
            if want_forecast:
                forecast_map = await provider.fetch_earnings_forecasts(
                    symbols, report_periods
                )
                fetch_failed_periods = self._collect_failed_periods(forecast_map, report_periods)
            if want_express:
                express_map = await provider.fetch_earnings_express(
                    symbols, report_periods
                )
        except Exception as exc:
            logger.warning(
                "data_earnings fetch failed err=%s: %s",
                type(exc).__name__, exc,
            )
            await emit_debug_event(
                "operation_data_earnings.failed",
                {
                    "tool": self.name,
                    "error_code": "earnings_fetch_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "earnings_fetch_failed",
                    f"failed to fetch earnings: {exc}",
                    "check the data_source and network",
                ),
                is_error=True,
            )

        results: list[dict[str, Any]] = []
        for code in symbols:
            await emit_debug_event(
                "operation_data_earnings.symbol.started",
                {"tool": self.name, "code": code},
            )
            outcome = self._build_symbol_outcome(
                code,
                forecast_map.get(code, []) if want_forecast else [],
                express_map.get(code, []) if want_express else [],
                normalized,
                fetch_failed_periods,
            )
            results.append(outcome)
            await emit_debug_event(
                "operation_data_earnings.symbol.completed",
                {
                    "tool": self.name,
                    "code": code,
                    "status": outcome["status"],
                    "forecast_count": (outcome.get("forecast") or {}).get("count", 0),
                    "express_count": (outcome.get("express") or {}).get("count", 0),
                },
            )

        successes = [r for r in results if r.get("status") == "ok"]
        failures = [r for r in results if r.get("status") == "failed"]

        manifest_path = self._write_manifest(
            results=results, normalized=normalized, report_periods=report_periods
        )

        payload: dict[str, Any] = {
            "status": "ok" if not failures else ("partial" if successes else "failed"),
            "kind": normalized["kind"],
            "requested_start": normalized["requested_start"],
            "requested_end": normalized["requested_end"],
            "report_periods": report_periods,
            "symbols_total": len(symbols),
            "symbols_succeeded": len(successes),
            "symbols_failed": len(failures),
            "manifest_path": manifest_path,
            "symbols": results,
        }

        await emit_debug_event(
            "operation_data_earnings.created",
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
            raise _InvalidDataEarningsArgument(
                "missing_symbol_input",
                "pass exactly one of code / symbols / universe_file",
                "pick a single input mode",
            )
        if len(provided_keys) > 1:
            raise _InvalidDataEarningsArgument(
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
        if data_source not in _SUPPORTED_EARNINGS_SOURCES:
            raise _InvalidDataEarningsArgument(
                "unknown_data_source",
                f"unknown data_source {data_source!r}",
                f"use one of: {', '.join(_SUPPORTED_EARNINGS_SOURCES)}",
            )

        kind = kwargs.get("kind") or "both"
        if kind not in _SUPPORTED_KINDS:
            raise _InvalidDataEarningsArgument(
                "invalid_kind",
                f"unknown kind {kind!r}",
                f"use one of: {', '.join(_SUPPORTED_KINDS)}",
            )

        market_tool = MarketDataFetcher()
        try:
            requested_start, requested_end, _label = market_tool._resolve_window(
                period=kwargs.get("period"),
                start_date=kwargs.get("start_date"),
                end_date=kwargs.get("end_date"),
            )
        except _ConflictingRange as exc:
            raise _InvalidDataEarningsArgument(
                "conflicting_range_args",
                str(exc),
                "Pass either period OR start_date/end_date, not both.",
            ) from exc
        except _InvalidDate as exc:
            raise _InvalidDataEarningsArgument(
                "invalid_date",
                str(exc),
                "Use YYYY-MM-DD and ensure start_date <= end_date.",
            ) from exc
        except _InvalidPeriod as exc:
            raise _InvalidDataEarningsArgument(
                "invalid_period",
                str(exc),
                "Use <N><unit> with unit in d/w/m/mo/y, e.g. 1y or 6mo.",
            ) from exc

        report_periods = _resolve_report_periods(requested_start, requested_end)
        if not report_periods:
            raise _InvalidDataEarningsArgument(
                "no_report_periods",
                f"no fiscal quarter-ends fall inside "
                f"{requested_start.isoformat()}..{requested_end.isoformat()}",
                "widen the window so it covers at least one quarter-end "
                "(03-31 / 06-30 / 09-30 / 12-31)",
            )

        return {
            "codes": codes,
            "kind": kind,
            "data_source": data_source,
            "requested_start": requested_start.isoformat(),
            "requested_end": requested_end.isoformat(),
            "report_periods": report_periods,
        }

    def _collect_failed_periods(
        self, provider_map: dict[str, list[Any]], report_periods: list[str]
    ) -> list[Any]:
        """The provider records failed periods in-band via debug events; this
        stub is kept for symmetry / future structured return. Returns []."""
        return []

    # ------------------------------------------------------------------
    # Per-symbol outcome
    # ------------------------------------------------------------------

    def _build_symbol_outcome(
        self,
        code: str,
        forecasts: list[Any],
        expresses: list[Any],
        normalized: dict[str, Any],
        failed_periods: list[Any],
    ) -> dict[str, Any]:
        outcome: dict[str, Any] = {"code": code}
        have_forecast = bool(forecasts)
        have_express = bool(expresses)

        if have_forecast:
            forecasts.sort(key=lambda r: r.report_period, reverse=True)
            outcome["forecast"] = {
                "count": len(forecasts),
                "path": self._persist_forecasts(code, forecasts),
                "report_periods": sorted(
                    {r.report_period for r in forecasts}, reverse=True
                ),
                "latest": [
                    {
                        "report_period": r.report_period,
                        "announce_date": r.announce_date,
                        "preannounce_type": r.preannounce_type,
                        "forecast_indicator": r.forecast_indicator,
                        "change_pct": r.change_pct,
                    }
                    for r in forecasts[:5]
                ],
            }
        if have_express:
            expresses.sort(key=lambda r: r.report_period, reverse=True)
            outcome["express"] = {
                "count": len(expresses),
                "path": self._persist_express(code, expresses),
                "report_periods": sorted(
                    {r.report_period for r in expresses}, reverse=True
                ),
                "latest": [
                    {
                        "report_period": r.report_period,
                        "announce_date": r.announce_date,
                        "eps": r.eps,
                        "net_profit": r.net_profit,
                        "net_profit_prev_yoy": r.net_profit_prev_yoy,
                        "roe": r.roe,
                    }
                    for r in expresses[:5]
                ],
            }

        if have_forecast or have_express:
            outcome["status"] = "ok"
            outcome["data_source"] = normalized["data_source"]
        else:
            # Empty across all requested kinds × periods. If the provider
            # recorded failed periods, frame it as a fetch failure rather
            # than a genuine empty — distinct failure modes.
            if failed_periods:
                outcome["status"] = "failed"
                outcome["error_code"] = "earnings_fetch_failed"
                outcome["error_type"] = "upstream_error"
                outcome["message"] = (
                    f"no earnings for {code} and periods "
                    f"{normalized['report_periods']} had upstream failures"
                )
            else:
                outcome["status"] = "failed"
                outcome["error_code"] = "earnings_empty"
                outcome["message"] = (
                    f"no earnings for {code} in report periods "
                    f"{normalized['report_periods']}"
                )
                outcome["hint"] = (
                    "widen the window (period/start_date/end_date) or try another symbol"
                )
        return outcome

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    def _persist_forecasts(self, code: str, forecasts: list[Any]) -> str:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "report_period": r.report_period,
                "announce_date": r.announce_date,
                "preannounce_type": r.preannounce_type,
                "forecast_indicator": r.forecast_indicator,
                "forecast_value": r.forecast_value,
                "change_pct": r.change_pct,
                "prev_year_value": r.prev_year_value,
                "change_description": r.change_description,
                "reason": r.reason,
            }
            for r in forecasts
        ]
        df = pd.DataFrame(rows)[list(_FORECAST_CSV_COLUMNS)]
        path = root / f"earnings_forecast_{_safe_code(code)}.csv"
        df.to_csv(path, index=False)
        return str(path)

    def _persist_express(self, code: str, expresses: list[Any]) -> str:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "report_period": r.report_period,
                "announce_date": r.announce_date,
                "eps": r.eps,
                "revenue": r.revenue,
                "revenue_prev_yoy": r.revenue_prev_yoy,
                "revenue_qoq": r.revenue_qoq,
                "net_profit": r.net_profit,
                "net_profit_prev_yoy": r.net_profit_prev_yoy,
                "net_profit_qoq": r.net_profit_qoq,
                "navs_per_share": r.navs_per_share,
                "roe": r.roe,
                "industry": r.industry,
            }
            for r in expresses
        ]
        df = pd.DataFrame(rows)[list(_EXPRESS_CSV_COLUMNS)]
        path = root / f"earnings_express_{_safe_code(code)}.csv"
        df.to_csv(path, index=False)
        return str(path)

    def _write_manifest(
        self,
        *,
        results: list[dict[str, Any]],
        normalized: dict[str, Any],
        report_periods: list[str],
    ) -> str:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "kind": "data_earnings",
            "earnings_kind": normalized["kind"],
            "data_source": normalized["data_source"],
            "requested_start": normalized["requested_start"],
            "requested_end": normalized["requested_end"],
            "report_periods": report_periods,
            "symbols": [
                {
                    "code": r.get("code"),
                    "status": r.get("status"),
                    "forecast_count": (r.get("forecast") or {}).get("count"),
                    "express_count": (r.get("express") or {}).get("count"),
                    "error_code": r.get("error_code"),
                }
                for r in results
            ],
        }
        manifest_path = root / "data_earnings_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(manifest_path)

    def _summary_header(self, payload: dict[str, Any]) -> str:
        return (
            f"data earnings ({payload['kind']}): "
            f"{payload['symbols_succeeded']}/{payload['symbols_total']} symbols ok "
            f"(status={payload['status']}, periods={len(payload['report_periods'])})"
        )


__all__ = ["DataEarningsTool"]
