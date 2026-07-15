"""``data_lhb`` operation — A-share 龙虎榜 (dragon-tiger board), two modes.

Sits on the 龙虎榜 axis with two mutually-exclusive modes selected by whether a
``symbol`` was passed:

* **Market mode** (no ``symbol``): the whole-market daily board. For a single
  day or a date range it pulls the exchange's daily large-order / abnormal-move
  disclosure list (``stock_lhb_detail_em``), canonicalizes each name's code,
  persists the rows to a local CSV, and returns a preview. There is no
  per-symbol fan-out; it takes a single ``date`` OR a ``start`` / ``end`` range
  (defaulting to *today* Asia/Shanghai).
* **Seat mode** (``symbol`` given): one name's per-营业部 (trading desk)
  席位明细 for a single ``date`` (``stock_lhb_stock_detail_em``, 买入 + 卖出).
  Each seat is tagged with a best-effort 游资名 (``hot_money``) via the static
  starter library and flagged ``is_institution`` for 机构专用 desks. Seat mode
  requires a single ``date`` — ``start`` / ``end`` are rejected.

Failure-mode discipline (per CLAUDE.md §错误可见性, distinct error_codes):

* Market: no name made the board in the window (no upstream error) →
  ``lhb_empty`` (very likely a non-trading window or the after-hours snapshot
  hasn't updated yet).
* Seat: the name did NOT make the board on the requested day (akshare's own
  ``None``-subscript ``TypeError``) → ``lhb_no_seat_data`` — a **distinct**
  condition from a transport failure; confirm the name actually 上榜 that day.
* akshare raised (any other exception) on every retry → ``lhb_fetch_failed``
  with ``error_type`` carrying the exception class.
* Malformed / conflicting dates (incl. seat mode given a range) →
  ``invalid_date``.
* Unknown ``data_source`` → ``unknown_data_source``.
* Unknown kwargs → the ``_enforce_kwargs_contract`` ``unknown_arguments``.

Debug events (all key steps observable):

* ``operation_data_lhb.request`` — input keys
* ``operation_data_lhb.rejected`` — unknown_arguments
* ``operation_data_lhb.failed`` — validation / fetch / empty / no-seat-data
* ``operation_data_lhb.validated`` — resolved window + source + mode
* ``operation_data_lhb.created`` — final envelope summary
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

# Only akshare serves the 龙虎榜 today; ``auto`` resolves to it.
_SUPPORTED_LHB_SOURCES = ("auto", "akshare")

_A_SHARE_TZ = ZoneInfo("Asia/Shanghai")

# Number of preview rows returned inline under ``latest``.
_PREVIEW_ROWS = 20

# CSV column order (canonical symbol + key 中文 fields).
_LHB_CSV_COLUMNS = (
    "symbol",
    "code",
    "name",
    "on_date",
    "reason",
    "interpretation",
    "change_pct",
    "close_price",
    "net_buy_amount",
    "buy_amount",
    "sell_amount",
    "turnover_rate",
    "circulating_mv",
)

# CSV column order for seat mode (canonical symbol + date + 席位 fields).
_LHB_SEAT_CSV_COLUMNS = (
    "symbol",
    "date",
    "side",
    "seat_name",
    "seat_type",
    "hot_money",
    "is_institution",
    "buy_amount",
    "sell_amount",
    "net_amount",
    "buy_pct",
    "sell_pct",
    "provider",
)


class _InvalidLhbArgument(ValueError):
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


def _build_dragon_tiger_provider(data_source: str):
    """Resolve a :class:`DragonTigerProvider` for the requested source.

    ``auto`` and ``akshare`` both resolve to akshare — the only 龙虎榜 source
    available today. Kept as an explicit dispatch so an unknown id surfaces a
    structured ``unknown_data_source`` rather than failing late.
    """
    if data_source in ("auto", "akshare"):
        from doyoutrade.data.lhb_akshare import AkshareDragonTigerProvider

        return AkshareDragonTigerProvider(), "akshare"
    raise _InvalidLhbArgument(
        "unknown_data_source",
        f"unknown data_source {data_source!r}",
        f"use one of: {', '.join(_SUPPORTED_LHB_SOURCES)}",
    )


def _compact_date(raw: Any, *, field: str) -> str:
    """Normalize one ``YYYY-MM-DD`` / ``YYYYMMDD`` input to ``YYYYMMDD``.

    Raises :class:`_InvalidLhbArgument` (``invalid_date``) on any non-string /
    malformed / non-calendar date. We do NOT build a trading calendar — a
    non-trading day flows through and surfaces as ``lhb_empty`` upstream.
    """
    if not isinstance(raw, str):
        raise _InvalidLhbArgument(
            "invalid_date",
            f"{field} must be a YYYY-MM-DD string, got {type(raw).__name__}({raw!r})",
            "pass dates as YYYY-MM-DD, e.g. 2026-07-03",
        )
    text = raw.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        compact = text.replace("-", "")
    elif re.match(r"^\d{8}$", text):
        compact = text
    else:
        raise _InvalidLhbArgument(
            "invalid_date",
            f"{field}={raw!r} is not a valid YYYY-MM-DD date",
            "use YYYY-MM-DD, e.g. 2026-07-03",
        )
    try:
        datetime.strptime(compact, "%Y%m%d")
    except ValueError as exc:
        raise _InvalidLhbArgument(
            "invalid_date",
            f"{field}={raw!r} is not a valid calendar date: {exc}",
            "use a real YYYY-MM-DD date",
        ) from exc
    return compact


def _resolve_window(
    date: Any, start: Any, end: Any
) -> tuple[str, str]:
    """Resolve the caller's ``date`` / ``start`` / ``end`` to a compact window.

    * ``date`` given → single-day window (start == end == that day). It is
      mutually exclusive with ``start`` / ``end``.
    * ``start`` / ``end`` given → an explicit range; both must be present.
    * Nothing given → *today* in Asia/Shanghai (single day). We do NOT build
      our own trading calendar; a non-trading window surfaces as ``lhb_empty``.
    """
    has_date = date is not None
    has_range = start is not None or end is not None
    if has_date and has_range:
        raise _InvalidLhbArgument(
            "invalid_date",
            "date is mutually exclusive with start / end",
            "pass either --date (single day) or --start/--end (range), not both",
        )
    if has_date:
        compact = _compact_date(date, field="date")
        return compact, compact
    if has_range:
        if start is None or end is None:
            raise _InvalidLhbArgument(
                "invalid_date",
                "start and end must both be provided for a range",
                "pass both --start and --end, or a single --date",
            )
        start_compact = _compact_date(start, field="start")
        end_compact = _compact_date(end, field="end")
        if start_compact > end_compact:
            raise _InvalidLhbArgument(
                "invalid_date",
                f"start={start!r} is after end={end!r}",
                "pass start <= end",
            )
        return start_compact, end_compact
    now = datetime.now(timezone.utc).astimezone(_A_SHARE_TZ)
    today = now.strftime("%Y%m%d")
    return today, today


def _resolve_seat_date(date: Any, start: Any, end: Any) -> str:
    """Resolve the seat-mode single ``date`` to a compact ``YYYYMMDD`` token.

    Seat mode is per-name, per-*day*: it supports exactly one ``date`` and
    rejects a ``start`` / ``end`` range with a structured ``invalid_date``
    (whose hint says seat mode is single-day only). When ``date`` is omitted it
    defaults to *today* in Asia/Shanghai — matching market mode's default.
    """
    if start is not None or end is not None:
        raise _InvalidLhbArgument(
            "invalid_date",
            "seat mode (--symbol) supports a single --date only, not a range",
            "drop --start/--end and pass one --date, e.g. --date 2026-07-03",
        )
    if date is not None:
        return _compact_date(date, field="date")
    now = datetime.now(timezone.utc).astimezone(_A_SHARE_TZ)
    return now.strftime("%Y%m%d")


class DataLhbTool(OperationHandler):
    name = "data_lhb"
    description = (
        "Fetch the A-share 龙虎榜 (dragon-tiger board) in one of two modes. "
        "MARKET mode (no symbol): the exchange's daily large-order / "
        "abnormal-move disclosure list for a single day or a date range — a "
        "MARKET-WIDE per-day list, not a per-symbol series. Pass either date "
        "(single day) or start/end (range); both default to today "
        "(Asia/Shanghai). A non-trading window surfaces as lhb_empty. SEAT mode "
        "(symbol given): one name's per-营业部 (trading desk) 买入/卖出 席位明细 "
        "for a single date — each seat tagged with a best-effort 游资名 "
        "(hot_money) and flagged is_institution for 机构专用 desks. Seat mode "
        "needs a single date (a start/end range is rejected as invalid_date); a "
        "name not on the board that day returns the DISTINCT lhb_no_seat_data "
        "(not lhb_fetch_failed). Both modes write a local CSV with the canonical "
        "symbol."
    )
    category = "data"
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": (
                    "Canonical CODE.EXCHANGE (e.g. 600519.SH). When given, "
                    "switches to per-seat / 游资 detail mode for that name on a "
                    "single date (a range is rejected). Omit for the "
                    "market-level daily board."
                ),
            },
            "date": {
                "type": "string",
                "description": (
                    "Single trading day YYYY-MM-DD. Market mode: mutually "
                    "exclusive with start / end. Seat mode: the only date input "
                    "(range not allowed). Default: today (Asia/Shanghai)."
                ),
            },
            "start": {
                "type": "string",
                "description": (
                    "Inclusive range start YYYY-MM-DD (with end). Market mode "
                    "only — rejected in seat mode."
                ),
            },
            "end": {
                "type": "string",
                "description": (
                    "Inclusive range end YYYY-MM-DD (with start). Market mode "
                    "only — rejected in seat mode."
                ),
            },
            "data_source": {
                "type": "string",
                "enum": list(_SUPPORTED_LHB_SOURCES),
                "default": "auto",
                "description": "龙虎榜 provider id (akshare only today).",
            },
        },
        "additionalProperties": False,
    }

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                "operation_data_lhb.rejected",
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
            "operation_data_lhb.request",
            {"tool": self.name, "input_keys": sorted(kwargs.keys())},
        )

        symbol = kwargs.get("symbol")
        if symbol is not None:
            return await self._execute_seat_mode(kwargs)

        try:
            start_date, end_date = _resolve_window(
                kwargs.get("date"), kwargs.get("start"), kwargs.get("end")
            )
            data_source = kwargs.get("data_source") or "auto"
            provider, source_name = _build_dragon_tiger_provider(data_source)
        except _InvalidLhbArgument as exc:
            await emit_debug_event(
                "operation_data_lhb.failed",
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
            "operation_data_lhb.validated",
            {
                "tool": self.name,
                "mode": "market",
                "start_date": start_date,
                "end_date": end_date,
                "data_source": source_name,
            },
        )

        try:
            rows = await provider.fetch_dragon_tiger(start_date, end_date)
        except Exception as exc:
            logger.exception(
                "data_lhb upstream fetch failure [%s..%s] data_source=%s",
                start_date, end_date, source_name,
            )
            await emit_debug_event(
                "operation_data_lhb.failed",
                {
                    "tool": self.name,
                    "start_date": start_date,
                    "end_date": end_date,
                    "error_code": "lhb_fetch_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "lhb_fetch_failed",
                    f"failed to fetch 龙虎榜 for [{start_date}..{end_date}]: {exc}",
                    "check the data_source and network",
                ),
                is_error=True,
            )

        if not rows:
            await emit_debug_event(
                "operation_data_lhb.failed",
                {
                    "tool": self.name,
                    "start_date": start_date,
                    "end_date": end_date,
                    "error_code": "lhb_empty",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "lhb_empty",
                    f"no 龙虎榜 rows for [{start_date}..{end_date}]",
                    "confirm the window contains a trading day and the after-hours snapshot has updated",
                ),
                is_error=True,
            )

        lhb_path = self._persist_rows(start_date, end_date, rows)
        manifest_path = self._write_manifest(
            start_date=start_date,
            end_date=end_date,
            data_source=source_name,
            lhb_path=lhb_path,
            count=len(rows),
        )

        latest = [self._row_dict(r) for r in rows[:_PREVIEW_ROWS]]

        payload: dict[str, Any] = {
            "mode": "market",
            "status": "ok",
            "start_date": start_date,
            "end_date": end_date,
            "data_source": source_name,
            "count": len(rows),
            "lhb_path": lhb_path,
            "manifest_path": manifest_path,
            "latest": latest,
        }

        await emit_debug_event(
            "operation_data_lhb.created",
            {
                "tool": self.name,
                "mode": "market",
                "start_date": start_date,
                "end_date": end_date,
                "status": "ok",
                "count": len(rows),
            },
        )

        header = (
            f"龙虎榜 [{start_date}..{end_date}]: {len(rows)} 条 "
            f"(source={source_name}, status=ok)"
        )
        return ToolResult(text=append_json_payload(header, payload), is_error=False)

    # ------------------------------------------------------------------
    # Seat mode (per-name / 游资 detail)
    # ------------------------------------------------------------------

    async def _execute_seat_mode(self, kwargs: dict[str, Any]) -> ToolResult:
        """Per-name 席位明细 for one symbol on a single day.

        Distinct error_codes, all observable: a symbol not on the board that day
        raises :class:`LhbNoSeatDataError` upstream → ``lhb_no_seat_data`` (NOT
        the transport-failure ``lhb_fetch_failed``); a range in seat mode →
        ``invalid_date``; any other upstream exception → ``lhb_fetch_failed``.
        """
        from doyoutrade.data.lhb_akshare import LhbNoSeatDataError

        symbol = str(kwargs.get("symbol") or "").strip()
        try:
            if not symbol:
                raise _InvalidLhbArgument(
                    "invalid_symbol",
                    "symbol must be a non-empty CODE.EXCHANGE string",
                    "pass e.g. --symbol 600519.SH",
                )
            date_compact = _resolve_seat_date(
                kwargs.get("date"), kwargs.get("start"), kwargs.get("end")
            )
            data_source = kwargs.get("data_source") or "auto"
            provider, source_name = _build_dragon_tiger_provider(data_source)
        except _InvalidLhbArgument as exc:
            await emit_debug_event(
                "operation_data_lhb.failed",
                {
                    "tool": self.name,
                    "mode": "seats",
                    "symbol": symbol,
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
            "operation_data_lhb.validated",
            {
                "tool": self.name,
                "mode": "seats",
                "symbol": symbol,
                "date": date_compact,
                "data_source": source_name,
            },
        )

        try:
            seats = await provider.fetch_seat_detail(symbol, date_compact)
        except LhbNoSeatDataError as exc:
            # Distinct from a transport failure: the name simply wasn't on the
            # board that day. Report it as its own error_code so the model can
            # tell "confirm the name 上榜" apart from "akshare is down".
            logger.info(
                "data_lhb seat mode: %s not on board date=%s (no seat data): %s",
                symbol, date_compact, exc,
            )
            await emit_debug_event(
                "operation_data_lhb.failed",
                {
                    "tool": self.name,
                    "mode": "seats",
                    "symbol": symbol,
                    "date": date_compact,
                    "error_code": "lhb_no_seat_data",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "lhb_no_seat_data",
                    f"{symbol} has no 龙虎榜席位 for date={date_compact}: {exc}",
                    "confirm the name actually 上榜 (made the board) that day",
                ),
                is_error=True,
            )
        except Exception as exc:
            logger.exception(
                "data_lhb seat mode upstream fetch failure symbol=%s date=%s "
                "data_source=%s",
                symbol, date_compact, source_name,
            )
            await emit_debug_event(
                "operation_data_lhb.failed",
                {
                    "tool": self.name,
                    "mode": "seats",
                    "symbol": symbol,
                    "date": date_compact,
                    "error_code": "lhb_fetch_failed",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            return ToolResult(
                text=format_error_text(
                    "lhb_fetch_failed",
                    f"failed to fetch 龙虎榜席位 for {symbol} date={date_compact}: {exc}",
                    "check the data_source and network",
                ),
                is_error=True,
            )

        # An empty seat list (upstream returned rows but none survived parsing,
        # or both sides came back empty for a name that WAS on the board) is a
        # distinct "no seat data" condition — surface the same error_code.
        if not seats:
            await emit_debug_event(
                "operation_data_lhb.failed",
                {
                    "tool": self.name,
                    "mode": "seats",
                    "symbol": symbol,
                    "date": date_compact,
                    "error_code": "lhb_no_seat_data",
                },
            )
            return ToolResult(
                text=format_error_text(
                    "lhb_no_seat_data",
                    f"{symbol} returned no 龙虎榜席位 rows for date={date_compact}",
                    "confirm the name actually 上榜 (made the board) that day",
                ),
                is_error=True,
            )

        buy_seats = [self._seat_dict(s) for s in seats if s.side == "买入"]
        sell_seats = [self._seat_dict(s) for s in seats if s.side == "卖出"]
        seats_path = self._persist_seats(symbol, date_compact, seats)
        manifest_path = self._write_seat_manifest(
            symbol=symbol,
            date=date_compact,
            data_source=source_name,
            seats_path=seats_path,
            buy_count=len(buy_seats),
            sell_count=len(sell_seats),
        )

        payload: dict[str, Any] = {
            "mode": "seats",
            "status": "ok",
            "symbol": symbol,
            "date": date_compact,
            "data_source": source_name,
            "buy_count": len(buy_seats),
            "sell_count": len(sell_seats),
            "buy_seats": buy_seats,
            "sell_seats": sell_seats,
            "seats_path": seats_path,
            "manifest_path": manifest_path,
        }

        await emit_debug_event(
            "operation_data_lhb.created",
            {
                "tool": self.name,
                "mode": "seats",
                "symbol": symbol,
                "date": date_compact,
                "status": "ok",
                "buy_count": len(buy_seats),
                "sell_count": len(sell_seats),
            },
        )

        header = (
            f"龙虎榜席位 {symbol} [{date_compact}]: "
            f"买入 {len(buy_seats)} 席 / 卖出 {len(sell_seats)} 席 "
            f"(source={source_name}, status=ok)"
        )
        return ToolResult(text=append_json_payload(header, payload), is_error=False)

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_dict(row: Any) -> dict[str, Any]:
        return {
            "symbol": row.symbol,
            "code": row.code,
            "name": row.name,
            "on_date": row.on_date,
            "reason": row.reason,
            "interpretation": row.interpretation,
            "change_pct": row.change_pct,
            "close_price": row.close_price,
            "net_buy_amount": row.net_buy_amount,
            "buy_amount": row.buy_amount,
            "sell_amount": row.sell_amount,
            "turnover_rate": row.turnover_rate,
            "circulating_mv": row.circulating_mv,
        }

    def _persist_rows(self, start_date: str, end_date: str, rows: list[Any]) -> str:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        records = [self._row_dict(r) for r in rows]
        # Keep a rectangular CSV with the documented column order even when
        # empty (never reached here — empty is a distinct error_code — but the
        # explicit column list guards against a phantom-column drift).
        df = pd.DataFrame(records, columns=list(_LHB_CSV_COLUMNS))
        path = root / f"lhb_{_safe_code(start_date)}_{_safe_code(end_date)}.csv"
        df.to_csv(path, index=False)
        return str(path)

    def _write_manifest(
        self,
        *,
        start_date: str,
        end_date: str,
        data_source: str,
        lhb_path: str,
        count: int,
    ) -> str:
        import json

        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "kind": "data_lhb",
            "start_date": start_date,
            "end_date": end_date,
            "data_source": data_source,
            "lhb_path": lhb_path,
            "count": count,
        }
        manifest_path = (
            root
            / f"data_lhb_manifest_{_safe_code(start_date)}_{_safe_code(end_date)}.json"
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(manifest_path)

    # ------------------------------------------------------------------
    # Seat-mode filesystem helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _seat_dict(seat: Any) -> dict[str, Any]:
        return {
            "symbol": seat.symbol,
            "date": seat.date,
            "side": seat.side,
            "seat_name": seat.seat_name,
            "seat_type": seat.seat_type,
            "hot_money": seat.hot_money,
            "is_institution": seat.is_institution,
            "buy_amount": seat.buy_amount,
            "sell_amount": seat.sell_amount,
            "net_amount": seat.net_amount,
            "buy_pct": seat.buy_pct,
            "sell_pct": seat.sell_pct,
            "provider": seat.provider,
        }

    def _persist_seats(self, symbol: str, date: str, seats: list[Any]) -> str:
        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        records = [self._seat_dict(s) for s in seats]
        # Explicit column order guards against phantom-column drift.
        df = pd.DataFrame(records, columns=list(_LHB_SEAT_CSV_COLUMNS))
        path = root / f"lhb_seats_{_safe_code(symbol)}_{_safe_code(date)}.csv"
        df.to_csv(path, index=False)
        return str(path)

    def _write_seat_manifest(
        self,
        *,
        symbol: str,
        date: str,
        data_source: str,
        seats_path: str,
        buy_count: int,
        sell_count: int,
    ) -> str:
        import json

        root = _get_artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "kind": "data_lhb_seats",
            "symbol": symbol,
            "date": date,
            "data_source": data_source,
            "seats_path": seats_path,
            "buy_count": buy_count,
            "sell_count": sell_count,
        }
        manifest_path = (
            root
            / f"data_lhb_seats_manifest_{_safe_code(symbol)}_{_safe_code(date)}.json"
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(manifest_path)


__all__ = ["DataLhbTool"]
