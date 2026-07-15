from __future__ import annotations

import inspect
import json
import math
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterator, List

from doyoutrade.core.cycle_persist_context import (
    current_tick_run_kind,
    current_tick_session_id,
    current_trigger_id,
)

from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.trace import INVALID_SPAN_CONTEXT, NonRecordingSpan, set_span_in_context

from doyoutrade.core.cycle_state import CycleRunState
from doyoutrade.core.signal_generator_protocol import (
    SignalGenerationContext,
    SignalGeneratorProtocol,
)
from doyoutrade.core.worker_protocols import (
    ApprovalGateProtocol,
    ExecutionAdapterProtocol,
    IntentValidatorProtocol,
    RiskEngineProtocol,
)
from doyoutrade.account.protocol import AccountReader
from doyoutrade.core.post_cycle_account import build_post_cycle_account
from doyoutrade.core.protocols import TradingDataProvider, UniverseProvider
from doyoutrade.data.simulated_bar_marks import (
    merge_simulated_bar_marks_into_market,
    mock_trading_store_from_account_reader,
)
from doyoutrade.execution.settlement import (
    settlement_mode as resolve_settlement_mode,
    trading_day_from_cycle_time,
)
from doyoutrade.debug import emit_debug_event
from doyoutrade.debug.context import (
    cycle_code_version as _cycle_code_version_var,
    cycle_code_hash as _cycle_code_hash_var,
    debug_observability_enabled,
)
from doyoutrade.money import json_sanitize
from doyoutrade.money.decimal_helpers import decimal_from_number, decimal_to_json_str
from doyoutrade.models.invoke_errors import exception_to_invoke_error, failure_message_from_error
from doyoutrade.observability import get_logger, get_tracer
from doyoutrade.observability.worker_span_context import (
    mark_worker_ancestor_spans_error,
    worker_phase_span,
    worker_run_cycle_span,
)
from doyoutrade.core.models import (
    CycleReport,
    MarketContext,
    OrderIntent,
    PositionSnapshot,
    RiskDecision,
    TaskBudgetPositionUsage,
    TaskBudgetSnapshot,
)
from doyoutrade.execution.approval import ApprovalResult
from doyoutrade.execution.fill_pricing import close_price_for_symbol
from doyoutrade.execution.validator import OrderIntentValidator
from doyoutrade.runtime.cycle_task import CycleTask


logger = get_logger(__name__)
tracer = get_tracer(__name__)


PHASES: List[str] = [
    "load_context",
    "refresh_market_state",
    "refresh_portfolio_state",
    "settle_positions",
    "build_universe",
    "generate_signals",
    "run_risk_checks",
    "evaluate_protections",
    "await_approval_if_needed",
    "dispatch_orders",
    "sync_fills_and_positions",
    "persist_trace_and_metrics",
]


def _current_trace_id() -> str:
    ctx = otel_trace.get_current_span().get_span_context()
    if ctx.is_valid:
        return f"{ctx.trace_id:032x}"
    return "-"


@contextmanager
def _detached_cycle_trace_root() -> Iterator[None]:
    """Clear inherited OTel parent so ``worker.run_cycle`` starts a new trace.

    Without this, spans created under an ambient trace (e.g. FastAPI request or a long-lived
    parent) share one ``trace_id`` across many cycles; ``cycle_runs.trace_id`` and debug-view
    ``list_spans_for_trace`` would then return the whole tree instead of a single cycle.
    """
    ctx = set_span_in_context(NonRecordingSpan(INVALID_SPAN_CONTEXT))
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        otel_context.detach(token)


def _market_context_trace_payload(market_context: MarketContext) -> dict[str, Any]:
    """Serialize market snapshot for phase spans."""
    prices = market_context.symbol_to_price or {}
    ticks = market_context.symbol_to_tick or {}
    return {
        "symbol_count": len(prices),
        "tick_symbol_count": len(ticks),
        "symbol_to_price": dict(prices),
        "symbol_to_tick": dict(ticks),
    }


def _market_snapshot_from_context(
    market_context: MarketContext,
) -> dict[str, dict[str, Any]]:
    """Compact per-symbol price snapshot persisted to cycle_runs.details.

    Pulls just the human-relevant fields out of each full tick (last price,
    previous close, intraday OHLC) and derives the percent change vs the prior
    close. Kept small on purpose — the full tick already lives on the phase
    span; this is the slice the strategy_signal_alert ``full`` push narrates.
    ``pct_change`` is ``None`` when the prior close is missing or zero so the
    consumer can say "unknown" rather than divide-by-zero into a bogus number.
    """

    ticks = market_context.symbol_to_tick or {}
    prices = market_context.symbol_to_price or {}
    out: dict[str, dict[str, Any]] = {}
    for symbol in sorted(set(ticks) | set(prices)):
        tick = ticks.get(symbol) or {}
        last_price = tick.get("last_price", prices.get(symbol))
        prev_close = tick.get("last_close")
        if prev_close is None:
            prev_close = tick.get("pre_close")
        pct_change: float | None = None
        try:
            if last_price is not None and prev_close is not None and float(prev_close) != 0:
                pct_change = round(
                    (float(last_price) - float(prev_close)) / float(prev_close) * 100.0,
                    2,
                )
        except (TypeError, ValueError):
            # Non-numeric tick fields are a data-source contract violation, not
            # something to silently zero out — leave pct_change=None and keep
            # the raw values so the operator can see what arrived.
            pct_change = None
        out[symbol] = {
            "last_price": last_price,
            "prev_close": prev_close,
            "pct_change": pct_change,
            "open": tick.get("open"),
            "high": tick.get("high"),
            "low": tick.get("low"),
        }
    return out


def _signal_generator_trace_meta(signal_generator: object) -> dict[str, Any]:
    """Lightweight identification payload for the strategy phase span."""
    meta: dict[str, Any] = {"signal_generator_type": type(signal_generator).__name__}
    config = getattr(signal_generator, "_config", None)
    if config is not None:
        for attr, key in (
            ("strategy_definition_id", "strategy_definition_id"),
            ("strategy_execution_profile", "strategy_execution_profile"),
        ):
            value = getattr(config, attr, None)
            if value:
                meta[key] = value
    # Include pinned code_version when available (set by pin_code_version()).
    pinned_version = getattr(signal_generator, "_pinned_version", None)
    if pinned_version:
        meta["code_version"] = pinned_version
    pinned_hash = getattr(signal_generator, "_pinned_code_hash", None)
    if pinned_hash:
        meta["code_hash"] = pinned_hash
    return meta


def _as_otel_attribute_value(val: Any) -> Any:
    if val is None:
        return ""
    if isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, (list, tuple)):
        return [str(x) for x in val]
    return str(val)


