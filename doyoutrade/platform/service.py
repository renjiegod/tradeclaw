from __future__ import annotations

import asyncio
import inspect
import logging
import traceback
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from contextlib import nullcontext, suppress
from dataclasses import asdict, dataclass, is_dataclass, replace as dataclasses_replace
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

from doyoutrade import engine_version as _engine_version
from doyoutrade.core.cycle_persist_context import (
    current_tick_run_kind,
    current_tick_session_id,
    current_trigger_id,
)
from doyoutrade.diagnostics import runtime_diag
from doyoutrade.debug import debug_session_scope, emit_debug_event
from doyoutrade.debug.context import debug_observability_enabled
from doyoutrade.debug.overrides import OverriddenUniverseProvider, PatchedDataProvider
from doyoutrade.config import AppConfig, DataSettings, ModelSettings, default_model_route_baseline
from doyoutrade.models.factory import build_model_adapter, wrap_with_recording
from doyoutrade.models.invocation_context import model_invocation_scope
from doyoutrade.models.route_resolution import resolve_model_settings
from doyoutrade.data.bars_cache_store import RepositoryBarsCacheStore
from doyoutrade.data.cached_bars import (
    BACKTEST_BARS_CACHE_EXPANSION_DAYS,
    build_backtest_cached_data_provider,
    install_cached_data_provider,
)
from doyoutrade.data.adjust_drift import (
    ANCHOR_OVERLAP_CALENDAR_DAYS,
    AdjustDriftReport,
    detect_adjust_drift,
)
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.local_market_bars import SUPPORTED_LOCAL_INTERVALS, _bar_dict, _query_bound
from doyoutrade.data.account_resolution import (
    ResolvedAccount,
    resolved_account_from_record,
)
from doyoutrade.data.factory import PROVIDER_MOCK, build_trading_data_stack, resolve_effective_provider
from doyoutrade.data.instrument_catalog.index_seeds import A_SHARE_INDEX_SEEDS, A_SHARE_INDEX_SYMBOLS
from doyoutrade.data.instrument_catalog.normalize import canonical_symbol_from_qmt_stock_code
from doyoutrade.data.instrument_catalog.validation import ensure_symbols_in_catalog
from doyoutrade.runtime.watchlist_universe import (
    resolve_watchlist_universe,
    split_universe_tokens,
)
from doyoutrade.data.simulated_bar_marks import (
    backtest_mtm_seed_symbol_list,
    mock_trading_store_from_account_reader,
    reset_mock_ledger_for_fresh_backtest,
    seed_mock_ledger_prices_for_cycle_time,
    seed_mock_ledger_prices_for_trading_day,
)
from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.core.models import CycleReport, AccountSnapshot, PositionSnapshot, intent_from_json
from doyoutrade.backtest import summary as backtest_summary
from doyoutrade.money.decimal_helpers import decimal_to_json_str
from doyoutrade.persistence.errors import RecordNotFoundError
from doyoutrade.persistence.repositories import TaskSnapshot, create_model_invocation_recorder
from doyoutrade.observability.debug_span_export import (
    debug_span_export_for_session,
    drain_debug_span_persist_queue,
)
from doyoutrade.observability.tracing import get_tracer
from opentelemetry import trace as trace_api
from opentelemetry.trace import Status, StatusCode
from doyoutrade.persistence.tick_session import TickSessionRepository
from doyoutrade.platform.backtest_config_merge import (
    build_cycle_task_config_with_backtest_overrides,
    normalize_backtest_config_overrides,
)
from doyoutrade.platform.local_market_bars_workspace import (
    build_local_market_summary,
    build_requested_window_coverage,
    build_sync_fetch_segments,
    choose_sync_execution_mode,
    empty_overlay_snapshot,
    LocalMarketSyncJob,
    normalize_overlay_item,
)
from doyoutrade.runtime.cycle_task import (
    CycleTask,
    CycleTaskConfig,
    cycle_task_config_from_params,
)
from doyoutrade.runtime.cycle_task import merge_task_settings
from doyoutrade.runtime.triggers import run_mode_for_intent

_INDEX_SEED_NAME_BY_SYMBOL: dict[str, str] = dict(A_SHARE_INDEX_SEEDS)


