"""``data_sector_heat`` operation — A-share 题材 / 板块热度榜 (sector-heat ranking).

Sits on the sector axis alongside ``data_sector`` (which lists board names /
constituents). For one board family (``concept`` default, or ``industry``) it
pulls the whole-board snapshot the akshare board-name endpoints already return
— 涨跌幅 / 总市值 / 换手率 / 上涨·下跌家数 / 领涨股 + 领涨股涨跌幅 — ranks the
boards by 涨跌幅 descending (板块涨幅榜 = a first-order read of the day's 主线
热度), persists the full ranking to a local CSV, and returns a top-N preview.

There is no date and no per-symbol fan-out — this is a market-wide board
snapshot for the requested family.

Failure-mode discipline (per CLAUDE.md §错误可见性, distinct error_codes):

* Empty board list (no upstream error) → ``sector_heat_empty``.
* akshare raised on every retry → ``sector_heat_fetch_failed`` with
  ``error_type`` carrying the exception class.
* ``sector_type`` not in {concept, industry} → ``invalid_sector_type``.
* Unknown ``data_source`` → ``unknown_data_source``.
* Unknown kwargs → the ``_enforce_kwargs_contract`` ``unknown_arguments``.

Debug events (all key steps observable):

* ``operation_data_sector_heat.request`` — input keys
* ``operation_data_sector_heat.rejected`` — unknown_arguments
* ``operation_data_sector_heat.failed`` — validation / fetch / empty failure
* ``operation_data_sector_heat.validated`` — resolved sector_type / top / source
* ``operation_data_sector_heat.created`` — final envelope summary
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

# Only akshare serves the board heat today; ``auto`` resolves to it.
_SUPPORTED_SECTOR_HEAT_SOURCES = ("auto", "akshare")

_SECTOR_TYPE_CONCEPT = "concept"
_SECTOR_TYPE_INDUSTRY = "industry"
_SUPPORTED_SECTOR_TYPES = (_SECTOR_TYPE_CONCEPT, _SECTOR_TYPE_INDUSTRY)

_DEFAULT_SECTOR_TYPE = _SECTOR_TYPE_CONCEPT
_DEFAULT_TOP = 30

# CSV column order.
_SECTOR_HEAT_CSV_COLUMNS = (
    "board_name",
    "board_code",
    "sector_type",
    "change_pct",
    "total_mv",
    "turnover_rate",
    "up_count",
    "down_count",
    "leader_stock",
    "leader_change_pct",
    "provider",
)


class _InvalidSectorHeatArgument(ValueError):
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


def _build_sector_heat_provider(data_source: str):
    """Resolve a :class:`SectorProvider` for the requested source.

    ``auto`` and ``akshare`` both resolve to akshare — the only sector-heat
    source available today. Kept as an explicit dispatch so an unknown id
    surfaces a structured ``unknown_data_source`` rather than failing late.
    """
    if data_source in ("auto", "akshare"):
        from doyoutrade.data.sector_akshare import AkshareSectorProvider

        return AkshareSectorProvider(), "akshare"
    raise _InvalidSectorHeatArgument(
        "unknown_data_source",
        f"unknown data_source {data_source!r}",
        f"use one of: {', '.join(_SUPPORTED_SECTOR_HEAT_SOURCES)}",
    )


def _resolve_sector_type(raw: Any) -> str:
    key = raw if raw is not None else _DEFAULT_SECTOR_TYPE
    if not isinstance(key, str) or key not in _SUPPORTED_SECTOR_TYPES:
        raise _InvalidSectorHeatArgument(
            "invalid_sector_type",
            f"sector_type={key!r} is not valid",
            f"use one of: {', '.join(_SUPPORTED_SECTOR_TYPES)}",
        )
    return key


def _resolve_top(raw: Any) -> int:
    if raw is None:
        return _DEFAULT_TOP
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _InvalidSectorHeatArgument(
            "validation_error",
            f"top must be a positive integer, got {type(raw).__name__}({raw!r})",
            "pass top as a positive integer, e.g. 30",
        )
    if raw <= 0:
        raise _InvalidSectorHeatArgument(
            "validation_error",
            f"top must be a positive integer, got {raw}",
            "pass top >= 1",
        )
    return raw


def _sort_key(row: Any) -> tuple[int, float]:
    """Sort by 涨跌幅 descending, pushing None values to the end.

    Returning ``(1, 0.0)`` for a None change_pct and ``(0, -value)`` otherwise
    keeps None rows last under an ascending Python sort without inventing a
    numeric value for them (per §错误可见性: no None → 0 coercion).
    """
    change = row.change_pct
    if change is None:
        return (1, 0.0)
    return (0, -change)


class DataSectorHeatTool(OperationHandler):
    name = "data_sector_heat"
    description = (
        "Fetch the A-share 题材 / 板块热度榜 (sector-heat ranking) — for one board "
        "family (sector_type=concept, default, or industry) it pulls the "
        "whole-board snapshot the akshare board-name endpoints return: 涨跌幅 "
        "(board change), 总市值 (market cap), 换手率 (turnover rate), 上涨/下跌家数 "
        "(advance/decline counts), and the 领涨股 (leader) + its 涨跌幅. Boards are "
        "ranked by 涨跌幅 (change_pct) DESCENDING — the 板块涨幅榜 is a first-order "
        "read of where the day's 主线 (dominant theme) heat sits. This is a "
        "MARKET-WIDE board snapshot, not a per-symbol series: there is no symbols "
        "input and NO date. top (default 30) picks how many boards are previewed "
        "under latest while the CSV holds the full ranking. Numeric columns the "
        "upstream omits come back None (never 0)."
    )
    category = "data"
    parameters = {
        "type": "object",
        "properties": {
            "sector_type": {
                "type": "string",
                "enum": list(_SUPPORTED_SECTOR_TYPES),
                "default": _DEFAULT_SECTOR_TYPE,
                "description": "concept (概念板块, default) or industry (行业板块).",
            },
            "top": {
                "type": "integer",
                "minimum": 1,
                "default": _DEFAULT_TOP,
                "description": "Boards previewed under latest (CSV holds the full ranking).",
            },
            "data_source": {
                "type": "string",
                "enum": list(_SUPPORTED_SECTOR_HEAT_SOURCES),
                "default": "auto",
                "description": "Sector-heat provider id (akshare only today).",
            },
        },
        "additionalProperties": False,
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                "operation_data_sector_heat.rejected",
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
            "operation_data_sector_heat.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )

        try:
            sector_type = _resolve_sector_type(kwargs.get("sector_type"))
            top = _resolve_top(kwargs.get("top"))
            data_source = kwargs.get("data_source") or "auto"
            provider, source_name = _build_sector_heat_provider(data_source)
        except _InvalidSectorHeatArgument as exc:
            await emit_debug_event(
                "operation_data_sector_heat.failed",
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
            "operation_data_sector_heat.validated",
            {
                "tool": self.name,
                "sector_type": sector_type,
                "top": top,
                "data_source": source_name,
            },
        )

        try:
            rows = await provider.get_sector_heat(sector_type)
        except Exception as exc:
            logger.exception(
                "data_sector_heat upstream fetch failure sector_type=%s data_source=%s",
                sector_type, source_name,
            )
            await emit_debug_event(
                "operation_data_sector_heat.failed",
                {
                    "tool": self.name,
                    "sector_type": sector_type,
                    "error_code": "sector_heat_fetch_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "sector_heat_fetch_failed",
                    f"failed to fetch sector heat (sector_type={sector_type}): {exc}",
                    "check the data_source and network",
                ),
                is_error=True,
            )

        if not rows:
            await emit_debug_event(
                "operation_data_sector_heat.failed",
                {
                    "tool": self.name,
                    "sector_type": sector_type,
                    "error_code": "sector_heat_empty",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "sector_heat_empty",
                    f"no sector-heat rows for sector_type={sector_type}",
                    "try the other sector_type, or check the data source",
                ),
                is_error=True,
            )

        ranked = sorted(rows, key=_sort_key)

        sector_heat_path = self._persist_rows(sector_type, ranked)
        manifest_path = self._write_manifest(
            sector_type=sector_type,
            data_source=source_name,
            sector_heat_path=sector_heat_path,
            count=len(ranked),
            top=top,
        )

        latest = [self._row_dict(r) for r in ranked[:top]]

        payload: dict[str, Any] = {
            "status": "ok",
            "sector_type": sector_type,
            "data_source": source_name,
            "count": len(ranked),
            "top": top,
            "sector_heat_path": sector_heat_path,
            "manifest_path": manifest_path,
            "latest": latest,
        }

        await emit_debug_event(
            "operation_data_sector_heat.created",
            {
                "tool": self.name,
                "sector_type": sector_type,
                "status": "ok",
                "count": len(ranked),
                "top": top,
            },
        )

        header = (
            f"题材/板块热度 sector_type={sector_type}: {len(ranked)} 个板块 "
            f"(top {top}, source={source_name}, status=ok)"
        )
        return ToolResult(text=append_json_payload(header, payload), is_error=False)

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_dict(row: Any) -> dict[str, Any]:
        return {
            "board_name": row.board_name,
            "board_code": row.board_code,
            "sector_type": row.sector_type,
            "change_pct": row.change_pct,
            "total_mv": row.total_mv,
            "turnover_rate": row.turnover_rate,
            "up_count": row.up_count,
            "down_count": row.down_count,
            "leader_stock": row.leader_stock,
            "leader_change_pct": row.leader_change_pct,
            "provider": row.provider,
        }

    def _persist_rows(self, sector_type: str, rows: list[Any]) -> str:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        records = [self._row_dict(r) for r in rows]
        df = pd.DataFrame(records, columns=list(_SECTOR_HEAT_CSV_COLUMNS))
        path = root / f"sector_heat_{_safe_code(sector_type)}.csv"
        df.to_csv(path, index=False)
        return str(path)

    def _write_manifest(
        self,
        *,
        sector_type: str,
        data_source: str,
        sector_heat_path: str,
        count: int,
        top: int,
    ) -> str:
        import json

        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "kind": "data_sector_heat",
            "sector_type": sector_type,
            "data_source": data_source,
            "sector_heat_path": sector_heat_path,
            "count": count,
            "top": top,
        }
        manifest_path = (
            root / f"data_sector_heat_manifest_{_safe_code(sector_type)}.json"
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(manifest_path)


__all__ = ["DataSectorHeatTool"]