def _phase_span_summary(phase: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Lightweight span attributes for debug UI (avoid duplicating full trace payload)."""
    if phase == "refresh_market_state":
        prices = payload.get("symbol_to_price") or {}
        ticks = payload.get("symbol_to_tick") or {}
        return {
            "symbol_count": payload.get("symbol_count", len(prices)),
            "tick_symbol_count": payload.get("tick_symbol_count", len(ticks)),
            "symbols": sorted(prices.keys()),
        }
    if phase == "refresh_portfolio_state":
        return {
            "cash": payload.get("cash"),
            "equity": payload.get("equity"),
            "position_count": payload.get("position_count"),
        }
    if phase == "build_universe":
        uni = payload.get("universe") or []
        return {
            "size": payload.get("size", len(uni)),
            "universe_count": len(uni),
        }
    if phase == "generate_signals":
        uni = payload.get("universe") or []
        out: dict[str, Any] = {
            "intent_count": payload.get("intent_count"),
            "universe_count": len(uni),
            "signal_generator_type": payload.get("signal_generator_type"),
        }
        for key in ("strategy_definition_id", "strategy_execution_profile"):
            if payload.get(key) is not None:
                out[key] = payload[key]
        return {k: v for k, v in out.items() if v is not None}
    # Default: scalars only (nested structures live in phase event payload).
    return {k: v for k, v in payload.items() if not isinstance(v, (dict, list))}


def _fill_record_for_cycle_details(fill: Any, *, cycle_run_id: str, cycle_time: datetime | None) -> dict[str, Any] | None:
    if fill is None:
        return None
    payload = asdict(fill) if is_dataclass(fill) else dict(fill) if isinstance(fill, dict) else None
    if payload is None:
        return None
    side = str(payload.get("side") or "").strip().lower()
    if side not in ("buy", "sell"):
        return None
    symbol = str(payload.get("symbol") or "").strip()
    if not symbol:
        return None
    try:
        quantity = float(payload.get("quantity"))
        price = float(payload.get("price"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(quantity) or not math.isfinite(price):
        return None
    if quantity <= 0 or price <= 0:
        return None
    payload["symbol"] = symbol
    payload["side"] = side
    payload["quantity"] = quantity
    payload["price"] = price
    payload["cycle_run_id"] = cycle_run_id
    if cycle_time is not None:
        payload["timestamp"] = cycle_time.isoformat()
    return json_sanitize(payload)


def _parse_cycle_clock_datetime(raw: object) -> datetime | None:
    """Return naive UTC instant for persistence / logical clock.

    Accepts ISO-8601 with ``Z`` or numeric offset (e.g. ``+08:00``), space instead of ``T``,
    or naive strings (treated as UTC wall time for backward compatibility).
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is not None:
            return raw.astimezone(timezone.utc).replace(tzinfo=None)
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if " " in s and "T" not in s:
            s = s.replace(" ", "T", 1)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None


def _cycle_clock_runtime_params(cycle_persist_context: dict[str, Any] | None) -> tuple[str, datetime | None, dict[str, Any] | None]:
    ctx = dict(cycle_persist_context or {})
    rp = ctx.get("runtime_params")
    rp = dict(rp) if isinstance(rp, dict) else None
    raw_clock = ctx.get("cycle_time")
    if raw_clock is None:
        raw_clock = ctx.get("cycle_time_utc")
    if raw_clock is None and rp:
        io = rp.get("input_overrides")
        if isinstance(io, dict):
            raw_clock = io.get("cycle_time")
            if raw_clock is None:
                raw_clock = io.get("cycle_time_utc")
    dt = _parse_cycle_clock_datetime(raw_clock)
    mode = "simulated" if dt is not None else "wall"
    return mode, dt, rp


def _market_profile_from_cycle_context(cycle_persist_context: dict[str, Any] | None) -> str:
    ctx = dict(cycle_persist_context or {})
    rp = ctx.get("runtime_params")
    if isinstance(rp, dict):
        io = rp.get("input_overrides")
        if isinstance(io, dict):
            raw = io.get("market_profile")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return "cn_a_share"


def _bar_interval_from_cycle_context(cycle_persist_context: dict[str, Any] | None) -> str:
    ctx = dict(cycle_persist_context or {})
    rp = ctx.get("runtime_params")
    if isinstance(rp, dict):
        io = rp.get("input_overrides")
        if isinstance(io, dict):
            raw = io.get("bar_interval")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return "1d"


@dataclass
class TradingWorker:
    data_provider: TradingDataProvider
    account_reader: AccountReader
    universe_provider: UniverseProvider
    #: Pluggable :class:`SignalGeneratorProtocol` that turns universe + market
    #: state into already-sized :class:`OrderIntent` rows. The canonical
    #: implementation is
    #: :class:`~doyoutrade.strategy_sdk.runner.StrategyRunner`
    #: (Strategy + PositionManager).
    signal_generator: SignalGeneratorProtocol
    risk_engine: RiskEngineProtocol
    execution_adapter: ExecutionAdapterProtocol
    run_mode: str = "paper"
    approval_gate: ApprovalGateProtocol | None = None
    intent_validator: IntentValidatorProtocol | None = None
    cycle_run_repository: Any | None = None
    trade_fill_repository: Any | None = None
    last_run_id: str = ""
    cycle_task: CycleTask | None = None
    #: Optional portfolio circuit breaker (doyoutrade.execution.protection.
    #: ProtectionEngine). ``None`` → no protection phase (default). Held on the
    #: worker so its equity peak accumulates across cycles within a run.
    protection_engine: Any | None = None
    #: Owning account id (acct-…), threaded onto pending approvals so a card /
    #: the web UI can attribute a held order to its account. "" when unbound.
    account_id: str = ""

    def _new_cycle_state(self, run_id: str) -> CycleRunState:
        inst = self.cycle_task
        if inst is not None:
            return CycleRunState(
                run_id=run_id,
                trace_id=_current_trace_id(),
                task_id=inst.task_id,
                agent_name=inst.config.name,
                cycle_task=inst,
            )
        return CycleRunState(
            run_id=run_id,
            trace_id=_current_trace_id(),
            task_id="-",
            agent_name="-",
            cycle_task=None,
        )

    def _portfolio_source(self) -> str:
        return str(getattr(self.account_reader, "portfolio_source", "ledger"))

    async def _capture_post_cycle_account(
        self,
        symbol_to_price: dict[str, float] | None,
        *,
        cycle_time: datetime | None = None,
    ) -> dict[str, Any]:
        acct = await _maybe_await(self.account_reader.get_account_snapshot())
        pos = await _maybe_await(self.account_reader.get_positions())
        captured_at: datetime | None = None
        if cycle_time is not None:
            captured_at = (
                cycle_time
                if cycle_time.tzinfo is not None
                else cycle_time.replace(tzinfo=timezone.utc)
            )
        return build_post_cycle_account(
            account=acct,
            positions=list(pos),
            source=self._portfolio_source(),
            symbol_to_price=symbol_to_price,
            captured_at=captured_at,
            account_reader_class=type(self.account_reader).__name__,
            data_provider_class=type(self.data_provider).__name__,
        )

    def _task_budget_caps_from_config(self) -> tuple[Decimal | None, float | None]:
        config = getattr(self.cycle_task, "config", None)
        if config is None:
            return None, None
        max_amount = getattr(config, "max_task_position_amount", None)
        max_ratio = getattr(config, "max_task_position_ratio", None)
        amount_dec = (
            decimal_from_number(max_amount) if max_amount is not None else None
        )
        ratio_value = float(max_ratio) if max_ratio is not None else None
        return amount_dec, ratio_value

    @staticmethod
    def _task_budget_unit_price(
        symbol: str,
        market_context: MarketContext,
        positions: list[PositionSnapshot],
    ) -> tuple[Decimal | None, str]:
        price = close_price_for_symbol(symbol, market_context)
        if price > 0:
            return decimal_from_number(price), "market_context"
        for position in positions:
            if position.symbol != symbol:
                continue
            if position.market_price is not None and position.market_price > 0:
                return decimal_from_number(position.market_price), "position.market_price"
            if (
                position.market_value is not None
                and position.market_value > 0
                and position.quantity
            ):
                return (
                    decimal_from_number(position.market_value)
                    / decimal_from_number(position.quantity),
                    "position.market_value",
                )
            if position.cost_price > 0:
                return position.cost_price, "position.cost_price"
        return None, "missing"

    async def _build_task_budget_snapshot(
        self,
        *,
        state: CycleRunState,
        account_snapshot,
        positions: list[PositionSnapshot],
        market_context: MarketContext,
    ) -> TaskBudgetSnapshot | None:
        max_task_amount, max_task_ratio = self._task_budget_caps_from_config()
        if max_task_amount is None and max_task_ratio is None:
            return None

        cap_candidates: list[Decimal] = []
        if max_task_amount is not None:
            cap_candidates.append(max_task_amount)
        if max_task_ratio is not None:
            cap_candidates.append(
                account_snapshot.equity * decimal_from_number(max_task_ratio)
            )
        budget_cap = min(cap_candidates) if cap_candidates else None
        warnings: list[str] = []
        repo = self.trade_fill_repository
        fill_rows: list[dict[str, Any]] = []
        if repo is None or not hasattr(repo, "list_for_task"):
            warnings.append("trade_fill_repository_unavailable")
        else:
            fill_rows = await _maybe_await(
                repo.list_for_task(task_id=state.task_id, source_mode=self.run_mode)
            )

        net_quantity_by_symbol: dict[str, Decimal] = {}
        for row in fill_rows:
            symbol = str(row.get("symbol") or "").strip()
            side = str(row.get("side") or "").strip().lower()
            if not symbol or side not in ("buy", "sell"):
                continue
            try:
                quantity = Decimal(str(row.get("quantity")))
            except Exception:
                warnings.append(f"invalid_fill_quantity:{symbol}")
                continue
            if quantity <= 0:
                warnings.append(f"non_positive_fill_quantity:{symbol}")
                continue
            signed = quantity if side == "buy" else -quantity
            net_quantity_by_symbol[symbol] = net_quantity_by_symbol.get(symbol, Decimal(0)) + signed

        broker_qty_by_symbol: dict[str, int] = {}
        for position in positions:
            try:
                broker_qty = int(decimal_from_number(position.quantity))
            except Exception:
                continue
            if broker_qty > 0:
                broker_qty_by_symbol[position.symbol] = (
                    broker_qty_by_symbol.get(position.symbol, 0) + broker_qty
                )

        usages: list[TaskBudgetPositionUsage] = []
        current_usage = Decimal(0)
        for symbol, net_qty in sorted(net_quantity_by_symbol.items()):
            if net_qty <= 0:
                if net_qty < 0:
                    warnings.append(f"negative_task_quantity:{symbol}")
                continue
            logical_qty = int(net_qty)
            broker_qty = broker_qty_by_symbol.get(symbol, 0)
            capped_qty = min(logical_qty, broker_qty)
            if capped_qty <= 0:
                if logical_qty > 0:
                    warnings.append(f"broker_position_missing:{symbol}")
                continue
            if capped_qty < logical_qty:
                warnings.append(f"task_quantity_clamped_to_broker:{symbol}")
            unit_price, price_source = self._task_budget_unit_price(
                symbol, market_context, positions
            )
            if unit_price is None or unit_price <= 0:
                warnings.append(f"missing_price:{symbol}")
                continue
            market_value = decimal_from_number(capped_qty) * unit_price
            current_usage += market_value
            usages.append(
                TaskBudgetPositionUsage(
                    symbol=symbol,
                    quantity=capped_qty,
                    market_value=market_value,
                    price=unit_price,
                    price_source=price_source,
                )
            )

        remaining_budget = (
            max(Decimal(0), budget_cap - current_usage)
            if budget_cap is not None
            else Decimal(0)
        )
        snapshot = TaskBudgetSnapshot(
            max_task_position_amount=max_task_amount,
            max_task_position_ratio=max_task_ratio,
            budget_cap=budget_cap,
            current_usage=current_usage,
            remaining_budget=remaining_budget,
            positions=tuple(usages),
            warnings=tuple(warnings),
        )
        span = otel_trace.get_current_span()
        if span is not None:
            try:
                span.set_attribute("task_budget.enabled", True)
                if budget_cap is not None:
                    span.set_attribute("task_budget.cap", str(budget_cap))
                span.set_attribute("task_budget.current_usage", str(current_usage))
                span.set_attribute("task_budget.remaining", str(remaining_budget))
            except Exception:
                logger.debug(
                    "task budget span attribute set failed run_id=%s", state.run_id
                )
        await emit_debug_event(
            "task_budget_snapshot",
            {
                "task_id": state.task_id,
                "run_id": state.run_id,
                "source_mode": self.run_mode,
                "budget_cap": str(budget_cap) if budget_cap is not None else None,
                "current_usage": str(current_usage),
                "remaining_budget": str(remaining_budget),
                "max_task_position_amount": (
                    str(max_task_amount) if max_task_amount is not None else None
                ),
                "max_task_position_ratio": max_task_ratio,
                "positions": [asdict(item) for item in usages],
                "warnings": warnings,
            },
        )
        return snapshot

    async def _persist_cycle_run_begin(
        self,
        state: CycleRunState,
        cycle_persist_context: dict[str, Any] | None,
        *,
        code_version: str | None = None,
        code_hash: str | None = None,
    ) -> None:
        repo = self.cycle_run_repository
        if repo is None or not debug_observability_enabled.get():
            return
        ctx = cycle_persist_context or {}
        clock_mode, cycle_time, runtime_params = _cycle_clock_runtime_params(cycle_persist_context)
        session_id = ctx.get("session_id") or current_tick_session_id.get()
        run_kind = ctx.get("run_kind") or current_tick_run_kind.get() or "scheduled"
        trigger_id = ctx.get("trigger_id") or current_trigger_id.get()
        await repo.create_started(
            run_id=state.run_id,
            task_id=state.task_id,
            agent_name=state.agent_name,
            session_id=session_id,
            trace_id=state.trace_id,
            run_mode=self.run_mode,
            run_kind=str(run_kind),
            clock_mode=clock_mode,
            cycle_time=cycle_time,
            runtime_params=runtime_params,
            code_version=code_version,
            code_hash=code_hash,
            trigger_id=trigger_id,
        )

    async def _persist_cycle_run_done(
        self,
        state: CycleRunState,
        *,
        snap: dict[str, Any],
        status: str,
        report: CycleReport | None = None,
        failure_message: str = "",
        failure_error: dict[str, Any] | None = None,
    ) -> None:
        repo = self.cycle_run_repository
        if repo is None or not debug_observability_enabled.get():
            return
        cycle_failed = bool(report.cycle_failed) if report is not None else bool(failure_message)
        msg = failure_message or (report.failure_message if report is not None else "") or ""
        details_patch: dict[str, Any] = {"universe": list(snap.get("universe") or [])}
        if snap.get("position_intents") is not None:
            details_patch["position_intents"] = json_sanitize(list(snap["position_intents"]))
        if snap.get("fills") is not None:
            details_patch["fills"] = json_sanitize(list(snap["fills"]))
        if snap.get("post_cycle_account") is not None:
            details_patch["post_cycle_account"] = snap["post_cycle_account"]
        if snap.get("market_snapshot") is not None:
            details_patch["market_snapshot"] = json_sanitize(snap["market_snapshot"])
        if snap.get("task_budget") is not None:
            details_patch["task_budget"] = json_sanitize(snap["task_budget"])
        if snap.get("signal_diagnostics") is not None:
            details_patch["signal_diagnostics"] = json_sanitize(snap["signal_diagnostics"])
        if report is not None and report.failure_error is not None:
            details_patch["failure_error"] = json_sanitize(dict(report.failure_error))
        elif failure_error is not None:
            details_patch["failure_error"] = json_sanitize(dict(failure_error))
        await repo.finalize(
            state.run_id,
            status=status,
            details_patch=details_patch,
            cycle_failed=cycle_failed,
            failure_message=msg,
            completed_phases_json=list(report.completed_phases) if report and report.completed_phases else None,
            submitted_count=report.submitted_count if report is not None else None,
            vetoed_count=report.vetoed_count if report is not None else None,
            pending_approval_count=report.pending_approval_count if report is not None else None,
        )

    async def _persist_trade_fill_realtime(
        self,
        *,
        state: CycleRunState,
        fill_payload: dict[str, Any],
        rationale: str | None,
        parent_run_id: str | None,
        session_id: str | None,
    ) -> None:
        repo = self.trade_fill_repository
        if repo is None:
            return
        symbol = str(fill_payload.get("symbol") or "").strip()
        side = str(fill_payload.get("side") or "").strip().lower()
        if side not in ("buy", "sell") or not symbol:
            return
        try:
            quantity = Decimal(str(fill_payload.get("quantity")))
            price = Decimal(str(fill_payload.get("price")))
        except Exception:
            return
        if quantity <= 0 or price <= 0:
            return
        filled_at = _parse_cycle_clock_datetime(fill_payload.get("timestamp")) or state.cycle_time
        if filled_at is None:
            filled_at = datetime.now(timezone.utc).replace(tzinfo=None)
        intent_id_raw = fill_payload.get("intent_id")
        intent_id = str(intent_id_raw).strip() if intent_id_raw not in (None, "") else None
        rationale_raw = fill_payload.get("rationale")
        if rationale_raw in (None, ""):
            rationale_value = rationale
        else:
            rationale_value = str(rationale_raw)
        amount = decimal_to_json_str(quantity * price)
        # Persist the A-share fee when one was charged (fee model active);
        # stays None on fee-free runs so historic rows / golden tests are
        # unchanged. The float also rides in raw_payload (asdict of the fill),
        # which is what the backtest summary reads back for FIFO reconciliation.
        fee_value: str | None = None
        raw_fee = fill_payload.get("fee")
        if raw_fee not in (None, "", 0, 0.0):
            try:
                fee_dec = Decimal(str(raw_fee))
                if fee_dec > 0:
                    fee_value = decimal_to_json_str(fee_dec)
            except Exception:
                logger.warning(
                    "trade fill persist: unparseable fee=%r symbol=%s; persisting null",
                    raw_fee, symbol,
                )
        try:
            entry_tag = fill_payload.get("entry_tag")
            exit_tag = fill_payload.get("exit_tag")
            exit_reason = fill_payload.get("exit_reason")
            inserted = await repo.insert_fill(
                task_id=state.task_id,
                cycle_run_id=state.run_id,
                run_id=parent_run_id,
                session_id=session_id,
                symbol=symbol,
                side=side,
                quantity=decimal_to_json_str(quantity),
                price=decimal_to_json_str(price),
                amount=amount,
                fee=fee_value,
                currency=None,
                intent_id=intent_id,
                rationale=rationale_value,
                entry_tag=str(entry_tag) if isinstance(entry_tag, str) and entry_tag else None,
                exit_tag=str(exit_tag) if isinstance(exit_tag, str) and exit_tag else None,
                exit_reason=str(exit_reason) if isinstance(exit_reason, str) and exit_reason else None,
                filled_at=filled_at,
                source_mode=self.run_mode,
                raw_payload=fill_payload,
            )
        except Exception as exc:
            await emit_debug_event(
                "trade_fill_persist_failed",
                {
                    "task_id": state.task_id,
                    "run_id": state.run_id,
                    "parent_run_id": parent_run_id,
                    "cycle_run_id": state.run_id,
                    "symbol": symbol,
                    "side": side,
                    "intent_id": intent_id,
                    "error": str(exc),
                },
            )
            raise
        await emit_debug_event(
            "trade_fill_persisted",
            {
                "task_id": state.task_id,
                "run_id": state.run_id,
                "parent_run_id": parent_run_id,
                "cycle_run_id": state.run_id,
                "symbol": symbol,
                "side": side,
                "intent_id": intent_id,
                "inserted": bool(inserted),
            },
        )

    async def run_cycle(self, cycle_persist_context: dict[str, Any] | None = None) -> CycleReport:
        """执行一轮完整交易循环（通常由调度器按 tick 触发）。

        数据流概览：行情来自 data_provider；账户与持仓来自 account_reader；universe_provider
        与二者组合 → 规则产出候选单
        → Agent 审核 → 结构化 OrderIntent → 校验 → 风控 → 审批门 → 执行适配器。

        阶段名与 `PHASES` 及设计文档主循环一致；`CycleRunState` 在同一轮内透传，便于关联
        run_id、trace 与 Agent 实例。

        ``cycle_persist_context`` 可选，用于调试/回测时钟与运行时参数（写入 ``cycle_runs``）。
        """
        run_id = f"run-{uuid.uuid4()}"
        self.last_run_id = run_id
        with _detached_cycle_trace_root():
            with worker_run_cycle_span(tracer) as _cycle_span:
                # Tag the cycle's root span with run_mode so trace consumers
                # (debug UI, OTel exporters) can filter / distinguish
                # signal_only / paper / live / backtest runs at a glance.
                # (§最低同步要求: new branch → OTel span attribute.)
                try:
                    _cycle_span.set_attribute("worker.mode", str(self.run_mode))
                    _cycle_span.set_attribute("run_id", run_id)
                except Exception:
                    # Span may be a NonRecordingSpan when tracing is disabled;
                    # attribute set is best-effort and never breaks the cycle.
                    logger.debug("worker.run_cycle span attribute set failed run_id=%s", run_id)
                state = self._new_cycle_state(run_id)
                persist_ctx = dict(cycle_persist_context or {})
                parent_run_raw = persist_ctx.get("run_id")
                parent_run_id = str(parent_run_raw).strip() if parent_run_raw else None
                session_raw = persist_ctx.get("session_id") or current_tick_session_id.get()
                session_id = str(session_raw).strip() if session_raw else None
                cm, ct, _ = _cycle_clock_runtime_params(cycle_persist_context)
                state.clock_mode = cm
                state.cycle_time = ct
                state.market_profile = _market_profile_from_cycle_context(cycle_persist_context)
                state.settlement_mode = resolve_settlement_mode(
                    state.market_profile,
                    self._portfolio_source(),
                )
                # Resolve the optional A-share fee model from the task's
                # fee_config. None / empty → no transaction cost (default),
                # so fee-free runs and existing golden tests are unchanged.
                _fee_cfg = (
                    state.cycle_task.config.fee_config
                    if state.cycle_task is not None
                    else None
                )
                if _fee_cfg:
                    from doyoutrade.execution.fees import fee_model_from_config

                    state.fee_model = fee_model_from_config(_fee_cfg)
                else:
                    state.fee_model = None
                snap: dict[str, Any] = {"universe": [], "position_intents": None}
                # --- Step 7.3: Pin strategy code version before any fallible step ---
                # Resolve current_version atomically so a concurrent assistant edit
                # (which bumps current_version) cannot affect this cycle's compilation.
                # The pinned values are written to cycle_runs at the single call to
                # _persist_cycle_run_begin below, so the record always reflects what
                # was actually used — regardless of whether pinning succeeded or failed.
                pinned_code_version: str | None = None
                pinned_code_hash: str | None = None
                _pin_error: Exception | None = None
                _pin_err_code: str = "strategy_pin_failed"
                _pin_fn = getattr(self.signal_generator, "pin_code_version", None)
                if callable(_pin_fn):
                    try:
                        pinned_code_version, pinned_code_hash = await _pin_fn()
                    except Exception as _pin_exc:
                        # Capture error; emit structured event + log so the failure is
                        # visible (§错误可见性 — any failure affecting cycle direction must
                        # appear).  _persist_cycle_run_begin / _done are called below,
                        # outside this except block, so there is exactly ONE begin call
                        # per run_cycle invocation.
                        _pin_error = _pin_exc
                        _pin_err_code = getattr(_pin_exc, "error_code", "strategy_pin_failed")
                        await emit_debug_event(
                            "strategy_version_pin_failed",
                            {
                                "error_code": _pin_err_code,
                                "run_id": run_id,
                                "task_id": state.task_id,
                                "error": str(_pin_exc),
                                "hint": (
                                    "finalize_strategy_authoring must be called before "
                                    "the first cycle run; check strategy_no_current_version "
                                    "events for the definition."
                                ),
                            },
                        )
                        logger.warning(
                            "worker cycle aborted: strategy version pin failed "
                            "agent_name=%s task_id=%s run_id=%s error_code=%s error=%s",
                            state.agent_name,
                            state.task_id,
                            run_id,
                            _pin_err_code,
                            str(_pin_exc),
                        )

                # Exactly ONE _persist_cycle_run_begin call per run_cycle invocation.
                # When pin failed, code_version / code_hash remain None (not written to
                # cycle_runs), but the row is still created so _persist_cycle_run_done
                # can close it with status="failed".
                await self._persist_cycle_run_begin(
                    state,
                    cycle_persist_context,
                    code_version=pinned_code_version,
                    code_hash=pinned_code_hash,
                )

                if _pin_error is not None:
                    await self._persist_cycle_run_done(
                        state,
                        snap=snap,
                        status="failed",
                        report=None,
                        failure_message=str(_pin_error),
                    )
                    raise _pin_error
                logger.info(
                    "worker cycle started agent_name=%s task_id=%s run_id=%s run_mode=%s phase=%s",
                    state.agent_name,
                    state.task_id,
                    state.run_id,
                    self.run_mode,
                    state.phase,
                )
                # Inject pinned version into the debug-event context so all span
                # events emitted during this cycle automatically carry code_version /
                # code_hash (§最低同步要求: OTel span attribute + debug event).
                # Direct ContextVar access is used here (rather than
                # worker_code_version_scope) because the try/except cycle-error handler
                # below must wrap the entire cycle body including the reset — the
                # context-manager form would require re-indenting the whole body.  The
                # finally block below guarantees the reset on every exit path.
                _cv_token = _cycle_code_version_var.set(pinned_code_version)
                _ch_token = _cycle_code_hash_var.set(pinned_code_hash)
                try:
                    state.enter_phase("load_context")
                    await self._record_phase(state, "load_context", {"status": "start"})
    
                    state.enter_phase("refresh_market_state")
                    with worker_phase_span(tracer, "worker.phase.refresh_market_state"):
                        positions_preview: list[PositionSnapshot] = []
                        if state.clock_mode == "simulated" and state.cycle_time is not None:
                            positions_preview = list(await _maybe_await(self.account_reader.get_positions()))
    
                        market_context = await _maybe_await(self.data_provider.get_market_context())
                        if state.clock_mode == "simulated" and state.cycle_time is not None:
                            market_context = await merge_simulated_bar_marks_into_market(
                                data_provider=self.data_provider,
                                account_reader=self.account_reader,
                                cycle_state=state,
                                market_context=market_context,
                                positions_preview=positions_preview,
                                bar_interval=_bar_interval_from_cycle_context(cycle_persist_context),
                            )
                        quotes = market_context.symbol_to_price
                        logger.info(
                            "market quotes fetched run_id=%s task_id=%s count=%s quotes=%s",
                            run_id,
                            state.task_id,
                            len(quotes),
                            dict(sorted(quotes.items())),
                        )
                        for sym in sorted(market_context.symbol_to_tick.keys()):
                            logger.info(
                                "market tick detail run_id=%s task_id=%s symbol=%s tick=%s",
                                run_id,
                                state.task_id,
                                sym,
                                json.dumps(
                                    market_context.symbol_to_tick[sym],
                                    ensure_ascii=False,
                                ),
                            )
                        await self._record_phase(
                            state,
                            "refresh_market_state",
                            _market_context_trace_payload(market_context),
                        )
                        snap["market_snapshot"] = _market_snapshot_from_context(
                            market_context
                        )
    
                    state.enter_phase("refresh_portfolio_state")
                    with worker_phase_span(tracer, "worker.phase.refresh_portfolio_state"):
                        account_snapshot = await _maybe_await(self.account_reader.get_account_snapshot())
                        positions = await _maybe_await(self.account_reader.get_positions())
                        await self._record_phase(
                            state,
                            "refresh_portfolio_state",
                            json_sanitize(
                                {
                                    "cash": account_snapshot.cash,
                                    "equity": account_snapshot.equity,
                                    "position_count": len(positions),
                                    "positions": [asdict(p) for p in positions],
                                }
                            ),
                        )

                    state.enter_phase("settle_positions")
                    with worker_phase_span(tracer, "worker.phase.settle_positions") as _settle_span:
                        unlocked = False
                        trading_day_s: str | None = None
                        store = mock_trading_store_from_account_reader(self.account_reader)
                        if store is not None and state.settlement_mode in ("t0", "t1"):
                            store.ledger_settlement_mode = state.settlement_mode  # type: ignore[assignment]
                        # Push the optional A-share fee model onto the ledger before
                        # this cycle's dispatch (settle_positions runs before
                        # dispatch_orders). None → no transaction cost (default).
                        if store is not None:
                            store.fee_model = state.fee_model  # type: ignore[assignment]
                        if (
                            state.settlement_mode == "t1"
                            and self._portfolio_source() == "ledger"
                            and store is not None
                        ):
                            trading_day = trading_day_from_cycle_time(
                                state.cycle_time,
                                market_profile=state.market_profile,
                            )
                            trading_day_s = trading_day.isoformat()
                            unlocked = store.apply_settlement_trigger_b(trading_day)
                            if unlocked:
                                positions = list(
                                    await _maybe_await(self.account_reader.get_positions())
                                )
                        try:
                            _settle_span.set_attribute("run_id", state.run_id)
                            _settle_span.set_attribute("doyoutrade.settlement_mode", state.settlement_mode)
                            if trading_day_s:
                                _settle_span.set_attribute("doyoutrade.trading_day", trading_day_s)
                            _settle_span.set_attribute("doyoutrade.settlement.unlocked", unlocked)
                        except Exception:
                            logger.debug(
                                "settle_positions span attribute set failed run_id=%s",
                                state.run_id,
                            )
                        await self._record_phase(
                            state,
                            "settle_positions",
                            {
                                "settlement_mode": state.settlement_mode,
                                "trading_day": trading_day_s,
                                "unlocked": unlocked,
                                "portfolio_source": self._portfolio_source(),
                            },
                        )
                        if unlocked:
                            await emit_debug_event(
                                "settlement_day_unlocked",
                                {
                                    "run_id": state.run_id,
                                    "task_id": state.task_id,
                                    "trading_day": trading_day_s,
                                    "portfolio_source": self._portfolio_source(),
                                    "hint": "T+1 trigger B: available := quantity for new trading day",
                                },
                            )

                    state.task_budget_snapshot = await self._build_task_budget_snapshot(
                        state=state,
                        account_snapshot=account_snapshot,
                        positions=positions,
                        market_context=market_context,
                    )
                    if state.task_budget_snapshot is not None:
                        snap["task_budget"] = asdict(state.task_budget_snapshot)

                    state.enter_phase("build_universe")
                    with worker_phase_span(tracer, "worker.phase.build_universe"):
                        universe = await _maybe_await(
                            self.universe_provider.build_universe(
                                market_context, account_snapshot, positions, cycle_state=state
                            )
                        )
                        await self._record_phase(
                            state,
                            "build_universe",
                            {"size": len(universe), "universe": list(universe)},
                        )
                        snap["universe"] = list(universe)
    
                    state.enter_phase("generate_signals")
                    with worker_phase_span(tracer, "worker.phase.generate_signals"):
                        sg_ctx = SignalGenerationContext(
                            market_context=market_context,
                            universe=universe,
                            account_snapshot=account_snapshot,
                            positions=positions,
                            task_budget_snapshot=state.task_budget_snapshot,
                            cycle_state=state,
                        )
                        try:
                            intents = list(
                                await _maybe_await(self.signal_generator.generate_intents(sg_ctx))
                            )
                        except Exception as exc:
                            err = exception_to_invoke_error(exc, code="signal_generation_failed")
                            _sig_err_msg = failure_message_from_error(err)
                            mark_worker_ancestor_spans_error(_sig_err_msg)
                            await self._record_phase(
                                state,
                                "generate_signals",
                                {
                                    "intent_count": 0,
                                    "universe": list(universe),
                                    "status": "error",
                                    "error_code": err.get("code"),
                                    "error_message": err.get("message"),
                                    **_signal_generator_trace_meta(self.signal_generator),
                                },
                            )
                            await emit_debug_event(
                                "position_intents",
                                {
                                    "intent_count": 0,
                                    "intents": [],
                                    "status": "error",
                                    "error": err,
                                },
                            )
                            await emit_debug_event(
                                "cycle_aborted",
                                {
                                    "reason": "signal_generation_failed",
                                    "run_id": state.run_id,
                                    "error": err,
                                },
                            )
                            state.enter_phase("persist_trace_and_metrics")
                            await self._record_phase(
                                state,
                                "persist_trace_and_metrics",
                                {
                                    "submitted_count": 0,
                                    "vetoed_count": 0,
                                    "pending_approval_count": 0,
                                    "aborted": True,
                                    "abort_reason": "signal_generation_failed",
                                },
                            )
                            logger.warning(
                                "worker cycle aborted after signal failure agent_name=%s task_id=%s "
                                "run_id=%s error_code=%s error_type=%s %s",
                                state.agent_name,
                                state.task_id,
                                state.run_id,
                                err.get("code"),
                                err.get("type"),
                                _sig_err_msg,
                                extra={
                                    "doyoutrade_failure_error": dict(err),
                                },
                            )
                            report = CycleReport(
                                submitted_count=0,
                                vetoed_count=0,
                                pending_approval_count=0,
                                completed_phases=[
                                    "load_context",
                                    "refresh_market_state",
                                    "refresh_portfolio_state",
                                    "build_universe",
                                    "generate_signals",
                                    "persist_trace_and_metrics",
                                ],
                                cycle_failed=True,
                                failure_message=_sig_err_msg,
                                failure_error=dict(err),
                            )
                            snap["position_intents"] = []
                            snap["post_cycle_account"] = await self._capture_post_cycle_account(
                                market_context.symbol_to_price,
                                cycle_time=state.cycle_time,
                            )
                            await self._persist_cycle_run_done(
                                state,
                                snap=snap,
                                status="completed",
                                report=report,
                            )
                            await emit_debug_event("summary", asdict(report))
                            return report

                        await self._record_phase(
                            state,
                            "generate_signals",
                            {
                                "intent_count": len(intents),
                                "universe": list(universe),
                                **_signal_generator_trace_meta(self.signal_generator),
                            },
                        )
                        # Capture the per-symbol decision factors the generator
                        # wrote back onto the shared context (direction / tag /
                        # rationale / diagnostics) so they persist to
                        # cycle_runs.details for the signal_only 'full' push.
                        if sg_ctx.signal_diagnostics is not None:
                            snap["signal_diagnostics"] = sg_ctx.signal_diagnostics
                        await emit_debug_event(
                            "position_intents",
                            {
                                "intent_count": len(intents),
                                "intents": [asdict(intent) for intent in intents],
                            },
                        )

                    # --- signal_only short-circuit ----------------------------------
                    # When ``run_mode == 'signal_only'`` the cycle is intentionally
                    # informational: surface the strategy's intents as
                    # "signals" and skip the entire dispatch chain
                    # (validator / risk / approval / execution / sync_fills).
                    # The cycle still goes through ``persist_trace_and_metrics``
                    # so ``cycle_runs`` rows carry the same shape as paper /
                    # live / backtest runs — only ``submitted_count`` /
                    # ``vetoed_count`` stay at 0 and ``details.fills`` is empty.
                    # (§最低同步要求 + §错误可见性: new branch → OTel span +
                    # debug event + structured log + frontend types.)
                    if self.run_mode == "signal_only":
                        intent_count = len(intents)
                        post_cycle = await self._capture_post_cycle_account(
                            market_context.symbol_to_price,
                            cycle_time=state.cycle_time,
                        )
                        snap["post_cycle_account"] = post_cycle
                        state.enter_phase("persist_trace_and_metrics")
                        with worker_phase_span(
                            tracer, "worker.phase.signal_only_shortcut"
                        ) as _shortcut_span:
                            try:
                                _shortcut_span.set_attribute("worker.mode", "signal_only")
                                _shortcut_span.set_attribute("run_id", state.run_id)
                                _shortcut_span.set_attribute(
                                    "doyoutrade.signal_only.intent_count", intent_count
                                )
                            except Exception:
                                logger.debug(
                                    "signal_only shortcut span attribute set failed "
                                    "run_id=%s",
                                    state.run_id,
                                )
                            await emit_debug_event(
                                "worker.phase.signal_only_shortcut",
                                {
                                    "run_id": state.run_id,
                                    "task_id": state.task_id,
                                    "intent_count": intent_count,
                                    "hint": "no-dispatch by mode",
                                },
                            )
                        await self._record_phase(
                            state,
                            "persist_trace_and_metrics",
                            {
                                "submitted_count": 0,
                                "vetoed_count": 0,
                                "pending_approval_count": 0,
                                "mode": "signal_only",
                            },
                        )
                        logger.info(
                            "worker cycle shortcut: signal_only mode "
                            "agent_name=%s task_id=%s run_id=%s intent_count=%s",
                            state.agent_name,
                            state.task_id,
                            state.run_id,
                            intent_count,
                        )
                        report = CycleReport(
                            submitted_count=0,
                            vetoed_count=0,
                            pending_approval_count=0,
                            completed_phases=[
                                "load_context",
                                "refresh_market_state",
                                "refresh_portfolio_state",
                                "build_universe",
                                "generate_signals",
                                "persist_trace_and_metrics",
                            ],
                        )
                        snap["position_intents"] = [
                            json_sanitize(asdict(intent)) for intent in intents
                        ]
                        snap["fills"] = []
                        await self._persist_cycle_run_done(
                            state,
                            snap=snap,
                            status="completed",
                            report=report,
                        )
                        await emit_debug_event("summary", asdict(report))
                        return report
                    # ----------------------------------------------------------------

                    validator = self.intent_validator or OrderIntentValidator()
                    valid_intents = []
                    rejected_intents: list[dict[str, Any]] = []
                    for intent in intents:
                        validation = validator.validate(intent)
                        if validation.ok:
                            valid_intents.append(intent)
                            continue
                        rejected_intents.append(
                            {
                                "intent_id": intent.intent_id,
                                "symbol": intent.symbol,
                                "action": intent.action,
                                "amount": intent.amount,
                                "price_reference": intent.price_reference,
                                "error": validation.error,
                            }
                        )
                        logger.warning(
                            "worker intent dropped by validator agent_name=%s task_id=%s "
                            "run_id=%s intent_id=%s symbol=%s action=%s error=%s",
                            state.agent_name,
                            state.task_id,
                            state.run_id,
                            intent.intent_id,
                            intent.symbol,
                            intent.action,
                            validation.error,
                        )
                    if rejected_intents:
                        await emit_debug_event(
                            "intent_validation_failed",
                            {
                                "rejected_count": len(rejected_intents),
                                "rejected": rejected_intents,
                                "hint": (
                                    "OrderIntent rejected by validator. "
                                    "PositionManager should not produce invalid "
                                    "intents — see rejected[].error for the failed "
                                    "invariant (amount > 0, price > 0, action in {buy,sell})."
                                ),
                            },
                        )
    
                    state.enter_phase("run_risk_checks")
                    with worker_phase_span(tracer, "worker.phase.run_risk_checks"):
                        risk_decisions = self.risk_engine.evaluate(
                            valid_intents,
                            account_snapshot,
                            positions,
                            cycle_state=state,
                            settlement_mode=state.settlement_mode,  # type: ignore[arg-type]
                        )
                        decision_by_id = {rd.intent_id: rd for rd in risk_decisions}
                        await self._record_phase(
                            state, "run_risk_checks", {"risk_decision_count": len(risk_decisions)}
                        )
                        await emit_debug_event(
                            "risk_decisions",
                            {
                                "decision_count": len(risk_decisions),
                                "decisions": [asdict(d) for d in risk_decisions],
                            },
                        )
    
                    # Portfolio circuit breaker (opt-in; default-off when no
                    # protection_engine). Halts NEW entries on a peak-to-trough
                    # account drawdown breach while still allowing exits to
                    # unwind. Runs after risk checks, before approval/dispatch.
                    protection_halted = False
                    protection_reason = ""
                    if self.protection_engine is not None:
                        state.enter_phase("evaluate_protections")
                        with worker_phase_span(
                            tracer, "worker.phase.evaluate_protections"
                        ) as _prot_span:
                            prot = self.protection_engine.evaluate(account_snapshot.equity)
                            protection_halted = bool(prot.halted)
                            protection_reason = prot.reason or ""
                            _prot_span.set_attribute("run_id", state.run_id)
                            _prot_span.set_attribute(
                                "doyoutrade.protection.halted", protection_halted
                            )
                            if prot.drawdown_pct is not None:
                                _prot_span.set_attribute(
                                    "doyoutrade.protection.drawdown_pct", float(prot.drawdown_pct)
                                )
                            await self._record_phase(
                                state,
                                "evaluate_protections",
                                {"halted": protection_halted, "reason": protection_reason},
                            )
                            if protection_halted:
                                logger.warning(
                                    "worker protection halt agent_name=%s task_id=%s "
                                    "run_id=%s reason=%s peak=%s current=%s drawdown=%s",
                                    state.agent_name,
                                    state.task_id,
                                    state.run_id,
                                    protection_reason,
                                    prot.peak_equity,
                                    prot.current_equity,
                                    prot.drawdown_pct,
                                )
                                await emit_debug_event(
                                    "protection_triggered",
                                    {
                                        "reason": prot.reason,
                                        "peak_equity": str(prot.peak_equity),
                                        "current_equity": str(prot.current_equity),
                                        "drawdown_pct": prot.drawdown_pct,
                                        "hint": (
                                            "Account drawdown breached the configured "
                                            "max_drawdown_pct — new BUY entries are halted "
                                            "this cycle; SELL/exit intents still dispatch so "
                                            "positions can be unwound."
                                        ),
                                    },
                                )

                    submitted_count = 0
                    vetoed_count = 0
                    pending_approval_count = 0
                    # Intent ids held for human approval this cycle, so the
                    # position_intents snapshot (→ trigger digest) can mark them
                    # 待审批 and not read as "already placed" (§错误可见性).
                    pending_approval_intent_ids: set[str] = set()
                    actual_fills: list[dict[str, Any]] = []

                    for intent in valid_intents:
                        decision = decision_by_id.get(
                            intent.intent_id,
                            RiskDecision(intent_id=intent.intent_id, action="pass"),
                        )
                        # Protection halt vetoes new BUYs (counts as a veto —
                        # the order was not submitted); SELL/exit intents pass
                        # through so the strategy can still unwind.
                        if protection_halted and intent.action == "buy":
                            vetoed_count += 1
                            logger.info(
                                "worker intent halted by protection agent_name=%s task_id=%s "
                                "run_id=%s intent_id=%s symbol=%s reason=%s",
                                state.agent_name,
                                state.task_id,
                                state.run_id,
                                intent.intent_id,
                                intent.symbol,
                                protection_reason,
                            )
                            await emit_debug_event(
                                "intent_protection_halted",
                                {
                                    "intent_id": intent.intent_id,
                                    "symbol": intent.symbol,
                                    "action": intent.action,
                                    "amount": intent.amount,
                                    "reason": protection_reason,
                                },
                            )
                            continue
                        if decision.action == "veto":
                            vetoed_count += 1
                            logger.info(
                                "worker intent vetoed agent_name=%s task_id=%s run_id=%s "
                                "intent_id=%s symbol=%s action=%s reason=%s",
                                state.agent_name,
                                state.task_id,
                                state.run_id,
                                intent.intent_id,
                                intent.symbol,
                                intent.action,
                                decision.reason or "<unspecified>",
                            )
                            await emit_debug_event(
                                "intent_vetoed",
                                {
                                    "intent_id": intent.intent_id,
                                    "symbol": intent.symbol,
                                    "action": intent.action,
                                    "amount": intent.amount,
                                    "reason": decision.reason or "",
                                    "scaled_quantity": decision.scaled_quantity,
                                    "scaled_amount": decision.scaled_amount,
                                },
                            )
                            continue
    
                        state.enter_phase("await_approval_if_needed")
                        with worker_phase_span(tracer, "worker.phase.await_approval_if_needed"):
                            approval = await _maybe_await(
                                self._request_approval(intent, account_snapshot, market_context, state)
                            )
                            intent_notional = decimal_to_json_str(intent.quote_notional_decimal())
                            # Surface the approval decision on the OTel span too, not
                            # just the debug event, so a trace consumer sees which
                            # order was held / who must approve it (§最低同步要求).
                            approval_span = otel_trace.get_current_span()
                            if approval_span is not None:
                                approval_span.set_attribute("approval.status", approval.status)
                                approval_span.set_attribute("approval.notional", intent_notional)
                                approval_span.set_attribute("approval.mode", str(self.run_mode))
                                if approval.approval_id:
                                    approval_span.set_attribute("approval.id", approval.approval_id)
                            await self._record_phase(
                                state,
                                "await_approval_if_needed",
                                {"intent_id": intent.intent_id, "status": approval.status},
                            )
                            await emit_debug_event(
                                "approval_result",
                                {
                                    "intent_id": intent.intent_id,
                                    "status": approval.status,
                                    "approval_id": approval.approval_id,
                                    "symbol": intent.symbol,
                                    "action": intent.action,
                                    "notional": intent_notional,
                                    "mode": str(self.run_mode),
                                },
                            )
                        if approval.status == "pending":
                            pending_approval_count += 1
                            pending_approval_intent_ids.add(intent.intent_id)
                            continue
                        if approval.status != "approved":
                            vetoed_count += 1
                            logger.warning(
                                "worker intent blocked by approval gate agent_name=%s "
                                "task_id=%s run_id=%s intent_id=%s symbol=%s status=%s reason=%s",
                                state.agent_name,
                                state.task_id,
                                state.run_id,
                                intent.intent_id,
                                intent.symbol,
                                approval.status,
                                approval.reason or "<unspecified>",
                            )
                            await emit_debug_event(
                                "intent_approval_blocked",
                                {
                                    "intent_id": intent.intent_id,
                                    "symbol": intent.symbol,
                                    "action": intent.action,
                                    "approval_id": approval.approval_id,
                                    "status": approval.status,
                                    "reason": approval.reason or "",
                                },
                            )
                            continue
    
                        # Sole submit path for ALL adapters (mock/qmt/paper/
                        # backtest) — see CLAUDE.md consistency contract. The
                        # in-cycle approved path and the resume path (an approved
                        # pending intent dispatched by the scheduler) both flow
                        # through _dispatch_approved_intent so no adapter can be
                        # reached any other way. Counting stays here so §计数器
                        # semantics are explicit: a non-fill is a veto, never a
                        # phantom submitted++.
                        fill_payload = await self._dispatch_approved_intent(
                            intent,
                            state,
                            market_context,
                            parent_run_id=parent_run_id,
                            session_id=session_id,
                        )
                        if fill_payload is not None:
                            actual_fills.append(fill_payload)
                            submitted_count += 1
                        else:
                            vetoed_count += 1
    
                    state.enter_phase("sync_fills_and_positions")
                    post_cycle = await self._capture_post_cycle_account(
                        quotes,
                        cycle_time=state.cycle_time,
                    )
                    await self._record_phase(
                        state,
                        "sync_fills_and_positions",
                        {
                            "submitted_count": submitted_count,
                            "post_cycle_equity": post_cycle["account"]["equity"],
                            "post_cycle_position_count": len(post_cycle["positions"]),
                            "portfolio_source": post_cycle["source"],
                        },
                    )
                    snap["post_cycle_account"] = post_cycle
                    state.enter_phase("persist_trace_and_metrics")
                    await self._record_phase(
                        state,
                        "persist_trace_and_metrics",
                        {
                            "submitted_count": submitted_count,
                            "vetoed_count": vetoed_count,
                            "pending_approval_count": pending_approval_count,
                        },
                    )
    
                    logger.info(
                        "worker cycle completed agent_name=%s task_id=%s run_id=%s "
                        "submitted_count=%s vetoed_count=%s pending_approval_count=%s",
                        state.agent_name,
                        state.task_id,
                        state.run_id,
                        submitted_count,
                        vetoed_count,
                        pending_approval_count,
                    )
                    report = CycleReport(
                        submitted_count=submitted_count,
                        vetoed_count=vetoed_count,
                        pending_approval_count=pending_approval_count,
                        completed_phases=list(PHASES),
                    )
                    snap["position_intents"] = [
                        {
                            **json_sanitize(asdict(intent)),
                            "pending_approval": intent.intent_id in pending_approval_intent_ids,
                        }
                        for intent in intents
                    ]
                    if actual_fills:
                        snap["fills"] = actual_fills
                        # Surface on the report so callers (e.g. the backtest
                        # loop in fast mode) can collect fills without reading
                        # back from cycle_runs, which may not be persisted.
                        report.fills = [json_sanitize(dict(f)) for f in actual_fills]
                    await self._persist_cycle_run_done(
                        state,
                        snap=snap,
                        status="completed",
                        report=report,
                    )
                    await emit_debug_event("summary", asdict(report))
                    return report
                except Exception as exc:
                    _worker_err = exception_to_invoke_error(exc, code="worker_cycle_failed")
                    _worker_msg = failure_message_from_error(_worker_err)
                    mark_worker_ancestor_spans_error(_worker_msg)
                    try:
                        snap["post_cycle_account"] = await self._capture_post_cycle_account(
                            None,
                            cycle_time=state.cycle_time,
                        )
                    except Exception:
                        pass
                    await self._persist_cycle_run_done(
                        state,
                        snap=snap,
                        status="failed",
                        report=None,
                        failure_message=_worker_msg,
                        failure_error=_worker_err,
                    )
                    await emit_debug_event(
                        "error",
                        {
                            "message": "worker_cycle_failed",
                            "run_id": state.run_id,
                        },
                    )
                    logger.exception(
                        "worker cycle failed agent_name=%s task_id=%s run_id=%s",
                        state.agent_name,
                        state.task_id,
                        state.run_id,
                    )
                    raise
                finally:
                    # Reset code-version context vars so they don't leak to the
                    # next cycle run on the same event-loop task (defensive reset).
                    _cycle_code_version_var.reset(_cv_token)
                    _cycle_code_hash_var.reset(_ch_token)

    def _request_approval(self, intent, account_snapshot, market_context, state: CycleRunState) -> ApprovalResult:
        if self.approval_gate is None:
            return ApprovalResult(status="approved", intent_id=intent.intent_id)
        inst = state.cycle_task
        if inst is not None:
            ic = inst.config
            return self.approval_gate.request(
                intent,
                account_snapshot,
                market_context,
                self.run_mode,
                cycle_state=state,
                min_notional_for_approval=ic.min_notional_for_approval,
                timeout_seconds=ic.approval_timeout_seconds,
                account_id=self.account_id or None,
            )
        return self.approval_gate.request(
            intent,
            account_snapshot,
            market_context,
            self.run_mode,
            cycle_state=state,
            account_id=self.account_id or None,
        )

    async def _dispatch_approved_intent(
        self,
        intent,
        state: CycleRunState,
        market_context,
        *,
        parent_run_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Submit one **already-approved** intent to the execution adapter.

        The SOLE physical submit path for every adapter (mock ``PaperExecution``
        / real ``QmtExecution`` / backtest ``SimulatedBroker``). Both the
        in-cycle approved branch and the scheduler's resume path call this, so an
        approved order reaches a broker in exactly one place — the keystone of
        mock/qmt consistency (CLAUDE.md).

        Returns the persisted fill payload on success, or ``None`` when the
        adapter rejected the order (a zero-fill / non-fill). The caller owns the
        submitted/vetoed counter so a non-fill can never be miscounted as a
        submission (§计数器). On rejection this emits ``dispatch_rejected``;
        the adapter itself emits the granular ``execution_zero_fill`` /
        ``qmt_order_rejected`` reason.
        """
        state.enter_phase("dispatch_orders")
        with worker_phase_span(tracer, "worker.phase.dispatch_orders"):
            fill = await _maybe_await(
                self.execution_adapter.submit_intent(
                    intent, cycle_state=state, market_context=market_context
                )
            )
            fill_payload = _fill_record_for_cycle_details(
                fill,
                cycle_run_id=state.run_id,
                cycle_time=state.cycle_time,
            )
            if fill_payload is not None:
                if fill_payload.get("rationale") in (None, "") and intent.rationale:
                    fill_payload["rationale"] = intent.rationale
                # Propagate signal_tag (factor identifier) from the OrderIntent
                # so it reaches TradeFillRecord as entry_tag (buy) / exit_tag (sell).
                intent_signal_tag = getattr(intent, "signal_tag", "") or ""
                if intent_signal_tag:
                    if intent.action == "buy":
                        fill_payload.setdefault("entry_tag", intent_signal_tag)
                    elif intent.action == "sell":
                        fill_payload.setdefault("exit_tag", intent_signal_tag)
                # Propagate the optional exit categorization (Signal.exit_reason →
                # OrderIntent.exit_reason) onto the SELL fill so the backtest
                # summary's by_exit_reason block and trade_fills.exit_reason can
                # attribute the round-trip by kind.
                intent_exit_reason = getattr(intent, "exit_reason", None)
                if intent.action == "sell" and intent_exit_reason:
                    fill_payload.setdefault("exit_reason", intent_exit_reason)
                    span = otel_trace.get_current_span()
                    if span is not None:
                        span.set_attribute("exit_reason", str(intent_exit_reason))
                await self._persist_trade_fill_realtime(
                    state=state,
                    fill_payload=fill_payload,
                    rationale=intent.rationale,
                    parent_run_id=parent_run_id,
                    session_id=session_id,
                )
                await self._record_phase(
                    state,
                    "dispatch_orders",
                    {"intent_id": intent.intent_id, "status": "submitted"},
                )
                return fill_payload
            # Adapter rejected the intent (zero-quantity fill, missing amount,
            # broker reject, etc.) — already emits execution_zero_fill /
            # qmt_order_rejected. Surface the worker-side view too so
            # cycle_runs.details makes plain why submitted_count did not grow.
            logger.warning(
                "worker dispatch rejected agent_name=%s task_id=%s "
                "run_id=%s intent_id=%s symbol=%s action=%s "
                "(execution adapter returned no fill)",
                state.agent_name,
                state.task_id,
                state.run_id,
                intent.intent_id,
                intent.symbol,
                intent.action,
            )
            await emit_debug_event(
                "dispatch_rejected",
                {
                    "intent_id": intent.intent_id,
                    "symbol": intent.symbol,
                    "action": intent.action,
                    "amount": intent.amount,
                    "hint": (
                        "execution_adapter.submit_intent returned None / non-fill. "
                        "Cross-check the matching execution_zero_fill / "
                        "qmt_order_rejected event for the rejection reason."
                    ),
                },
            )
            await self._record_phase(
                state,
                "dispatch_orders",
                {"intent_id": intent.intent_id, "status": "rejected"},
            )
            return None

    async def dispatch_preapproved_intent(self, intent, *, run_id: str) -> dict[str, Any] | None:
        """Dispatch an ALREADY-APPROVED intent held across cycles (resume path).

        The scheduler's resume sweep calls this when a human approved an order
        after the cycle that produced it ended. It re-submits through the SAME
        ``_dispatch_approved_intent`` path as an in-cycle order — so mock and qmt
        behave identically — and persists the fill against the ORIGINAL
        ``run_id`` so it stays correlated with the cycle / approval / spans that
        produced it (run_id 贯穿).

        ``market_context`` is None on purpose: the order dispatches at its
        captured ``intent.price_reference`` (the price the approver saw, from
        which the notional was computed), so resume is deterministic and needs
        no fresh quote. For a QMT LIMIT order that becomes the limit price.
        """
        state = self._new_cycle_state(run_id)
        state.cycle_time = datetime.now(timezone.utc).replace(tzinfo=None)
        with tracer.start_as_current_span(
            "approval.resume.dispatch",
            attributes={
                "doyoutrade.span_type": "approval_resume",
                "run_id": run_id,
                "intent_id": getattr(intent, "intent_id", ""),
                "symbol": getattr(intent, "symbol", ""),
                "action": getattr(intent, "action", ""),
            },
        ):
            return await self._dispatch_approved_intent(
                intent,
                state,
                None,
                parent_run_id=None,
                session_id=None,
            )

    async def _record_phase(self, state: CycleRunState, phase: str, payload: dict):
        summary = _phase_span_summary(phase, payload)
        attr_summary = {k: _as_otel_attribute_value(v) for k, v in summary.items()}
        extra_attrs: dict[str, Any] = {}
        # Attach strategy code version to ALL phase spans so the OTel trace carries
        # the pinned version alongside run_id for every phase (§最低同步要求).
        # This is unconditional so the version is visible regardless of which phase a
        # span belongs to — not just "generate_signals".
        pinned_version = getattr(self.signal_generator, "_pinned_version", None)
        if pinned_version:
            extra_attrs["doyoutrade.strategy.code_version"] = str(pinned_version)
        pinned_hash = getattr(self.signal_generator, "_pinned_code_hash", None)
        if pinned_hash:
            extra_attrs["doyoutrade.strategy.code_hash"] = str(pinned_hash)
        with tracer.start_as_current_span(
            "worker.phase",
            attributes={
                "doyoutrade.span_type": "phase",
                "run_id": state.run_id,
                "phase": phase,
                **attr_summary,
                **extra_attrs,
            },
        ):
            await emit_debug_event(
                "phase",
                {
                    "phase": phase,
                    "payload": dict(payload),
                },
            )
            details = " ".join(f"{key}={value}" for key, value in summary.items())
            if details:
                logger.info(
                    "worker phase completed agent_name=%s task_id=%s run_id=%s phase=%s %s",
                    state.agent_name,
                    state.task_id,
                    state.run_id,
                    phase,
                    details,
                )
            else:
                logger.info(
                    "worker phase completed agent_name=%s task_id=%s run_id=%s phase=%s",
                    state.agent_name,
                    state.task_id,
                    state.run_id,
                    phase,
                )

    async def aclose(self):
        for candidate in (self.data_provider, self.execution_adapter):
            close = getattr(candidate, "aclose", None)
            if close is not None:
                await _maybe_await(close())


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value
