"""``data_fund_flow`` operation — A-share 资金流排名 (fund-flow ranking).

Sits on the whole-market fund-flow axis: for a rolling window (``今日`` / ``3日``
/ ``5日`` / ``10日``) it pulls the main / super-large / large / medium / small net
inflow ranking for either individual stocks (``stock_individual_fund_flow_rank``)
or sector boards (``stock_sector_fund_flow_rank``), sorts by main net inflow
descending, persists the full ranking to a local CSV, and returns a top-N
preview.

There is no date — the window is the rolling ``period``. This is a market-wide
operation: no per-symbol fan-out, no ``symbols`` input.

Failure-mode discipline (per CLAUDE.md §错误可见性, distinct error_codes):

* Empty ranking (no upstream error) → ``fund_flow_empty``.
* akshare raised on every retry → ``fund_flow_fetch_failed`` with
  ``error_type`` carrying the exception class.
* ``period`` not in the scope's allowed set → ``invalid_period`` (individual =
  {今日,3日,5日,10日}; sector = {今日,5日,10日} — no 3日).
* ``sector_type`` not one of 行业 / 概念 / 地域 → ``invalid_sector_type``.
* Unknown ``data_source`` → ``unknown_data_source``.
* Unknown kwargs → the ``_enforce_kwargs_contract`` ``unknown_arguments``.

Debug events (all key steps observable):

* ``operation_data_fund_flow.request`` — input keys
* ``operation_data_fund_flow.rejected`` — unknown_arguments
* ``operation_data_fund_flow.failed`` — validation / fetch / empty failure
* ``operation_data_fund_flow.validated`` — resolved scope / period / sector_type
* ``operation_data_fund_flow.created`` — final envelope summary
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from doyoutrade.api.operations.market_data import _get_artifacts_root, _safe_code
from doyoutrade.debug import emit_debug_event
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._prose import append_json_payload, format_error_text, format_unknown_args

logger = logging.getLogger(__name__)

# Only akshare serves the fund-flow rankings today; ``auto`` resolves to it.
_SUPPORTED_FUND_FLOW_SOURCES = ("auto", "akshare")

_SCOPE_INDIVIDUAL = "individual"
_SCOPE_SECTOR = "sector"
_SUPPORTED_SCOPES = (_SCOPE_INDIVIDUAL, _SCOPE_SECTOR)

# Per-scope allowed period sets. Sector has NO 3日 upstream.
_PERIODS_INDIVIDUAL = ("今日", "3日", "5日", "10日")
_PERIODS_SECTOR = ("今日", "5日", "10日")

# CLI-facing sector-type → akshare ``sector_type`` upstream token.
_SECTOR_TYPE_MAP = {
    "行业": "行业资金流",
    "概念": "概念资金流",
    "地域": "地域资金流",
}

_DEFAULT_PERIOD = "今日"
_DEFAULT_SECTOR_TYPE = "概念"
_DEFAULT_TOP = 30

# CSV column order (canonical symbol + all flow fields).
_FUND_FLOW_CSV_COLUMNS = (
    "scope",
    "symbol",
    "code",
    "name",
    "latest_price",
    "change_pct",
    "main_net_amount",
    "main_net_pct",
    "super_large_net_amount",
    "large_net_amount",
    "medium_net_amount",
    "small_net_amount",
    "lead_stock",
)


class _InvalidFundFlowArgument(ValueError):
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


def _build_fund_flow_provider(data_source: str):
    """Resolve a :class:`FundFlowProvider` for the requested source.

    ``auto`` and ``akshare`` both resolve to akshare — the only fund-flow source
    available today. Kept as an explicit dispatch so an unknown id surfaces a
    structured ``unknown_data_source`` rather than failing late.
    """
    if data_source in ("auto", "akshare"):
        from doyoutrade.data.fund_flow_akshare import AkshareFundFlowProvider

        return AkshareFundFlowProvider(), "akshare"
    raise _InvalidFundFlowArgument(
        "unknown_data_source",
        f"unknown data_source {data_source!r}",
        f"use one of: {', '.join(_SUPPORTED_FUND_FLOW_SOURCES)}",
    )


def _resolve_scope(raw: Any) -> str:
    if raw is None:
        return _SCOPE_INDIVIDUAL
    if not isinstance(raw, str) or raw not in _SUPPORTED_SCOPES:
        raise _InvalidFundFlowArgument(
            "validation_error",
            f"scope must be one of {list(_SUPPORTED_SCOPES)}, got {raw!r}",
            "use scope=individual (per-stock) or scope=sector (per-board)",
        )
    return raw


def _resolve_period(raw: Any, scope: str) -> str:
    allowed = _PERIODS_INDIVIDUAL if scope == _SCOPE_INDIVIDUAL else _PERIODS_SECTOR
    period = raw if raw is not None else _DEFAULT_PERIOD
    if period not in allowed:
        raise _InvalidFundFlowArgument(
            "invalid_period",
            f"period={period!r} is not allowed for scope={scope!r}",
            f"use one of: {', '.join(allowed)}",
        )
    return period


def _resolve_sector_type(raw: Any) -> str:
    """Map the CLI-facing sector-type to the akshare upstream token.

    Only consulted for the ``sector`` scope. ``None`` defaults to 概念.
    """
    key = raw if raw is not None else _DEFAULT_SECTOR_TYPE
    if key not in _SECTOR_TYPE_MAP:
        raise _InvalidFundFlowArgument(
            "invalid_sector_type",
            f"sector_type={key!r} is not valid",
            f"use one of: {', '.join(_SECTOR_TYPE_MAP)}",
        )
    return key


def _resolve_top(raw: Any) -> int:
    if raw is None:
        return _DEFAULT_TOP
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _InvalidFundFlowArgument(
            "validation_error",
            f"top must be a positive integer, got {type(raw).__name__}({raw!r})",
            "pass top as a positive integer, e.g. 30",
        )
    if raw <= 0:
        raise _InvalidFundFlowArgument(
            "validation_error",
            f"top must be a positive integer, got {raw}",
            "pass top >= 1",
        )
    return raw


def _sort_key(row: Any) -> tuple[int, float]:
    """Sort by main net inflow descending, pushing None values to the end.

    Returning ``(1, 0.0)`` for a None amount and ``(0, -value)`` otherwise keeps
    None rows last under an ascending Python sort without inventing a numeric
    value for them (per §错误可见性: no ``int(脏值)`` / None → 0 coercion).
    """
    amount = row.main_net_amount
    if amount is None:
        return (1, 0.0)
    return (0, -amount)


class DataFundFlowTool(OperationHandler):
    name = "data_fund_flow"
    description = (
        "Fetch A-share 资金流排名 (fund-flow ranking) — main / super-large / "
        "large / medium / small net inflow — for individual stocks "
        "(scope=individual, default) or sector boards (scope=sector) over a "
        "rolling period (今日 / 3日 / 5日 / 10日; sector has NO 3日). This is a "
        "MARKET-WIDE ranking, not a per-symbol series: there is no symbols "
        "input and NO date (the window is the rolling period). For scope=sector "
        "pass sector_type ∈ {行业, 概念, 地域} (default 概念). Rows are ranked by "
        "main net inflow (净额) descending; top (default 30) picks how many are "
        "previewed under latest while the CSV holds the full ranking. The "
        "individual endpoint's columns are period-prefixed so they are matched "
        "by substring; the sector endpoint tolerates missing columns."
    )
    category = "data"
    parameters = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": list(_SUPPORTED_SCOPES),
                "default": _SCOPE_INDIVIDUAL,
                "description": "individual (per-stock, default) or sector (per-board).",
            },
            "period": {
                "type": "string",
                "description": (
                    "Rolling window. individual: 今日/3日/5日/10日; "
                    "sector: 今日/5日/10日 (no 3日). Default 今日."
                ),
            },
            "sector_type": {
                "type": "string",
                "enum": list(_SECTOR_TYPE_MAP),
                "default": _DEFAULT_SECTOR_TYPE,
                "description": "sector scope only: 行业 / 概念 / 地域 (default 概念).",
            },
            "top": {
                "type": "integer",
                "minimum": 1,
                "default": _DEFAULT_TOP,
                "description": "Rows previewed under latest (CSV holds the full ranking).",
            },
            "data_source": {
                "type": "string",
                "enum": list(_SUPPORTED_FUND_FLOW_SOURCES),
                "default": "auto",
                "description": "Fund-flow provider id (akshare only today).",
            },
        },
        "additionalProperties": False,
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                "operation_data_fund_flow.rejected",
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

        await emit_debug_event(
            "operation_data_fund_flow.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )

        try:
            scope = _resolve_scope(kwargs.get("scope"))
            period = _resolve_period(kwargs.get("period"), scope)
            sector_type_key: str | None = None
            sector_type_upstream: str | None = None
            if scope == _SCOPE_SECTOR:
                sector_type_key = _resolve_sector_type(kwargs.get("sector_type"))
                sector_type_upstream = _SECTOR_TYPE_MAP[sector_type_key]
            top = _resolve_top(kwargs.get("top"))
            data_source = kwargs.get("data_source") or "auto"
            provider, source_name = _build_fund_flow_provider(data_source)
        except _InvalidFundFlowArgument as exc:
            await emit_debug_event(
                "operation_data_fund_flow.failed",
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

        await emit_debug_event(
            "operation_data_fund_flow.validated",
            {
                "tool": self.name,
                "scope": scope,
                "period": period,
                "sector_type": sector_type_key,
                "top": top,
                "data_source": source_name,
            },
        )

        try:
            rows = await provider.fetch_fund_flow(
                scope, period, sector_type=sector_type_upstream
            )
        except Exception as exc:
            logger.exception(
                "data_fund_flow upstream fetch failure scope=%s period=%s data_source=%s",
                scope, period, source_name,
            )
            await emit_debug_event(
                "operation_data_fund_flow.failed",
                {
                    "tool": self.name,
                    "scope": scope,
                    "period": period,
                    "error_code": "fund_flow_fetch_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "fund_flow_fetch_failed",
                    f"failed to fetch fund flow (scope={scope}, period={period}): {exc}",
                    "check the data_source and network",
                ),
                is_error=True,
            )

        if not rows:
            await emit_debug_event(
                "operation_data_fund_flow.failed",
                {
                    "tool": self.name,
                    "scope": scope,
                    "period": period,
                    "error_code": "fund_flow_empty",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "fund_flow_empty",
                    f"no fund-flow rows for scope={scope} period={period}",
                    "try another period / scope, or check the data source",
                ),
                is_error=True,
            )

        ranked = sorted(rows, key=_sort_key)

        fund_flow_path = self._persist_rows(scope, period, ranked)
        manifest_path = self._write_manifest(
            scope=scope,
            period=period,
            sector_type=sector_type_key,
            data_source=source_name,
            fund_flow_path=fund_flow_path,
            count=len(ranked),
            top=top,
        )

        latest = [self._row_dict(r) for r in ranked[:top]]

        payload: dict[str, Any] = {
            "status": "ok",
            "scope": scope,
            "period": period,
            "data_source": source_name,
            "count": len(ranked),
            "top": top,
            "fund_flow_path": fund_flow_path,
            "manifest_path": manifest_path,
            "latest": latest,
        }
        if scope == _SCOPE_SECTOR:
            payload["sector_type"] = sector_type_key

        await emit_debug_event(
            "operation_data_fund_flow.created",
            {
                "tool": self.name,
                "scope": scope,
                "period": period,
                "sector_type": sector_type_key,
                "status": "ok",
                "count": len(ranked),
                "top": top,
            },
        )

        sector_suffix = f"/{sector_type_key}" if scope == _SCOPE_SECTOR else ""
        header = (
            f"资金流排名 scope={scope}{sector_suffix} period={period}: "
            f"{len(ranked)} 条 (top {top}, source={source_name}, status=ok)"
        )
        return ToolResult(text=append_json_payload(header, payload), is_error=False)

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_dict(row: Any) -> dict[str, Any]:
        return {
            "scope": row.scope,
            "symbol": row.symbol,
            "code": row.code,
            "name": row.name,
            "latest_price": row.latest_price,
            "change_pct": row.change_pct,
            "main_net_amount": row.main_net_amount,
            "main_net_pct": row.main_net_pct,
            "super_large_net_amount": row.super_large_net_amount,
            "large_net_amount": row.large_net_amount,
            "medium_net_amount": row.medium_net_amount,
            "small_net_amount": row.small_net_amount,
            "lead_stock": row.lead_stock,
        }

    def _persist_rows(self, scope: str, period: str, rows: list[Any]) -> str:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        records = [self._row_dict(r) for r in rows]
        df = pd.DataFrame(records, columns=list(_FUND_FLOW_CSV_COLUMNS))
        path = root / f"fund_flow_{_safe_code(scope)}_{_safe_code(period)}.csv"
        df.to_csv(path, index=False)
        return str(path)

    def _write_manifest(
        self,
        *,
        scope: str,
        period: str,
        sector_type: str | None,
        data_source: str,
        fund_flow_path: str,
        count: int,
        top: int,
    ) -> str:
        import json

        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "kind": "data_fund_flow",
            "scope": scope,
            "period": period,
            "sector_type": sector_type,
            "data_source": data_source,
            "fund_flow_path": fund_flow_path,
            "count": count,
            "top": top,
        }
        manifest_path = (
            root
            / f"data_fund_flow_manifest_{_safe_code(scope)}_{_safe_code(period)}.json"
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(manifest_path)


__all__ = ["DataFundFlowTool"]
