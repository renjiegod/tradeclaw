"""``data_chips`` operation — A-share 筹码分布 (chip distribution) for one symbol.

Single-mode tool: given a canonical ``symbol``, fetches the most recent
``days`` daily 筹码分布 rows (获利比例 / 平均成本 / 90%·70% 成本集中度) via
akshare ``stock_cyq_em`` and persists them to a local CSV.

Failure-mode discipline (per CLAUDE.md §错误可见性, distinct error_codes),
mirrors ``data_lhb``:

* akshare raised (any other exception) on every retry → distinct
  ``chip_distribution_fetch_failed`` with ``error_type`` carrying the
  exception class.
* A genuinely empty result (ETF / index / delisted name — 筹码分布 is an
  A-share-individual-stock-only signal) → distinct ``chip_distribution_empty``
  (not a fetch error).
* Missing/empty ``symbol`` → ``invalid_symbol``.
* Unknown ``data_source`` → ``unknown_data_source``.
* Unknown kwargs → the ``_enforce_kwargs_contract`` ``unknown_arguments``.

Debug events (all key steps observable):

* ``operation_data_chips.request`` — input keys
* ``operation_data_chips.rejected`` — unknown_arguments
* ``operation_data_chips.failed`` — validation / fetch / empty
* ``operation_data_chips.validated`` — resolved symbol + days + source
* ``operation_data_chips.created`` — final envelope summary
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

# Only akshare serves 筹码分布 today; ``auto`` resolves to it.
_SUPPORTED_CHIP_SOURCES = ("auto", "akshare")

_DEFAULT_DAYS = 1
_MAX_DAYS = 90

_CHIP_CSV_COLUMNS = (
    "symbol",
    "date",
    "profit_ratio",
    "avg_cost",
    "cost_90_low",
    "cost_90_high",
    "concentration_90",
    "cost_70_low",
    "cost_70_high",
    "concentration_70",
    "provider",
)


class _InvalidChipDistributionArgument(ValueError):
    """Structured argument failure carrying a stable ``error_code``."""

    def __init__(
        self,
        error_code: str,
        message: str,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.hint = hint


def _build_chip_distribution_provider(data_source: str):
    """Resolve a chip-distribution provider for the requested source.

    ``auto`` and ``akshare`` both resolve to akshare — the only 筹码分布
    source available today. Kept as an explicit dispatch so an unknown id
    surfaces a structured ``unknown_data_source`` rather than failing late.
    """
    if data_source in ("auto", "akshare"):
        from doyoutrade.data.chip_distribution_akshare import AkshareChipDistributionProvider

        return AkshareChipDistributionProvider(), "akshare"
    raise _InvalidChipDistributionArgument(
        "unknown_data_source",
        f"unknown data_source {data_source!r}",
        f"use one of: {', '.join(_SUPPORTED_CHIP_SOURCES)}",
    )


def _resolve_days(raw: Any) -> int:
    if raw is None:
        return _DEFAULT_DAYS
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _InvalidChipDistributionArgument(
            "invalid_days",
            f"days must be an int, got {type(raw).__name__}({raw!r})",
            f"pass an integer between 1 and {_MAX_DAYS}",
        )
    if raw < 1 or raw > _MAX_DAYS:
        raise _InvalidChipDistributionArgument(
            "invalid_days",
            f"days={raw} out of range [1, {_MAX_DAYS}]",
            f"pass an integer between 1 and {_MAX_DAYS}",
        )
    return raw


class DataChipsTool(OperationHandler):
    name = "data_chips"
    description = (
        "Fetch A-share 筹码分布 (chip distribution / 筹码集中度) for one canonical "
        "symbol: 获利比例 (profit ratio), 平均成本 (avg cost), and the 90%/70% "
        "cost-band concentration akshare computes from OHLCV + turnover. "
        "A-share individual stocks only — ETFs/indices/non-A-share names "
        "return the distinct chip_distribution_empty (never a fabricated "
        "snapshot). Defaults to the single latest trading day; pass days>1 "
        "for a short trend window. Writes a local CSV with the canonical "
        "symbol."
    )
    category = "data"
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Canonical CODE.EXCHANGE (e.g. 600519.SH).",
            },
            "days": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_DAYS,
                "default": _DEFAULT_DAYS,
                "description": (
                    f"Most recent N trading days of 筹码分布 (1-{_MAX_DAYS}). "
                    "Default 1 (latest day only)."
                ),
            },
            "data_source": {
                "type": "string",
                "enum": list(_SUPPORTED_CHIP_SOURCES),
                "default": "auto",
                "description": "筹码分布 provider id (akshare only today).",
            },
        },
        "required": ["symbol"],
        "additionalProperties": False,
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                "operation_data_chips.rejected",
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
            "operation_data_chips.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )

        symbol = str(kwargs.get("symbol") or "").strip()
        try:
            if not symbol:
                raise _InvalidChipDistributionArgument(
                    "invalid_symbol",
                    "symbol must be a non-empty CODE.EXCHANGE string",
                    "pass e.g. --symbol 600519.SH",
                )
            days = _resolve_days(kwargs.get("days"))
            data_source = kwargs.get("data_source") or "auto"
            provider, source_name = _build_chip_distribution_provider(data_source)
        except _InvalidChipDistributionArgument as exc:
            await emit_debug_event(
                "operation_data_chips.failed",
                {
                    "tool": self.name,
                    "symbol": symbol,
                    "error_code": exc.error_code,
                    "message": str(exc),
                    "hint": exc.hint,
                },
            )
            return ToolResult(
                text=format_error_text(exc.error_code, str(exc), exc.hint),
                is_error=True,
            )

        await emit_debug_event(
            "operation_data_chips.validated",
            {
                "tool": self.name,
                "symbol": symbol,
                "days": days,
                "data_source": source_name,
            },
        )

        try:
            rows = await provider.fetch_chip_distribution(symbol, days=days)
        except Exception as exc:
            logger.exception(
                "data_chips upstream fetch failure symbol=%s days=%d data_source=%s",
                symbol, days, source_name,
            )
            await emit_debug_event(
                "operation_data_chips.failed",
                {
                    "tool": self.name,
                    "symbol": symbol,
                    "days": days,
                    "error_code": "chip_distribution_fetch_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "chip_distribution_fetch_failed",
                    f"failed to fetch 筹码分布 for {symbol}: {exc}",
                    "check the symbol and data_source",
                ),
                is_error=True,
            )

        if not rows:
            await emit_debug_event(
                "operation_data_chips.failed",
                {
                    "tool": self.name,
                    "symbol": symbol,
                    "days": days,
                    "error_code": "chip_distribution_empty",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "chip_distribution_empty",
                    f"no 筹码分布 rows for {symbol}",
                    "筹码分布 only covers A-share individual stocks — confirm "
                    "this is not an ETF/index/delisted name",
                ),
                is_error=True,
            )

        chips_path = self._persist_rows(symbol, rows)
        manifest_path = self._write_manifest(
            symbol=symbol,
            days=days,
            data_source=source_name,
            chips_path=chips_path,
            count=len(rows),
        )

        latest = [self._row_dict(r) for r in rows]

        payload: dict[str, Any] = {
            "status": "ok",
            "symbol": symbol,
            "days": days,
            "data_source": source_name,
            "count": len(rows),
            "chips_path": chips_path,
            "manifest_path": manifest_path,
            "latest": latest,
        }

        await emit_debug_event(
            "operation_data_chips.created",
            {
                "tool": self.name,
                "symbol": symbol,
                "days": days,
                "status": "ok",
                "count": len(rows),
            },
        )

        header = f"筹码分布 {symbol}: {len(rows)} 条 (source={source_name}, status=ok)"
        return ToolResult(text=append_json_payload(header, payload), is_error=False)

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_dict(row: Any) -> dict[str, Any]:
        return {
            "symbol": row.symbol,
            "date": row.date,
            "profit_ratio": row.profit_ratio,
            "avg_cost": row.avg_cost,
            "cost_90_low": row.cost_90_low,
            "cost_90_high": row.cost_90_high,
            "concentration_90": row.concentration_90,
            "cost_70_low": row.cost_70_low,
            "cost_70_high": row.cost_70_high,
            "concentration_70": row.concentration_70,
            "provider": row.provider,
        }

    def _persist_rows(self, symbol: str, rows: list[Any]) -> str:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        records = [self._row_dict(r) for r in rows]
        df = pd.DataFrame(records, columns=list(_CHIP_CSV_COLUMNS))
        path = root / f"chips_{_safe_code(symbol)}.csv"
        df.to_csv(path, index=False)
        return str(path)

    def _write_manifest(
        self,
        *,
        symbol: str,
        days: int,
        data_source: str,
        chips_path: str,
        count: int,
    ) -> str:
        import json

        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "kind": "data_chips",
            "symbol": symbol,
            "days": days,
            "data_source": data_source,
            "chips_path": chips_path,
            "count": count,
        }
        path = root / f"data_chips_manifest_{_safe_code(symbol)}.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)


__all__ = ["DataChipsTool"]
