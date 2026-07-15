"""``data_market_breadth`` operation — A-share limit-up / down / broken-board breadth.

Sits on the whole-market 打板 (limit-hitting) breadth axis: for one
trading day it pulls the 涨停 / 跌停 / 炸板 pools, aggregates a 市场涨停面板
(limit-up panel), a 连板梯队 (consecutive-limit ladder), and a rule-based
情绪温度计 (sentiment thermometer), and persists each pool to a local CSV.
This is the core data axis for A-share short-term 打板 traders.

Unlike ``data_research_reports`` / ``data_news`` this is a *market-wide*
single-day operation — there is no per-symbol fan-out, no window, and no
``symbols`` input. It takes a single optional ``date`` (defaulting to
today in Asia/Shanghai) plus a ``data_source``.

Failure-mode discipline (per CLAUDE.md §错误可见性, distinct error_codes):

* All three pools empty (no upstream errors) → ``market_breadth_empty``
  (very likely a non-trading day or the after-hours snapshot hasn't
  updated yet).
* All three pools failed upstream → ``market_breadth_fetch_failed`` with
  ``error_type`` carrying the first pool's exception class.
* Some pools succeeded, some failed → ``status: partial`` (never a whole
  failure), with the failed pools named in ``pool_errors``.
* Malformed ``date`` → ``invalid_date``.
* Unknown ``data_source`` → ``unknown_data_source``.
* Unknown kwargs → the ``_enforce_kwargs_contract`` ``unknown_arguments``.

The sentiment layer is a **rule-based, single-day, non-predictive**
label — it only describes the current day's state (see ``_classify_sentiment``)
and never gives buy / sell advice, consistent with the assistant's
应答纪律.

Debug events (all key steps observable):

* ``operation_data_market_breadth.request`` — input keys
* ``operation_data_market_breadth.rejected`` — unknown_arguments
* ``operation_data_market_breadth.failed`` — validation / fetch / empty failure
* ``operation_data_market_breadth.validated`` — resolved trade_date + source
* ``operation_data_market_breadth.created`` — final envelope summary
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from doyoutrade.api.operations.market_data import _get_artifacts_root, _safe_code
from doyoutrade.debug import emit_debug_event
from doyoutrade.tools import OperationHandler, ToolResult
from doyoutrade.tools._prose import append_json_payload, format_error_text, format_unknown_args

logger = logging.getLogger(__name__)

# Only akshare serves the limit pools today; ``auto`` resolves to it.
_SUPPORTED_BREADTH_SOURCES = ("auto", "akshare")

_A_SHARE_TZ = ZoneInfo("Asia/Shanghai")

# Fixed disclaimer echoed on every sentiment label (single-day snapshot,
# non-predictive, not investment advice). Aligned with main_agent.j2 应答纪律.
_SENTIMENT_DISCLAIMER = (
    "本标签基于当日涨跌停/连板/炸板的规则描述，是单日快照，非预测、非投资建议；"
    "完整情绪周期需结合多日趋势。"
)

# CSV column order per pool (canonical symbol + original 中文 columns).
_POOL_CSV_COLUMNS = (
    "symbol",
    "code",
    "name",
    "change_pct",
    "latest_price",
    "turnover",
    "circulating_mv",
    "total_mv",
    "turnover_rate",
    "industry",
    "streak",
    "broken_board_count",
    "first_seal_time",
    "last_seal_time",
)


class _InvalidBreadthArgument(ValueError):
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


def _build_market_breadth_provider(data_source: str):
    """Resolve a :class:`MarketBreadthProvider` for the requested source.

    ``auto`` and ``akshare`` both resolve to akshare — the only breadth
    source available today. Kept as an explicit dispatch so an unknown id
    surfaces a structured ``unknown_data_source`` rather than failing late.
    """
    if data_source in ("auto", "akshare"):
        from doyoutrade.data.limit_pool_akshare import AkshareMarketBreadthProvider

        return AkshareMarketBreadthProvider(), "akshare"
    raise _InvalidBreadthArgument(
        "unknown_data_source",
        f"unknown data_source {data_source!r}",
        f"use one of: {', '.join(_SUPPORTED_BREADTH_SOURCES)}",
    )


def _resolve_trade_date(raw: Any) -> str:
    """Resolve the caller's ``date`` to a ``YYYYMMDD`` upstream token.

    Accepts ``YYYY-MM-DD`` (the CLI-facing / assistant-facing shape) or a
    bare ``YYYYMMDD``. ``None`` defaults to *today* in Asia/Shanghai — we do
    NOT build our own trading calendar; a non-trading day flows through and
    surfaces as ``market_breadth_empty`` from the upstream pools.
    """
    if raw is None:
        now = datetime.now(timezone.utc).astimezone(_A_SHARE_TZ)
        return now.strftime("%Y%m%d")
    if not isinstance(raw, str):
        raise _InvalidBreadthArgument(
            "invalid_date",
            f"date must be a YYYY-MM-DD string, got {type(raw).__name__}({raw!r})",
            "pass date as YYYY-MM-DD, e.g. 2026-07-03",
        )
    text = raw.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        compact = text.replace("-", "")
    elif re.match(r"^\d{8}$", text):
        compact = text
    else:
        raise _InvalidBreadthArgument(
            "invalid_date",
            f"date={raw!r} is not a valid YYYY-MM-DD date",
            "use YYYY-MM-DD, e.g. 2026-07-03",
        )
    # Validate it's a real calendar date (rejects 2026-13-40).
    try:
        datetime.strptime(compact, "%Y%m%d")
    except ValueError as exc:
        raise _InvalidBreadthArgument(
            "invalid_date",
            f"date={raw!r} is not a valid calendar date: {exc}",
            "use a real YYYY-MM-DD date",
        ) from exc
    return compact


def _classify_sentiment(
    *,
    zt: int,
    dt: int,
    zb: int,
    max_streak: int,
    broken_rate: float,
) -> dict[str, Any]:
    """Rule-based single-day sentiment label from breadth aggregates.

    The thresholds are **explicit and ordered** — the label only describes
    the current day and is never a prediction / buy-sell signal. The raw
    inputs are echoed back under ``inputs`` so the caller can judge for
    themselves rather than trusting a black box.
    """
    if dt >= zt or broken_rate >= 0.4 or (zt < 25 and max_streak <= 3):
        label = "退潮/低迷"
    elif zt >= 80 and max_streak >= 6 and broken_rate < 0.2:
        label = "高潮/亢奋"
    elif zt >= 50 and max_streak >= 5:
        label = "发酵/活跃"
    elif broken_rate >= 0.25:
        label = "分歧加剧"
    else:
        label = "中性"

    reason = (
        f"涨停 {zt} 家、跌停 {dt} 家、炸板 {zb} 家、"
        f"最高 {max_streak} 连板、炸板率 {broken_rate:.0%}"
    )
    return {
        "label": label,
        "reason": reason,
        "disclaimer": _SENTIMENT_DISCLAIMER,
        "inputs": {
            "limit_up_count": zt,
            "limit_down_count": dt,
            "broken_board_count": zb,
            "max_streak": max_streak,
            "broken_board_rate": broken_rate,
        },
    }


class DataMarketBreadthTool(OperationHandler):
    name = "data_market_breadth"
    description = (
        "Fetch the A-share limit-up (涨停) / limit-down (跌停) / broken-board "
        "(炸板) pools for one trading day and aggregate a market limit-up "
        "panel, a consecutive-limit ladder (连板梯队), and a rule-based "
        "sentiment thermometer (情绪温度计). This is the core data axis for "
        "short-term 打板 traders. ``date`` defaults to today (Asia/Shanghai); "
        "a non-trading day surfaces as market_breadth_empty. Each pool is "
        "written to a local CSV (limit_up_pool / limit_down_pool / "
        "broken_board_pool). The sentiment label is a single-day, "
        "rule-based, NON-PREDICTIVE description — it is never investment "
        "advice. When one pool fails but others succeed the run returns "
        "status: partial and names the failed pool in pool_errors."
    )
    category = "data"
    parameters = {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Trading day YYYY-MM-DD (default: today, Asia/Shanghai).",
            },
            "data_source": {
                "type": "string",
                "enum": list(_SUPPORTED_BREADTH_SOURCES),
                "default": "auto",
                "description": "Breadth provider id (akshare only today).",
            },
        },
        "additionalProperties": False,
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                "operation_data_market_breadth.rejected",
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
            "operation_data_market_breadth.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )

        try:
            trade_date = _resolve_trade_date(kwargs.get("date"))
            data_source = kwargs.get("data_source") or "auto"
            provider, source_name = _build_market_breadth_provider(data_source)
        except _InvalidBreadthArgument as exc:
            await emit_debug_event(
                "operation_data_market_breadth.failed",
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
            "operation_data_market_breadth.validated",
            {"tool": self.name, "trade_date": trade_date, "data_source": source_name},
        )

        try:
            breadth = await provider.fetch_market_breadth(trade_date)
        except Exception as exc:
            logger.exception(
                "data_market_breadth unexpected fetch failure trade_date=%s data_source=%s",
                trade_date, source_name,
            )
            await emit_debug_event(
                "operation_data_market_breadth.failed",
                {
                    "tool": self.name,
                    "trade_date": trade_date,
                    "error_code": "market_breadth_fetch_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "market_breadth_fetch_failed",
                    f"failed to fetch market breadth for {trade_date}: {exc}",
                    "check the data_source and network",
                ),
                is_error=True,
            )

        zt = breadth.limit_up_count
        dt = breadth.limit_down_count
        zb = breadth.broken_board_count
        pool_errors = dict(breadth.pool_errors)

        # Empty vs fetch-failed vs partial — three distinct outcomes.
        if zt == 0 and dt == 0 and zb == 0:
            if pool_errors:
                # Every pool that had data also errored → treat as fetch failure.
                first_err = next(iter(pool_errors.values()))
                error_type = first_err.split(":", 1)[0].strip()
                await emit_debug_event(
                    "operation_data_market_breadth.failed",
                    {
                        "tool": self.name,
                        "trade_date": trade_date,
                        "error_code": "market_breadth_fetch_failed",
                        "error_type": error_type,
                        "pool_errors": pool_errors,
                    },
                )
                return ToolResult(
                    text=format_error_text(
                        "market_breadth_fetch_failed",
                        f"all limit pools failed for {trade_date}: {pool_errors}",
                        "check the data_source and network",
                    ),
                    is_error=True,
                )
            await emit_debug_event(
                "operation_data_market_breadth.failed",
                {
                    "tool": self.name,
                    "trade_date": trade_date,
                    "error_code": "market_breadth_empty",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "market_breadth_empty",
                    f"no limit-up / limit-down / broken-board data for {trade_date}",
                    "confirm it is a trading day and the after-hours snapshot has updated",
                ),
                is_error=True,
            )

        limit_up_path = self._persist_pool("limit_up_pool", trade_date, breadth.limit_up)
        limit_down_path = self._persist_pool("limit_down_pool", trade_date, breadth.limit_down)
        broken_board_path = self._persist_pool(
            "broken_board_pool", trade_date, breadth.broken_board
        )
        manifest_path = self._write_manifest(
            trade_date=trade_date,
            data_source=source_name,
            limit_up_path=limit_up_path,
            limit_down_path=limit_down_path,
            broken_board_path=broken_board_path,
            pool_errors=pool_errors,
        )

        sentiment = _classify_sentiment(
            zt=zt,
            dt=dt,
            zb=zb,
            max_streak=breadth.max_streak,
            broken_rate=breadth.broken_board_rate,
        )

        status = "partial" if pool_errors else "ok"

        payload: dict[str, Any] = {
            "status": status,
            "trade_date": trade_date,
            "data_source": source_name,
            "limit_up_count": zt,
            "limit_down_count": dt,
            "broken_board_count": zb,
            "broken_board_rate": round(breadth.broken_board_rate, 4),
            "max_streak": breadth.max_streak,
            "ladder": breadth.ladder,
            "sentiment": sentiment,
            "limit_up_path": limit_up_path,
            "limit_down_path": limit_down_path,
            "broken_board_path": broken_board_path,
            "manifest_path": manifest_path,
            "pool_errors": pool_errors,
        }

        await emit_debug_event(
            "operation_data_market_breadth.created",
            {
                "tool": self.name,
                "trade_date": trade_date,
                "status": status,
                "limit_up_count": zt,
                "limit_down_count": dt,
                "broken_board_count": zb,
                "max_streak": breadth.max_streak,
                "sentiment_label": sentiment["label"],
                "pool_errors": sorted(pool_errors.keys()),
            },
        )

        header = self._summary_header(payload)
        return ToolResult(text=append_json_payload(header, payload), is_error=False)

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    def _persist_pool(self, kind: str, trade_date: str, stocks: list[Any]) -> str:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "symbol": s.symbol,
                "code": s.code,
                "name": s.name,
                "change_pct": s.change_pct,
                "latest_price": s.latest_price,
                "turnover": s.turnover,
                "circulating_mv": s.circulating_mv,
                "total_mv": s.total_mv,
                "turnover_rate": s.turnover_rate,
                "industry": s.industry,
                "streak": s.streak,
                "broken_board_count": s.broken_board_count,
                "first_seal_time": s.first_seal_time,
                "last_seal_time": s.last_seal_time,
            }
            for s in stocks
        ]
        # Keep a rectangular CSV with the documented column order even when
        # the pool is empty (an empty pool still writes a header-only CSV).
        df = pd.DataFrame(rows, columns=list(_POOL_CSV_COLUMNS))
        path = root / f"{kind}_{_safe_code(trade_date)}.csv"
        df.to_csv(path, index=False)
        return str(path)

    def _write_manifest(
        self,
        *,
        trade_date: str,
        data_source: str,
        limit_up_path: str,
        limit_down_path: str,
        broken_board_path: str,
        pool_errors: dict[str, str],
    ) -> str:
        import json

        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "kind": "data_market_breadth",
            "trade_date": trade_date,
            "data_source": data_source,
            "limit_up_path": limit_up_path,
            "limit_down_path": limit_down_path,
            "broken_board_path": broken_board_path,
            "pool_errors": pool_errors,
        }
        manifest_path = root / f"data_market_breadth_manifest_{_safe_code(trade_date)}.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(manifest_path)

    def _summary_header(self, payload: dict[str, Any]) -> str:
        return (
            f"market breadth {payload['trade_date']}: 涨停 {payload['limit_up_count']} / "
            f"跌停 {payload['limit_down_count']} / 炸板 {payload['broken_board_count']} "
            f"(最高 {payload['max_streak']} 连板, 炸板率 {payload['broken_board_rate']:.0%}, "
            f"情绪={payload['sentiment']['label']}, status={payload['status']})"
        )


__all__ = ["DataMarketBreadthTool"]