class AccountResolutionError(RuntimeError):
    """Raised when a live cycle cannot resolve a usable account. Carries a
    structured ``reason`` / ``account_id`` so callers branch on the field, not
    the message (CLAUDE.md §错误可见性)."""

    def __init__(self, message: str, *, reason: str, account_id: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.account_id = account_id


@dataclass(frozen=True)
class AgentTemplate:
    name: str
    default_mode: str
    default_orchestrator_mode: str


tracer = get_tracer(__name__)


_TRACEBACK_TAIL_MAX_CHARS = 400


def _deep_merge_settings(base: dict, patch: dict) -> dict:
    """Deep-merge ``patch`` into ``base`` for task ``settings`` payloads.

    - dict-valued keys are merged recursively
    - list / scalar / tuple values from ``patch`` replace the corresponding key
    - keys present in ``base`` but absent from ``patch`` are preserved

    Returns a new dict and never mutates the inputs.
    """

    out: dict[str, Any] = dict(base) if isinstance(base, dict) else {}
    if not isinstance(patch, dict):
        return out
    for key, new_value in patch.items():
        old_value = out.get(key)
        if isinstance(new_value, dict) and isinstance(old_value, dict):
            out[key] = _deep_merge_settings(old_value, new_value)
        else:
            out[key] = new_value
    return out


def _normalize_task_identifier(identifier: str | CycleTask | TaskSnapshot | Any) -> str:
    if isinstance(identifier, str):
        return identifier
    task_id = getattr(identifier, "task_id", None)
    if isinstance(task_id, str) and task_id.strip():
        return task_id.strip()
    raise TypeError(f"task identifier must be str-like or expose .task_id, got {type(identifier).__name__}")


def _format_failure_message(exc: BaseException) -> tuple[str, str, str]:
    """Return (error_message, error_type, traceback_tail) that are always non-empty.

    Empty exception messages (``RuntimeError()``, ``CancelledError`` etc.) used to
    flow through ``str(exc)`` and surface as ``error_message=""`` on
    ``debug_sessions``. We always fall back to the exception type name so the
    failure path stays debuggable end-to-end.
    """

    raw = str(exc).strip()
    error_type = type(exc).__name__
    message = raw if raw else f"{error_type} (no message)"
    tb = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    if not tb:
        tb = error_type
    if len(tb) > _TRACEBACK_TAIL_MAX_CHARS:
        tb = tb[-_TRACEBACK_TAIL_MAX_CHARS:]
    return message, error_type, tb


DEFAULT_TEMPLATES: Dict[str, AgentTemplate] = {
    "single-agent-trend": AgentTemplate(
        name="Single Agent / Trend Following",
        default_mode="paper",
        default_orchestrator_mode="single-agent",
    ),
    "single-agent-event": AgentTemplate(
        name="Single Agent / Event Driven",
        default_mode="paper",
        default_orchestrator_mode="single-agent",
    ),
    "multi-role-rtr": AgentTemplate(
        name="Multi Role / Research + Trader + Risk",
        default_mode="paper",
        default_orchestrator_mode="multi-role",
    ),
}

BACKTEST_MAX_TRADING_DAYS = 2_147_483_647
BACKTEST_CHART_CYCLE_LIMIT = 450
BACKTEST_CHART_INDICATOR_WARMUP_TRADING_DAYS = 250
KLINE_DISPLAY_ADJUST = DEFAULT_BAR_ADJUST
SUPPORTED_LOCAL_SYNC_MODES = frozenset({"fill_gap", "force_refresh"})
# How long a GET /market/bars read-through backfill blocks on the inline
# upstream fetch before escalating to a background job. The read never 500s on
# timeout — it degrades to returning the (still-empty) local view plus a hint,
# and the escalated job finishes the fetch out of band.
_READ_THROUGH_INLINE_TIMEOUT_SECONDS = 25.0
_DEPRECATED_TASK_SETTINGS_FIELDS = (
    "template_id",
    "signal_mode",
    "execution_strategy",
    "enabled_skills",
    "account_id",
    "model_id",
)
_INTRADAY_BACKTEST_INTERVALS = frozenset({"1m", "5m", "15m", "30m", "60m"})


def _hydrate_backtest_ledger_from_checkpoint(worker, checkpoint: dict | None) -> None:
    if not checkpoint:
        return
    store = mock_trading_store_from_account_reader(worker.account_reader)
    if store is not None:
        store.restore_ledger_checkpoint(checkpoint)


def _backtest_calendar_range_from_row(row: dict) -> tuple[date, date]:
    rs = str(row.get("range_start_utc") or "")
    re = str(row.get("range_end_utc") or "")
    d0 = date.fromisoformat(rs[:10])
    d1 = date.fromisoformat(re[:10])
    return d0, d1


def _parse_backtest_calendar_date(value: object, *, field_name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a YYYY-MM-DD string")
    raw = value.strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc


def _is_intraday_backtest_interval(interval: str | None) -> bool:
    return str(interval or "").strip().lower() in _INTRADAY_BACKTEST_INTERVALS


def _daily_backtest_cycle_time(trading_day: date) -> datetime:
    # UTC+8 15:00 (A股收盘) = UTC 07:00 on same day
    return datetime.combine(trading_day, time(7, 0, 0))


def _backtest_cycle_time_to_input_override(
    cycle_time: datetime,
    *,
    intraday: bool,
) -> str:
    if intraday:
        return cycle_time.replace(microsecond=0).isoformat()
    return cycle_time.isoformat() + "Z"


def _backtest_cycle_time_from_bar_timestamp(raw: object) -> datetime | None:
    normalized = normalize_bar_timestamp(raw)
    if not normalized:
        return None
    try:
        if len(normalized) == 10:
            return datetime.combine(date.fromisoformat(normalized), time(0, 0, 0))
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


async def _backtest_timeline_symbols(worker: Any, config: CycleTaskConfig) -> list[str]:
    ordered: list[str] = list(getattr(config, "universe", ()) or ())
    positions = await worker.account_reader.get_positions()
    for position in positions:
        ordered.append(position.symbol)
    return list(dict.fromkeys(str(symbol).strip() for symbol in ordered if str(symbol).strip()))


async def _build_intraday_backtest_cycle_times(
    *,
    data_provider: Any,
    symbols: list[str],
    range_start: date,
    range_end: date,
    interval: str,
    trading_dates: list[str],
) -> list[datetime]:
    expected_days = set(trading_dates)
    seen_days: set[str] = set()
    timeline: dict[datetime, None] = {}
    if not expected_days or not symbols:
        return []
    start_text = range_start.isoformat()
    end_text = datetime.combine(range_end, time(23, 59, 59)).isoformat()
    for symbol in symbols:
        bars = await data_provider.get_bars(
            symbol,
            start_text,
            end_text,
            interval=interval,
            adjust=DEFAULT_BAR_ADJUST,
        )
        for bar in bars:
            point = _backtest_cycle_time_from_bar_timestamp(bar.timestamp)
            if point is None:
                continue
            day_key = point.date().isoformat()
            if day_key not in expected_days:
                continue
            timeline[point] = None
            seen_days.add(day_key)
        if seen_days >= expected_days:
            break
    return sorted(timeline.keys())


async def _build_backtest_cycle_times(
    *,
    worker: Any,
    config: CycleTaskConfig,
    range_start: date,
    range_end: date,
    interval: str,
    trading_dates: list[str],
) -> list[datetime]:
    interval_key = str(interval or "1d").strip().lower() or "1d"
    if not _is_intraday_backtest_interval(interval_key):
        return [_daily_backtest_cycle_time(date.fromisoformat(item)) for item in trading_dates]
    symbols = await _backtest_timeline_symbols(worker, config)
    return await _build_intraday_backtest_cycle_times(
        data_provider=worker.data_provider,
        symbols=symbols,
        range_start=range_start,
        range_end=range_end,
        interval=interval_key,
        trading_dates=trading_dates,
    )


async def _chart_bars_start_with_warmup(
    *,
    data_provider: Any,
    range_start: date,
    warmup_trading_days: int = BACKTEST_CHART_INDICATOR_WARMUP_TRADING_DAYS,
    startup_history: int | None = None,
) -> str:
    """Resolve chart bar start day by prepending enough warmup trading days.

    MACD/EMA family indicators depend on historical state. For stable chart values in the
    selected backtest range, pull additional history before ``range_start``.

    When ``startup_history`` is provided (per-strategy
    :attr:`doyoutrade.strategy_sdk.Strategy.startup_history`), the effective
    warmup is ``max(warmup_trading_days, ceil(startup_history * 1.5) + 10)``
    so the chart at least covers what the backtest engine itself consumed.
    The 1.5 factor + 10-day pad keeps the chart noticeably wider than the
    raw ``startup_history`` so indicators built on top of the strategy's
    base history (e.g. an MACD pre-warmup) still settle on the displayed
    window. The 250-day floor remains the default chart UX, independent
    of strategy.
    """
    effective_warmup = warmup_trading_days
    if startup_history is not None and startup_history > 0:
        scaled = int(startup_history * 1.5)
        if scaled < startup_history * 1.5:
            scaled += 1
        effective_warmup = max(warmup_trading_days, scaled + 10)
    if effective_warmup <= 0:
        return range_start.isoformat()
    # Approximate calendar expansion (3x) to cover weekends/holidays, then snap to trading dates.
    calendar_start = (range_start - timedelta(days=effective_warmup * 3)).isoformat()
    trading_dates = await data_provider.get_trading_dates(
        calendar_start,
        range_start.isoformat(),
    )
    if not trading_dates:
        return range_start.isoformat()
    return trading_dates[max(0, len(trading_dates) - effective_warmup)]


def _chart_symbols_from_trade_fills(fill_rows: list[dict[str, Any]], fallback: list[str]) -> list[str]:
    symbols: list[str] = []
    for row in fill_rows:
        symbol = str(row.get("symbol") or "").strip()
        if symbol:
            symbols.append(symbol)
    symbols.extend(sym for sym in fallback if sym)
    return list(dict.fromkeys(symbols))


def _backtest_fill_record_from_details(payload: dict, *, run_id_fallback: str) -> backtest_summary.FillRecord | None:
    """Coerce a ``cycle_runs.details.fills`` row into a typed ``FillRecord``.

    Returns ``None`` for malformed rows (missing side, zero quantity, unparseable
    timestamp). The summary path tolerates skipped fills without altering FIFO
    semantics.
    """

    if not isinstance(payload, dict):
        return None
    side = str(payload.get("side") or "").strip().lower()
    if side not in ("buy", "sell"):
        return None
    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        return None
    raw_qty = payload.get("quantity")
    try:
        qty = int(round(float(raw_qty)))
    except (TypeError, ValueError) as exc:
        logger.error("Failed to parse quantity %r for symbol %s: %s", raw_qty, symbol, exc)
        return None
    if qty <= 0:
        return None
    raw_price = payload.get("price")
    try:
        price = Decimal(str(raw_price))
    except Exception as exc:
        logger.error("Failed to parse price %r for symbol %s: %s", raw_price, symbol, exc)
        return None
    raw_ts = payload.get("timestamp")
    ts: datetime | None = None
    if isinstance(raw_ts, datetime):
        ts = raw_ts
    elif isinstance(raw_ts, str) and raw_ts.strip():
        s = raw_ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(s)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        ts = parsed
    if ts is None:
        return None
    intent_id_raw = payload.get("intent_id")
    intent_id = str(intent_id_raw) if intent_id_raw not in (None, "") else None
    cycle_run_id = str(payload.get("cycle_run_id") or run_id_fallback or "")
    # Fee is absent / null on fee-free runs (the historic default) → 0. A
    # malformed fee on an otherwise-valid fill is logged and treated as 0
    # rather than dropping the fill (FIFO/quantity stay correct; only the
    # cost attribution is lost — surfaced, not silent).
    raw_fee = payload.get("fee")
    fee = Decimal("0")
    if raw_fee not in (None, ""):
        try:
            fee = Decimal(str(raw_fee))
        except Exception as exc:
            logger.warning("Failed to parse fee %r for symbol %s: %s; treating as 0", raw_fee, symbol, exc)
    # Exit categorization rides in the fill payload (worker dispatch stamps
    # OrderIntent.exit_reason onto SELL fills). Absent/empty → None, which the
    # summary's by_exit_reason block ignores. No coercion: an unrecognized
    # string is passed through verbatim (the SDK already validated it at
    # Signal.sell construction time).
    raw_exit_reason = payload.get("exit_reason")
    exit_reason = (
        str(raw_exit_reason).strip()
        if isinstance(raw_exit_reason, str) and raw_exit_reason.strip()
        else None
    )

    # Factor tags ride in the fill payload too (worker dispatch routes
    # OrderIntent.signal_tag → entry_tag on buys / exit_tag on sells). These
    # were previously dropped here, so by_tag attribution saw nothing. Lift
    # them through; absent/empty → None.
    def _clean_tag(value: Any) -> str | None:
        return str(value).strip() if isinstance(value, str) and value.strip() else None

    entry_tag = _clean_tag(payload.get("entry_tag"))
    exit_tag = _clean_tag(payload.get("exit_tag"))
    return backtest_summary.FillRecord(
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        quantity=qty,
        price=price,
        timestamp=ts,
        intent_id=intent_id,
        cycle_run_id=cycle_run_id,
        fee=fee,
        exit_reason=exit_reason,
        entry_tag=entry_tag,
        exit_tag=exit_tag,
    )


def _backtest_symbol_to_price_from_worker(worker: Any) -> dict[str, Any] | None:
    """Return the latest mock-store price map for backtest finalize fallback.

    Returns ``None`` outside backtests driven by ``MockTradingDataProvider``.
    Used by :func:`_backtest_final_positions` because ``StoreBackedAccountReader``
    does not MTM ``PositionSnapshot.market_price`` at end-of-run; without this
    map, ``last_price`` / ``market_value`` / ``weight_pct`` on the open-position
    section of ``backtest_summary`` are dropped to ``None``.
    """
    store = mock_trading_store_from_account_reader(worker.account_reader)
    if store is None:
        return None
    try:
        ck = store.ledger_checkpoint()
    except Exception:
        return None
    raw = ck.get("symbol_to_price") if isinstance(ck, dict) else None
    return raw if isinstance(raw, dict) else None


def _backtest_final_positions(
    positions: list[PositionSnapshot],
    *,
    symbol_to_price: dict[str, Any] | None = None,
) -> list[backtest_summary.FinalPosition]:
    out: list[backtest_summary.FinalPosition] = []
    for p in positions:
        try:
            qty_int = int(round(float(p.quantity)))
        except (TypeError, ValueError):
            continue
        if qty_int == 0:
            continue
        avail: int | None = None
        if p.available is not None:
            try:
                avail = int(round(float(p.available)))
            except (TypeError, ValueError):
                avail = None
        last_price: Decimal | None = None
        if p.market_price is not None:
            try:
                last_price = Decimal(str(p.market_price))
            except Exception:
                last_price = None
        if last_price is None and symbol_to_price:
            raw_px = symbol_to_price.get(p.symbol)
            if raw_px is not None:
                try:
                    last_price = Decimal(str(raw_px))
                except Exception:
                    last_price = None
        market_value: Decimal | None = None
        if p.market_value is not None:
            try:
                market_value = Decimal(str(p.market_value))
            except Exception:
                market_value = None
        if market_value is None and last_price is not None:
            try:
                market_value = Decimal(qty_int) * last_price
            except Exception:
                market_value = None
        out.append(
            backtest_summary.FinalPosition(
                symbol=p.symbol,
                name=p.name,
                quantity=qty_int,
                available=avail,
                cost_price=Decimal(p.cost_price),
                last_price=last_price,
                market_value=market_value,
            )
        )
    return out


def _chart_trades_from_trade_fills(
    fill_rows: list[dict[str, Any]], selected_symbol: str
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    for row in fill_rows:
        if str(row.get("symbol") or "") != selected_symbol:
            continue
        price_raw = row.get("price")
        qty_raw = row.get("quantity")
        try:
            price = float(price_raw) if price_raw is not None else None
        except (TypeError, ValueError):
            price = None
        try:
            quantity = float(qty_raw) if qty_raw is not None else None
        except (TypeError, ValueError):
            quantity = None
        trades.append(
            {
                "timestamp": row.get("filled_at"),
                "side": row.get("side"),
                "price": price,
                "quantity": quantity,
                "intent_id": row.get("intent_id"),
                "rationale": row.get("rationale"),
                "cycle_run_id": row.get("cycle_run_id"),
                # Factor identifier copied from Signal.tag → OrderIntent.signal_tag
                # → TradeFillRecord.entry_tag / exit_tag; surfaces in the
                # frontend chart so per-factor attribution is visible.
                "entry_tag": row.get("entry_tag"),
                "exit_tag": row.get("exit_tag"),
                # Exit categorization (Signal.exit_reason); null on buys /
                # uncategorized exits.
                "exit_reason": row.get("exit_reason"),
            }
        )
    return sorted(trades, key=lambda item: str(item.get("timestamp") or ""))


def _serialize_config(config: CycleTaskConfig) -> dict:
    # Build nested agent block from config fields.
    agent_block: dict[str, Any] = {
        "react_max_turns": config.react_max_turns,
        "signal_tool_names": list(config.signal_tool_names),
        "enabled_skills": list(config.enabled_skills),
        "position_constraints": {
            "max_single_order_amount": config.max_single_order_amount,
            "max_position_ratio": config.max_position_ratio,
            "review_equity_fraction": config.review_equity_fraction,
            "lot_size": config.lot_size,
            "rebalance_hysteresis_lots": config.rebalance_hysteresis_lots,
            "max_task_position_amount": config.max_task_position_amount,
            "max_task_position_ratio": config.max_task_position_ratio,
        },
        "approval": {
            "min_notional_for_approval": config.min_notional_for_approval,
            "timeout_seconds": config.approval_timeout_seconds,
        },
    }

    strategy_block: dict[str, Any] = {}
    if config.strategy_definition_id:
        strategy_block = {
            "definition_id": config.strategy_definition_id,
            "parameter_overrides": dict(config.strategy_parameter_overrides),
            "execution_profile": config.strategy_execution_profile,
        }

    result: dict[str, Any] = {
        "name": config.name,
        "mode": config.mode,
        "description": config.description,
        "data_provider": config.data_provider,
        "universe": list(config.universe),
        "strategy_preferences": config.strategy_preferences,
    }
    if config.model_route_name:
        result["model_route_name"] = config.model_route_name
    if strategy_block:
        result["strategy"] = strategy_block
    result["agent"] = agent_block
    if config.account_id:
        result["account_id"] = config.account_id
    if config.data_cache is not None:
        # Echo the per-task data-cache policy in the nested input shape so the
        # frontend round-trips it (load → edit → save) without dropping it.
        result["data_cache"] = config.data_cache.to_settings_block()
    return result


def _serialize_value(value):
    if isinstance(value, Decimal):
        return decimal_to_json_str(value)
    if is_dataclass(value):
        return _serialize_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_serialize_value(item) for item in value)
    return value


def _serialize_session(snapshot) -> dict:
    effective_config = snapshot.effective_config
    if isinstance(effective_config, dict):
        cleaned_effective = dict(effective_config)
        for field in _DEPRECATED_TASK_SETTINGS_FIELDS:
            cleaned_effective.pop(field, None)
        effective_config = cleaned_effective
    return {
        "session_id": snapshot.session_id,
        "task_id": snapshot.task_id,
        "status": snapshot.status,
        "run_id": snapshot.run_id,
        "error_message": snapshot.error_message or "",
        "error_type": getattr(snapshot, "error_type", None),
        "traceback_tail": getattr(snapshot, "traceback_tail", None),
        "input_overrides": snapshot.input_overrides,
        "effective_config": effective_config,
        "session_type": snapshot.session_type,
        "created_at": snapshot.created_at.isoformat(),
        "started_at": snapshot.started_at.isoformat() if snapshot.started_at is not None else None,
        "finished_at": snapshot.finished_at.isoformat() if snapshot.finished_at is not None else None,
    }


def _drop_deprecated_task_read_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return dict(payload)


def _usable_trace_id(raw: str | None) -> str | None:
    """Return normalized 32-hex trace id, or None if missing / OTel-invalid."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text or text == "-":
        return None
    if len(text) != 32:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return text


def _serialize_span(snapshot) -> dict:
    return {
        "span_id": snapshot.span_id,
        "trace_id": snapshot.trace_id,
        "parent_span_id": snapshot.parent_span_id,
        "session_id": snapshot.session_id,
        "name": snapshot.name,
        "span_type": snapshot.span_type,
        "start_time": snapshot.start_time.isoformat() if snapshot.start_time else None,
        "end_time": snapshot.end_time.isoformat() if snapshot.end_time else None,
        "duration_ms": snapshot.duration_ms,
        "attributes": snapshot.attributes,
        "status": snapshot.status,
        "span_source": snapshot.span_source,
    }


def _extract_signal_timeline(
    spans: list[dict],
    cycle_runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate per-cycle ``strategy_runner_cycle`` events into a timeline.

    Each strategy cycle emits a ``strategy_runner_cycle`` span event with
    counts (``signals_buy`` / ``signals_sell`` / ``signals_hold`` /
    ``signals_target_exposure`` / ``signals_target_quantity``) and a
    ``per_symbol_tags`` map. The export pipeline persists these onto the
    span row under ``attributes._events`` (see
    :mod:`doyoutrade.observability.debug_span_export`). This helper walks
    every span in the supplied set, picks the matching event payloads,
    and correlates each to its owning ``cycle_runs`` row via ``trace_id``
    so the timeline can also surface ``run_id`` + ``cycle_time``.

    Returns an empty list when no ``strategy_runner_cycle`` events are
    found — keeping the absence visible (vs. omitting the key) so the
    frontend can distinguish "strategy never emitted signals" from "old
    debug payload without the field". Operators tracing a zero-trade run
    against request1.json should be able to see one entry per cycle with
    ``signals_buy=0`` and a ``per_symbol_tags`` value of e.g.
    ``{"600522.SH": "macd_dead_cross_no_pos"}`` — no longer needing to
    reimplement MACD in raw ``ewm`` locally to confirm "no crosses".
    """

    if not spans:
        return []

    # ``trace_id`` (lowercase 32-char hex) is the join key. ``cycle_runs``
    # rows may store the same trace_id as bytes or upper/lowercase hex
    # depending on the producer; normalise both sides to lowercase string.
    cycle_index: dict[str, dict[str, Any]] = {}
    for row in cycle_runs or []:
        raw_trace = row.get("trace_id")
        if not raw_trace:
            continue
        cycle_index[str(raw_trace).lower()] = row

    timeline: list[dict[str, Any]] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        attrs = span.get("attributes")
        if not isinstance(attrs, dict):
            continue
        events = attrs.get("_events")
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("event_type") != "strategy_runner_cycle":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            trace_id_raw = span.get("trace_id")
            trace_id = str(trace_id_raw).lower() if trace_id_raw else None
            cycle_row = cycle_index.get(trace_id) if trace_id else None
            entry: dict[str, Any] = {
                # ``span.start_time`` is ISO-8601 from ``_serialize_span``;
                # use it as the timeline-ordering key — robust against
                # cycles that didn't make it into ``cycle_runs`` (e.g.
                # partial backtest with the row not yet persisted).
                "span_id": span.get("span_id"),
                "trace_id": trace_id,
                "span_start_time": span.get("start_time"),
                "signals_buy": payload.get("signals_buy"),
                "signals_sell": payload.get("signals_sell"),
                "signals_hold": payload.get("signals_hold"),
                "signals_target_exposure": payload.get("signals_target_exposure"),
                "signals_target_quantity": payload.get("signals_target_quantity"),
                "per_symbol_tags": payload.get("per_symbol_tags") or {},
                "universe_size": payload.get("universe_size"),
                "strategy_name": payload.get("strategy_name"),
                "strategy_class": payload.get("strategy_class"),
            }
            if cycle_row is not None:
                entry["run_id"] = cycle_row.get("run_id")
                entry["cycle_time"] = cycle_row.get("cycle_time")
                entry["cycle_time_utc"] = cycle_row.get("cycle_time_utc")
            else:
                entry["run_id"] = None
                entry["cycle_time"] = None
                entry["cycle_time_utc"] = None
            timeline.append(entry)

    # Sort by cycle_time when available, falling back to span_start_time.
    # ``None`` cycles end up at the tail (stable on span_start_time).
    def _sort_key(entry: dict[str, Any]) -> tuple[int, str]:
        ct = entry.get("cycle_time") or entry.get("cycle_time_utc")
        if ct:
            return (0, str(ct))
        sst = entry.get("span_start_time")
        if sst:
            return (1, str(sst))
        return (2, "")

    timeline.sort(key=_sort_key)
    return timeline


def _summarize_signal_timeline(
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compact summary of ``signal_timeline`` for the top of debug-view.

    The full ``signal_timeline`` (one entry per cycle, with per-symbol
    tags) lives at the bottom of the debug-view payload and is the FIRST
    casualty when an agent's tool-result is truncated for context (a
    20-bar backtest with rich per-symbol tags is already 100KB+; see
    request1.json turn 2 where the agent had to fall back to reading
    ``~/.doyoutrade/sessions/.../tool-results/*.json`` manually because
    the inline result cut off before ``signal_timeline`` was reached).

    This summary is intentionally tiny (~hundreds of bytes regardless of
    cycle count) so the consuming dict can place it BEFORE ``cycle_runs``
    / ``spans`` / ``signal_timeline`` in the response — Python's
    insertion-order dicts plus stable JSON serialisation guarantee that
    even a 1KB truncation budget surfaces the summary.

    Fields:

    * ``total_cycles`` — how many ``strategy_runner_cycle`` events we saw
    * ``total_signals_buy`` / ``..._sell`` / ``..._hold`` /
      ``..._target_exposure`` / ``..._target_quantity`` — sum across cycles
    * ``top_hold_tags`` / ``top_buy_tags`` / ``top_sell_tags`` — top 5
      tag → count maps; pulls the operator's eye to "what dominated"
      without needing to enumerate per_symbol_tags by hand
    * ``first_cycle_time`` / ``last_cycle_time`` — temporal bounds
    * ``first_buy_cycle_time`` / ``first_sell_cycle_time`` — first
      actionable signal (or ``None`` when zero-trade)
    * ``zero_trade`` — convenience boolean (no buy/sell across the run)

    Returns an empty-but-shaped dict when the timeline is empty so
    callers can rely on the structure being present.
    """

    summary: dict[str, Any] = {
        "total_cycles": 0,
        "total_signals_buy": 0,
        "total_signals_sell": 0,
        "total_signals_hold": 0,
        "total_signals_target_exposure": 0,
        "total_signals_target_quantity": 0,
        "top_hold_tags": {},
        "top_buy_tags": {},
        "top_sell_tags": {},
        "top_target_exposure_tags": {},
        "top_target_quantity_tags": {},
        "first_cycle_time": None,
        "last_cycle_time": None,
        "first_buy_cycle_time": None,
        "first_sell_cycle_time": None,
        "first_target_exposure_cycle_time": None,
        "first_target_quantity_cycle_time": None,
        "zero_trade": True,
    }
    if not timeline:
        return summary

    from collections import Counter

    buy_counter: Counter[str] = Counter()
    sell_counter: Counter[str] = Counter()
    hold_counter: Counter[str] = Counter()
    target_exposure_counter: Counter[str] = Counter()
    target_quantity_counter: Counter[str] = Counter()
    total_buy = 0
    total_sell = 0
    total_hold = 0
    total_target_exposure = 0
    total_target_quantity = 0

    for entry in timeline:
        buy = int(entry.get("signals_buy") or 0)
        sell = int(entry.get("signals_sell") or 0)
        hold = int(entry.get("signals_hold") or 0)
        target_exposure = int(entry.get("signals_target_exposure") or 0)
        target_quantity = int(entry.get("signals_target_quantity") or 0)
        total_buy += buy
        total_sell += sell
        total_hold += hold
        total_target_exposure += target_exposure
        total_target_quantity += target_quantity

        tags = entry.get("per_symbol_tags") or {}
        if isinstance(tags, dict):
            # Without a per-symbol direction breakdown we can only attribute
            # tags to buckets by which counter has activity this cycle.
            # When a cycle has e.g. ``signals_buy=1`` and one tag, it's the
            # buy tag; when multiple directions fire, we put each tag into
            # all active counters — better to over-count tag-frequency
            # than to drop it (operators looking for "did warmup ever fire"
            # need to see the answer regardless of the attribution heuristic).
            for tag in tags.values():
                if not tag:
                    continue
                if buy > 0:
                    buy_counter[str(tag)] += 1
                if sell > 0:
                    sell_counter[str(tag)] += 1
                if target_exposure > 0:
                    target_exposure_counter[str(tag)] += 1
                if target_quantity > 0:
                    target_quantity_counter[str(tag)] += 1
                if hold > 0 and buy == 0 and sell == 0:
                    hold_counter[str(tag)] += 1

        cycle_time = entry.get("cycle_time") or entry.get("span_start_time")
        if cycle_time:
            if summary["first_cycle_time"] is None:
                summary["first_cycle_time"] = cycle_time
            summary["last_cycle_time"] = cycle_time
            if buy > 0 and summary["first_buy_cycle_time"] is None:
                summary["first_buy_cycle_time"] = cycle_time
            if sell > 0 and summary["first_sell_cycle_time"] is None:
                summary["first_sell_cycle_time"] = cycle_time
            if (
                target_exposure > 0
                and summary["first_target_exposure_cycle_time"] is None
            ):
                summary["first_target_exposure_cycle_time"] = cycle_time
            if (
                target_quantity > 0
                and summary["first_target_quantity_cycle_time"] is None
            ):
                summary["first_target_quantity_cycle_time"] = cycle_time

    summary["total_cycles"] = len(timeline)
    summary["total_signals_buy"] = total_buy
    summary["total_signals_sell"] = total_sell
    summary["total_signals_hold"] = total_hold
    summary["total_signals_target_exposure"] = total_target_exposure
    summary["total_signals_target_quantity"] = total_target_quantity
    summary["top_hold_tags"] = dict(hold_counter.most_common(5))
    summary["top_buy_tags"] = dict(buy_counter.most_common(5))
    summary["top_sell_tags"] = dict(sell_counter.most_common(5))
    summary["top_target_exposure_tags"] = dict(
        target_exposure_counter.most_common(5)
    )
    summary["top_target_quantity_tags"] = dict(
        target_quantity_counter.most_common(5)
    )
    summary["zero_trade"] = (
        total_buy + total_sell + total_target_exposure + total_target_quantity
    ) == 0
    return summary


class _DebugExecutionAdapter:
    def __init__(self, inner):
        self._inner = inner

    async def submit_intent(self, intent, *, cycle_state=None, market_context=None):
        result = await self._inner.submit_intent(
            intent, cycle_state=cycle_state, market_context=market_context
        )
        await emit_debug_event(
            "execution",
            {
                "intent": _serialize_value(intent),
                "result": _serialize_value(result),
            },
        )
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _parse_cycle_run_wall_time_query(raw: str | None) -> datetime | None:
    """Parse query bounds for ``cycle_runs.wall_started_at`` (stored as naive UTC)."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    normalized = text.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid wall time filter: {raw!r}") from exc
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _parse_cycle_run_wall_time_range(
    after_raw: str | None, before_raw: str | None,
) -> tuple[datetime | None, datetime | None]:
    """Parse both bounds AND reject reversed ranges.

    Without this check, ``started_after > started_before`` produces a
    SQL predicate that can never match — the API silently returns
    ``{"items": [], "total": 0}`` and the operator can't tell whether
    the filter is wrong or the data really is empty. Same shape as the
    cron next-fire-distance guard: a "syntactically valid but
    semantically absurd" input that needs an explicit raise.
    """
    after = _parse_cycle_run_wall_time_query(after_raw)
    before = _parse_cycle_run_wall_time_query(before_raw)
    if after is not None and before is not None and after > before:
        raise ValueError(
            f"started_after ({after_raw!r}) must be <= started_before "
            f"({before_raw!r}); reversed ranges would silently match no "
            "rows. Swap the bounds or drop one of them."
        )
    return after, before


class TradingPlatformService:
    def __init__(
        self,
        scheduler,
        app_cfg: AppConfig,
        worker_factory: Callable[[CycleTaskConfig, ModelSettings, Any], Any],
        task_repository,
        account_repository=None,
        system_state_repository=None,
        debug_session_repository=None,
        debug_session_span_repository=None,
        model_invocation_repository=None,
        cycle_run_repository=None,
        run_repository=None,
        trade_fill_repository=None,
        tick_session_repository: TickSessionRepository | None = None,
        task_trigger_repository=None,
        monitor_rule_repository=None,
        monitor_alert_repository=None,
        templates: Optional[Dict[str, AgentTemplate]] = None,
        default_data_provider: str = "auto",
        instrument_catalog_repository=None,
        app_data_settings: DataSettings | None = None,
        model_route_repository=None,
        strategy_runtime=None,
        cached_bars_repository=None,
        market_bars_repository=None,
        market_sync_service=None,
        watchlist_repository=None,
        decision_signal_repository=None,
    ):
        self.scheduler = scheduler
        self.app_cfg = app_cfg
        self.worker_factory = worker_factory
        self.task_repository = task_repository
        self.account_repository = account_repository
        self.watchlist_repository = watchlist_repository
        self.system_state_repository = system_state_repository
        self.model_route_repository = model_route_repository
        self.instrument_catalog_repository = instrument_catalog_repository
        self._app_data_settings = app_data_settings
        self.strategy_runtime = strategy_runtime
        self.debug_session_repository = debug_session_repository
        self.debug_session_span_repository = debug_session_span_repository
        self.model_invocation_repository = model_invocation_repository
        self.cycle_run_repository = cycle_run_repository
        self.run_repository = run_repository
        self.trade_fill_repository = trade_fill_repository
        self.cached_bars_repository = cached_bars_repository
        self.market_bars_repository = market_bars_repository
        self.market_sync_service = market_sync_service
        self._market_data_sync_runner = market_sync_service
        self.tick_session_repository = tick_session_repository
        self.task_trigger_repository = task_trigger_repository
        self.monitor_rule_repository = monitor_rule_repository
        self.monitor_alert_repository = monitor_alert_repository
        self.decision_signal_repository = decision_signal_repository
        # One-shot "wiring missing" notice so the backtest hook logs the skip
        # exactly once instead of on every finalize (visibility without spam).
        self._decision_signal_skip_logged = False
        self.templates = templates or DEFAULT_TEMPLATES
        self.default_data_provider = (default_data_provider or "auto").strip().lower() or "auto"
        self.tasks: Dict[str, CycleTask] = {}
        self.debug_tasks: Dict[str, asyncio.Task] = {}
        self.backtest_tasks: Dict[str, asyncio.Task] = {}
        self._local_market_sync_jobs: Dict[str, LocalMarketSyncJob] = {}
        self._local_market_sync_tasks: Dict[str, asyncio.Task] = {}
        # Read-through backfill (GET /market/bars self-heal) coordination:
        # a per-(provider, adjust, symbol, interval) lock serializes concurrent
        # first-views of the same empty chart so they don't each fire an upstream
        # fetch, and an in-flight map dedupes background jobs by the same key.
        self._local_market_read_through_locks: Dict[tuple[str, str, str, str], asyncio.Lock] = {}
        self._local_market_read_through_jobs: Dict[tuple[str, str, str, str], str] = {}
        self._closing = False
        self._backtest_pause_pending: Dict[str, bool] = {}
        self._backtest_pause_waiters: Dict[str, asyncio.Event] = {}
        # Serializes wall-clock cycles per task across tick_once (manual/cron) and
        # run_trigger so two Trigger fires (or a manual tick) never run two concurrent
        # cycles on the same worker (required for safe per-fire run_mode override and
        # as the per-task overlap guard).
        self._task_cycle_locks: Dict[str, asyncio.Lock] = {}
        self.kill_switch_enabled = False
        existing_error_handler = getattr(self.scheduler, "on_task_error", None)

        async def persist_task_error(task_id: str, error_message: str):
            instance = self.tasks.get(task_id)
            if instance is not None:
                instance.status = "error"
                instance.last_error = error_message
            await self.task_repository.update_status(task_id, "error", error_message)
            if existing_error_handler is not None:
                await existing_error_handler(task_id, error_message)

        self.scheduler.on_task_error = persist_task_error

    async def _resolve_worker_model_settings(
        self, config: CycleTaskConfig, *, route_name_override: str | None = None
    ) -> ModelSettings:
        effective = (route_name_override or config.model_route_name or "").strip()
        if not effective:
            return default_model_route_baseline()
        if self.model_route_repository is None:
            raise RuntimeError(
                "model route repository is required to resolve model_route_name "
                f"{effective!r} but was not configured"
            )
        return await resolve_model_settings(
            route_name=effective,
            route_repository=self.model_route_repository,
        )

    async def _resolve_worker_account(self, config: CycleTaskConfig) -> ResolvedAccount:
        """Resolve the account a worker cycle runs against.

        Mirrors :meth:`_resolve_worker_model_settings` (pre-resolve, then hand
        the value to the synchronous ``worker_factory``). Resolution order:
        the task's explicit ``account_id`` → otherwise the default account.

        Failure modes are kept visible (CLAUDE.md §错误可见性), never silently
        downgraded to mock:

        * No matching / no default account AND ``mode == "live"`` → raise
          :class:`AccountResolutionError` (emits ``account_resolution_failed``).
        * The bound account exists but is disabled → raise (``account_disabled``).
        * No account for a non-live mode (backtest/paper/signal-data) → return a
          connectionless :class:`ResolvedAccount` so the auto chain falls back
          to baostock/akshare (emits ``qmt_connection_unavailable``).
        """
        account_id = (getattr(config, "account_id", "") or "").strip()
        record: dict | None = None
        if self.account_repository is not None:
            if account_id:
                record = await self.account_repository.get_account(account_id)
            else:
                record = await self.account_repository.get_default_account()

        if record is None:
            reason = "no_account_bound" if not account_id else "account_not_found"
            if account_id and self.account_repository is not None:
                # The id was explicitly given but didn't resolve — distinguish
                # "missing" from "disabled" for the caller.
                raw = await self.account_repository.get_account(account_id)
                if raw is not None and not raw.get("enabled", True):
                    reason = "account_disabled"
            if str(config.mode) == "live":
                await self._emit_account_failure(config, account_id, reason)
                raise AccountResolutionError(
                    f"task {config.name!r} (mode=live) could not resolve an account "
                    f"(account_id={account_id or '(none)'}, reason={reason}); "
                    "bind an enabled account or set a default account.",
                    reason=reason,
                    account_id=account_id,
                )
            # Non-live: market data may still come from baostock/akshare.
            await self._emit_qmt_unavailable(config, account_id, reason)
            return ResolvedAccount(
                account_id="",
                name="",
                mode="mock",
                base_url="",
                token=None,
                timeout_seconds=5.0,
                qmt_account_id=None,
                session_id=None,
                mock_cash=0.0,
                mock_equity=0.0,
            )

        if not record.get("enabled", True):
            await self._emit_account_failure(config, account_id, "account_disabled")
            raise AccountResolutionError(
                f"task {config.name!r} bound account {account_id!r} is disabled",
                reason="account_disabled",
                account_id=account_id,
            )
        return resolved_account_from_record(record)

    async def _build_worker(
        self, config: CycleTaskConfig, model_settings: ModelSettings, resolved_account: ResolvedAccount
    ):
        """Resolve any ``@watchlist:<tag>`` universe tokens to concrete symbols
        (eager, observable — CLAUDE.md §最低同步要求) and hand the resolved config
        to the synchronous ``worker_factory``.

        Mechanism (A) of the watchlist→strategy contract: a task universe may
        reference a watchlist tag; expansion happens here (before the data stack
        is built) so the worker's data provider + ``StaticUniverseProvider`` see
        concrete symbols. Mechanism (B) — ``ctx.dp.watchlist_symbols`` — is wired
        separately via the ``InstanceSignalGenerator`` watchlist snapshot.
        """
        config = await self._resolve_watchlist_universe_config(config)
        return self.worker_factory(config, model_settings, resolved_account)

    async def _resolve_watchlist_universe_config(
        self, config: CycleTaskConfig
    ) -> CycleTaskConfig:
        """Return ``config`` with ``@watchlist:<tag>`` universe tokens expanded.

        No tokens → returned unchanged (no DB read). Tokens present but no
        watchlist repository → hard, visible failure (§错误可见性). Emits a
        ``watchlist_universe_resolved`` debug event + OTel span event so a 0-symbol
        resolution can never silently produce an empty universe.
        """
        universe = list(config.universe)
        _plain, tags = split_universe_tokens(universe)
        if not tags:
            return config
        if self.watchlist_repository is None:
            raise RuntimeError(
                "watchlist universe tokens (@watchlist:<tag>) require a watchlist "
                "repository, but none is configured on this service"
            )
        resolved = await resolve_watchlist_universe(
            universe, self.watchlist_repository, emit=None
        )
        payload = {
            "tags": list(tags),
            "resolved_count": len(resolved),
            "plain_count": len(_plain),
            "source": "watchlist_universe",
            "hint": (
                ""
                if resolved
                else "watchlist tag(s) resolved to 0 symbols; add stocks via "
                "`doyoutrade-cli watchlist add` or widen the tag filter"
            ),
        }
        span = trace_api.get_current_span()
        try:
            span.add_event(
                "watchlist_universe_resolved",
                {
                    "watchlist.tags": ",".join(tags),
                    "watchlist.resolved_count": len(resolved),
                    "watchlist.plain_count": len(_plain),
                },
            )
        except Exception:  # noqa: BLE001 — observability must not break worker build
            pass
        await emit_debug_event("watchlist_universe_resolved", payload)
        logger.info(
            "watchlist universe resolved tags=%s resolved=%d plain=%d",
            ",".join(tags),
            len(resolved),
            len(_plain),
        )
        if list(resolved) != universe:
            config = dataclasses_replace(config, universe=tuple(resolved))
        return config

    # --- Account CRUD (backing the /accounts API + doyoutrade-cli account) ----

    @staticmethod
    def _normalize_account_payload(payload: dict, *, partial: bool) -> dict:
        """Validate + shape an account create/update payload. ``partial`` skips
        required-field checks (update sends only changed fields)."""
        out: dict[str, Any] = {}
        if "mode" in payload or not partial:
            mode = str(payload.get("mode") or "live").strip().lower()
            if mode not in ("live", "mock"):
                raise ValueError(f"mode must be 'live' or 'mock', got {mode!r}")
            out["mode"] = mode
        if "name" in payload or not partial:
            name = str(payload.get("name") or "").strip()
            if not name:
                raise ValueError("name is required")
            out["name"] = name
        for key in ("base_url", "token", "qmt_account_id", "qmt_terminal_id", "session_id"):
            if key in payload:
                val = payload.get(key)
                out[key] = None if val is None else str(val)
        if "base_url" in out and out["base_url"] is None:
            out["base_url"] = ""
        for key in ("timeout_seconds", "mock_cash", "mock_equity"):
            if key in payload and payload.get(key) is not None:
                out[key] = float(payload[key])
        if "mock_positions" in payload:
            mp = payload.get("mock_positions")
            if mp is not None and not isinstance(mp, list):
                raise ValueError("mock_positions must be a list of {symbol,quantity,cost_price}")
            out["mock_positions"] = mp or []
        for key in ("is_default", "enabled"):
            if key in payload and payload.get(key) is not None:
                out[key] = bool(payload[key])
        return out

    async def list_accounts(self) -> list[dict]:
        if self.account_repository is None:
            return []
        return await self.account_repository.list_accounts()

    async def get_account(self, account_id: str) -> dict:
        if self.account_repository is None:
            raise RuntimeError("account repository is not configured")
        record = await self.account_repository.get_account(account_id)
        if record is None:
            raise KeyError(f"account_not_found: {account_id}")
        return record

    async def get_account_statement(
        self,
        account_id: str | None = None,
        *,
        asof: date | None = None,
        captured_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Fetch a QMT-backed account statement for one account / trading day.

        This exposes the same gatherer used by ``daily_review`` through the
        API/CLI surface so assistants can fetch a real broker snapshot on
        demand instead of reverse-engineering cron internals.
        """
        if self.account_repository is None:
            raise RuntimeError("account repository is not configured")

        normalized_account_id = (
            str(account_id).strip() if isinstance(account_id, str) else ""
        )
        if normalized_account_id:
            record = await self.account_repository.get_account(normalized_account_id)
            if record is None:
                raise KeyError(f"account_not_found: {normalized_account_id}")
            if not record.get("enabled", True):
                raise RuntimeError(
                    f"account_disabled: account {normalized_account_id!r} is disabled"
                )
        else:
            record = await self.account_repository.get_default_account()
            if record is None:
                raise RuntimeError(
                    "default_account_not_found: no enabled default account is configured"
                )

        from doyoutrade.account.qmt_reader import QmtAccountReader
        from doyoutrade.account.statement import gather_account_statement
        from doyoutrade.infra.qmt import create_qmt_proxy_rest_client

        resolved = resolved_account_from_record(record)
        client = create_qmt_proxy_rest_client(
            resolved, session_persist=self.account_repository.update_session_id
        )
        effective_asof = asof or datetime.now().date()
        effective_captured_at = captured_at or datetime.now(timezone.utc)
        try:
            reader = QmtAccountReader(client)
            statement = await gather_account_statement(
                reader,
                asof=effective_asof,
                captured_at=effective_captured_at,
                source=reader.portfolio_source,
            )
        finally:
            await client.aclose()

        statement["account_id"] = record["id"]
        statement["account_name"] = record.get("name")
        statement["account_mode"] = record.get("mode")
        statement["resolved_via_default"] = not normalized_account_id
        return statement

    async def create_account(self, payload: dict) -> dict:
        if self.account_repository is None:
            raise RuntimeError("account repository is not configured")
        data = self._normalize_account_payload(payload, partial=False)
        make_default = bool(data.pop("is_default", False))
        record = await self.account_repository.upsert_account(data)
        if make_default:
            record = await self.account_repository.set_default(record["id"]) or record
        return record

    async def update_account(self, account_id: str, payload: dict) -> dict:
        if self.account_repository is None:
            raise RuntimeError("account repository is not configured")
        existing = await self.account_repository.get_account(account_id)
        if existing is None:
            raise KeyError(f"account_not_found: {account_id}")
        data = self._normalize_account_payload(payload, partial=True)
        make_default = data.pop("is_default", None)
        data["id"] = account_id
        record = await self.account_repository.upsert_account(data)
        if make_default is True:
            record = await self.account_repository.set_default(account_id) or record
        return record

    async def set_default_account(self, account_id: str) -> dict:
        if self.account_repository is None:
            raise RuntimeError("account repository is not configured")
        record = await self.account_repository.set_default(account_id)
        if record is None:
            raise KeyError(f"account_not_found: {account_id}")
        return record

    async def delete_account(self, account_id: str) -> None:
        if self.account_repository is None:
            raise RuntimeError("account repository is not configured")
        existing = await self.account_repository.get_account(account_id)
        if existing is None:
            raise KeyError(f"account_not_found: {account_id}")
        # Refuse to orphan a task that explicitly binds this account.
        users = await self._tasks_bound_to_account(account_id)
        if users:
            raise ValueError(
                f"account_in_use: account {account_id!r} is bound by task(s) "
                f"{', '.join(users[:5])}; rebind or delete those tasks first"
            )
        await self.account_repository.delete_account(account_id)

    # --- Watchlist CRUD (backing the /watchlist API + doyoutrade-cli watchlist) -

    @staticmethod
    def _normalize_watchlist_payload(payload: dict, *, partial: bool) -> dict:
        """Validate + shape a watchlist entry create/update payload. ``partial``
        skips required-field checks (update sends only changed fields). Patch
        semantics: only keys present in ``payload`` are written, so ``tags`` is
        never clobbered unless the caller explicitly sends it (CLAUDE.md
        §Assistant 工具入参规范 patch rule)."""
        out: dict[str, Any] = {}
        if "symbol" in payload or not partial:
            symbol = str(payload.get("symbol") or "").strip()
            if not symbol:
                raise ValueError("symbol is required")
            out["symbol"] = symbol
        if "display_name" in payload:
            val = payload.get("display_name")
            out["display_name"] = None if val is None else str(val)
        if "note" in payload:
            out["note"] = str(payload.get("note") or "")
        if "tags" in payload:
            tags = payload.get("tags")
            if tags is None:
                tags = []
            if not isinstance(tags, list):
                raise ValueError("tags must be a list of strings")
            out["tags"] = [str(t).strip() for t in tags if str(t).strip()]
        if "sort_order" in payload and payload.get("sort_order") is not None:
            try:
                out["sort_order"] = int(payload["sort_order"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"sort_order must be an integer, got {payload.get('sort_order')!r}"
                ) from exc
        return out

    async def list_watchlist(self, tag: str | None = None) -> list[dict]:
        if self.watchlist_repository is None:
            return []
        return await self.watchlist_repository.list_entries(tag)

    async def get_watchlist_entry(self, entry_id: str) -> dict:
        if self.watchlist_repository is None:
            raise RuntimeError("watchlist repository is not configured")
        record = await self.watchlist_repository.get_entry(entry_id)
        if record is None:
            raise KeyError(f"watchlist_not_found: {entry_id}")
        return record

    async def add_watchlist_entry(self, payload: dict) -> dict:
        if self.watchlist_repository is None:
            raise RuntimeError("watchlist repository is not configured")
        data = self._normalize_watchlist_payload(payload, partial=False)
        # Keep the instrument catalog aware of the symbol so detail/K-line and
        # data-sync can resolve it. Best-effort: log at ERROR when catalog
        # ensure fails, but do not fail the watchlist add itself.
        if self.instrument_catalog_repository is not None:
            try:
                await ensure_symbols_in_catalog(
                    self.instrument_catalog_repository, [data["symbol"]]
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "watchlist add: ensure_symbols_in_catalog failed symbol=%s "
                    "error_type=%s error=%s",
                    data["symbol"],
                    type(exc).__name__,
                    exc,
                )
        return await self.watchlist_repository.upsert_entry(data)

    async def update_watchlist_entry(self, entry_id: str, payload: dict) -> dict:
        if self.watchlist_repository is None:
            raise RuntimeError("watchlist repository is not configured")
        existing = await self.watchlist_repository.get_entry(entry_id)
        if existing is None:
            raise KeyError(f"watchlist_not_found: {entry_id}")
        data = self._normalize_watchlist_payload(payload, partial=True)
        data["id"] = entry_id
        return await self.watchlist_repository.upsert_entry(data)

    async def delete_watchlist_entry(self, entry_id: str) -> None:
        if self.watchlist_repository is None:
            raise RuntimeError("watchlist repository is not configured")
        existing = await self.watchlist_repository.get_entry(entry_id)
        if existing is None:
            raise KeyError(f"watchlist_not_found: {entry_id}")
        await self.watchlist_repository.delete_entry(entry_id)

    async def list_watchlist_tags(self) -> list[dict]:
        if self.watchlist_repository is None:
            return []
        return await self.watchlist_repository.list_tags()

    async def _tasks_bound_to_account(self, account_id: str) -> list[str]:
        records = await self.task_repository.list_tasks()
        bound: list[str] = []
        for rec in records:
            settings = getattr(rec, "settings", None) or {}
            if isinstance(settings, dict) and (settings.get("account_id") or "") == account_id:
                bound.append(getattr(rec, "task_id", "") or "")
        return bound

    async def _resolve_market_account(self) -> ResolvedAccount | None:
        """The default account as a market-only :class:`ResolvedAccount` (no
        trading identity) for pure market-data paths (catalog sync, etc.).
        Returns None when no default account exists."""
        if self.account_repository is None:
            return None
        record = await self.account_repository.get_default_account()
        if record is None:
            return None
        return resolved_account_from_record(record).market_only()

    async def _validate_bound_account(self, account_id) -> None:
        """If a task binds an ``account_id``, it must resolve to an enabled
        account. Raises ``ValueError`` (→ API 400 ``account_not_found`` /
        ``account_disabled``) so the binding can't silently dangle."""
        aid = (account_id or "").strip() if isinstance(account_id, str) else ""
        if not aid:
            return
        if self.account_repository is None:
            raise ValueError("account repository is not configured; cannot bind account_id")
        record = await self.account_repository.get_account(aid)
        if record is None:
            raise ValueError(f"account_not_found: no account with id {aid!r}")
        if not record.get("enabled", True):
            raise ValueError(f"account_disabled: account {aid!r} is disabled")

    async def _emit_account_failure(
        self, config: CycleTaskConfig, account_id: str, reason: str
    ) -> None:
        span = trace_api.get_current_span()
        try:
            span.set_attribute("account.id", account_id or "")
            span.set_attribute("account.resolved", False)
            span.add_event(
                "account_resolution_failed", {"reason": reason}
            )
        except Exception:  # noqa: BLE001 — observability must not break the cycle
            pass
        await emit_debug_event(
            "account_resolution_failed",
            {
                "task_name": config.name,
                "account_id": account_id or None,
                "mode": str(config.mode),
                "reason": reason,
                "hint": "bind an enabled account_id or set a default account "
                "(POST /accounts/<id>/set-default)",
            },
        )

    async def _emit_qmt_unavailable(
        self, config: CycleTaskConfig, account_id: str, reason: str
    ) -> None:
        await emit_debug_event(
            "qmt_connection_unavailable",
            {
                "task_name": config.name,
                "account_id": account_id or None,
                "mode": str(config.mode),
                "reason": reason,
                "hint": "create a default account with base_url to use QMT data; "
                "falling back to baostock/akshare",
            },
        )

    @staticmethod
    def _mask_api_key(key: str) -> str:
        if not key:
            return ""
        if len(key) <= 4:
            return "****"
        return "****" + key[-4:]

    async def ensure_model_route_exists(self, route_name: str) -> None:
        name = (route_name or "").strip()
        if not name:
            raise ValueError("model_route_name is required")
        if self.model_route_repository is None:
            raise RuntimeError("model route repository is not configured")
        await self.model_route_repository.get_by_route_name(name)

    def _serialize_route_public(self, rec) -> dict:
        return {
            "id": rec.id,
            "route_name": rec.route_name,
            "provider_kind": rec.provider_kind,
            "base_url": rec.base_url,
            "api_key_masked": self._mask_api_key(rec.api_key),
            "target_model": rec.target_model,
            "settings": dict(rec.settings) if rec.settings else None,
            "created_at": rec.created_at.isoformat(),
            "updated_at": rec.updated_at.isoformat(),
        }

    async def list_model_routes_api(self) -> list[dict]:
        if self.model_route_repository is None:
            raise RuntimeError("model routes are not configured")
        rows = await self.model_route_repository.list_routes()
        return [self._serialize_route_public(r) for r in rows]

    async def get_model_route_api(self, route_name: str) -> dict:
        if self.model_route_repository is None:
            raise RuntimeError("model routes are not configured")
        rec = await self.model_route_repository.get_by_route_name(route_name)
        return self._serialize_route_public(rec)

    async def create_model_route_api(self, payload: dict) -> dict:
        if self.model_route_repository is None:
            raise RuntimeError("model routes are not configured")
        rec = await self.model_route_repository.create(
            route_name=str(payload["route_name"]).strip(),
            provider_kind=str(payload["provider_kind"]).strip(),
            api_key=str(payload.get("api_key") or ""),
            base_url=payload.get("base_url"),
            target_model=payload.get("target_model"),
            settings=payload.get("settings"),
        )
        return self._serialize_route_public(rec)

    async def update_model_route_api(self, route_id: str, payload: dict) -> dict:
        if self.model_route_repository is None:
            raise RuntimeError("model routes are not configured")
        kw: dict = {}
        if "route_name" in payload:
            kw["route_name"] = str(payload["route_name"]).strip()
        if "provider_kind" in payload:
            kw["provider_kind"] = str(payload["provider_kind"]).strip()
        if "base_url" in payload:
            kw["base_url"] = payload.get("base_url")
        if "api_key" in payload:
            kw["api_key"] = str(payload["api_key"])
        if "target_model" in payload:
            kw["target_model"] = payload.get("target_model")
        if "settings" in payload:
            kw["settings"] = payload.get("settings")
        rec = await self.model_route_repository.update(route_id, **kw)
        return self._serialize_route_public(rec)

    async def delete_model_route_api(self, route_id: str) -> None:
        if self.model_route_repository is None:
            raise RuntimeError("model routes are not configured")
        await self.model_route_repository.delete(route_id)

    async def reveal_model_route_api_key(self, route_id: str) -> dict:
        if self.model_route_repository is None:
            raise RuntimeError("model routes are not configured")
        rec = await self.model_route_repository.get_by_id(route_id)
        return {"api_key": rec.api_key}

    async def prepare_model_route_test(self, route_id: str) -> tuple[Any, str]:
        """Resolve *route_id* into a recording-wrapped adapter for a connectivity test.

        Raises before any streaming starts so the API layer can map validation
        failures (bad provider_kind, missing api_key/base_url, unresolvable
        route) to a normal HTTP status instead of a 200 SSE stream that fails
        mid-flight.
        """
        if self.model_route_repository is None:
            raise RuntimeError("model routes are not configured")
        rec = await self.model_route_repository.get_by_id(route_id)
        model_settings = await resolve_model_settings(
            route_name=rec.route_name,
            route_repository=self.model_route_repository,
        )
        adapter = build_model_adapter(model_settings)
        recorder = (
            create_model_invocation_recorder(self.model_invocation_repository)
            if self.model_invocation_repository is not None
            else None
        )
        adapter = wrap_with_recording(
            adapter,
            provider=model_settings.provider,
            provider_kind=model_settings.provider_kind,
            model=model_settings.model,
            recorder=recorder,
        )
        return adapter, rec.route_name

    async def stream_model_route_test(self, adapter: Any, route_name: str, prompt: str):
        """Drive a real streaming ``agent_turn`` call and yield structured chunks.

        Not a cycle/job — ``model_invocation_scope`` gets ``cycle_state=None``,
        mirroring the assistant_loop invocation-context precedent so the call
        still lands in ``model_invocations`` with ``run_id``/``task_id`` null
        and ``model_route_name`` set.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        done_marker: dict[str, Any] = {"__done__": True}

        async def on_text_delta(text: str) -> None:
            await queue.put({"type": "delta", "text": text})

        async def _run() -> None:
            try:
                messages = [{"role": "user", "content": prompt}]
                with model_invocation_scope(
                    None, "model_route_test", extras={"model_route_name": route_name}
                ):
                    await adapter.agent_turn(messages, on_text_delta=on_text_delta)
                await queue.put({"type": "done"})
            except Exception as exc:
                logger.warning(
                    "model_route_test failed route_name=%s error_type=%s error=%s",
                    route_name,
                    type(exc).__name__,
                    exc,
                )
                await queue.put(
                    {"type": "error", "error_type": type(exc).__name__, "message": str(exc)}
                )
            finally:
                await queue.put(done_marker)

        task = asyncio.create_task(_run())
        try:
            while True:
                item = await queue.get()
                if item is done_marker:
                    break
                yield item
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _effective_merged_settings(
        self, record: TaskSnapshot, settings_patch: dict | None
    ) -> dict:
        base = dict(record.settings) if record.settings else {}
        if settings_patch is not None:
            merged = {**base, **settings_patch}
        else:
            merged = base
        return merge_task_settings(merged)

    def _mock_catalog_skip_reason(self, data_provider: str | None) -> str | None:
        """Return a debug-event ``reason`` when ``data_provider`` resolves to mock.

        ``mock`` synthesizes bars in-process (see ``_build_mock_stack`` in
        ``doyoutrade/data/factory.py``) and never touches the real
        ``instrument_catalog`` table, so requiring symbols to already be
        registered there is a false gate for this one provider — it blocks
        the provider whose entire point is to run without any real
        market-data registration. Returns ``None`` (no skip) for every other
        provider so the catalog gate stays exactly as strict as before.
        """
        effective = resolve_effective_provider(data_provider, self.default_data_provider)
        return "mock_data_provider" if effective == PROVIDER_MOCK else None

    async def _ensure_new_task_catalog_symbols(
        self, stored_settings: dict, *, data_provider: str | None = None
    ) -> None:
        # @watchlist:<tag> tokens are references resolved at worker assembly, not
        # literal symbols — strip them before catalog validation (their member
        # symbols are catalog-ensured when added via `watchlist add`).
        plain, _tags = split_universe_tokens(list(stored_settings.get("universe") or []))
        skip_reason = self._mock_catalog_skip_reason(data_provider)
        if skip_reason is not None:
            await emit_debug_event(
                "task_catalog_check_skipped",
                {"reason": skip_reason, "symbols": plain, "stage": "create_task"},
            )
            return
        # tradable_only=True: a task universe feeds order generation, so indices
        # (is_tradable=False) must be rejected — they live in the catalog only
        # for watchlist / charting (生产稳定性: 指数不可下单).
        await ensure_symbols_in_catalog(
            self.instrument_catalog_repository, plain, tradable_only=True
        )

    async def _ensure_update_task_catalog_symbols(
        self, record: TaskSnapshot, settings_patch: dict | None
    ) -> None:
        eff = self._effective_merged_settings(record, settings_patch)
        plain, _tags = split_universe_tokens(list(eff.get("universe") or []))
        skip_reason = self._mock_catalog_skip_reason(record.data_provider)
        if skip_reason is not None:
            await emit_debug_event(
                "task_catalog_check_skipped",
                {"reason": skip_reason, "symbols": plain, "stage": "update_task"},
            )
            return
        await ensure_symbols_in_catalog(
            self.instrument_catalog_repository, plain, tradable_only=True
        )

    @property
    def _session_repo(self):
        return self.debug_session_repository

    async def create_task(
        self,
        name: str,
        template_id: str | None = None,
        orchestrator_mode: str | None = None,
        mode: Optional[str] = None,
        description: str = "",
        data_provider: Optional[str] = None,
        settings: Optional[dict] = None,
    ) -> CycleTask:
        stored_settings = merge_task_settings(settings)
        mrn = (stored_settings.get("model_route_name") or "").strip()
        if mrn:
            await self.ensure_model_route_exists(mrn)
        await self._ensure_new_task_catalog_symbols(stored_settings, data_provider=data_provider)
        # When an account is bound, it must exist and be enabled. (Whether a
        # *live* cycle has a usable account is enforced at run time in
        # ``_resolve_worker_account`` — a task may be created before its account
        # exists.)
        await self._validate_bound_account(stored_settings.get("account_id"))
        # Validate QMT connection when data_provider is explicitly set to qmt:
        # the bound account (or the default account) must supply a base_url.
        effective_dp = resolve_effective_provider(data_provider, self.default_data_provider)
        if effective_dp == "qmt" and self.account_repository is not None:
            acct_record = None
            bound = (stored_settings.get("account_id") or "").strip()
            if bound:
                acct_record = await self.account_repository.get_account(bound)
            else:
                acct_record = await self.account_repository.get_default_account()
            if acct_record is None or not (acct_record.get("base_url") or "").strip():
                raise ValueError(
                    "data provider 'qmt' requires an account with base_url; "
                    "bind an account_id with a base_url, set a default account, "
                    "or use provider 'mock' / 'auto'"
                )
        config = cycle_task_config_from_params(
            name=name,
            mode=mode or "paper",
            description=description,
            data_provider=data_provider,
            universe=list(stored_settings.get("universe") or []),
            settings=stored_settings,
        )
        ms = await self._resolve_worker_model_settings(config)
        acct = await self._resolve_worker_account(config)
        worker = await self._build_worker(config, ms, acct)
        record = await self.task_repository.create_task(
            task_id=str(uuid.uuid4()),
            name=name,
            mode=config.mode,
            description=description,
            data_provider=data_provider,
            status="configured",
            last_error="",
            settings=stored_settings,
        )
        instance = CycleTask(
            task_id=record.task_id,
            config=config,
            worker=worker,
            status=record.status,
            last_error=record.last_error,
        )
        worker.cycle_task = instance

        self.tasks[instance.task_id] = instance
        self.scheduler.register(instance)
        return instance

    async def clone_task(
        self,
        source_identifier: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> CycleTask:
        """Create a fresh task that copies an existing task's configuration.

        Useful for backtest tasks, which become one-shot once a run exists. Cloning
        produces a brand-new ``configured`` task that can immediately be backtested
        again, without forcing the caller to recreate strategy bindings or universe.
        """

        source = await self.task_repository.get_task(source_identifier)
        cloned_settings: dict[str, Any] = (
            dict(source.settings) if isinstance(source.settings, dict) else {}
        )
        # Preserve the universe column even if it was not duplicated into settings.
        if source.universe and not isinstance(cloned_settings.get("universe"), list):
            cloned_settings["universe"] = list(source.universe)
        cloned_name = name.strip() if isinstance(name, str) and name.strip() else f"{source.name}_copy"
        cloned_description = description if description is not None else source.description
        return await self.create_task(
            name=cloned_name,
            mode=source.mode,
            description=cloned_description,
            data_provider=source.data_provider,
            settings=cloned_settings,
        )

    async def list_tasks(self):
        records = await self.task_repository.list_tasks()
        return [await self.get_task_status(record.task_id) for record in records]

    async def list_tasks_page(
        self,
        *,
        q: str | None,
        status: str | None,
        mode: str | None,
        limit: int,
        offset: int,
        definition_id: str | None = None,
        modes: list[str] | None = None,
    ) -> dict[str, Any]:
        records, total = await self.task_repository.list_tasks_page(
            q=q,
            status=status,
            mode=mode,
            limit=limit,
            offset=offset,
            definition_id=definition_id,
            modes=modes,
        )
        items = [await self.get_task_status(record.task_id) for record in records]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    async def list_tasks_summary(
        self,
        *,
        q: str | None,
        status: str | None,
        mode: str | None,
        limit: int,
        offset: int,
        definition_id: str | None = None,
    ) -> dict[str, Any]:
        records, total = await self.task_repository.list_tasks_page(
            q=q,
            status=status,
            mode=mode,
            limit=limit,
            offset=offset,
            definition_id=definition_id,
        )
        items = [
            {
                "task_id": r.task_id,
                "name": r.name,
                "status": r.status,
                "mode": r.mode,
                "last_error": r.last_error or None,
            }
            for r in records
        ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    async def build_task_duplicate_preset(self, task_id: str) -> dict[str, Any]:
        record = await self.task_repository.get_task(task_id)
        config = cycle_task_config_from_params(
            name=record.name,
            mode=record.mode,
            description=record.description,
            data_provider=record.data_provider,
            universe=list(record.universe or []),
            settings=record.settings,
        )
        result: dict[str, Any] = {
            "name": f"{record.name}-copy",
            "mode": record.mode,
            "description": record.description or "",
            "data_provider": record.data_provider,
            "universe_symbols": list(config.universe),
            "enabled_skills": list(config.enabled_skills),
        }
        if config.strategy_definition_id:
            result["strategy"] = {
                "definition_id": config.strategy_definition_id,
                "parameter_overrides": dict(config.strategy_parameter_overrides),
                "execution_profile": config.strategy_execution_profile,
            }
        return result

    def list_templates(self):
        return [
            {
                "name": template.name,
                "default_mode": template.default_mode,
            }
            for template in self.templates.values()
        ]

    async def list_instrument_catalog(
        self,
        *,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        tradable_only: bool = False,
    ) -> dict:
        if self.instrument_catalog_repository is None:
            raise RuntimeError("instrument catalog repository not configured")
        items, total = await self.instrument_catalog_repository.list_page(
            q=q, limit=limit, offset=offset, tradable_only=tradable_only
        )
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    async def get_instrument_catalog_item(self, symbol: str) -> dict | None:
        if self.instrument_catalog_repository is None:
            raise RuntimeError("instrument catalog repository not configured")
        requested = str(symbol or "").strip().upper()
        if not requested:
            return None

        row = await self.instrument_catalog_repository.get(requested)
        if row is not None:
            return row

        if requested in A_SHARE_INDEX_SYMBOLS:
            name = _INDEX_SEED_NAME_BY_SYMBOL.get(requested) or requested
            return {
                "symbol": requested,
                "display_name": name,
                "market": "CN",
                "instrument_type": "index",
                "is_tradable": False,
                "last_sync_source": "index_seed",
                "last_sync_at": None,
                "raw": {"source": "index_seed", "name": name},
                "created_at": None,
                "updated_at": None,
            }

        fallback = canonical_symbol_from_qmt_stock_code(requested)
        if fallback != requested:
            return await self.instrument_catalog_repository.get(fallback)
        return None

    async def sync_instrument_catalog(
        self,
        *,
        source: str,
        mode: str,
        symbols: list[str] | None = None,
    ) -> dict:
        if self.instrument_catalog_repository is None:
            raise RuntimeError("instrument catalog repository not configured")
        src = (source or "").strip().lower()
        if src == "akshare":
            from doyoutrade.data.instrument_catalog.sync_akshare import sync_akshare_catalog

            return await sync_akshare_catalog(
                self.instrument_catalog_repository,
                mode=mode,
                symbols=symbols,
            )
        if src == "qmt":
            account = await self._resolve_market_account()
            if account is None or not account.has_connection:
                raise ValueError(
                    "QMT sync requires a default account with base_url; "
                    "create one (POST /accounts ... set-default) or sync from 'akshare'"
                )
            from doyoutrade.data.instrument_catalog.sync_qmt import sync_qmt_catalog
            from doyoutrade.infra.qmt import create_qmt_proxy_rest_client

            client = create_qmt_proxy_rest_client(account)
            try:
                return await sync_qmt_catalog(
                    self.instrument_catalog_repository,
                    client,
                    mode=mode,
                    symbols=symbols,
                )
            finally:
                await client.aclose()
        raise ValueError(f"unknown instrument catalog sync source: {source!r}")

    async def delete_instrument_catalog_symbols(self, symbols: list[str]) -> dict:
        if self.instrument_catalog_repository is None:
            raise RuntimeError("instrument catalog repository not configured")
        n = await self.instrument_catalog_repository.delete_symbols(symbols)
        return {"deleted": n}

    async def clear_instrument_catalog(self, *, confirm: str) -> dict:
        """Wipe the catalog. ``confirm`` must equal ``clear_all_instrument_catalog``."""
        if self.instrument_catalog_repository is None:
            raise RuntimeError("instrument catalog repository not configured")
        expected = "clear_all_instrument_catalog"
        if (confirm or "").strip() != expected:
            raise ValueError(f"confirm must be exactly {expected!r}")
        n = await self.instrument_catalog_repository.delete_all()
        return {"deleted": n}

    def resolve_task_id(self, identifier: str) -> str:
        if identifier in self.tasks:
            return identifier
        for instance in self.tasks.values():
            if instance.config.name == identifier:
                return instance.task_id
        raise KeyError(f"task not found: {identifier}")

    async def start_task(self, identifier: str):
        record = await self.task_repository.get_task(identifier)
        if record.mode == "backtest":
            raise ValueError("backtest task does not support start")
        self.kill_switch_enabled = await self.system_state_repository.get_kill_switch_enabled()
        if self.kill_switch_enabled:
            raise RuntimeError("kill switch enabled")
        instance = await self._load_or_build_task(record)
        self.scheduler.start(instance.task_id)
        await self.task_repository.update_status(instance.task_id, "running", "")
        return instance

    async def pause_task(self, identifier: str):
        record = await self.task_repository.get_task(identifier)
        if record.mode == "backtest":
            raise ValueError("backtest task does not support pause")
        instance = await self._load_or_build_task(record)
        self.scheduler.pause(instance.task_id)
        await self.task_repository.update_status(instance.task_id, "paused", "")
        return instance

    async def stop_task(self, identifier: str):
        record = await self.task_repository.get_task(identifier)
        if record.mode == "backtest":
            raise ValueError("backtest task does not support stop")
        instance = await self._load_or_build_task(record)
        self.scheduler.stop(instance.task_id)
        await self.task_repository.update_status(instance.task_id, "stopped", "")
        return instance

    async def delete_task(self, identifier: str) -> None:
        record = await self.task_repository.get_task(identifier)
        task_id = record.task_id
        cached = self.tasks.pop(task_id, None)
        self.scheduler.unregister(task_id)
        if cached is not None:
            close = getattr(cached.worker, "aclose", None)
            if close is not None:
                await close()
        await self.task_repository.delete_task(task_id)

    async def delete_tasks(self, task_ids: list[str]) -> None:
        records = [await self.task_repository.get_task(task_id) for task_id in task_ids]
        running = [record.task_id for record in records if str(record.status) == "running"]
        if running:
            joined = ", ".join(running)
            raise RuntimeError(f"running tasks cannot be deleted: {joined}")
        for record in records:
            await self.delete_task(record.task_id)

    async def dispatch_resumed_approval(self, approval) -> dict:
        """Re-dispatch one approved-pending order to its task's running worker.

        Called by the scheduler resume sweep. Routes the order through the
        task's live worker so mock and qmt go through the identical
        ``_dispatch_approved_intent`` → ``submit_intent`` path; the fill is
        persisted against the approval's original ``run_id``.

        Returns ``{"status": ...}`` where status is one of:
        ``dispatched`` (order reached the adapter; ``fill`` present),
        ``skipped`` (task not running / no worker / a cycle is in flight — retry
        next sweep, NOT a failure), ``invalid`` (no intent payload to resume),
        or ``failed`` (deserialize error or adapter rejection).
        """
        task_id = getattr(approval, "task_id", None)
        intent_payload = getattr(approval, "intent_payload", None)
        if not intent_payload:
            return {"status": "invalid", "reason": "missing_intent_payload"}
        instance = self.scheduler.tasks.get(task_id) if task_id else None
        if instance is None or getattr(instance, "status", None) != "running":
            return {"status": "skipped", "reason": "task_not_running"}
        worker = getattr(instance, "worker", None)
        if worker is None:
            return {"status": "skipped", "reason": "worker_unavailable"}
        # A cycle in flight holds the per-task lock; don't block the scheduler
        # loop — retry on the next sweep (mirrors the trigger overlap guard).
        if self._cycle_lock(task_id).locked():
            return {"status": "skipped", "reason": "cycle_in_flight"}
        try:
            intent = intent_from_json(intent_payload)
        except Exception as exc:
            return {
                "status": "failed",
                "reason": "intent_deserialize_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        run_id = getattr(approval, "run_id", None) or ""
        async with self._cycle_lock(task_id):
            fill_payload = await worker.dispatch_preapproved_intent(intent, run_id=run_id)
        if fill_payload is None:
            return {"status": "failed", "reason": "adapter_rejected"}
        return {"status": "dispatched", "fill": fill_payload, "run_id": run_id}

    async def tick_once(
        self,
        source: str = "manual",
        *,
        task_ids: list[str] | None = None,
    ):
        """Execute one tick cycle with optional source label.

        Continuous trading no longer flows through ``tick_once`` — that is now the
        ``TriggerScheduler`` firing ``interval`` triggers via ``run_trigger``. This
        path remains for "manual" HTTP ticks and "cron"-driven fires. "debug"
        sessions use ``start_debug_session()`` instead.

        Args:
            source: "manual" for HTTP API, "cron" for cron-driven fires (the legacy
                "scheduled" label is still accepted but no longer driven by a loop).
            task_ids: when supplied, only these running tasks are ticked. When
                ``None``, all running tasks are ticked.
        """
        self.kill_switch_enabled = await self.system_state_repository.get_kill_switch_enabled()
        if self.kill_switch_enabled:
            return 0

        if source not in ("scheduled", "manual", "cron"):
            raise ValueError(f"Unknown source: {source}")

        if self.tick_session_repository is None:
            raise RuntimeError("tick_session_repository not configured")

        executed = 0
        all_running = [task for task in self.scheduler.tasks.values() if task.status == "running"]
        if task_ids is not None:
            wanted = set(task_ids)
            running = [t for t in all_running if t.task_id in wanted]
            missing = wanted - {t.task_id for t in all_running}
            if missing:
                runtime_diag(
                    f"tick_once: source={source} missing_or_not_running_task_ids={sorted(missing)}"
                )
        else:  # "scheduled" / "manual" / "cron" without explicit task_ids
            running = all_running

        runtime_diag(f"tick_once: source={source} running_tasks={len(running)}")
        for instance in running:
            if source == "scheduled":
                session = await self.tick_session_repository.get_or_create_scheduled_session(instance.task_id)
                span_source = "scheduled"
            elif source == "cron":
                runtime_diag(f"tick_once: create_cron_session start task_id={instance.task_id}")
                session = await self.tick_session_repository.create_cron_session(instance.task_id)
                runtime_diag(
                    f"tick_once: create_cron_session done session_id={session.session_id}"
                )
                span_source = "cron"
            else:  # manual
                runtime_diag(f"tick_once: create_manual_session start task_id={instance.task_id}")
                session = await self.tick_session_repository.create_manual_session(instance.task_id)
                runtime_diag(
                    f"tick_once: create_manual_session done session_id={session.session_id}"
                )
                span_source = "manual"

            async with self._cycle_lock(instance.task_id):
                ran = await self._execute_instance_cycle(
                    instance,
                    session=session,
                    run_kind=source,
                    span_source=span_source,
                    finalize=source in ("manual", "cron"),
                )
            if ran:
                executed += 1

        return executed

    def _cycle_lock(self, task_id: str) -> asyncio.Lock:
        """Per-task lock serializing wall-clock cycles across tick_once + run_trigger."""
        lock = self._task_cycle_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._task_cycle_locks[task_id] = lock
        return lock

    async def _execute_instance_cycle(
        self,
        instance,
        *,
        session,
        run_kind: str,
        span_source: str,
        trigger_id: str | None = None,
        run_mode_override: str | None = None,
        finalize: bool = False,
    ) -> bool:
        """Run exactly one ``worker.run_cycle`` for ``instance`` under ``session``.

        Shared by ``tick_once`` (scheduled/manual/cron) and ``run_trigger``
        (run_kind='trigger'). Sets the cycle contextvars (run_kind, session id,
        trigger id), optionally overrides the worker run_mode for THIS fire only
        (save/restore — safe because the caller holds the per-task ``_cycle_lock``),
        and — when ``finalize`` — drains the debug span queue + attaches the run_id to
        the session + marks it finished, so the fresh session can pivot to its
        cycle_runs row via ``debug_sessions.run_id``.

        Returns True if the cycle executed (False if it raised, after recording the
        error on the instance + scheduler.on_task_error).
        """
        export_ctx = (
            debug_span_export_for_session(session.session_id, span_source)
            if self.debug_session_span_repository is not None
            else nullcontext()
        )
        prev_run_mode = getattr(instance.worker, "run_mode", None)
        override_applied = run_mode_override is not None and run_mode_override != prev_run_mode
        if override_applied:
            instance.worker.run_mode = run_mode_override
        token_kind = current_tick_run_kind.set(run_kind)
        token_sid = current_tick_session_id.set(session.session_id)
        token_trg = current_trigger_id.set(trigger_id)
        executed = False
        try:
            with export_ctx:
                try:
                    runtime_diag("execute_instance_cycle: worker.run_cycle() invoke")
                    result = instance.worker.run_cycle()
                    if inspect.isawaitable(result):
                        await result
                    executed = True
                except Exception as exc:
                    instance.status = "error"
                    instance.last_error = str(exc)
                    handler = getattr(self.scheduler, "on_task_error", None)
                    if handler is not None:
                        await handler(instance.task_id, str(exc))
        finally:
            current_trigger_id.reset(token_trg)
            current_tick_session_id.reset(token_sid)
            current_tick_run_kind.reset(token_kind)
            if override_applied:
                instance.worker.run_mode = prev_run_mode
            if finalize:
                runtime_diag("execute_instance_cycle: drain_debug_span_persist_queue start")
                await drain_debug_span_persist_queue()
                runtime_diag("execute_instance_cycle: drain_debug_span_persist_queue done")
                # Surface the just-completed worker run_id on debug_sessions so
                # downstream debug-view tooling can pivot from the fresh
                # cron/manual/trigger session to its cycle_runs row.
                last_run_id = getattr(instance.worker, "last_run_id", "") or ""
                if last_run_id and self.debug_session_repository is not None:
                    try:
                        await self.debug_session_repository.attach_run_id(
                            session.session_id, last_run_id,
                        )
                    except Exception:
                        runtime_diag(
                            "execute_instance_cycle: attach_run_id failed "
                            f"session_id={session.session_id} run_id={last_run_id}"
                        )
                await self._session_repo.mark_finished(
                    session.session_id,
                    status="finished",
                    error_message="",
                )
        return executed

    async def run_trigger(self, trigger) -> str | None:
        """Fire one Trigger: run its parent task's cycle once with the trigger's
        ``execution_intent`` (per-fire run_mode override) and tag ``cycle_runs.trigger_id``.

        Returns the resulting cycle ``run_id`` (or ``None`` if the kill switch is on,
        the parent task isn't running, or the cycle did not execute). Delivery is a
        Phase 2 concern; this method only runs the cycle and threads attribution. The
        caller (``TriggerScheduler``) owns due-ness, next_fire recompute, and the
        ``trigger_skipped`` / ``trigger_fire_failed`` structured events.
        """
        self.kill_switch_enabled = await self.system_state_repository.get_kill_switch_enabled()
        if self.kill_switch_enabled:
            return None
        if self.tick_session_repository is None:
            raise RuntimeError("tick_session_repository not configured")
        instance = self.scheduler.tasks.get(trigger.task_id)
        if instance is None or instance.status != "running":
            runtime_diag(
                "run_trigger: parent task not running "
                f"trigger_id={trigger.id} task_id={trigger.task_id}"
            )
            return None
        base_run_mode = getattr(instance.worker, "run_mode", "paper")
        effective_run_mode = run_mode_for_intent(trigger.execution_intent, base_run_mode)
        session = await self.tick_session_repository.create_trigger_session(trigger.task_id)
        async with self._cycle_lock(trigger.task_id):
            await self._execute_instance_cycle(
                instance,
                session=session,
                run_kind="trigger",
                span_source="trigger",
                trigger_id=trigger.id,
                run_mode_override=effective_run_mode,
                finalize=True,
            )
        return getattr(instance.worker, "last_run_id", "") or None

    async def list_backtest_jobs(self, identifier: str, *, limit: int = 50, offset: int = 0) -> dict:
        if self.run_repository is None:
            return {"items": [], "total": 0}
        record = await self.task_repository.get_task(identifier)
        items, total = await self.run_repository.list_for_task(
            record.task_id,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "total": total}

    async def list_backtest_jobs_global(
        self,
        *,
        task_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        if self.run_repository is None:
            return {"items": [], "total": 0}
        items, total = await self.run_repository.list_jobs(
            task_id,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "total": total}

    async def get_backtest_job(self, identifier: str, run_id: str) -> dict:
        if self.run_repository is None:
            raise RecordNotFoundError(f"backtest job not found: {run_id}")
        record = await self.task_repository.get_task(identifier)
        row = await self.run_repository.get(run_id)
        if row is None or row.get("task_id") != record.task_id:
            raise RecordNotFoundError(f"backtest job not found: {run_id}")
        return row

    async def get_backtest_summary(self, run_id: str) -> dict:
        """Resolve ``run_id`` and return the persisted backtest summary plus
        the run header so the agent can read a dense, fixed-schema result in
        one hop.

        Layout:

        - ``summary_state="ok"`` — ``summary`` is the JSON written by
          :func:`doyoutrade.backtest.summary.summary_to_json` for this run.
        - ``summary_state="missing"`` — the task has never persisted a
          summary (e.g. the run is still in flight or finalize raced).
        - ``summary_state="stale"`` — the task carries a summary but for a
          *different* run_id (a newer backtest overwrote it).
          ``latest_summary_run_id`` names the run currently stored.

        Raises :class:`RecordNotFoundError` only when the run row itself is
        missing — the stale / missing cases are reported via
        ``summary_state`` so the caller can decide how to recover.
        """

        if self.run_repository is None:
            raise RecordNotFoundError(f"backtest job not found: {run_id}")
        run_row = await self.run_repository.get(run_id)
        if run_row is None:
            raise RecordNotFoundError(f"backtest job not found: {run_id}")
        task_id = run_row.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise RecordNotFoundError(
                f"backtest job is missing task_id: {run_id}"
            )

        task_record = await self.task_repository.get_task(task_id)
        raw_summary = task_record.backtest_summary
        summary = dict(raw_summary) if isinstance(raw_summary, dict) else None

        # ``backtest_job_id`` is the canonical match key (added 2026-05). Older
        # summaries persisted before that change carry only ``run_id`` (which
        # is the final cycle_run_id). The fallback keeps stale-but-readable
        # rows usable until the next backtest overwrites them.
        latest_summary_run_id: str | None = None
        if isinstance(summary, dict):
            stored_job_id = summary.get("backtest_job_id")
            if isinstance(stored_job_id, str) and stored_job_id:
                latest_summary_run_id = stored_job_id
            else:
                stored_run_id = summary.get("run_id")
                if isinstance(stored_run_id, str) and stored_run_id:
                    latest_summary_run_id = stored_run_id

        if summary is None:
            summary_state = "missing"
            attached_summary: dict | None = None
        elif latest_summary_run_id == run_id:
            summary_state = "ok"
            attached_summary = summary
        else:
            summary_state = "stale"
            attached_summary = None

        return {
            "run": run_row,
            "task_id": task_id,
            "summary": attached_summary,
            "summary_state": summary_state,
            "latest_summary_run_id": latest_summary_run_id,
        }

    async def get_backtest_chart(
        self,
        identifier: str,
        run_id: str,
        *,
        symbol: str | None = None,
    ) -> dict:
        if self.run_repository is None:
            raise RecordNotFoundError(f"backtest job not found: {run_id}")
        record = await self.task_repository.get_task(identifier)
        row = await self.run_repository.get(run_id)
        if row is None or row.get("task_id") != record.task_id:
            raise RecordNotFoundError(f"backtest job not found: {run_id}")

        fill_rows: list[dict[str, Any]] = []
        if self.trade_fill_repository is not None:
            fill_rows = await self.trade_fill_repository.list_for_task_run(
                task_id=record.task_id,
                run_id=run_id,
            )
        symbols = _chart_symbols_from_trade_fills(fill_rows, list(record.universe or []))
        selected_symbol = (symbol or "").strip() or (symbols[0] if symbols else "")
        if symbol is not None and selected_symbol and selected_symbol not in symbols:
            raise ValueError(f"symbol is not part of this backtest run: {selected_symbol}")

        warnings: list[str] = []
        bars: list[dict[str, Any]] = []
        if selected_symbol:
            instance = await self._load_or_build_task(record)
            range_start = date.fromisoformat(str(row.get("range_start_utc") or "")[:10])
            # Reuse the per-strategy ``startup_history`` so the chart's
            # warmup is at least as wide as the backtest engine's preload.
            # Failure to resolve leaves the legacy 250-day floor in
            # place — that's a safe, visible default for chart UX.
            chart_startup_history = await self._resolve_strategy_startup_history(
                strategy_definition_id=instance.config.strategy_definition_id or None,
            )
            start = await _chart_bars_start_with_warmup(
                data_provider=instance.worker.data_provider,
                range_start=range_start,
                startup_history=chart_startup_history,
            )
            end = str(row.get("range_end_utc") or "")[:10]
            bar_interval = str(row.get("bar_interval") or "1d")
            raw_bars = await instance.worker.data_provider.get_bars(
                selected_symbol,
                start,
                end,
                interval=bar_interval,
                adjust=KLINE_DISPLAY_ADJUST,
            )
            bars = [asdict(bar) if is_dataclass(bar) else dict(bar) for bar in raw_bars]
        else:
            warnings.append("no symbols available for this backtest run")

        if selected_symbol and not bars:
            warnings.append(f"no bars available for {selected_symbol}")

        return {
            "run": row,
            "symbols": symbols,
            "selected_symbol": selected_symbol,
            "adjust": KLINE_DISPLAY_ADJUST,
            "bars": bars,
            "volume_mode": "amount_available"
            if any(bar.get("amount") is not None for bar in bars)
            else "volume_only",
            "trades": _chart_trades_from_trade_fills(fill_rows, selected_symbol),
            "warnings": warnings,
        }

    async def get_local_market_bars(
        self,
        *,
        symbol: str,
        interval: str = "1d",
        start: str | None = None,
        end: str | None = None,
        provider: str | None = None,
        adjust: str | None = None,
        backfill: bool = True,
    ) -> dict[str, Any]:
        """Read OHLCV bars from the local ``market_bars`` warehouse for UI charts.

        When the warehouse has no rows for the requested window and
        ``backfill`` is set (the default for UI reads), the read self-heals:
        it fetches the missing bars upstream and upserts them under the *same*
        (provider, adjust, symbol, interval) key it just read with, then
        re-reads so this same call returns the freshly warmed bars. Oversized
        windows are pushed to a background job instead of blocking. Pass
        ``backfill=False`` for a pure read (used by callers / tests that must
        observe the un-warmed state)."""
        if self.market_bars_repository is None:
            raise RuntimeError(
                "local market data is unavailable: configure market_data.database_url"
            )

        normalized_symbol = symbol.strip()
        if not normalized_symbol:
            raise ValueError("symbol is required")

        normalized_interval = (interval or "1d").strip().lower()
        if normalized_interval not in SUPPORTED_LOCAL_INTERVALS:
            raise ValueError(
                f"unsupported interval: {normalized_interval!r}; "
                f"supported: {sorted(SUPPORTED_LOCAL_INTERVALS)}"
            )

        effective_provider = (
            (provider or "").strip()
            or (self.app_cfg.market_data.default_provider or "").strip()
            or self.default_data_provider
        )
        effective_adjust = (adjust or DEFAULT_BAR_ADJUST).strip().lower() or DEFAULT_BAR_ADJUST

        today = datetime.now(timezone.utc).date()
        effective_end = (end or "").strip() or today.isoformat()
        if start and str(start).strip():
            effective_start = str(start).strip()
        else:
            lookback_days = max(30, int(self.app_cfg.market_data.lookback_years) * 365)
            effective_start = (today - timedelta(days=lookback_days)).isoformat()

        try:
            start_bound = _query_bound(
                effective_start,
                interval=normalized_interval,
                is_end=False,
            )
            end_bound = _query_bound(
                effective_end,
                interval=normalized_interval,
                is_end=True,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if end_bound < start_bound:
            raise ValueError(
                f"requested_end must be on or after requested_start, got {effective_start!r}..{effective_end!r}"
            )

        def _shape_bars(source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "timestamp": row["timestamp"],
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row["volume"],
                    "amount": row.get("amount"),
                }
                for row in source_rows
            ]

        rows = await self.market_bars_repository.bars_in_range(
            provider=effective_provider,
            adjust=effective_adjust,
            symbol=normalized_symbol,
            interval=normalized_interval,
            start=start_bound,
            end=end_bound,
        )
        bars = _shape_bars(rows)

        backfill_info: dict[str, Any] | None = None
        if not bars and backfill:
            # Self-heal: nothing stored for this window. Fetch upstream on
            # demand and upsert under the same key we just read with, then
            # re-read so this call returns the freshly warmed bars.
            backfill_info = await self._read_through_backfill_local_market_bars(
                symbol=normalized_symbol,
                interval=normalized_interval,
                start=effective_start,
                end=effective_end,
                start_bound=start_bound,
                end_bound=end_bound,
                provider=effective_provider,
                adjust=effective_adjust,
            )
            rows = await self.market_bars_repository.bars_in_range(
                provider=effective_provider,
                adjust=effective_adjust,
                symbol=normalized_symbol,
                interval=normalized_interval,
                start=start_bound,
                end=end_bound,
            )
            bars = _shape_bars(rows)

        warnings: list[str] = []
        if not bars:
            warnings.append(
                f"no local bars for {normalized_symbol} in [{effective_start}, {effective_end}]"
            )
            if backfill_info and backfill_info.get("hint"):
                warnings.append(str(backfill_info["hint"]))

        sync_state = await self.market_bars_repository.get_sync_state(
            provider=effective_provider,
            adjust=effective_adjust,
            symbol=normalized_symbol,
            interval=normalized_interval,
        )

        return {
            "symbol": normalized_symbol,
            "interval": normalized_interval,
            "provider": effective_provider,
            "adjust": effective_adjust,
            "start": effective_start,
            "end": effective_end,
            "bars": bars,
            "volume_mode": "amount_available"
            if any(bar.get("amount") is not None for bar in bars)
            else "volume_only",
            "summary": build_local_market_summary(bars),
            "coverage": build_requested_window_coverage(
                interval=normalized_interval,
                requested_start=effective_start,
                requested_end=effective_end,
                bars=bars,
                sync_state=sync_state,
            ),
            "available_overlays": await self._list_local_market_overlay_candidates(
                symbol=normalized_symbol,
                start=effective_start,
                end=effective_end,
            ),
            "sync_state": sync_state,
            "backfill": backfill_info,
            "backfill_job_id": (backfill_info or {}).get("job_id"),
            "warnings": warnings,
        }

    async def _read_through_backfill_local_market_bars(
        self,
        *,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        start_bound: datetime,
        end_bound: datetime,
        provider: str,
        adjust: str,
    ) -> dict[str, Any]:
        """Warm an empty ``/market/bars`` window on demand (read-through).

        Fetches upstream and upserts under the *same* (provider, adjust,
        symbol, interval) key the read used — so key drift can't reintroduce
        the "rendered but empty" chart bug. Bounded windows fill inline (the
        caller re-reads and returns them in the same response); oversized
        windows are pushed to the existing async sync-job machinery so the UI
        read never blocks on a huge fetch. Best-effort: any failure degrades
        to a structured warning + debug event, never a 500.
        """
        execution_mode = choose_sync_execution_mode(
            interval=interval, start=start, end=end
        )
        base_payload = {
            "symbol": symbol,
            "interval": interval,
            "provider": provider,
            "adjust": adjust,
            "requested_start": start,
            "requested_end": end,
            "execution_mode": execution_mode,
        }
        await emit_debug_event("market_bars_read_through.triggered", dict(base_payload))

        key = (provider, adjust, symbol, interval)
        lock = self._local_market_read_through_locks.setdefault(key, asyncio.Lock())
        async with lock:
            # A concurrent read that held the lock first may have already
            # warmed this window — re-check before spending an upstream fetch.
            existing = await self.market_bars_repository.bars_in_range(
                provider=provider,
                adjust=adjust,
                symbol=symbol,
                interval=interval,
                start=start_bound,
                end=end_bound,
            )
            if existing:
                await emit_debug_event(
                    "market_bars_read_through.skipped",
                    {
                        **base_payload,
                        "reason": "already_filled",
                        "hint": "another read-through already warmed this window",
                    },
                )
                return {
                    "attempted": True,
                    "status": "already_filled",
                    "execution_mode": execution_mode,
                }

            if execution_mode == "async":
                job = self._enqueue_read_through_job(
                    symbol=symbol,
                    interval=interval,
                    start=start,
                    end=end,
                    provider=provider,
                    adjust=adjust,
                )
                await emit_debug_event(
                    "market_bars_read_through.job_enqueued",
                    {
                        **base_payload,
                        "job_id": job.job_id,
                        "hint": "window too large for inline fetch; syncing in the "
                        "background — poll GET /market/bars/sync-jobs/{job_id} then re-read",
                    },
                )
                return {
                    "attempted": True,
                    "status": "job_enqueued",
                    "execution_mode": execution_mode,
                    "job_id": job.job_id,
                    "hint": "syncing in the background; retry shortly",
                }

            try:
                result = await asyncio.wait_for(
                    self._run_local_market_range_sync(
                        symbol=symbol,
                        interval=interval,
                        start=start,
                        end=end,
                        provider=provider,
                        adjust=adjust,
                        mode="fill_gap",
                    ),
                    timeout=_READ_THROUGH_INLINE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                # Don't lose the work: finish it in the background.
                job = self._enqueue_read_through_job(
                    symbol=symbol,
                    interval=interval,
                    start=start,
                    end=end,
                    provider=provider,
                    adjust=adjust,
                )
                logger.warning(
                    "market bars read-through inline fetch timed out symbol=%s "
                    "interval=%s provider=%s adjust=%s; escalated to job_id=%s",
                    symbol,
                    interval,
                    provider,
                    adjust,
                    job.job_id,
                )
                await emit_debug_event(
                    "market_bars_read_through.timeout",
                    {
                        **base_payload,
                        "job_id": job.job_id,
                        "timeout_seconds": _READ_THROUGH_INLINE_TIMEOUT_SECONDS,
                        "hint": "inline fetch exceeded timeout; syncing in the background",
                    },
                )
                return {
                    "attempted": True,
                    "status": "timeout_enqueued",
                    "execution_mode": execution_mode,
                    "job_id": job.job_id,
                    "hint": "still syncing in the background; retry shortly",
                }
            except Exception as exc:  # noqa: BLE001 — read must degrade, not 500
                logger.warning(
                    "market bars read-through inline fetch failed symbol=%s "
                    "interval=%s provider=%s adjust=%s error_type=%s error=%s",
                    symbol,
                    interval,
                    provider,
                    adjust,
                    type(exc).__name__,
                    exc,
                )
                await emit_debug_event(
                    "market_bars_read_through.failed",
                    {
                        **base_payload,
                        "error_type": type(exc).__name__,
                        "error": str(exc) or type(exc).__name__,
                        "hint": "upstream fetch failed; check the symbol, provider "
                        "availability, and interval support for this instrument",
                    },
                )
                return {
                    "attempted": True,
                    "status": "failed",
                    "execution_mode": execution_mode,
                    "error_type": type(exc).__name__,
                    "error": str(exc) or type(exc).__name__,
                    "hint": "upstream fetch failed; see server logs",
                }

            upserted = int(result.get("upserted_count", 0))
            await emit_debug_event(
                "market_bars_read_through.inline_filled",
                {
                    **base_payload,
                    "upserted_count": upserted,
                    "fetched_segments": result.get("fetched_segments", []),
                },
            )
            if upserted:
                return {
                    "attempted": True,
                    "status": "inline_filled",
                    "execution_mode": execution_mode,
                    "upserted_count": upserted,
                }
            return {
                "attempted": True,
                "status": "upstream_empty",
                "execution_mode": execution_mode,
                "upserted_count": 0,
                "hint": "upstream returned no bars for this window — the instrument "
                "may have no data at this interval",
            }

    def _enqueue_read_through_job(
        self,
        *,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        provider: str,
        adjust: str,
    ) -> LocalMarketSyncJob:
        """Enqueue (or reuse) a background sync job for a read-through miss.

        Dedupes by (provider, adjust, symbol, interval) so repeatedly opening
        the same not-yet-synced chart reuses one in-flight job instead of
        piling up duplicate upstream fetches.
        """
        key = (provider, adjust, symbol, interval)
        existing_job_id = self._local_market_read_through_jobs.get(key)
        if existing_job_id is not None:
            existing = self._local_market_sync_jobs.get(existing_job_id)
            if existing is not None and existing.status in {"pending", "running"}:
                return existing

        job = self._create_local_market_sync_job(
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            provider=provider,
            adjust=adjust,
            mode="fill_gap",
        )
        self._local_market_read_through_jobs[key] = job.job_id
        task = asyncio.create_task(
            self._run_local_market_range_sync_job(job),
            name=f"local-market-read-through-{job.job_id}",
        )
        self._local_market_sync_tasks[job.job_id] = task
        task.add_done_callback(
            lambda _task, job_id=job.job_id: self._local_market_sync_tasks.pop(job_id, None)
        )
        return job

    async def sync_local_market_bars_range(
        self,
        *,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        provider: str | None = None,
        adjust: str | None = None,
        mode: str,
    ) -> dict[str, Any]:
        if self.market_bars_repository is None:
            raise RuntimeError("local market data is unavailable: configure market_data.database_url")

        normalized_symbol = str(symbol or "").strip()
        if not normalized_symbol:
            raise ValueError("symbol is required")

        normalized_interval = str(interval or "").strip().lower()
        if normalized_interval not in SUPPORTED_LOCAL_INTERVALS:
            raise ValueError(
                f"unsupported interval: {normalized_interval!r}; "
                f"supported: {sorted(SUPPORTED_LOCAL_INTERVALS)}"
            )

        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in SUPPORTED_LOCAL_SYNC_MODES:
            raise ValueError(
                f"unsupported sync mode: {normalized_mode!r}; "
                f"supported: {sorted(SUPPORTED_LOCAL_SYNC_MODES)}"
            )

        effective_start = str(start or "").strip()
        effective_end = str(end or "").strip()
        if not effective_start:
            raise ValueError("start is required")
        if not effective_end:
            raise ValueError("end is required")

        effective_provider = resolve_effective_provider(
            provider,
            (self.app_cfg.market_data.default_provider or self.default_data_provider),
        )
        effective_adjust = str(adjust or DEFAULT_BAR_ADJUST).strip().lower() or DEFAULT_BAR_ADJUST

        start_bound = _query_bound(effective_start, interval=normalized_interval, is_end=False)
        end_bound = _query_bound(effective_end, interval=normalized_interval, is_end=True)
        if end_bound < start_bound:
            raise ValueError(
                f"requested_end must be on or after requested_start, got {effective_start!r}..{effective_end!r}"
            )

        execution_mode = choose_sync_execution_mode(
            interval=normalized_interval,
            start=effective_start,
            end=effective_end,
        )
        if execution_mode == "sync":
            result = await self._run_local_market_range_sync(
                symbol=normalized_symbol,
                interval=normalized_interval,
                start=effective_start,
                end=effective_end,
                provider=effective_provider,
                adjust=effective_adjust,
                mode=normalized_mode,
            )
            return {
                "status": "ok",
                "execution_mode": "sync",
                "mode": normalized_mode,
                "requested_range": {"start": effective_start, "end": effective_end},
                "fetched_segments": result["fetched_segments"],
                "upserted_count": result["upserted_count"],
                "adjust_drift_refreshed": bool(result.get("adjust_drift_refreshed", False)),
                "warnings": result.get("warnings", []),
            }

        job = self._create_local_market_sync_job(
            symbol=normalized_symbol,
            interval=normalized_interval,
            start=effective_start,
            end=effective_end,
            provider=effective_provider,
            adjust=effective_adjust,
            mode=normalized_mode,
        )
        task = asyncio.create_task(
            self._run_local_market_range_sync_job(job),
            name=f"local-market-sync-{job.job_id}",
        )
        self._local_market_sync_tasks[job.job_id] = task
        task.add_done_callback(
            lambda _task, job_id=job.job_id: self._local_market_sync_tasks.pop(job_id, None)
        )
        return {
            "status": "accepted",
            "execution_mode": "async",
            "job_id": job.job_id,
            "mode": normalized_mode,
            "requested_range": {"start": effective_start, "end": effective_end},
            "warnings": [],
        }

    async def get_local_market_sync_job(self, job_id: str) -> dict[str, Any]:
        job = self._local_market_sync_jobs.get(job_id)
        if job is None:
            raise RecordNotFoundError(f"local market sync job not found: {job_id}")
        payload = asdict(job)
        payload["requested_range"] = {
            "start": payload.pop("requested_start"),
            "end": payload.pop("requested_end"),
        }
        payload.pop("warnings", None)
        return payload

    def _create_local_market_sync_job(
        self,
        *,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        provider: str,
        adjust: str,
        mode: str,
    ) -> LocalMarketSyncJob:
        job = LocalMarketSyncJob(
            job_id=f"lmjob-{uuid.uuid4()}",
            status="pending",
            mode=mode,
            symbol=symbol,
            interval=interval,
            provider=provider,
            adjust=adjust,
            requested_start=start,
            requested_end=end,
        )
        self._local_market_sync_jobs[job.job_id] = job
        return job

    async def _run_local_market_range_sync_job(self, job: LocalMarketSyncJob) -> None:
        job.status = "running"
        job.started_at = datetime.now(timezone.utc).isoformat()
        try:
            result = await self._run_local_market_range_sync(
                symbol=job.symbol,
                interval=job.interval,
                start=job.requested_start,
                end=job.requested_end,
                provider=job.provider,
                adjust=job.adjust,
                mode=job.mode,
            )
        except Exception as exc:
            logger.exception(
                "local market sync job failed job_id=%s symbol=%s interval=%s "
                "provider=%s adjust=%s error_type=%s error=%s",
                job.job_id,
                job.symbol,
                job.interval,
                job.provider,
                job.adjust,
                type(exc).__name__,
                exc,
            )
            job.status = "failed"
            job.finished_at = datetime.now(timezone.utc).isoformat()
            job.error_code = "local_market_sync_failed"
            job.error_type = type(exc).__name__
            job.error_message = str(exc) or type(exc).__name__
            job.hint = "check local market sync request bounds, upstream provider availability, and repository writes"
            return

        job.status = "ok"
        job.finished_at = datetime.now(timezone.utc).isoformat()
        job.fetched_segments = list(result["fetched_segments"])
        job.warnings = list(result.get("warnings", []))
        job.upserted_count = int(result["upserted_count"])
        job.adjust_drift_refreshed = bool(result.get("adjust_drift_refreshed", False))

    async def _run_local_market_range_sync(
        self,
        *,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        provider: str,
        adjust: str,
        mode: str,
    ) -> dict[str, Any]:
        runner = self._market_data_sync_runner
        sync_range = getattr(runner, "sync_range", None)
        if callable(sync_range):
            return await sync_range(
                symbol=symbol,
                interval=interval,
                start=start,
                end=end,
                provider=provider,
                adjust=adjust,
                mode=mode,
            )
        return await self._run_local_market_range_sync_direct(
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            provider=provider,
            adjust=adjust,
            mode=mode,
        )

    async def _run_local_market_range_sync_direct(
        self,
        *,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        provider: str,
        adjust: str,
        mode: str,
    ) -> dict[str, Any]:
        if self.market_bars_repository is None:
            raise RuntimeError("local market bars repository not configured")

        existing_bars = await self.market_bars_repository.bars_in_range(
            provider=provider,
            adjust=adjust,
            symbol=symbol,
            interval=interval,
            start=_query_bound(start, interval=interval, is_end=False),
            end=_query_bound(end, interval=interval, is_end=True),
        )
        fetch_segments = build_sync_fetch_segments(
            interval=interval,
            requested_start=start,
            requested_end=end,
            bars=existing_bars,
            mode=mode,
        )
        if not fetch_segments:
            return {
                "fetched_segments": [],
                "upserted_count": 0,
                "adjust_drift_refreshed": False,
                "warnings": ["requested range already covered locally"],
            }

        account = await self._resolve_market_account()
        data_provider, _universe_provider, _account_reader = build_trading_data_stack(
            provider,
            self.app_cfg.data,
            [symbol],
            account=account,
        )
        start_bound = _query_bound(start, interval=interval, is_end=False)
        end_bound = _query_bound(end, interval=interval, is_end=True)
        warnings: list[str] = []
        try:
            bar_payloads: list[dict[str, Any]] = []
            fetched_segments: list[dict[str, str]] = []
            for segment in fetch_segments:
                fetch_start = segment.start
                if mode == "fill_gap" and existing_bars:
                    # fill_gap segments are by construction disjoint from the
                    # locally stored bars, so widen each fetch backwards into
                    # covered territory to obtain anchor days for adjust-factor
                    # drift comparison (clamped at the earliest local bar).
                    fetch_start = self._anchored_local_market_fetch_start(
                        segment_start=segment.start,
                        interval=interval,
                        existing_bars=existing_bars,
                    )
                fetched = await data_provider.get_bars(
                    symbol,
                    fetch_start,
                    segment.end,
                    interval=interval,
                    adjust=adjust,
                )
                segment_payloads = [_bar_dict(bar, interval=interval) for bar in fetched]
                if not segment_payloads:
                    warnings.append(
                        "upstream returned no bars for requested segment "
                        f"{segment.start}..{segment.end}"
                    )
                    continue
                if existing_bars:
                    drift_report = detect_adjust_drift(existing_bars, segment_payloads)
                    if drift_report.drifted:
                        return await self._refresh_local_market_history_after_adjust_drift(
                            symbol=symbol,
                            interval=interval,
                            provider=provider,
                            adjust=adjust,
                            mode=mode,
                            data_provider=data_provider,
                            requested_end=end,
                            start_bound=start_bound,
                            end_bound=end_bound,
                            existing_bars=existing_bars,
                            report=drift_report,
                            warnings=warnings,
                        )
                bar_payloads.extend(segment_payloads)
                fetched_segments.append(
                    {"start": segment.start, "end": segment.end, "status": "fetched"}
                )
            upserted_count = 0
            if bar_payloads:
                upserted_count = await self.market_bars_repository.upsert_bars(
                    provider=provider,
                    adjust=adjust,
                    interval=interval,
                    bars=bar_payloads,
                )
                covered_start, covered_end = self._local_market_sync_payload_bounds(
                    bar_payloads,
                    interval=interval,
                )
                await self._mark_local_market_sync_success(
                    provider=provider,
                    adjust=adjust,
                    symbol=symbol,
                    interval=interval,
                    target_start=start_bound,
                    target_end=end_bound,
                    covered_start=covered_start,
                    covered_end=covered_end,
                )
            elif not warnings:
                warnings.append("upstream returned no bars for requested range")
            return {
                "fetched_segments": fetched_segments,
                "upserted_count": upserted_count,
                "adjust_drift_refreshed": False,
                "warnings": warnings,
            }
        except Exception as exc:
            await self._mark_local_market_sync_failure(
                provider=provider,
                adjust=adjust,
                symbol=symbol,
                interval=interval,
                requested_start=start_bound,
                requested_end=end_bound,
                exc=exc,
            )
            raise
        finally:
            close = getattr(data_provider, "aclose", None)
            if close is not None:
                await close()

    def _anchored_local_market_fetch_start(
        self,
        *,
        segment_start: str,
        interval: str,
        existing_bars: list[dict[str, Any]],
    ) -> str:
        """Widen a fill_gap fetch segment backwards into covered territory.

        Returns the (possibly earlier) fetch start so the upstream response
        contains anchor days overlapping locally stored bars, enabling
        adjust-factor drift detection. Clamped at the earliest local bar so we
        never request data before our own coverage just for anchoring.
        """

        segment_bound = _query_bound(segment_start, interval=interval, is_end=False)
        earliest_local = min(
            _query_bound(str(bar.get("timestamp")), interval=interval, is_end=False)
            for bar in existing_bars
        )
        anchored = max(
            earliest_local,
            segment_bound - timedelta(days=ANCHOR_OVERLAP_CALENDAR_DAYS),
        )
        if anchored >= segment_bound:
            return segment_start
        if interval == "1d":
            return anchored.date().isoformat()
        return anchored.isoformat()

    async def _refresh_local_market_history_after_adjust_drift(
        self,
        *,
        symbol: str,
        interval: str,
        provider: str,
        adjust: str,
        mode: str,
        data_provider: Any,
        requested_end: str,
        start_bound: datetime,
        end_bound: datetime,
        existing_bars: list[dict[str, Any]],
        report: AdjustDriftReport,
        warnings: list[str],
    ) -> dict[str, Any]:
        """Escalate a drifted sync to a wholesale refresh of the local history.

        An ex-rights/dividend event rescales the entire qfq price history, so
        patching only the requested window would leave older locally stored
        bars on the stale factor (10x price cliffs). Refresh everything from
        the earliest local coverage through the requested end. Failures here
        must surface — they propagate to the existing sync-failure path.
        """

        local_start_bound: datetime | None = None
        get_sync_state = getattr(self.market_bars_repository, "get_sync_state", None)
        if callable(get_sync_state):
            sync_state = await get_sync_state(
                provider=provider,
                adjust=adjust,
                symbol=symbol,
                interval=interval,
            )
            if isinstance(sync_state, dict):
                local_start_bound = self._coerce_local_market_sync_bound(
                    sync_state.get("covered_start"),
                    interval=interval,
                    is_end=False,
                )
        if local_start_bound is None:
            local_start_bound = min(
                _query_bound(str(bar.get("timestamp")), interval=interval, is_end=False)
                for bar in existing_bars
            )
        full_start_bound = min(local_start_bound, start_bound)
        if interval == "1d":
            full_start = full_start_bound.date().isoformat()
        else:
            full_start = full_start_bound.isoformat()

        try:
            refreshed = await data_provider.get_bars(
                symbol,
                full_start,
                requested_end,
                interval=interval,
                adjust=adjust,
            )
            refresh_payloads = [_bar_dict(bar, interval=interval) for bar in refreshed]
            if not refresh_payloads:
                raise RuntimeError(
                    "local_market_adjust_drift_refresh_empty: upstream returned no bars "
                    f"for full refresh {full_start}..{requested_end} "
                    f"symbol={symbol} interval={interval} provider={provider} adjust={adjust}; "
                    "check upstream provider availability"
                )
            upserted_count = await self.market_bars_repository.upsert_bars(
                provider=provider,
                adjust=adjust,
                interval=interval,
                bars=refresh_payloads,
            )
            covered_start, covered_end = self._local_market_sync_payload_bounds(
                refresh_payloads,
                interval=interval,
            )
            await self._mark_local_market_sync_success(
                provider=provider,
                adjust=adjust,
                symbol=symbol,
                interval=interval,
                target_start=min(full_start_bound, start_bound),
                target_end=end_bound,
                covered_start=covered_start,
                covered_end=covered_end,
            )
        except Exception as exc:
            logger.exception(
                "local market adjust-drift full refresh failed symbol=%s interval=%s "
                "provider=%s adjust=%s range=%s..%s error_type=%s error=%s",
                symbol,
                interval,
                provider,
                adjust,
                full_start,
                requested_end,
                type(exc).__name__,
                exc,
            )
            raise

        logger.warning(
            "local market adjust drift detected; refreshed history wholesale "
            "symbol=%s interval=%s provider=%s adjust=%s mode=%s "
            "max_rel_deviation=%.6f overlap_count=%d refreshed=%s..%s upserted=%d",
            symbol,
            interval,
            provider,
            adjust,
            mode,
            report.max_rel_deviation,
            report.overlap_count,
            full_start,
            requested_end,
            upserted_count,
        )
        await emit_debug_event(
            "local_market_sync_adjust_drift_refreshed",
            {
                **report.as_payload(),
                "symbol": symbol,
                "interval": interval,
                "provider": provider,
                "adjust": adjust,
                "mode": mode,
                "refreshed_start": full_start,
                "refreshed_end": requested_end,
                "upserted_count": upserted_count,
                "hint": (
                    "ex-rights/dividend rescaled qfq history; "
                    "local warehouse refreshed wholesale"
                ),
            },
        )
        warnings.append("检测到复权因子变化（除权/除息），已自动全量重刷本地K线缓存")
        return {
            "fetched_segments": [
                {"start": full_start, "end": requested_end, "status": "fetched"}
            ],
            "upserted_count": upserted_count,
            "adjust_drift_refreshed": True,
            "warnings": warnings,
        }

    async def _mark_local_market_sync_success(
        self,
        *,
        provider: str,
        adjust: str,
        symbol: str,
        interval: str,
        target_start: datetime,
        target_end: datetime,
        covered_start: datetime,
        covered_end: datetime,
    ) -> None:
        mark_success = getattr(self.market_bars_repository, "mark_sync_success", None)
        get_sync_state = getattr(self.market_bars_repository, "get_sync_state", None)
        if not callable(mark_success):
            return
        merged_target_start = target_start
        merged_target_end = target_end
        merged_covered_start = covered_start
        merged_covered_end = covered_end
        if callable(get_sync_state):
            sync_state = await get_sync_state(
                provider=provider,
                adjust=adjust,
                symbol=symbol,
                interval=interval,
            )
            if isinstance(sync_state, dict):
                existing_target_start = self._coerce_local_market_sync_bound(
                    sync_state.get("target_start"),
                    interval=interval,
                    is_end=False,
                )
                existing_target_end = self._coerce_local_market_sync_bound(
                    sync_state.get("target_end"),
                    interval=interval,
                    is_end=True,
                )
                existing_start = self._coerce_local_market_sync_bound(
                    sync_state.get("covered_start"),
                    interval=interval,
                    is_end=False,
                )
                existing_end = self._coerce_local_market_sync_bound(
                    sync_state.get("covered_end"),
                    interval=interval,
                    is_end=True,
                )
                if existing_target_start is not None:
                    merged_target_start = min(merged_target_start, existing_target_start)
                if existing_target_end is not None:
                    merged_target_end = max(merged_target_end, existing_target_end)
                if existing_start is not None:
                    merged_covered_start = min(merged_covered_start, existing_start)
                if existing_end is not None:
                    merged_covered_end = max(merged_covered_end, existing_end)
        await mark_success(
            provider=provider,
            adjust=adjust,
            symbol=symbol,
            interval=interval,
            target_start=merged_target_start,
            target_end=merged_target_end,
            covered_start=merged_covered_start,
            covered_end=merged_covered_end,
        )

    async def _mark_local_market_sync_failure(
        self,
        *,
        provider: str,
        adjust: str,
        symbol: str,
        interval: str,
        requested_start: datetime,
        requested_end: datetime,
        exc: Exception,
    ) -> None:
        mark_failure = getattr(self.market_bars_repository, "mark_sync_failure", None)
        if not callable(mark_failure):
            return
        await mark_failure(
            provider=provider,
            adjust=adjust,
            symbol=symbol,
            interval=interval,
            target_start=requested_start,
            target_end=requested_end,
            error_code="local_market_sync_failed",
            error_type=type(exc).__name__,
            error_message=str(exc) or type(exc).__name__,
        )

    def _coerce_local_market_sync_bound(
        self,
        value: object,
        *,
        interval: str,
        is_end: bool,
    ) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value
        else:
            dt = _query_bound(str(value), interval=interval, is_end=is_end)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _local_market_sync_payload_bounds(
        self,
        bar_payloads: list[dict[str, Any]],
        *,
        interval: str,
    ) -> tuple[datetime, datetime]:
        if not bar_payloads:
            raise ValueError("bar_payloads must not be empty when computing sync payload bounds")
        covered_start: datetime | None = None
        covered_end: datetime | None = None
        for payload in bar_payloads:
            timestamp = str(payload.get("timestamp") or "").strip()
            if not timestamp:
                raise ValueError(f"market sync payload timestamp missing: {payload!r}")
            payload_start = _query_bound(timestamp, interval=interval, is_end=False)
            payload_end = _query_bound(timestamp, interval=interval, is_end=True)
            covered_start = payload_start if covered_start is None else min(covered_start, payload_start)
            covered_end = payload_end if covered_end is None else max(covered_end, payload_end)
        assert covered_start is not None and covered_end is not None
        return covered_start, covered_end

    async def _list_local_market_overlay_candidates(
        self,
        *,
        symbol: str,
        start: str,
        end: str,
    ) -> dict[str, list[dict[str, Any]]]:
        tasks = await self.task_repository.list_tasks()
        matching_tasks = [task for task in tasks if self._task_matches_symbol(task, symbol=symbol)]
        return {
            "backtest_trades": await self._list_backtest_overlay_candidates(
                symbol=symbol,
                start=start,
                end=end,
                tasks=matching_tasks,
            ),
            "task_fills": await self._list_task_fill_overlay_candidates(
                symbol=symbol,
                start=start,
                end=end,
                tasks=matching_tasks,
            ),
            "signals": await self._list_signal_overlay_candidates(
                symbol=symbol,
                start=start,
                end=end,
                tasks=matching_tasks,
            ),
        }

    async def get_local_market_overlays(
        self,
        *,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        overlay_kind: str,
        run_id: str | None = None,
        task_id: str | None = None,
        signal_source_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_interval = str(interval or "").strip().lower()
        if normalized_interval not in SUPPORTED_LOCAL_INTERVALS:
            raise ValueError(
                f"unsupported interval: {normalized_interval!r}; "
                f"supported: {sorted(SUPPORTED_LOCAL_INTERVALS)}"
            )
        _query_bound(start, interval=normalized_interval, is_end=False)
        _query_bound(end, interval=normalized_interval, is_end=True)

        normalized_kind = str(overlay_kind or "").strip().lower()
        if normalized_kind == "backtest_trades":
            return await self._get_backtest_trade_overlays(
                symbol=symbol,
                start=start,
                end=end,
                run_id=run_id,
            )
        if normalized_kind == "task_fills":
            return await self._get_task_fill_overlays(
                symbol=symbol,
                start=start,
                end=end,
                task_id=task_id,
            )
        if normalized_kind == "signals":
            return await self._get_signal_overlays(
                symbol=symbol,
                start=start,
                end=end,
                signal_source_id=signal_source_id or task_id,
            )
        raise ValueError(f"unsupported overlay_kind: {normalized_kind!r}")

    async def _list_backtest_overlay_candidates(
        self,
        *,
        symbol: str,
        start: str,
        end: str,
        tasks: list[Any],
    ) -> list[dict[str, Any]]:
        if self.run_repository is None:
            return []
        candidates: list[dict[str, Any]] = []
        for task in tasks:
            runs, _total = await self.run_repository.list_for_task(task.task_id, limit=50, offset=0)
            for run in runs:
                if str(run.get("mode") or "").strip().lower() != "backtest":
                    continue
                if not self._run_overlaps_window(run, start=start, end=end):
                    continue
                fills = await self._list_trade_fills_for_run(task.task_id, str(run.get("run_id") or ""), symbol=symbol)
                if not fills:
                    continue
                candidates.append(
                    {
                        "id": str(run.get("run_id") or ""),
                        "run_id": str(run.get("run_id") or ""),
                        "task_id": task.task_id,
                        "label": f"{task.name} · {run.get('run_id')}",
                        "status": run.get("status"),
                    }
                )
        return candidates

    async def _list_task_fill_overlay_candidates(
        self,
        *,
        symbol: str,
        start: str,
        end: str,
        tasks: list[Any],
    ) -> list[dict[str, Any]]:
        if self.run_repository is None:
            return []
        candidates: list[dict[str, Any]] = []
        for task in tasks:
            runs, _total = await self.run_repository.list_for_task(task.task_id, limit=50, offset=0)
            matching_run_count = 0
            for run in runs:
                if str(run.get("mode") or "").strip().lower() == "backtest":
                    continue
                fills = await self._list_trade_fills_for_run(task.task_id, str(run.get("run_id") or ""), symbol=symbol)
                fills = [row for row in fills if self._overlay_timestamp_in_range(row.get("filled_at"), start=start, end=end)]
                if fills:
                    matching_run_count += 1
            if matching_run_count:
                candidates.append(
                    {
                        "id": task.task_id,
                        "task_id": task.task_id,
                        "label": task.name,
                        "run_count": matching_run_count,
                    }
                )
        return candidates

    async def _list_signal_overlay_candidates(
        self,
        *,
        symbol: str,
        start: str,
        end: str,
        tasks: list[Any],
    ) -> list[dict[str, Any]]:
        if self.cycle_run_repository is None:
            return []
        candidates: list[dict[str, Any]] = []
        for task in tasks:
            cycles, _total = await self.cycle_run_repository.list_for_task(task.task_id, limit=100, offset=0)
            items = self._signal_overlay_items_from_cycle_runs(
                cycles,
                symbol=symbol,
                start=start,
                end=end,
            )
            if items:
                candidates.append(
                    {
                        "id": task.task_id,
                        "task_id": task.task_id,
                        "label": task.name,
                        "item_count": len(items),
                    }
                )
        return candidates

    async def _get_backtest_trade_overlays(
        self,
        *,
        symbol: str,
        start: str,
        end: str,
        run_id: str | None,
    ) -> dict[str, Any]:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id is required for backtest_trades overlays")
        if self.run_repository is None:
            raise RuntimeError("backtest runs unavailable")
        run = await self.run_repository.get(normalized_run_id)
        if run is None:
            raise RecordNotFoundError(f"backtest run not found: {normalized_run_id}")
        task = await self.task_repository.get_task(str(run.get("task_id") or ""))
        source = {
            "id": normalized_run_id,
            "run_id": normalized_run_id,
            "task_id": task.task_id,
            "label": f"{task.name} · {normalized_run_id}",
        }
        snapshot = empty_overlay_snapshot("backtest_trades", source)
        fills = await self._list_trade_fills_for_run(task.task_id, normalized_run_id, symbol=symbol)
        snapshot["items"] = [
            normalize_overlay_item(
                timestamp=str(row.get("filled_at") or ""),
                kind="trade_fill",
                side=str(row.get("side") or "").strip().lower() or None,
                price=self._coerce_overlay_price(row.get("price")),
                label=str(row.get("side") or "").strip().upper() or "TRADE",
                details={
                    "quantity": row.get("quantity"),
                    "intent_id": row.get("intent_id"),
                    "rationale": row.get("rationale"),
                    "cycle_run_id": row.get("cycle_run_id"),
                    "entry_tag": row.get("entry_tag"),
                    "exit_tag": row.get("exit_tag"),
                    "exit_reason": row.get("exit_reason"),
                    "source_mode": row.get("source_mode"),
                },
            )
            for row in fills
            if self._overlay_timestamp_in_range(row.get("filled_at"), start=start, end=end)
        ]
        return snapshot

    async def _get_task_fill_overlays(
        self,
        *,
        symbol: str,
        start: str,
        end: str,
        task_id: str | None,
    ) -> dict[str, Any]:
        normalized_task_id = str(task_id or "").strip()
        if not normalized_task_id:
            raise ValueError("task_id is required for task_fills overlays")
        if self.run_repository is None:
            raise RuntimeError("task fills unavailable")
        task = await self.task_repository.get_task(normalized_task_id)
        runs, _total = await self.run_repository.list_for_task(task.task_id, limit=100, offset=0)
        source = {
            "id": task.task_id,
            "task_id": task.task_id,
            "label": task.name,
        }
        snapshot = empty_overlay_snapshot("task_fills", source)
        items: list[dict[str, Any]] = []
        for run in runs:
            if str(run.get("mode") or "").strip().lower() == "backtest":
                continue
            fills = await self._list_trade_fills_for_run(task.task_id, str(run.get("run_id") or ""), symbol=symbol)
            for row in fills:
                if not self._overlay_timestamp_in_range(row.get("filled_at"), start=start, end=end):
                    continue
                items.append(
                    normalize_overlay_item(
                        timestamp=str(row.get("filled_at") or ""),
                        kind="trade_fill",
                        side=str(row.get("side") or "").strip().lower() or None,
                        price=self._coerce_overlay_price(row.get("price")),
                        label=str(row.get("side") or "").strip().upper() or "TRADE",
                        details={
                            "quantity": row.get("quantity"),
                            "intent_id": row.get("intent_id"),
                            "rationale": row.get("rationale"),
                            "cycle_run_id": row.get("cycle_run_id"),
                            "entry_tag": row.get("entry_tag"),
                            "exit_tag": row.get("exit_tag"),
                            "exit_reason": row.get("exit_reason"),
                            "run_id": row.get("run_id"),
                            "source_mode": row.get("source_mode"),
                        },
                    )
                )
        snapshot["items"] = sorted(items, key=lambda item: str(item.get("timestamp") or ""))
        return snapshot

    async def _get_signal_overlays(
        self,
        *,
        symbol: str,
        start: str,
        end: str,
        signal_source_id: str | None,
    ) -> dict[str, Any]:
        normalized_source_id = str(signal_source_id or "").strip()
        if not normalized_source_id:
            raise ValueError("signal_source_id is required for signals overlays")
        if self.cycle_run_repository is None:
            raise RuntimeError("signal overlays unavailable")
        task = await self.task_repository.get_task(normalized_source_id)
        cycles, _total = await self.cycle_run_repository.list_for_task(task.task_id, limit=200, offset=0)
        source = {
            "id": task.task_id,
            "task_id": task.task_id,
            "label": task.name,
        }
        snapshot = empty_overlay_snapshot("signals", source)
        snapshot["items"] = self._signal_overlay_items_from_cycle_runs(
            cycles,
            symbol=symbol,
            start=start,
            end=end,
        )
        return snapshot

    async def _list_trade_fills_for_run(
        self,
        task_id: str,
        run_id: str,
        *,
        symbol: str,
    ) -> list[dict[str, Any]]:
        if self.trade_fill_repository is None or not run_id:
            return []
        return await self.trade_fill_repository.list_for_task_run(
            task_id=task_id,
            run_id=run_id,
            symbol=symbol,
        )

    def _signal_overlay_items_from_cycle_runs(
        self,
        cycles: list[dict[str, Any]],
        *,
        symbol: str,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for cycle in cycles:
            timestamp = str(cycle.get("cycle_time") or cycle.get("wall_started_at") or "").strip()
            if not self._overlay_timestamp_in_range(timestamp, start=start, end=end):
                continue
            details = cycle.get("details")
            if not isinstance(details, dict):
                continue
            for decision in details.get("decisions") or []:
                if not isinstance(decision, dict):
                    continue
                if str(decision.get("symbol") or "").strip() != symbol:
                    continue
                side = str(decision.get("side") or decision.get("action") or "").strip().lower() or None
                label = str(decision.get("signal") or decision.get("label") or side or "signal").upper()
                items.append(
                    normalize_overlay_item(
                        timestamp=timestamp,
                        kind="signal",
                        side=side,
                        price=self._coerce_overlay_price(decision.get("price")),
                        label=label,
                        details={
                            "run_id": cycle.get("run_id"),
                            "task_id": cycle.get("task_id"),
                            "decision": dict(decision),
                        },
                    )
                )
        return sorted(items, key=lambda item: str(item.get("timestamp") or ""))

    def _task_matches_symbol(self, task: Any, *, symbol: str) -> bool:
        task_symbols = {str(value).strip() for value in (task.universe or ()) if str(value).strip()}
        settings = task.settings if isinstance(task.settings, dict) else {}
        watch_symbols = settings.get("watch_symbols") if isinstance(settings.get("watch_symbols"), list) else []
        task_symbols.update(str(value).strip() for value in watch_symbols if str(value).strip())
        return symbol in task_symbols

    def _run_overlaps_window(self, run: dict[str, Any], *, start: str, end: str) -> bool:
        run_start = str(run.get("range_start_utc") or "").strip()
        run_end = str(run.get("range_end_utc") or "").strip()
        if not run_start or not run_end:
            return False
        return not (
            _query_bound(run_end, interval="1d", is_end=True) < _query_bound(start, interval="1d", is_end=False)
            or _query_bound(run_start, interval="1d", is_end=False) > _query_bound(end, interval="1d", is_end=True)
        )

    def _overlay_timestamp_in_range(self, value: object, *, start: str, end: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        start_bound = _query_bound(start, interval="5m", is_end=False)
        end_bound = _query_bound(end, interval="5m", is_end=True)
        try:
            point = _query_bound(text, interval="5m", is_end=False)
        except ValueError:
            point = _query_bound(text, interval="1d", is_end=False)
        return start_bound <= point <= end_bound

    def _coerce_overlay_price(self, value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def _list_backtest_cycles_for_chart(self, task_id: str, run_row: dict) -> list[dict[str, Any]]:
        if self.cycle_run_repository is None:
            return []
        session_id = run_row.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return []
        items, _total = await self.cycle_run_repository.list_for_task(
            task_id,
            limit=BACKTEST_CHART_CYCLE_LIMIT,
            offset=0,
            session_id=session_id,
            run_mode="backtest",
        )
        return items

    async def pause_backtest_job(self, identifier: str, run_id: str) -> dict:
        if self.run_repository is None:
            raise RuntimeError("backtest jobs unavailable")
        record = await self.task_repository.get_task(identifier)
        row = await self.run_repository.get(run_id)
        if row is None or row.get("task_id") != record.task_id:
            raise RecordNotFoundError(f"backtest job not found: {run_id}")
        if self._backtest_pause_pending.get(run_id) or run_id in self._backtest_pause_waiters:
            raise RuntimeError("pause already in progress for this backtest job")
        if row.get("status") != "running":
            raise RuntimeError("only a running backtest job can be paused")
        task = self.backtest_tasks.get(run_id)
        if task is None:
            await self.run_repository.mark_paused(run_id)
            await emit_debug_event(
                "backtest_job_paused",
                {"run_id": run_id, "task_id": record.task_id},
            )
            updated = await self.run_repository.get(run_id)
            assert updated is not None
            return updated
        ev = asyncio.Event()
        self._backtest_pause_waiters[run_id] = ev
        self._backtest_pause_pending[run_id] = True
        try:
            await ev.wait()
        finally:
            self._backtest_pause_waiters.pop(run_id, None)
        updated = await self.run_repository.get(run_id)
        assert updated is not None
        if updated.get("status") == "paused":
            return updated
        if updated.get("status") == "failed":
            msg = (updated.get("error_message") or "backtest job failed").strip() or "backtest job failed"
            raise RuntimeError(msg)
        raise RuntimeError(f"backtest job could not be paused (status={updated.get('status')!r})")

    def _wake_backtest_pause_waiter_if_any(self, job_id: str) -> None:
        ev = self._backtest_pause_waiters.pop(job_id, None)
        if ev is not None and not ev.is_set():
            ev.set()

    async def resume_backtest_job(self, identifier: str, run_id: str) -> dict:
        if self.run_repository is None:
            raise RuntimeError("backtest jobs unavailable")
        record = await self.task_repository.get_task(identifier)
        row = await self.run_repository.get(run_id)
        if row is None or row.get("task_id") != record.task_id:
            raise RecordNotFoundError(f"backtest job not found: {run_id}")
        if row.get("status") != "paused":
            raise RuntimeError("only a paused backtest job can be resumed")
        if row.get("stop_requested"):
            raise RuntimeError("backtest job is stopping; cannot resume")
        await self.run_repository.mark_resumed(run_id)
        await self._ensure_backtest_task(run_id, record.task_id)
        await emit_debug_event(
            "backtest_job_resumed",
            {"run_id": run_id, "task_id": record.task_id},
        )
        updated = await self.run_repository.get(run_id)
        assert updated is not None
        return updated

    async def stop_backtest_job(self, identifier: str, run_id: str) -> dict:
        if self.run_repository is None:
            raise RuntimeError("backtest jobs unavailable")
        inst = await self.task_repository.get_task(identifier)
        row = await self.run_repository.get(run_id)
        if row is None or row.get("task_id") != inst.task_id:
            raise RecordNotFoundError(f"backtest job not found: {run_id}")
        status = row.get("status")
        if status in ("completed", "failed", "stopped"):
            raise RuntimeError("backtest job has already finished")
        await self.run_repository.set_stop_requested(run_id, True)
        self._wake_backtest_pause_waiter_if_any(run_id)
        ds_id = row.get("session_id") or ""
        if not isinstance(ds_id, str):
            ds_id = str(ds_id)
        task = self.backtest_tasks.get(run_id)
        if task is None:
            await self._finalize_backtest_job_cooperative_stop(
                job_id=run_id,
                task_id=inst.task_id,
                session_id=ds_id,
                reason="stopped",
            )
        out = await self.run_repository.get(run_id)
        assert out is not None
        return out

    async def _ensure_backtest_task(self, job_id: str, task_id: str) -> None:
        if job_id in self.backtest_tasks:
            return
        task = asyncio.create_task(
            self._run_backtest_job_body(job_id, task_id),
            name=f"doyoutrade-backtest-job-{job_id}",
        )
        self.backtest_tasks[job_id] = task
        task.add_done_callback(lambda _task, job_id=job_id: self.backtest_tasks.pop(job_id, None))

    async def restore_backtest_jobs(self) -> None:
        if self.run_repository is None:
            return
        rows = await self.run_repository.list_jobs_with_statuses(("running", "paused"))
        for row in rows:
            await self._ensure_backtest_task(row["run_id"], row["task_id"])
        if rows:
            await emit_debug_event(
                "backtest_jobs_restored",
                {"count": len(rows), "run_ids": [r["run_id"] for r in rows]},
            )

    async def _await_backtest_outer_unpause(self, job_id: str) -> bool:
        """Wait while status is ``paused``. Returns True if cooperative stop should run."""
        assert self.run_repository is not None
        while True:
            row = await self.run_repository.get(job_id)
            if row is None:
                return False
            if row.get("stop_requested"):
                return True
            if row.get("status") != "paused":
                return False
            await asyncio.sleep(0.25)

    async def start_backtest_job(
        self,
        identifier: str,
        *,
        range_start: str,
        range_end: str,
        market_profile: str | None = None,
        bar_interval: str | None = None,
        config_overrides: dict | None = None,
        model_route_name: str | None = None,
        debug_enabled: bool = True,
    ) -> dict:
        if self.run_repository is None or self.debug_session_repository is None:
            raise RuntimeError("backtest jobs unavailable")
        self.kill_switch_enabled = await self.system_state_repository.get_kill_switch_enabled()
        if self.kill_switch_enabled:
            raise RuntimeError("kill switch enabled")

        record = await self.task_repository.get_task(identifier)
        if record.mode != "backtest":
            raise ValueError("runs are only supported for backtest tasks")
        if record.status == "running":
            raise ValueError("pause or stop the task before running a backtest job (avoids overlapping ticks)")

        if await self.run_repository.has_any_job(record.task_id):
            raise ValueError("backtest task already has a run")

        active_debug = await self.debug_session_repository.get_active_debug_session(record.task_id)
        if active_debug is not None:
            raise RuntimeError(f"cannot start backtest while debug session active: {active_debug.session_id}")

        d0 = _parse_backtest_calendar_date(range_start, field_name="range_start")
        d1 = _parse_backtest_calendar_date(range_end, field_name="range_end")
        if d1 < d0:
            raise ValueError("range_end must be on or after range_start")

        profile = (market_profile or "cn_a_share").strip() or "cn_a_share"
        interval = (bar_interval or "1d").strip() or "1d"

        normalized_overrides = normalize_backtest_config_overrides(config_overrides)
        merged_cfg = build_cycle_task_config_with_backtest_overrides(record, normalized_overrides)
        # Strip @watchlist:<tag> references before catalog validation; they are
        # resolved to concrete symbols inside _build_worker below.
        _plain_universe, _ = split_universe_tokens(list(merged_cfg.universe))
        # data_provider=mock never touches the real instrument catalog (see
        # _mock_catalog_skip_reason) — skip the gate and emit a debug event
        # instead of silently dropping the check (§错误可见性: legitimate
        # skip must stay observable). Note: for a --definition auto-created
        # task this check is largely redundant with the one already done in
        # create_task's _ensure_new_task_catalog_symbols (same data_provider,
        # same universe) — kept here too so --task mode (an existing task
        # whose universe/data_provider changed after creation) is covered.
        _skip_reason = self._mock_catalog_skip_reason(merged_cfg.data_provider)
        if _skip_reason is not None:
            await emit_debug_event(
                "backtest_catalog_check_skipped",
                {"task_id": record.task_id, "reason": _skip_reason, "symbols": _plain_universe},
            )
        else:
            # tradable_only=True: a backtest universe must also exclude indices —
            # backtesting a non-tradable instrument is meaningless and keeps the
            # universe contract consistent with live tasks.
            await ensure_symbols_in_catalog(
                self.instrument_catalog_repository,
                _plain_universe,
                tradable_only=True,
            )
        effective_route = (model_route_name or "").strip()
        probe_ms = await self._resolve_worker_model_settings(
            merged_cfg,
            route_name_override=effective_route or None,
        )
        probe_acct = await self._resolve_worker_account(merged_cfg)
        probe_worker = await self._build_worker(merged_cfg, probe_ms, probe_acct)
        dates = await probe_worker.data_provider.get_trading_dates(d0.isoformat(), d1.isoformat())
        if not dates:
            raise ValueError("no trading days in the selected range")
        if len(dates) > BACKTEST_MAX_TRADING_DAYS:
            raise ValueError(
                f"trading days in range ({len(dates)}) exceed limit {BACKTEST_MAX_TRADING_DAYS}; narrow the date range",
            )
        cycle_times = await _build_backtest_cycle_times(
            worker=probe_worker,
            config=merged_cfg,
            range_start=d0,
            range_end=d1,
            interval=interval,
            trading_dates=list(dates),
        )
        if not cycle_times:
            if _is_intraday_backtest_interval(interval):
                raise ValueError(
                    f"no {interval} bars in the selected range for the task universe"
                )
            raise ValueError("no backtest bars in the selected range")

        job_id = f"btjob-{uuid.uuid4()}"
        # Fast (non-debug) backtests skip the debug session entirely: no
        # debug_sessions / debug_session_spans / span events / cycle_runs /
        # model_invocations are persisted (see _run_backtest_job_body). This is an
        # intentional, recorded relaxation (runs.debug_enabled + the log below),
        # not a silent drop of observability.
        debug_enabled = bool(debug_enabled)
        session_id: str | None = f"backtest-{uuid.uuid4()}" if debug_enabled else None
        range_start_utc = datetime(d0.year, d0.month, d0.day, 0, 0, 0)
        range_end_utc = datetime(d1.year, d1.month, d1.day, 0, 0, 0)

        if debug_enabled:
            await self.debug_session_repository.create_session(
                session_id=session_id,
                task_id=record.task_id,
                config_overrides=dict(normalized_overrides) if normalized_overrides is not None else None,
                input_overrides={
                    "run_id": job_id,
                    "range_start": range_start,
                    "range_end": range_end,
                    "market_profile": profile,
                    "bar_interval": interval,
                },
                session_type="backtest",
            )
        else:
            logger.info(
                "backtest run_id=%s task_id=%s debug disabled: fast mode, "
                "skipping debug_session/spans/cycle_runs/model_invocations",
                job_id,
                record.task_id,
            )

        # Run-time provenance snapshot:
        # - config_snapshot_json: full effective CycleTaskConfig so analytics
        #   can reproduce the run even after the source task is edited.
        # - engine_version: which worker/runner/compiler version produced it.
        # - strategy_code_hash: hash of the strategy source compiled at start.
        # - code_version: version label (e.g. "v0001-abc123ef") pinned at start;
        #   a concurrent finalize_strategy_authoring bump does not affect this run.
        effective_config = _serialize_config(merged_cfg)
        strategy_code_hash: str | None = None
        code_version: str | None = None
        if self.strategy_runtime is not None:
            defn = None
            if merged_cfg.strategy_definition_id:
                defn = await self.strategy_runtime.definition_repository.get_definition(
                    merged_cfg.strategy_definition_id
                )
            if defn is not None:
                strategy_code_hash = defn.code_hash
                code_version = defn.current_version

        await self.run_repository.create_pending(
            run_id=job_id,
            task_id=record.task_id,
            mode="backtest",
            market_profile=profile,
            bar_interval=interval,
            range_start_utc=range_start_utc,
            range_end_utc=range_end_utc,
            session_id=session_id,
            bars_total=len(cycle_times),
            debug_enabled=debug_enabled,
            config_overrides_json=dict(normalized_overrides) if normalized_overrides is not None else None,
            model_route_name=effective_route or None,
            config_snapshot_json=effective_config,
            engine_version=_engine_version(),
            strategy_code_hash=strategy_code_hash,
            code_version=code_version,
        )
        if debug_enabled and session_id is not None:
            await self.debug_session_repository.mark_running(
                session_id,
                run_id=None,
                effective_config=effective_config,
            )
        await self.run_repository.mark_running(job_id)

        await emit_debug_event(
            "backtest_job_started",
            {
                "run_id": job_id,
                "task_id": record.task_id,
                "session_id": session_id,
                "has_config_overrides": bool(normalized_overrides),
                "config_override_keys": sorted(normalized_overrides.keys()) if normalized_overrides else [],
                "model_route_name": effective_route or None,
            },
        )

        await self._ensure_backtest_task(job_id, record.task_id)

        row = await self.run_repository.get(job_id)
        if row is None:
            raise RuntimeError(
                f"backtest run row missing immediately after create_pending: {job_id}"
            )
        return row

    async def _finalize_backtest_job_cooperative_stop(
        self,
        *,
        job_id: str,
        task_id: str,
        session_id: str,
        reason: str,
    ) -> None:
        assert self.run_repository is not None
        assert self.debug_session_repository is not None
        await self.run_repository.finalize_stopped(job_id)
        with suppress(Exception):
            await self.debug_session_repository.mark_finished(
                session_id,
                status="stopped",
                error_message="",
            )
        await drain_debug_span_persist_queue()
        await emit_debug_event(
            "backtest_job_stopped",
            {"run_id": job_id, "task_id": task_id, "message": reason},
        )

    async def _collect_backtest_fills(
        self,
        *,
        run_id: str,
        task_id: str,
        fills_buffer: list[backtest_summary.FillRecord],
        report: CycleReport | None = None,
    ) -> None:
        """Append the latest cycle's fills into ``fills_buffer``.

        Prefers ``report.fills`` (always available, including fast mode where
        cycle_runs are not persisted); falls back to ``cycle_runs.details.fills``
        when a report is not supplied.
        """

        if report is not None:
            for raw in report.fills or []:
                fr = _backtest_fill_record_from_details(raw, run_id_fallback=run_id or "")
                if fr is not None:
                    fills_buffer.append(fr)
            return

        if not run_id or self.cycle_run_repository is None:
            return
        try:
            row = await self.cycle_run_repository.get_for_task(task_id, run_id)
        except Exception:
            return
        if not isinstance(row, dict):
            return
        details = row.get("details")
        if not isinstance(details, dict):
            return
        for raw in details.get("fills") or []:
            fr = _backtest_fill_record_from_details(raw, run_id_fallback=run_id)
            if fr is not None:
                fills_buffer.append(fr)

    async def _resolve_strategy_startup_history(
        self,
        *,
        strategy_definition_id: str | None = None,
        emit_failure_event: bool = False,
    ) -> int | None:
        """Look up ``Strategy.startup_history`` for the definition backing
        the current run.

        Sourced from ``StrategyDefinitionSnapshot.capabilities_json`` (set
        by :func:`StrategyCompiler._describe_strategy`). Returns ``None``
        when the lookup is not possible — never raises, never falls back
        to a magic default. Callers treat ``None`` as "no warmup signal";
        the warmup anomaly check fails open in that case rather than
        fabricating a flag.

        The binding is resolved purely from ``strategy_definition_id``
        (StrategyInstance / ``si-`` bindings were removed).

        Failure modes are logged with the exception type so an operator
        can tell the difference between "no runtime configured" and
        "definition missing / capabilities malformed".

        When ``emit_failure_event=True`` *and* ``strategy_definition_id``
        was provided, every failure path also emits a structured
        ``backtest_startup_history_unresolved`` debug event with a
        ``reason`` field, the ``strategy_definition_id``, and a ``hint``
        describing the operator fix. The cache preload path opts into
        this so genuine resolution failures become trace-grep-able
        instead of disappearing into a stdout warning. The summary
        path leaves it off (default) so a single backtest doesn't
        produce two identical events.
        """

        resolved_definition_id = (strategy_definition_id or "").strip()
        if not resolved_definition_id:
            return None
        if self.strategy_runtime is None:
            logger.error(
                "backtest startup_history: strategy_runtime not configured "
                "definition_id=%r",
                resolved_definition_id,
            )
            if emit_failure_event:
                await self._emit_startup_history_unresolved(
                    resolved_definition_id,
                    reason="strategy_runtime_not_configured",
                    hint=(
                        "strategy_runtime is not wired on this service instance "
                        "(read-only deployment?); preload falls back to the legacy "
                        f"{BACKTEST_BARS_CACHE_EXPANSION_DAYS}-day expansion."
                    ),
                )
            return None
        definition_repo = getattr(self.strategy_runtime, "definition_repository", None)
        if definition_repo is None:
            logger.error(
                "backtest startup_history: definition_repository missing "
                "definition_id=%r",
                resolved_definition_id,
            )
            if emit_failure_event:
                await self._emit_startup_history_unresolved(
                    resolved_definition_id,
                    reason="strategy_runtime_repository_missing",
                    hint=(
                        "strategy_runtime is missing definition_repository — "
                        "fix bootstrap wiring."
                    ),
                )
            return None
        try:
            defn = await definition_repo.get_definition(resolved_definition_id)
        except Exception as exc:  # noqa: BLE001 — diagnostic logging only
            logger.exception(
                "backtest startup_history: strategy_definition lookup failed for "
                "definition_id=%r error_type=%s error=%s",
                resolved_definition_id,
                type(exc).__name__,
                exc,
            )
            if emit_failure_event:
                await self._emit_startup_history_unresolved(
                    resolved_definition_id,
                    reason="definition_lookup_failed",
                    exc_type=type(exc).__name__,
                    exc_message=str(exc)[:500],
                    hint=(
                        "the sd-... referenced by this binding was not found — "
                        "the definition may have been deleted or the link is stale."
                    ),
                )
            return None
        definition_id = getattr(defn, "definition_id", resolved_definition_id)
        caps = defn.capabilities_json if isinstance(defn.capabilities_json, dict) else None
        if not caps:
            # Stored capability snapshot is empty (older definitions never
            # persisted it). Rather than fall back to the legacy 21-day
            # expansion — which silently under-preloads and forces a per-bar
            # cache miss / live re-fetch for the warmup window — compile the
            # source and read the authoritative ``Strategy.startup_history``.
            fallback = self._startup_history_from_compiled_definition(defn)
            if fallback is not None:
                logger.info(
                    "backtest startup_history: capabilities empty for "
                    "definition_id=%r; resolved startup_history=%d via compile "
                    "fallback",
                    definition_id,
                    fallback,
                )
                return fallback
            logger.error(
                "backtest startup_history: capabilities missing and compile fallback "
                "failed definition_id=%r",
                definition_id,
            )
            if emit_failure_event:
                await self._emit_startup_history_unresolved(
                    definition_id,
                    reason="capabilities_missing",
                    hint=(
                        "definition.capabilities_json is empty and the compile "
                        "fallback could not read Strategy.startup_history — re-run "
                        "`doyoutrade-cli strategy definition update <sd-...>` so the "
                        "compiler re-derives the capability map."
                    ),
                )
            return None
        raw = caps.get("startup_history")
        if raw is None:
            fallback = self._startup_history_from_compiled_definition(defn)
            if fallback is not None:
                logger.info(
                    "backtest startup_history: `startup_history` field missing "
                    "for definition_id=%r; resolved startup_history=%d via "
                    "compile fallback",
                    definition_id,
                    fallback,
                )
                return fallback
            logger.error(
                "backtest startup_history: field missing and compile fallback failed "
                "definition_id=%r",
                definition_id,
            )
            if emit_failure_event:
                await self._emit_startup_history_unresolved(
                    definition_id,
                    reason="startup_history_field_missing",
                    hint=(
                        "definition.capabilities_json has no `startup_history` field "
                        "and the compile fallback could not read it — re-run "
                        "`doyoutrade-cli strategy definition update <sd-...>` "
                        "to refresh capabilities, and confirm the Strategy subclass "
                        "declares the class attribute."
                    ),
                )
            return None
        if isinstance(raw, bool):
            # ``True`` / ``False`` is not a sensible startup_history; reject
            # rather than silently coercing to 1 / 0.
            logger.error(
                "backtest startup_history: capability for definition_id=%r "
                "is bool=%r — treating as unknown",
                definition_id,
                raw,
            )
            if emit_failure_event:
                await self._emit_startup_history_unresolved(
                    definition_id,
                    reason="startup_history_bool",
                    raw_value=raw,
                    hint=(
                        "`startup_history` was serialized as a bool — most likely a "
                        "strategy_sdk bug; check the Strategy subclass declaration."
                    ),
                )
            return None
        if isinstance(raw, int):
            if raw > 0:
                return int(raw)
            logger.error(
                "backtest startup_history: non-positive int for definition_id=%r "
                "raw_value=%r",
                definition_id,
                raw,
            )
            if emit_failure_event:
                await self._emit_startup_history_unresolved(
                    definition_id,
                    reason="startup_history_non_positive",
                    raw_value=raw,
                    hint="`startup_history` must be a positive int.",
                )
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.error(
                "backtest startup_history: capability for definition_id=%r "
                "is not int-like (type=%s value=%r)",
                definition_id,
                type(raw).__name__,
                raw,
            )
            if emit_failure_event:
                await self._emit_startup_history_unresolved(
                    definition_id,
                    reason="startup_history_type_invalid",
                    raw_value=repr(raw)[:200],
                    raw_type=type(raw).__name__,
                    hint=(
                        "`startup_history` must be int-like — check the Strategy "
                        "subclass declaration and the compiler's capability extractor."
                    ),
                )
            return None
        if value > 0:
            return value
        logger.error(
            "backtest startup_history: non-positive parsed value for "
            "definition_id=%r raw_value=%r",
            definition_id,
            value,
        )
        if emit_failure_event:
            await self._emit_startup_history_unresolved(
                definition_id,
                reason="startup_history_non_positive",
                raw_value=value,
                hint="`startup_history` must be a positive int.",
            )
        return None

    def _startup_history_from_compiled_definition(self, defn: Any) -> int | None:
        """Authoritative fallback for ``startup_history`` when the stored
        capability snapshot is empty/stale: compile the definition's source
        and read the ``Strategy.startup_history`` class attribute (the same
        value the live runner uses).

        Returns ``None`` — never raises — on any failure (no runtime, no
        version, compile error, missing/invalid attribute), so the caller's
        legacy expansion still applies. Failures are logged with the
        exception type so the difference between "compiled but no attribute"
        and "compile failed" stays visible.
        """
        runtime = self.strategy_runtime
        if runtime is None:
            return None
        try:
            code_version = getattr(defn, "current_version", None)
            if not code_version:
                return None
            code_root = runtime.storage.version_dir(defn.definition_id, code_version)
            compile_result = runtime.compiler.validate_directory(code_root)
            if not compile_result.success or compile_result.artifact is None:
                logger.error(
                    "backtest startup_history: compile fallback failed to compile "
                    "definition_id=%r version=%r errors=%s",
                    getattr(defn, "definition_id", None),
                    code_version,
                    "; ".join(compile_result.errors) if compile_result.errors else "",
                )
                return None
            value = getattr(
                compile_result.artifact.strategy_class, "startup_history", None
            )
            if isinstance(value, bool):
                return None
            if isinstance(value, int) and value > 0:
                return int(value)
            return None
        except Exception as exc:  # noqa: BLE001 — diagnostic fallback only
            logger.exception(
                "backtest startup_history: compile fallback raised for "
                "definition_id=%r error_type=%s error=%s",
                getattr(defn, "definition_id", None),
                type(exc).__name__,
                exc,
            )
            return None

    async def _emit_startup_history_unresolved(
        self,
        strategy_definition_id: str,
        *,
        reason: str,
        hint: str,
        **extra: Any,
    ) -> None:
        """Surface a structured ``backtest_startup_history_unresolved``
        debug event so genuine preload failures are trace-grep-able.

        Set ``span`` attributes on the active span too, so OTel-side
        consumers see the same diagnostic without parsing the event
        payload.
        """

        payload: dict[str, Any] = {
            "strategy_definition_id": strategy_definition_id,
            "reason": reason,
            "hint": hint,
        }
        payload.update(extra)
        await emit_debug_event("backtest_startup_history_unresolved", payload)
        span = trace_api.get_current_span()
        if span.is_recording():
            span.set_attribute("backtest.startup_history_unresolved", True)
            span.set_attribute("backtest.startup_history_unresolved_reason", reason)

    async def _persist_backtest_summary(
        self,
        *,
        job_id: str,
        task_id: str,
        run_id: str,
        range_start_utc: datetime | None,
        range_end_utc: datetime | None,
        bar_interval: str,
        starting_equity: Decimal,
        end_snapshot: AccountSnapshot,
        end_positions: list[PositionSnapshot],
        equity_history: tuple[backtest_summary.EquityPoint, ...],
        fills: tuple[backtest_summary.FillRecord, ...],
        trading_dates: tuple[str, ...],
        bars_total: int | None = None,
        status: str,
        last_error: str | None,
        symbol_to_price: dict[str, Any] | None = None,
        strategy_definition_id: str | None = None,
    ) -> None:
        """Compute the backtest summary and write it to ``tasks.backtest_summary``.

        The function is best-effort; failures are logged via ``emit_debug_event`` but
        never abort the surrounding finalize path. Status is left as-is when computing
        the summary itself raises so the loop's existing ``run_repository`` writes are
        not undone.

        ``strategy_definition_id`` is optional — when present, the function
        looks up ``Strategy.startup_history`` from the definition's
        ``capabilities_json`` and persists it on the summary so the
        warmup-insufficient anomaly check can fire. When absent (or the
        runtime cannot resolve it) the field stays ``None`` and the
        anomaly check fails open.
        """

        startup_history = await self._resolve_strategy_startup_history(
            strategy_definition_id=strategy_definition_id,
        )
        bars_total_count = int(bars_total) if bars_total is not None else len(trading_dates)

        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        with tracer.start_as_current_span("backtest.summary.compute") as span:
            span.set_attribute("task_id", task_id)
            span.set_attribute("run_id", run_id)
            span.set_attribute("job_id", job_id)
            span.set_attribute("status", status)
            span.set_attribute("bars_total", bars_total_count)
            span.set_attribute("bars_completed", len(equity_history))
            span.set_attribute("fills_count", len(fills))
            if startup_history is not None:
                span.set_attribute("startup_history", int(startup_history))
            payload: dict[str, Any] | None = None
            try:
                summary_obj = backtest_summary.compute_summary(
                    run_id=run_id,
                    backtest_job_id=job_id,
                    range_start_utc=range_start_utc or now_utc,
                    range_end_utc=range_end_utc or now_utc,
                    bar_interval=bar_interval,
                    starting_equity=starting_equity,
                    ending_equity=Decimal(end_snapshot.equity),
                    final_cash=Decimal(end_snapshot.cash),
                    final_positions=_backtest_final_positions(
                        end_positions, symbol_to_price=symbol_to_price
                    ),
                    equity_history=equity_history,
                    fills=fills,
                    trading_dates=trading_dates,
                    completed_at=now_utc,
                    startup_history=startup_history,
                    bars_total=bars_total_count,
                )
                payload = backtest_summary.summary_to_json(summary_obj)
                task_record = await self.task_repository.get_task(task_id)
                payload["data_provider"] = task_record.data_provider
                payload["data_provider_effective"] = resolve_effective_provider(
                    task_record.data_provider,
                    self.default_data_provider,
                )
                span.set_attribute("closed_trades", int(payload.get("trade_count_closed") or 0))
                span.set_attribute("open_trades", int(payload.get("trade_count_open") or 0))
                span.set_attribute("max_drawdown_pct", str(payload.get("max_drawdown_pct") or "0"))
                debug_payload = {k: v for k, v in payload.items() if k != "equity_curve"}
                await emit_debug_event("backtest_summary", debug_payload)
                # The legacy ``backtest_summary_warmup_insufficient`` event
                # was driven by ``bars_total < startup_history`` — a
                # predicate that mistook the user's report-window length
                # for a preload failure (see ``summary_warmup_insufficient``
                # in doyoutrade.backtest.summary for the full rationale).
                # The authoritative preload-failure signal is the SDK
                # runner's per-cycle ``strategy_base_history_insufficient``
                # debug event; the summary-compute span no longer fabricates
                # a duplicate event from the wrong predicate.
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)[:500]))
                await emit_debug_event(
                    "backtest_summary_compute_error",
                    {
                        "run_id": run_id,
                        "task_id": task_id,
                        "job_id": job_id,
                        "error": str(exc),
                    },
                )
                payload = None
            try:
                await self.task_repository.update_backtest_summary_and_status(
                    task_id,
                    summary=payload,
                    status=status,
                    last_error=last_error,
                )
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)[:500]))
                await emit_debug_event(
                    "backtest_summary_persist_error",
                    {
                        "run_id": run_id,
                        "task_id": task_id,
                        "job_id": job_id,
                        "error": str(exc),
                    },
                )
        # 功能 5: extract decision signals from the finished run and verify them
        # against cached bars. Best-effort — never blocks backtest finalize.
        if status == "completed":
            await self._persist_decision_signals_from_run(
                job_id=job_id,
                task_id=task_id,
                run_id=run_id,
                fills=fills,
            )

    async def _persist_decision_signals_from_run(
        self,
        *,
        job_id: str,
        task_id: str,
        run_id: str,
        fills: tuple,
        horizon: str = "5d",
    ) -> None:
        """功能 5 hook: persist ``decision_signals`` rows from a completed backtest.

        Source of truth is the run's executed fills (``backtest_summary.FillRecord``
        — the structured BUY/SELL signal stream already extracted from
        ``cycle_runs.details.fills``), so direction/price/cycle attribution come
        for free. Each fill maps to one signal (``create_if_absent`` dedupes on
        ``run_id + symbol + action + horizon``); newly created signals are then
        evaluated against cached bars when enough post-anchor history exists.

        Best-effort: any failure emits ``decision_signal.persist_failed`` +
        ``logger.warning`` and returns — the backtest finalize path is never
        aborted. Success emits ``decision_signal.persisted`` and
        ``decision_signal.outcome_evaluated`` counts.
        """
        repo = self.decision_signal_repository
        if repo is None:
            if not self._decision_signal_skip_logged:
                logger.info(
                    "decision signal persistence skipped: decision_signal_repository "
                    "not wired (run_id=%s task_id=%s)",
                    run_id,
                    task_id,
                )
                self._decision_signal_skip_logged = True
            return
        if not fills:
            return
        from doyoutrade.backtest import decision_signal_eval

        try:
            with tracer.start_as_current_span("backtest.decision_signals.persist") as span:
                span.set_attribute("run_id", run_id)
                span.set_attribute("task_id", task_id)
                span.set_attribute("job_id", job_id)
                span_ctx = trace_api.get_current_span().get_span_context()
                trace_id = (
                    format(span_ctx.trace_id, "032x") if span_ctx.trace_id else None
                )
                horizon_days = decision_signal_eval.parse_horizon_days(horizon)
                try:
                    task_record = await self.task_repository.get_task(task_id)
                    provider_effective = resolve_effective_provider(
                        task_record.data_provider, self.default_data_provider
                    )
                except RecordNotFoundError:
                    provider_effective = resolve_effective_provider(
                        None, self.default_data_provider
                    )

                created_signals = []
                deduped_count = 0
                for fill in fills:
                    side = str(fill.side).lower()
                    if side == "sell" and fill.exit_reason in ("take_profit", "stop_loss"):
                        action = fill.exit_reason
                    else:
                        action = side
                    anchor_date = fill.timestamp.date().isoformat()
                    reason = fill.entry_tag if side == "buy" else (fill.exit_tag or fill.exit_reason)
                    snapshot, created = await repo.create_if_absent(
                        task_id=task_id,
                        run_id=run_id,
                        cycle_run_id=fill.cycle_run_id,
                        trace_id=trace_id,
                        source="backtest",
                        symbol=fill.symbol,
                        action=action,
                        horizon=horizon,
                        reason=str(reason) if reason else None,
                        metadata_json={
                            "anchor_date": anchor_date,
                            "fill_price": str(fill.price),
                            "job_id": job_id,
                        },
                    )
                    if created:
                        created_signals.append(snapshot)
                    else:
                        deduped_count += 1
                span.set_attribute("signals_created", len(created_signals))
                span.set_attribute("signals_deduped", deduped_count)
                await emit_debug_event(
                    "decision_signal.persisted",
                    {
                        "run_id": run_id,
                        "task_id": task_id,
                        "job_id": job_id,
                        "count": len(created_signals),
                        "deduped": deduped_count,
                        "source": "backtest",
                        "horizon": horizon,
                    },
                )

                evaluated_count = 0
                for snapshot in created_signals:
                    try:
                        result = await self._evaluate_signal_against_cached_bars(
                            snapshot,
                            horizon_label=horizon,
                            horizon_days=horizon_days,
                            provider=provider_effective,
                        )
                        if result is not None:
                            evaluated_count += 1
                    except Exception as eval_exc:  # visible per-signal failure, keep going
                        logger.warning(
                            "decision signal outcome eval failed signal_id=%s run_id=%s "
                            "symbol=%s error_type=%s error=%s",
                            snapshot.id,
                            run_id,
                            snapshot.symbol,
                            type(eval_exc).__name__,
                            eval_exc,
                        )
                        await emit_debug_event(
                            "decision_signal.outcome_failed",
                            {
                                "run_id": run_id,
                                "signal_id": snapshot.id,
                                "symbol": snapshot.symbol,
                                "error_type": type(eval_exc).__name__,
                                "error": str(eval_exc),
                                "hint": "check cached_bars coverage and signal price fields",
                            },
                        )
                span.set_attribute("outcomes_evaluated", evaluated_count)
                await emit_debug_event(
                    "decision_signal.outcome_evaluated",
                    {
                        "run_id": run_id,
                        "task_id": task_id,
                        "count": evaluated_count,
                        "of_signals": len(created_signals),
                        "horizon": horizon,
                    },
                )
        except Exception as exc:
            logger.warning(
                "decision signal persistence failed run_id=%s task_id=%s "
                "error_type=%s error=%s",
                run_id,
                task_id,
                type(exc).__name__,
                exc,
            )
            await emit_debug_event(
                "decision_signal.persist_failed",
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "job_id": job_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "hint": (
                        "backtest finalize is unaffected; re-run evaluation via "
                        "POST /decision-signals/{id}/evaluate once the cause is fixed"
                    ),
                },
            )

    async def _evaluate_signal_against_cached_bars(
        self,
        snapshot,
        *,
        horizon_label: str,
        horizon_days: int,
        provider: str,
    ):
        """Feed cached bars into the pure evaluator and persist the outcome.

        Returns the outcome snapshot, or ``None`` when data was insufficient
        (skip is emitted as a structured ``decision_signal.outcome_skipped``
        event — never silent). Raises on real failures; the caller decides
        whether that aborts (API evaluate) or is contained (backtest hook).
        """
        from doyoutrade.backtest import decision_signal_eval

        repo = self.decision_signal_repository
        if repo is None:
            raise RuntimeError("decision_signal_repository is not configured")
        metadata = snapshot.metadata_json or {}
        anchor_date = str(metadata.get("anchor_date") or "")[:10]
        if len(anchor_date) != 10:
            anchor_date = snapshot.created_at.date().isoformat()
        bars: list[dict] = []
        if self.cached_bars_repository is not None:
            # Calendar buffer: horizon trading days ≈ horizon*2 calendar days,
            # plus slack for holidays/suspensions.
            end_date = (
                datetime.fromisoformat(anchor_date) + timedelta(days=horizon_days * 3 + 15)
            ).date().isoformat()
            bars = await self.cached_bars_repository.bars_in_range(
                provider=provider,
                symbol=snapshot.symbol,
                interval="1d",
                start=anchor_date,
                end=end_date,
            )
        result = decision_signal_eval.evaluate_decision_signal(
            {
                "action": snapshot.action,
                "anchor_date": anchor_date,
                "horizon": horizon_label,
                "target_price": snapshot.target_price,
                "stop_loss": snapshot.stop_loss,
            },
            bars,
            horizon_days=horizon_days,
        )
        if result.get("outcome") is None:
            logger.info(
                "decision signal outcome skipped signal_id=%s symbol=%s reason=%s "
                "bars_available=%s bars_required=%s",
                snapshot.id,
                snapshot.symbol,
                result.get("reason"),
                result.get("bars_available"),
                result.get("bars_required"),
            )
            await emit_debug_event(
                "decision_signal.outcome_skipped",
                {
                    "signal_id": snapshot.id,
                    "run_id": snapshot.run_id,
                    "symbol": snapshot.symbol,
                    "reason": str(result.get("reason") or "data_insufficient"),
                    "bars_available": result.get("bars_available"),
                    "bars_required": result.get("bars_required"),
                    "provider": provider,
                    "hint": "backfill cached bars for the post-anchor window, then re-evaluate",
                },
            )
            return None
        outcome = await repo.upsert_outcome(
            signal_id=snapshot.id,
            horizon=str(result["horizon"]),
            engine_version=str(result["engine_version"]),
            outcome=str(result["outcome"]),
            direction_expected=str(result["direction_expected"]),
            direction_correct=result.get("direction_correct"),
            anchor_date=str(result["anchor_date"]),
            eval_window_days=int(result["eval_window_days"]),
            entry_price=result.get("entry_price"),
            exit_price=result.get("exit_price"),
            max_gain_pct=result.get("max_gain_pct"),
            max_drawdown_pct=result.get("max_drawdown_pct"),
            return_pct=result.get("return_pct"),
        )
        if snapshot.status == "active":
            await repo.update_status(snapshot.id, "evaluated")
        return outcome

    # ── Decision signal API surface (thin service methods) ────────────────

    def _require_decision_signal_repo(self):
        if self.decision_signal_repository is None:
            raise RuntimeError("decision_signal_repository is not configured")
        return self.decision_signal_repository

    @staticmethod
    def _decision_signal_to_dict(snapshot) -> dict:
        payload = asdict(snapshot)
        for key in ("created_at", "updated_at", "expires_at"):
            value = payload.get(key)
            if isinstance(value, datetime):
                payload[key] = value.isoformat()
        return payload

    @staticmethod
    def _decision_outcome_to_dict(snapshot) -> dict:
        payload = asdict(snapshot)
        value = payload.get("created_at")
        if isinstance(value, datetime):
            payload["created_at"] = value.isoformat()
        return payload

    async def list_decision_signals(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        symbol: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        repo = self._require_decision_signal_repo()
        expired = await repo.expire_due_signals()
        if expired:
            logger.info("decision signals lazily expired count=%s", expired)
            await emit_debug_event("decision_signal.expired", {"count": expired})
        signals, total = await repo.list_signals(
            task_id=task_id,
            run_id=run_id,
            symbol=symbol,
            status=status,
            limit=limit,
            offset=offset,
        )
        return {
            "items": [self._decision_signal_to_dict(s) for s in signals],
            "total": total,
            "expired_now": expired,
        }

    async def get_decision_signal(self, signal_id: str) -> dict:
        """Signal + its outcomes. Raises ``RecordNotFoundError`` when missing."""
        repo = self._require_decision_signal_repo()
        snapshot = await repo.get_signal(signal_id)
        outcomes = await repo.list_outcomes(signal_id)
        payload = self._decision_signal_to_dict(snapshot)
        payload["outcomes"] = [self._decision_outcome_to_dict(o) for o in outcomes]
        return payload

    async def evaluate_decision_signal(
        self,
        signal_id: str,
        *,
        horizon: str | None = None,
        provider: str | None = None,
    ) -> dict:
        """On-demand outcome evaluation: cached bars → eval → upsert outcome.

        Raises ``RecordNotFoundError`` (unknown id) and ``ValueError`` (bad
        horizon / malformed signal price fields) for the API layer to map to
        404/400. Data insufficiency is a structured 200 skip, not an error.
        """
        from doyoutrade.backtest import decision_signal_eval

        repo = self._require_decision_signal_repo()
        snapshot = await repo.get_signal(signal_id)
        horizon_label = str(horizon or snapshot.horizon or "5d")
        horizon_days = decision_signal_eval.parse_horizon_days(horizon_label)
        if provider:
            provider_effective = resolve_effective_provider(
                provider, self.default_data_provider
            )
        else:
            requested = None
            if snapshot.task_id:
                try:
                    task_record = await self.task_repository.get_task(snapshot.task_id)
                    requested = task_record.data_provider
                except RecordNotFoundError:
                    requested = None
            provider_effective = resolve_effective_provider(
                requested, self.default_data_provider
            )
        outcome = await self._evaluate_signal_against_cached_bars(
            snapshot,
            horizon_label=horizon_label,
            horizon_days=horizon_days,
            provider=provider_effective,
        )
        if outcome is None:
            return {
                "status": "skipped",
                "reason": "data_insufficient",
                "signal_id": signal_id,
                "horizon": horizon_label,
                "provider": provider_effective,
            }
        await emit_debug_event(
            "decision_signal.outcome_evaluated",
            {
                "signal_id": signal_id,
                "run_id": snapshot.run_id,
                "symbol": snapshot.symbol,
                "count": 1,
                "horizon": horizon_label,
                "outcome": outcome.outcome,
            },
        )
        return {
            "status": "ok",
            "signal_id": signal_id,
            "outcome": self._decision_outcome_to_dict(outcome),
        }

    async def _run_backtest_job_body(self, job_id: str, task_id: str) -> None:
        assert self.run_repository is not None
        assert self.debug_session_repository is not None
        debug_session_id = ""
        # Set when this run executes in fast (non-debug) mode; reset in finally.
        _obs_token = None
        bt_summary_equity_history: list[backtest_summary.EquityPoint] = []
        bt_summary_fills_buffer: list[backtest_summary.FillRecord] = []
        bt_summary_range_start_utc: datetime | None = None
        bt_summary_range_end_utc: datetime | None = None
        bt_summary_bar_interval = "1d"
        bt_summary_dates: list[str] = []
        bt_summary_starting_equity = Decimal("0")

        try:
            job_row = await self.run_repository.get(job_id)
            if job_row is None:
                return
            ds_raw = job_row.get("session_id")
            debug_session_id = str(ds_raw) if ds_raw is not None else ""
            debug_enabled = bool(job_row.get("debug_enabled", True))
            # Fast mode: short-circuit debug observability persistence/serialization
            # (cycle_runs / model_invocations / span events) for the whole loop.
            # Span export to debug_session_spans is additionally gated below by not
            # entering debug_span_export_for_session. Business logic is untouched.
            if not debug_enabled:
                _obs_token = debug_observability_enabled.set(False)
            # Debug mode persists overrides on the debug session; fast mode (no
            # session) recovers them from the run row so the merged config is
            # identical on resume.
            if debug_enabled and debug_session_id:
                ds_row = await self.debug_session_repository.get_session(debug_session_id)
                bt_config_overrides = ds_row.config_overrides
            else:
                debug_session_id = ""
                bt_config_overrides = job_row.get("config_overrides_json")

            record = await self.task_repository.get_task(task_id)
            merged_cfg = build_cycle_task_config_with_backtest_overrides(
                record,
                bt_config_overrides,
            )
            jr = job_row.get("model_route_name")
            job_route = jr.strip() if isinstance(jr, str) and jr.strip() else ""
            run_ms = await self._resolve_worker_model_settings(
                merged_cfg,
                route_name_override=job_route or None,
            )
            run_acct = await self._resolve_worker_account(merged_cfg)
            worker = await self._build_worker(merged_cfg, run_ms, run_acct)
            bt_instance = CycleTask(
                task_id=record.task_id,
                config=merged_cfg,
                worker=worker,
                status=record.status,
                last_error=record.last_error,
            )
            worker.cycle_task = bt_instance

            d0, d1 = _backtest_calendar_range_from_row(job_row)
            bt_summary_range_start_utc = datetime.combine(d0, time(0, 0, 0))
            bt_summary_range_end_utc = datetime.combine(d1, time(0, 0, 0))
            bt_summary_bar_interval = str(job_row.get("bar_interval") or "1d")
            dates = await worker.data_provider.get_trading_dates(d0.isoformat(), d1.isoformat())
            bt_summary_dates = list(dates)
            full_cycle_times = await _build_backtest_cycle_times(
                worker=worker,
                config=merged_cfg,
                range_start=d0,
                range_end=d1,
                interval=bt_summary_bar_interval,
                trading_dates=bt_summary_dates,
            )
            start_idx = int(job_row.get("bars_completed") or 0)
            cycle_times = full_cycle_times[start_idx:] if start_idx < len(full_cycle_times) else []
            intraday_backtest = _is_intraday_backtest_interval(bt_summary_bar_interval)

            checkpoint_raw = job_row.get("ledger_checkpoint_json")
            chk = checkpoint_raw if isinstance(checkpoint_raw, dict) else None
            if chk:
                _hydrate_backtest_ledger_from_checkpoint(worker, chk)
            elif start_idx == 0:
                if reset_mock_ledger_for_fresh_backtest(worker.account_reader):
                    await emit_debug_event(
                        "backtest_ledger_reset",
                        {
                            "run_id": job_id,
                            "task_id": task_id,
                            "reason": "fresh_job_no_checkpoint",
                        },
                    )

            preload_export_ctx = (
                debug_span_export_for_session(debug_session_id, "backtest")
                if self.debug_session_span_repository is not None and debug_enabled
                else nullcontext()
            )
            with preload_export_ctx:
                if cycle_times:
                    first_cycle_time = cycle_times[0]
                    first_bar_day = first_cycle_time.date()
                    seed_syms = await backtest_mtm_seed_symbol_list(worker.account_reader, worker.cycle_task)
                    original_data_provider = worker.data_provider
                    # Resolve ``startup_history`` so the cache preload covers
                    # the strategy's warmup requirement; without this the
                    # left side of the preload window is the legacy 21-day
                    # constant which silently produces zero trades when a
                    # strategy declares a longer warmup. ``None`` falls back
                    # to the legacy expansion AND surfaces a warning via
                    # ``backtest_cache_preload_with_warmup`` so the failure
                    # mode is observable.
                    # ``emit_failure_event=True`` routes every distinguishable
                    # failure mode (no runtime, definition lookup raised,
                    # capabilities malformed,
                    # field missing / wrong type / non-positive) through
                    # ``backtest_startup_history_unresolved`` so operators
                    # can pivot on it without grep-ing stdout. The legacy
                    # 21-day fallback still applies on ``None`` so the
                    # backtest doesn't abort — the event is the diagnostic,
                    # not a hard error.
                    preload_startup_history = await self._resolve_strategy_startup_history(
                        strategy_definition_id=merged_cfg.strategy_definition_id or None,
                        emit_failure_event=True,
                    )
                    if (
                        preload_startup_history is None
                        and merged_cfg.strategy_definition_id
                    ):
                        logger.error(
                            "backtest preload: startup_history unresolved for "
                            "job_id=%r task_id=%r definition_id=%r — falling "
                            "back to legacy %d-day expansion. See the "
                            "backtest_startup_history_unresolved debug event "
                            "for the structured cause.",
                            job_id,
                            task_id,
                            merged_cfg.strategy_definition_id or None,
                            BACKTEST_BARS_CACHE_EXPANSION_DAYS,
                        )
                    cached_data_provider = await build_backtest_cached_data_provider(
                        original_data_provider,
                        run_id=job_id,
                        symbols=seed_syms,
                        range_start=d0,
                        range_end=d1,
                        interval=str(job_row.get("bar_interval") or "1d"),
                        startup_history=preload_startup_history,
                        store=(
                            RepositoryBarsCacheStore(self.cached_bars_repository)
                            if self.cached_bars_repository is not None
                            else None
                        ),
                    )
                    install_cached_data_provider(
                        worker,
                        cached_data_provider,
                        previous=original_data_provider,
                    )
                    if intraday_backtest:
                        await seed_mock_ledger_prices_for_cycle_time(
                            data_provider=worker.data_provider,
                            account_reader=worker.account_reader,
                            cycle_time=first_cycle_time,
                            symbols=seed_syms,
                            bar_interval=str(job_row.get("bar_interval") or "1d"),
                        )
                    else:
                        await seed_mock_ledger_prices_for_trading_day(
                            data_provider=worker.data_provider,
                            account_reader=worker.account_reader,
                            trading_day=first_bar_day,
                            symbols=seed_syms,
                            bar_interval=str(job_row.get("bar_interval") or "1d"),
                        )

            ref_eq = job_row.get("reference_starting_equity")
            if ref_eq is None:
                start_snap0 = await worker.account_reader.get_account_snapshot()
                await self.run_repository.set_reference_starting_equity_once(
                    job_id, float(start_snap0.equity)
                )
                refreshed = await self.run_repository.get(job_id)
                ref_eq = (refreshed or {}).get("reference_starting_equity")
            starting_equity = float(ref_eq or 0.0)
            bt_summary_starting_equity = Decimal(str(starting_equity))

            export_ctx = (
                debug_span_export_for_session(debug_session_id, "backtest")
                if self.debug_session_span_repository is not None and debug_enabled
                else nullcontext()
            )
            market_profile = str(job_row.get("market_profile") or "cn_a_share")

            with export_ctx:
                if not cycle_times:
                    self._backtest_pause_pending.pop(job_id, None)
                    self._wake_backtest_pause_waiter_if_any(job_id)
                    row_done = await self.run_repository.get(job_id)
                    if row_done and row_done.get("status") in ("completed", "failed", "stopped"):
                        return
                    end_snap = await worker.account_reader.get_account_snapshot()
                    end_positions = await worker.account_reader.get_positions()
                    ending_equity = float(end_snap.equity)
                    return_pct = None
                    if starting_equity > 0:
                        return_pct = (ending_equity - starting_equity) / starting_equity * 100.0
                    await self.run_repository.finalize_success(
                        job_id,
                        starting_equity=starting_equity,
                        ending_equity=ending_equity,
                        return_pct=return_pct,
                    )
                    await self._persist_backtest_summary(
                        job_id=job_id,
                        task_id=task_id,
                        run_id=worker.last_run_id or job_id,
                        range_start_utc=bt_summary_range_start_utc,
                        range_end_utc=bt_summary_range_end_utc,
                        bar_interval=bt_summary_bar_interval,
                        starting_equity=bt_summary_starting_equity,
                        end_snapshot=end_snap,
                        end_positions=list(end_positions),
                        symbol_to_price=_backtest_symbol_to_price_from_worker(worker),
                        equity_history=tuple(bt_summary_equity_history),
                        fills=tuple(bt_summary_fills_buffer),
                        trading_dates=tuple(bt_summary_dates),
                        bars_total=len(full_cycle_times),
                        status="completed",
                        last_error=None,
                        strategy_definition_id=merged_cfg.strategy_definition_id or None,
                    )
                    if debug_session_id:
                        await self.debug_session_repository.attach_run_id(debug_session_id, worker.last_run_id or "")
                        await self.debug_session_repository.mark_finished(
                            debug_session_id,
                            status="completed",
                            error_message="",
                        )
                    await drain_debug_span_persist_queue()
                    await emit_debug_event(
                        "backtest_job_completed",
                        {
                            "run_id": job_id,
                            "task_id": task_id,
                            "starting_equity": starting_equity,
                            "ending_equity": ending_equity,
                            "return_pct": return_pct,
                        },
                    )
                    return

                for i_rel, cycle_time in enumerate(cycle_times):
                    if await self._await_backtest_outer_unpause(job_id):
                        self._backtest_pause_pending.pop(job_id, None)
                        self._wake_backtest_pause_waiter_if_any(job_id)
                        await self._finalize_backtest_job_cooperative_stop(
                            job_id=job_id,
                            task_id=task_id,
                            session_id=debug_session_id,
                            reason="stopped",
                        )
                        return

                    row_gate = await self.run_repository.get(job_id)
                    if row_gate and row_gate.get("stop_requested"):
                        self._backtest_pause_pending.pop(job_id, None)
                        self._wake_backtest_pause_waiter_if_any(job_id)
                        await self._finalize_backtest_job_cooperative_stop(
                            job_id=job_id,
                            task_id=task_id,
                            session_id=debug_session_id,
                            reason="stopped",
                        )
                        return

                    date_str = cycle_time.date().isoformat()
                    cycle_iso = _backtest_cycle_time_to_input_override(
                        cycle_time,
                        intraday=intraday_backtest,
                    )
                    cycle_ctx: dict = {
                        "session_id": debug_session_id,
                        "run_kind": "backtest_bar",
                        "run_id": job_id,
                        "debug_enabled": debug_enabled,
                        "cycle_time": cycle_time,
                        "runtime_params": {
                            "input_overrides": {
                                "cycle_time": cycle_iso,
                                # Backward-compatible alias for old clients.
                                "cycle_time_utc": cycle_iso,
                                "run_id": job_id,
                                "market_profile": market_profile,
                                "bar_interval": str(job_row.get("bar_interval") or "1d"),
                            },
                        },
                    }
                    note = f"backtest_job={job_id} date={date_str}"
                    with debug_session_scope(debug_note=note):
                        report = await worker.run_cycle(cycle_persist_context=cycle_ctx)
                    end_snap_bar = await worker.account_reader.get_account_snapshot()
                    ending_eq_bar = float(end_snap_bar.equity)
                    bt_summary_equity_history.append(
                        backtest_summary.EquityPoint(
                            t=cycle_time,
                            equity=Decimal(str(ending_eq_bar)),
                        )
                    )
                    await self._collect_backtest_fills(
                        run_id=worker.last_run_id,
                        task_id=task_id,
                        fills_buffer=bt_summary_fills_buffer,
                        report=report,
                    )
                    return_pct_bar: float | None = None
                    if starting_equity > 0:
                        return_pct_bar = (ending_eq_bar - starting_equity) / starting_equity * 100.0
                    await self.run_repository.update_running_metrics(
                        job_id,
                        starting_equity=starting_equity,
                        ending_equity=ending_eq_bar,
                        return_pct=return_pct_bar,
                    )
                    i_abs = start_idx + i_rel
                    await self.run_repository.set_progress(job_id, bars_completed=i_abs + 1)
                    store = mock_trading_store_from_account_reader(worker.account_reader)
                    if store is not None:
                        await self.run_repository.save_ledger_checkpoint(
                            job_id, store.ledger_checkpoint()
                        )
                    if report.cycle_failed:
                        msg = report.failure_message or "cycle_failed"
                        self._backtest_pause_pending.pop(job_id, None)
                        self._wake_backtest_pause_waiter_if_any(job_id)
                        await self.run_repository.finalize_failed(job_id, error_message=msg)
                        end_positions_fail = await worker.account_reader.get_positions()
                        await self._persist_backtest_summary(
                            job_id=job_id,
                            task_id=task_id,
                            run_id=worker.last_run_id or job_id,
                            range_start_utc=bt_summary_range_start_utc,
                            range_end_utc=bt_summary_range_end_utc,
                            bar_interval=bt_summary_bar_interval,
                            starting_equity=bt_summary_starting_equity,
                            end_snapshot=end_snap_bar,
                            end_positions=list(end_positions_fail),
                            symbol_to_price=_backtest_symbol_to_price_from_worker(worker),
                            equity_history=tuple(bt_summary_equity_history),
                            fills=tuple(bt_summary_fills_buffer),
                            trading_dates=tuple(bt_summary_dates),
                            bars_total=len(full_cycle_times),
                            status="error",
                            last_error=msg,
                            strategy_definition_id=merged_cfg.strategy_definition_id or None,
                        )
                        if debug_session_id:
                            await self.debug_session_repository.mark_finished(
                                debug_session_id,
                                status="failed",
                                error_message=msg,
                                error_type="CycleFailure",
                                traceback_tail=msg[-_TRACEBACK_TAIL_MAX_CHARS:],
                            )
                        await drain_debug_span_persist_queue()
                        await emit_debug_event(
                            "backtest_job_failed",
                            {
                                "run_id": job_id,
                                "task_id": task_id,
                                "message": msg,
                                "error_type": "CycleFailure",
                            },
                        )
                        return
                    if self._backtest_pause_pending.pop(job_id, False):
                        await self.run_repository.mark_paused(job_id)
                        await emit_debug_event(
                            "backtest_job_paused",
                            {"run_id": job_id, "task_id": task_id},
                        )
                    self._wake_backtest_pause_waiter_if_any(job_id)

                end_snap = await worker.account_reader.get_account_snapshot()
                end_positions = await worker.account_reader.get_positions()
                ending_equity = float(end_snap.equity)
                return_pct = None
                if starting_equity > 0:
                    return_pct = (ending_equity - starting_equity) / starting_equity * 100.0

                await self.run_repository.finalize_success(
                    job_id,
                    starting_equity=starting_equity,
                    ending_equity=ending_equity,
                    return_pct=return_pct,
                )
                await self._persist_backtest_summary(
                    job_id=job_id,
                    task_id=task_id,
                    run_id=worker.last_run_id or job_id,
                    range_start_utc=bt_summary_range_start_utc,
                    range_end_utc=bt_summary_range_end_utc,
                    bar_interval=bt_summary_bar_interval,
                    starting_equity=bt_summary_starting_equity,
                    end_snapshot=end_snap,
                    end_positions=list(end_positions),
                    symbol_to_price=_backtest_symbol_to_price_from_worker(worker),
                    equity_history=tuple(bt_summary_equity_history),
                    fills=tuple(bt_summary_fills_buffer),
                    trading_dates=tuple(bt_summary_dates),
                    bars_total=len(full_cycle_times),
                    status="completed",
                    last_error=None,
                    strategy_definition_id=merged_cfg.strategy_definition_id or None,
                )
                if debug_session_id:
                    await self.debug_session_repository.attach_run_id(debug_session_id, worker.last_run_id or "")
                    await self.debug_session_repository.mark_finished(
                        debug_session_id,
                        status="completed",
                        error_message="",
                    )
                await drain_debug_span_persist_queue()
                await emit_debug_event(
                    "backtest_job_completed",
                    {
                        "run_id": job_id,
                        "task_id": task_id,
                        "starting_equity": starting_equity,
                        "ending_equity": ending_equity,
                        "return_pct": return_pct,
                    },
                )
        except asyncio.CancelledError:
            if self._closing:
                with suppress(Exception):
                    row = await self.run_repository.get(job_id)
                    ds = str((row or {}).get("session_id") or debug_session_id)
                    if row and row.get("stop_requested"):
                        self._backtest_pause_pending.pop(job_id, None)
                        self._wake_backtest_pause_waiter_if_any(job_id)
                        await self._finalize_backtest_job_cooperative_stop(
                            job_id=job_id,
                            task_id=task_id,
                            session_id=ds,
                            reason="stopped",
                        )
                    elif row and row.get("status") not in ("completed", "failed", "stopped"):
                        await self.run_repository.mark_paused_shutdown(job_id)
                        self._backtest_pause_pending.pop(job_id, None)
                        self._wake_backtest_pause_waiter_if_any(job_id)
            else:
                row = await self.run_repository.get(job_id)
                ds = str((row or {}).get("session_id") or debug_session_id)
                if row and row.get("status") not in ("completed", "failed", "stopped"):
                    await self.run_repository.finalize_failed(job_id, error_message="cancelled")
                    with suppress(Exception):
                        if ds:
                            await self.debug_session_repository.mark_finished(
                                ds,
                                status="failed",
                                error_message="cancelled",
                                error_type="CancelledError",
                                traceback_tail="cancelled",
                            )
                self._backtest_pause_pending.pop(job_id, None)
                self._wake_backtest_pause_waiter_if_any(job_id)
            raise
        except Exception as exc:
            message, error_type, tb_tail = _format_failure_message(exc)
            await self.run_repository.finalize_failed(job_id, error_message=message)
            with suppress(Exception):
                ds = debug_session_id
                if not ds:
                    row = await self.run_repository.get(job_id)
                    if row:
                        ds = str(row.get("session_id") or "")
                if ds:
                    await self.debug_session_repository.mark_finished(
                        ds,
                        status="failed",
                        error_message=message,
                        error_type=error_type,
                        traceback_tail=tb_tail,
                    )
            await emit_debug_event(
                "backtest_job_failed",
                {
                    "run_id": job_id,
                    "task_id": task_id,
                    "message": message,
                    "error_type": error_type,
                    "traceback_tail": tb_tail,
                },
            )
        finally:
            if _obs_token is not None:
                debug_observability_enabled.reset(_obs_token)
            self._backtest_pause_pending.pop(job_id, None)
            self._wake_backtest_pause_waiter_if_any(job_id)
            with suppress(Exception):
                await drain_debug_span_persist_queue()

    async def start_debug_session(
        self,
        identifier: str,
        *,
        input_overrides: dict | None = None,
    ) -> dict:
        if self.debug_session_repository is None:
            raise RuntimeError("debug sessions unavailable")

        record = await self.task_repository.get_task(identifier)
        if record.mode == "backtest":
            raise RuntimeError("backtest tasks do not support debug sessions")
        if record.status == "running":
            raise RuntimeError("running instances do not support debug")

        active = await self.debug_session_repository.get_active_debug_session(record.task_id)
        if active is not None:
            raise RuntimeError(f"debug session already running: {active.session_id}")

        if self.run_repository is not None and await self.run_repository.has_active_job(
            record.task_id
        ):
            raise RuntimeError("cannot start debug while a backtest job is pending or running")

        session_id = f"debug-{uuid.uuid4()}"
        created = await self.debug_session_repository.create_session(
            session_id=session_id,
            task_id=record.task_id,
            config_overrides=None,
            input_overrides=dict(input_overrides) if isinstance(input_overrides, dict) else None,
        )
        task = asyncio.create_task(
            self._run_debug_session(
                record.task_id,
                session_id,
                input_overrides=dict(input_overrides) if isinstance(input_overrides, dict) else {},
            ),
            name=f"doyoutrade-debug-session-{session_id}",
        )
        self.debug_tasks[session_id] = task
        task.add_done_callback(lambda _task, session_id=session_id: self.debug_tasks.pop(session_id, None))
        return _serialize_session(created)

    async def list_debug_sessions(self, identifier: str) -> list[dict]:
        if self.debug_session_repository is None:
            return []
        record = await self.task_repository.get_task(identifier)
        sessions = await self.debug_session_repository.list_sessions(record.task_id)
        return [_serialize_session(item) for item in sessions]

    async def get_debug_session(self, identifier: str, session_id: str) -> dict:
        if self.debug_session_repository is None:
            raise RecordNotFoundError(f"debug session not found: {session_id}")
        record = await self.task_repository.get_task(identifier)
        session = await self.debug_session_repository.get_session(session_id)
        if session.task_id != record.task_id:
            raise RecordNotFoundError(f"debug session not found: {session_id}")
        # Same read barrier as get_run_debug_view — see comment there.
        await drain_debug_span_persist_queue()
        spans = []
        if self.debug_session_span_repository is not None:
            span_records = await self.debug_session_span_repository.list_spans_for_session(session_id)
            spans = [_serialize_span(span) for span in span_records]
        model_invocations = []
        if self.model_invocation_repository is not None and session.run_id:
            model_invocations = await self.model_invocation_repository.list_invocations_for_run(session.run_id)
        return {
            **_serialize_session(session),
            "spans": spans,
            "model_invocations": model_invocations,
        }

    async def list_cycle_runs(
        self,
        identifier: str,
        *,
        limit: int = 50,
        offset: int = 0,
        run_id_contains: str | None = None,
        status: str | None = None,
        run_kind: str | None = None,
        run_mode: str | None = None,
        exclude_run_kind: str | None = None,
        started_after: str | None = None,
        started_before: str | None = None,
        run_id: str | None = None,
    ) -> dict:
        if self.cycle_run_repository is None:
            return {"items": [], "total": 0}
        record = await self.task_repository.get_task(_normalize_task_identifier(identifier))
        q = (run_id_contains or "").strip() or None
        st = (status or "").strip() or None
        rk = (run_kind or "").strip() or None
        rm = (run_mode or "").strip() or None
        erk = (exclude_run_kind or "").strip() or None
        bjid = (run_id or "").strip() or None
        after, before = _parse_cycle_run_wall_time_range(
            started_after, started_before,
        )
        cycle_session_id: str | None = None
        if bjid:
            if self.run_repository is None:
                raise RuntimeError("run_id filter requires run_repository")
            job_row = await self.run_repository.get(bjid)
            if job_row is None or job_row.get("task_id") != record.task_id:
                raise RecordNotFoundError(f"backtest job not found: {bjid}")
            raw_sid = job_row.get("session_id")
            cycle_session_id = str(raw_sid).strip() if raw_sid else None
            if not cycle_session_id:
                return {"items": [], "total": 0}
        items, total = await self.cycle_run_repository.list_for_task(
            record.task_id,
            limit=limit,
            offset=offset,
            run_id_contains=q,
            status=st,
            run_kind=rk,
            run_mode=rm,
            exclude_run_kind=erk,
            wall_started_at_after=after,
            wall_started_at_before=before,
            session_id=cycle_session_id,
        )
        return {"items": items, "total": total}

    async def list_cycle_runs_summary(
        self,
        identifier: str,
        *,
        limit: int,
        offset: int,
        run_id_contains: str | None,
        status: str | None,
        run_kind: str | None,
        run_mode: str | None,
        started_after: str | None,
        started_before: str | None,
        run_id: str | None,
    ) -> dict[str, Any]:
        if self.cycle_run_repository is None:
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        record = await self.task_repository.get_task(_normalize_task_identifier(identifier))
        q = (run_id_contains or "").strip() or None
        st = (status or "").strip() or None
        rk = (run_kind or "").strip() or None
        rm = (run_mode or "").strip() or None
        bjid = (run_id or "").strip() or None
        after, before = _parse_cycle_run_wall_time_range(
            started_after, started_before,
        )
        cycle_session_id: str | None = None
        if bjid:
            if self.run_repository is None:
                raise RuntimeError("run_id filter requires run_repository")
            job_row = await self.run_repository.get(bjid)
            if job_row is None or job_row.get("task_id") != record.task_id:
                raise RecordNotFoundError(f"backtest job not found: {bjid}")
            raw_sid = job_row.get("session_id")
            cycle_session_id = str(raw_sid).strip() if raw_sid else None
            if not cycle_session_id:
                return {"items": [], "total": 0, "limit": limit, "offset": offset}
        items, total = await self.cycle_run_repository.list_for_task(
            record.task_id,
            limit=limit,
            offset=offset,
            run_id_contains=q,
            status=st,
            run_kind=rk,
            run_mode=rm,
            exclude_run_kind=None,
            wall_started_at_after=after,
            wall_started_at_before=before,
            session_id=cycle_session_id,
        )
        summary_items = [
            {
                "run_id": item["run_id"],
                "task_id": item["task_id"],
                "status": item["status"],
                "run_kind": item["run_kind"],
                "run_mode": item["run_mode"],
                "started_at": item.get("wall_started_at"),
            }
            for item in items
        ]
        return {"items": summary_items, "total": total, "limit": limit, "offset": offset}

    async def get_cycle_run(self, run_id: str) -> dict:
        if self.cycle_run_repository is None:
            raise RecordNotFoundError(f"cycle run not found: {run_id}")
        row = await self.cycle_run_repository.get_by_run_id(run_id)
        if row is None:
            raise RecordNotFoundError(f"cycle run not found: {run_id}")
        return row

    async def _collect_session_debug_payload(
        self,
        *,
        session_id: str,
        task_id: str,
        primary_cycle_run: dict[str, Any] | None = None,
        resolved_from_type: str,
        resolved_from_id: str,
        run_row: dict[str, Any] | None = None,
    ) -> dict:
        # Same read barrier as get_run_debug_view — see comment there.
        await drain_debug_span_persist_queue()
        spans: list[dict] = []
        if self.debug_session_span_repository is not None:
            span_records = await self.debug_session_span_repository.list_spans_for_session(session_id)
            spans = [_serialize_span(span) for span in span_records]

        cycle_runs: list[dict[str, Any]] = []
        if self.cycle_run_repository is not None:
            cycle_runs, _ = await self.cycle_run_repository.list_for_task(
                task_id,
                limit=500,
                offset=0,
                session_id=session_id,
            )

        if primary_cycle_run is None and cycle_runs:
            primary_cycle_run = cycle_runs[0]

        session_payload: dict | None = None
        if self.debug_session_repository is not None:
            with suppress(RecordNotFoundError):
                session = await self.debug_session_repository.get_session(session_id)
                if session.task_id == task_id:
                    session_payload = _serialize_session(session)

        model_invocations: list[dict] = []
        if self.model_invocation_repository is not None:
            seen_invocation_ids: set[Any] = set()
            run_ids: list[str] = []
            if primary_cycle_run is not None and primary_cycle_run.get("run_id"):
                run_ids.append(str(primary_cycle_run["run_id"]))
            for row in cycle_runs:
                run_id = str(row.get("run_id") or "").strip()
                if run_id and run_id not in run_ids:
                    run_ids.append(run_id)
            if session_payload is not None:
                session_run_id = str(session_payload.get("run_id") or "").strip()
                if session_run_id and session_run_id not in run_ids:
                    run_ids.append(session_run_id)
            for run_id in run_ids:
                items = await self.model_invocation_repository.list_invocations_for_run(run_id)
                for item in items:
                    inv_id = item.get("id")
                    dedupe_key = inv_id if inv_id is not None else (
                        item.get("created_at"),
                        item.get("run_id"),
                        item.get("span_id"),
                    )
                    if dedupe_key in seen_invocation_ids:
                        continue
                    seen_invocation_ids.add(dedupe_key)
                    model_invocations.append(item)

        signal_timeline = _extract_signal_timeline(spans, cycle_runs)
        signal_timeline_summary = _summarize_signal_timeline(signal_timeline)

        # NOTE: ``signal_timeline_summary`` is placed FIRST so that even
        # when the consumer truncates the payload (request1.json turn 2),
        # the summary survives. ``cycle_runs`` / ``spans`` are bulky and
        # consume most of the budget — they come last.
        return {
            "signal_timeline_summary": signal_timeline_summary,
            "resolved_from": {
                "identifier": resolved_from_id,
                "identifier_type": resolved_from_type,
            },
            "backtest_job": run_row,
            "session": session_payload,
            "cycle_run": primary_cycle_run,
            "signal_timeline": signal_timeline,
            "cycle_runs": cycle_runs,
            "spans": spans,
            "model_invocations": model_invocations,
        }

    async def get_run_debug_view(self, run_id: str) -> dict:
        """Resolve a run/session identifier into a debug view.

        Preferred input is a ``cycle_runs.run_id``. For broader compatibility this also accepts
        ``runs.run_id`` (backtest job id) and ``debug_sessions.session_id``. Cycle-run inputs keep
        the narrow, trace-scoped view; broader carriers return session-scoped spans and aggregated
        model invocations across related cycle runs.
        """
        # Read barrier: span export persists through an async queue, and a backtest
        # flips its run status to completed *before* the queue is drained, so a
        # caller polling run status can read this view while the tail of the run's
        # spans (typically the newest cycle's whole trace) is still queued. Waiting
        # for the queue here makes the view read-your-writes; no-op when idle.
        await drain_debug_span_persist_queue()
        row: dict[str, Any] | None = None
        if self.cycle_run_repository is not None:
            row = await self.cycle_run_repository.get_by_run_id(run_id)
        if row is not None:
            task_id = row.get("task_id")

            trace_id = _usable_trace_id(row.get("trace_id"))
            spans: list[dict] = []
            if self.debug_session_span_repository is not None and trace_id is not None:
                span_snapshots = await self.debug_session_span_repository.list_spans_for_trace(trace_id)
                sid = row.get("session_id")
                if sid:
                    span_snapshots = [s for s in span_snapshots if s.session_id == sid]
                spans = [_serialize_span(s) for s in span_snapshots]

            model_invocations: list[dict] = []
            if self.model_invocation_repository is not None:
                model_invocations = await self.model_invocation_repository.list_invocations_for_run(run_id)

            session_payload: dict | None = None
            session_id = row.get("session_id")
            if session_id and self.debug_session_repository is not None:
                with suppress(RecordNotFoundError):
                    session = await self.debug_session_repository.get_session(session_id)
                    if session.task_id == task_id:
                        session_payload = _serialize_session(session)

            signal_timeline = _extract_signal_timeline(spans, [row])
            signal_timeline_summary = _summarize_signal_timeline(signal_timeline)

            # Summary first so truncated callers still see the diagnostic
            # signal — see comment on the session-scoped return for context.
            return {
                "signal_timeline_summary": signal_timeline_summary,
                "resolved_from": {
                    "identifier": run_id,
                    "identifier_type": "cycle_run",
                },
                "backtest_job": None,
                "session": session_payload,
                "cycle_run": row,
                "signal_timeline": signal_timeline,
                "cycle_runs": [row],
                "spans": spans,
                "model_invocations": model_invocations,
            }

        if self.run_repository is not None:
            job_row = await self.run_repository.get(run_id)
            if job_row is not None:
                run_debug_enabled = bool(job_row.get("debug_enabled", True))
                session_id = str(job_row.get("session_id") or "").strip()
                task_id = str(job_row.get("task_id") or "").strip()
                if not run_debug_enabled or not session_id:
                    # Fast-mode (non-debug) backtest: no debug session / spans /
                    # cycle runs / model invocations were ever persisted. Return an
                    # explicit, well-formed view so callers can tell this apart from
                    # a genuine "trace missing / lookup failed" fault. Run status +
                    # report are still available via the backtest summary.
                    return {
                        "signal_timeline_summary": None,
                        "resolved_from": {
                            "identifier": run_id,
                            "identifier_type": "backtest_job",
                        },
                        "debug_enabled": run_debug_enabled,
                        "debug_unavailable_reason": (
                            "debug_disabled" if not run_debug_enabled else "no_debug_session"
                        ),
                        "note": (
                            "This backtest ran with debug disabled (fast mode); no "
                            "spans, cycle runs or model invocations were recorded by "
                            "design. The run status and report remain available via "
                            "backtest summary."
                        ),
                        "backtest_job": job_row,
                        "session": None,
                        "cycle_run": None,
                        "signal_timeline": [],
                        "cycle_runs": [],
                        "spans": [],
                        "model_invocations": [],
                    }
                if not task_id:
                    raise RecordNotFoundError(
                        f"debug view unavailable for backtest job: {run_id} (missing task_id)"
                    )
                return await self._collect_session_debug_payload(
                    session_id=session_id,
                    task_id=task_id,
                    resolved_from_type="backtest_job",
                    resolved_from_id=run_id,
                    run_row=job_row,
                )

        if self.debug_session_repository is not None:
            with suppress(RecordNotFoundError):
                session = await self.debug_session_repository.get_session(run_id)
                return await self._collect_session_debug_payload(
                    session_id=session.session_id,
                    task_id=session.task_id,
                    resolved_from_type="debug_session",
                    resolved_from_id=run_id,
                )

        raise RecordNotFoundError(
            f"debug view not found: {run_id} (expected cycle run id, backtest job id, or debug session id)"
        )

    async def get_trace_debug_view(self, trace_id: str) -> dict:
        """Resolve an OpenTelemetry ``trace_id`` directly into a debug view.

        Unlike :meth:`get_run_debug_view` (which starts from a run/session id and
        derives the trace internally), this enters from the trace itself: spans,
        cycle runs, and model invocations are all looked up by ``trace_id`` and
        aggregated. A trace usually maps to a single cycle run, but the shape
        mirrors the run-scoped view so the same consumers work unchanged.
        """
        normalized = _usable_trace_id(trace_id)
        if normalized is None:
            raise RecordNotFoundError(
                f"invalid trace_id: {trace_id!r} (expected 32-char lowercase hex OpenTelemetry trace id)"
            )

        # Same read barrier as get_run_debug_view — see comment there.
        await drain_debug_span_persist_queue()
        spans: list[dict] = []
        if self.debug_session_span_repository is not None:
            span_snapshots = await self.debug_session_span_repository.list_spans_for_trace(normalized)
            spans = [_serialize_span(s) for s in span_snapshots]

        cycle_runs: list[dict[str, Any]] = []
        if self.cycle_run_repository is not None:
            cycle_runs = await self.cycle_run_repository.list_by_trace_id(normalized)

        model_invocations: list[dict] = []
        if self.model_invocation_repository is not None:
            model_invocations = await self.model_invocation_repository.list_invocations_for_trace(normalized)

        if not spans and not cycle_runs and not model_invocations:
            raise RecordNotFoundError(
                f"debug view not found for trace_id: {normalized} "
                "(no spans, cycle runs, or model invocations carry this trace)"
            )

        # Resolve the owning debug session from whichever carrier knows it.
        session_payload: dict | None = None
        session_id = ""
        for row in cycle_runs:
            sid = str(row.get("session_id") or "").strip()
            if sid:
                session_id = sid
                break
        if not session_id:
            for span in spans:
                sid = str(span.get("session_id") or "").strip()
                if sid:
                    session_id = sid
                    break
        if session_id and self.debug_session_repository is not None:
            with suppress(RecordNotFoundError):
                session = await self.debug_session_repository.get_session(session_id)
                session_payload = _serialize_session(session)

        primary_cycle_run = cycle_runs[0] if cycle_runs else None
        signal_timeline = _extract_signal_timeline(spans, cycle_runs)
        signal_timeline_summary = _summarize_signal_timeline(signal_timeline)

        # Summary first so truncated callers still see the diagnostic signal —
        # see comment on get_run_debug_view's cycle-run return for context.
        return {
            "signal_timeline_summary": signal_timeline_summary,
            "resolved_from": {
                "identifier": normalized,
                "identifier_type": "trace",
            },
            "backtest_job": None,
            "session": session_payload,
            "cycle_run": primary_cycle_run,
            "signal_timeline": signal_timeline,
            "cycle_runs": cycle_runs,
            "spans": spans,
            "model_invocations": model_invocations,
        }

    async def _load_or_build_task(self, record):
        cached = self.tasks.get(record.task_id)
        if cached is not None:
            cached.status = record.status
            cached.last_error = record.last_error
            return cached

        config = cycle_task_config_from_params(
            name=record.name,
            mode=record.mode,
            description=record.description,
            data_provider=record.data_provider,
            universe=list(record.universe or []),
            settings=record.settings,
        )
        ms = await self._resolve_worker_model_settings(config)
        acct = await self._resolve_worker_account(config)
        worker = await self._build_worker(config, ms, acct)
        instance = CycleTask(
            task_id=record.task_id,
            config=config,
            worker=worker,
            status=record.status,
            last_error=record.last_error,
        )
        worker.cycle_task = instance
        self.tasks[instance.task_id] = instance
        self.scheduler.register(instance)
        return instance

    def _build_debug_config(self, record) -> CycleTaskConfig:
        settings = merge_task_settings(record.settings)
        return cycle_task_config_from_params(
            name=record.name,
            mode=record.mode,
            description=record.description,
            data_provider=record.data_provider,
            universe=list(record.universe or []),
            settings=settings,
        )

    def _build_debug_worker(
        self,
        record,
        config: CycleTaskConfig,
        input_overrides: dict | None,
        *,
        model_settings: ModelSettings,
        resolved_account: ResolvedAccount,
    ):
        worker = self.worker_factory(config, model_settings, resolved_account)
        instance = CycleTask(
            task_id=record.task_id,
            config=config,
            worker=worker,
            status=record.status,
            last_error=record.last_error,
        )
        worker.cycle_task = instance
        if hasattr(worker, "data_provider"):
            worker.data_provider = PatchedDataProvider(worker.data_provider, input_overrides)
        if hasattr(worker, "universe_provider"):
            worker.universe_provider = OverriddenUniverseProvider(worker.universe_provider, input_overrides)
        if hasattr(worker, "strategy"):
            signal_target = getattr(worker.strategy, "signal_component", None) or worker.strategy
            if hasattr(worker, "data_provider") and hasattr(signal_target, "data_provider"):
                signal_target.data_provider = worker.data_provider
            if hasattr(signal_target, "debug_note"):
                raw_note = (input_overrides or {}).get("debug_note")
                signal_target.debug_note = str(raw_note).strip() if raw_note is not None else ""
        if hasattr(worker, "execution_adapter"):
            worker.execution_adapter = _DebugExecutionAdapter(worker.execution_adapter)
        return worker

    async def _run_debug_session(
        self,
        task_id: str,
        session_id: str,
        *,
        input_overrides: dict,
    ) -> None:
        assert self.debug_session_repository is not None
        try:
            record = await self.task_repository.get_task(task_id)
            config = self._build_debug_config(record)
            # Expand @watchlist:<tag> universe tokens before the (sync) debug
            # worker build so the data stack + persisted effective_config reflect
            # the concrete symbols actually replayed.
            config = await self._resolve_watchlist_universe_config(config)
            effective_config = _serialize_config(config)
            await self.debug_session_repository.mark_running(
                session_id,
                run_id=None,
                effective_config=effective_config,
            )
            dbg_ms = await self._resolve_worker_model_settings(config)
            dbg_acct = await self._resolve_worker_account(config)
            worker = self._build_debug_worker(
                record, config, input_overrides, model_settings=dbg_ms, resolved_account=dbg_acct
            )
            debug_note = str(input_overrides.get("debug_note", "")).strip() or None

            export_ctx = (
                debug_span_export_for_session(session_id, "debug")
                if self.debug_session_span_repository is not None
                else nullcontext()
            )
            cycle_ctx = {
                "session_id": session_id,
                "run_kind": "debug",
                "runtime_params": {
                    "input_overrides": dict(input_overrides or {}),
                },
            }
            with export_ctx:
                with debug_session_scope(debug_note=debug_note):
                    report = await worker.run_cycle(cycle_persist_context=cycle_ctx)
            # Update session with the actual run_id after cycle completes
            run_id_for_session = getattr(worker, "last_run_id", None) or ""
            await self.debug_session_repository.attach_run_id(session_id, run_id_for_session)
            if isinstance(report, CycleReport) and report.cycle_failed:
                if report.failure_error is not None:
                    await self.debug_session_repository.append_event(
                        session_id,
                        "signal_generation_failed",
                        {
                            "message": report.failure_message,
                            "error": report.failure_error,
                        },
                    )
                cycle_msg = report.failure_message or "signal_generation_failed"
                await self.debug_session_repository.mark_finished(
                    session_id,
                    status="failed",
                    error_message=cycle_msg,
                    error_type="CycleFailure",
                    traceback_tail=cycle_msg[-_TRACEBACK_TAIL_MAX_CHARS:],
                )
            else:
                await self.debug_session_repository.mark_finished(
                    session_id,
                    status="completed",
                    error_message="",
                )
        except Exception as exc:
            message, error_type, tb_tail = _format_failure_message(exc)
            await self.debug_session_repository.append_event(
                session_id,
                "session_failed",
                {"message": message, "error_type": error_type, "traceback_tail": tb_tail},
            )
            await self.debug_session_repository.mark_finished(
                session_id,
                status="failed",
                error_message=message,
                error_type=error_type,
                traceback_tail=tb_tail,
            )

    async def restore_tasks(self) -> int:
        restored = 0
        self.kill_switch_enabled = await self.system_state_repository.get_kill_switch_enabled()
        for record in await self.task_repository.list_tasks():
            try:
                instance = await self._load_or_build_task(record)
            except Exception as exc:
                await self.task_repository.update_status(record.task_id, "error", str(exc))
                continue

            if record.status == "running" and not self.kill_switch_enabled:
                try:
                    self.scheduler.start(instance.task_id)
                    restored += 1
                except Exception as exc:
                    instance.status = "error"
                    instance.last_error = str(exc)
                    await self.task_repository.update_status(instance.task_id, "error", str(exc))
        return restored

    async def set_kill_switch(self, enabled: bool):
        await self.system_state_repository.set_kill_switch_enabled(enabled)
        self.kill_switch_enabled = enabled
        if enabled:
            for record in await self.task_repository.list_tasks():
                if record.status != "running":
                    continue
                instance = self.tasks.get(record.task_id)
                if instance is not None and instance.status == "running":
                    self.scheduler.stop(instance.task_id)
                await self.task_repository.update_status(record.task_id, "stopped", "")

    async def get_system_state(self):
        records = await self.task_repository.list_tasks()
        kill_switch_enabled = await self.system_state_repository.get_kill_switch_enabled()
        return {
            "kill_switch_enabled": kill_switch_enabled,
            "task_count": len(records),
            "running_count": len([item for item in records if item.status == "running"]),
        }

    async def _resolve_strategy_name(
        self, definition_id: str | None, *, task_id: str
    ) -> str | None:
        """Resolve a strategy definition's display name for task serialization.

        Best-effort: returns ``None`` (never raises) when the task has no bound
        definition, no strategy runtime is wired, the definition is missing, or
        the lookup errors — a missing display name must not break the task read
        path. A missing definition is logged at info; unexpected errors at
        warning with the exception type so the swallow stays visible.
        """
        if not definition_id:
            return None
        repo = (
            getattr(self.strategy_runtime, "definition_repository", None)
            if self.strategy_runtime is not None
            else None
        )
        if repo is None:
            return None
        try:
            snapshot = await repo.get_definition(definition_id)
        except RecordNotFoundError:
            logger.error(
                "task %s references missing strategy definition %s; strategy_name unresolved",
                task_id,
                definition_id,
            )
            return None
        except Exception as exc:  # best-effort enrichment; keep the swallow visible
            logger.exception(
                "strategy_name lookup failed task=%s definition_id=%s error_type=%s error=%s",
                task_id,
                definition_id,
                type(exc).__name__,
                exc,
            )
            return None
        name = getattr(snapshot, "name", None)
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None

    async def get_task_status(self, identifier: str):
        record = await self.task_repository.get_task(identifier)
        instance = self.tasks.get(record.task_id)
        cycles = getattr(instance.worker, "cycles", None) if instance is not None else None
        effective = resolve_effective_provider(record.data_provider, self.default_data_provider)
        # Build config from record fields and serialize to nested agent/factor blocks.
        config = cycle_task_config_from_params(
            name=record.name,
            mode=record.mode,
            description=record.description,
            data_provider=record.data_provider,
            universe=list(record.universe),
            settings=record.settings,
        )
        serialized_settings = _serialize_config(config)
        strategy_name = await self._resolve_strategy_name(
            getattr(config, "strategy_definition_id", None),
            task_id=record.task_id,
        )
        return _drop_deprecated_task_read_fields(
            {
            "task_id": record.task_id,
            "name": record.name,
            "mode": record.mode,
            "description": record.description,
            "status": record.status,
            "cycles": cycles,
            "last_error": record.last_error,
            "data_provider": record.data_provider,
            "data_provider_effective": effective,
            "universe": list(record.universe),
            "execution_strategy": record.execution_strategy,
            "account_id": record.account_id,
            "model_id": record.model_id,
            "strategy_name": strategy_name,
            "settings": serialized_settings,
            "enabled_skills": list(record.enabled_skills),
            "backtest_summary": (
                dict(record.backtest_summary)
                if isinstance(record.backtest_summary, dict)
                else None
            ),
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            }
        )

    async def update_task(
        self,
        identifier: str,
        *,
        name: str | None = None,
        mode: str | None = None,
        description: str | None = None,
        data_provider: str | None = None,
        settings: dict | None = None,
    ) -> dict:
        record = await self.task_repository.get_task(identifier)
        if mode is not None:
            current_is_backtest = str(record.mode) == "backtest"
            next_is_backtest = str(mode) == "backtest"
            if current_is_backtest != next_is_backtest:
                raise ValueError(
                    "cannot switch task mode between trading and backtest; create a new task instead"
                )

        # Deep-merge the settings patch onto the existing record.settings so callers
        # don't have to resend the full settings object. Top-level keys absent from
        # the patch are preserved; nested dicts are merged recursively; lists and
        # scalars are replaced. Avoids the previous silent reset of fields like
        # ``universe`` / ``agent`` when only ``strategy`` was patched.
        applied_keys: list[str] = []
        merged_settings: dict | None = None
        if settings is not None:
            applied_keys = sorted(settings.keys())
            base_settings = dict(record.settings) if isinstance(record.settings, dict) else {}
            merged_settings = _deep_merge_settings(base_settings, settings)

        await self._ensure_update_task_catalog_symbols(record, merged_settings)

        merged_for_route = merged_settings if merged_settings is not None else settings
        if merged_for_route is not None and "model_route_name" in merged_for_route:
            raw_mrn = merged_for_route.get("model_route_name")
            if isinstance(raw_mrn, str) and raw_mrn.strip():
                await self.ensure_model_route_exists(raw_mrn.strip())
        # A patched account_id must resolve to an enabled account.
        if merged_for_route is not None and "account_id" in merged_for_route:
            await self._validate_bound_account(merged_for_route.get("account_id"))

        update_kwargs: dict[str, Any] = {
            "name": name,
            "mode": mode,
            "description": description,
            "data_provider": data_provider,
        }
        if settings is not None:
            update_kwargs["settings"] = merged_settings
        await self.task_repository.update_task(record.task_id, **update_kwargs)

        fresh = await self.task_repository.get_task(record.task_id)
        cached = self.tasks.get(record.task_id)
        if cached is not None:
            if name is not None:
                cached.config.name = name
            if mode is not None:
                cached.config.mode = mode
            if settings is not None:
                new_cfg = cycle_task_config_from_params(
                    name=fresh.name,
                    mode=fresh.mode,
                    description=fresh.description,
                    data_provider=fresh.data_provider,
                    universe=list(fresh.universe or []),
                    settings=fresh.settings,
                )
                close = getattr(cached.worker, "aclose", None)
                if close is not None:
                    await close()
                new_ms = await self._resolve_worker_model_settings(new_cfg)
                new_acct = await self._resolve_worker_account(new_cfg)
                new_worker = await self._build_worker(new_cfg, new_ms, new_acct)
                cached.config = new_cfg
                cached.worker = new_worker
                new_worker.cycle_task = cached

        result = await self.get_task_status(record.task_id)
        if applied_keys:
            # Marker is stripped by the assistant tool layer before returning to the
            # model; the API response leaves it for inspection by callers who care.
            result["__applied_settings_keys__"] = applied_keys
        return result

    async def aclose(self):
        self._closing = True
        pending_debug_tasks = list(self.debug_tasks.values())
        pending_backtest_tasks = list(self.backtest_tasks.values())
        for task in pending_debug_tasks + pending_backtest_tasks:
            task.cancel()
        for task in pending_debug_tasks + pending_backtest_tasks:
            with suppress(asyncio.CancelledError):
                await task
        for instance in self.tasks.values():
            close = getattr(instance.worker, "aclose", None)
            if close is not None:
                await close()
