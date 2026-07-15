"""MonitorDaemon — event-driven realtime evaluation of 盯盘规则.

Subscribes to the realtime quote stream as an in-process observer (not a WS
client, so it keeps streaming with the browser closed), evaluates each active
rule's condition tree per snapshot, and — on a rising-edge + cooldown-gated
fire — mints a run_id, opens a ``session_type='monitor'`` debug session + a
``monitor.condition`` / ``monitor.delivery`` span tree, persists a
``monitor_alerts`` row, and delivers via the channel pipeline.

Observability is deliberately fire-scoped: routine ticks evaluate in memory and
only bump counters; spans/sessions are opened on a fire or once per periodic
sweep (which emits ``monitor_sweep_summary`` with the aggregated skip/suppress
counters + does the daily state reset). This keeps ``debug_session_spans`` from
flooding while every user-visible state change (a fire) stays fully traceable
(CLAUDE.md §错误可见性 / §最低同步要求).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from doyoutrade.assistant.trading_sessions import is_ashare_continuous_trading
from doyoutrade.debug import emit_debug_event
from doyoutrade.monitoring.conditions import iter_referenced_presets
from doyoutrade.monitoring.dedup import DedupGate
from doyoutrade.monitoring.evaluator import EvalContext, MonitorEvalError, evaluate_tree
from doyoutrade.monitoring.observability import (
    EVENT_ALERT_FIRED,
    EVENT_CONDITION_TREE_INVALID,
    EVENT_SEAL_DATA_UNAVAILABLE,
    EVENT_SWEEP_SUMMARY,
    SPAN_CONDITION,
    SPAN_SWEEP,
    detached_trace_root,
    new_run_id,
)
from doyoutrade.monitoring.presets import PRESET_LABELS
from doyoutrade.monitoring.state import IntradayStateStore, trading_day_for
from doyoutrade.observability.debug_span_export import (
    debug_span_export_for_session,
    drain_debug_span_persist_queue,
)
from doyoutrade.observability.tracing import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer(__name__)

_NO_COOLDOWN = 0


@dataclass
class _Counters:
    evaluated: int = 0
    fired: int = 0
    suppressed_edge: int = 0
    suppressed_cooldown: int = 0
    seal_missing: int = 0
    out_of_session: int = 0
    eval_errors: int = 0
    delivered: int = 0
    delivery_failed: int = 0

    def reset(self) -> None:
        for f in self.__dataclass_fields__:
            setattr(self, f, 0)

    def as_dict(self) -> dict[str, int]:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}


def _condition_label(condition_json: dict) -> str:
    """A stable per-rule condition name used for dedup + the alert row.

    A single-preset rule uses that preset's name; anything else is 'composite'.
    """
    if isinstance(condition_json, dict) and "preset" in condition_json:
        return str(condition_json["preset"])
    return "composite"


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


class MonitorDaemon:
    def __init__(
        self,
        *,
        quote_stream_service,
        monitor_rule_repository,
        monitor_alert_repository,
        watchlist_repository=None,
        debug_session_repository=None,
        debug_session_span_repository=None,
        assistant_service=None,
        timezone: str = "Asia/Shanghai",
        sweep_interval_seconds: float = 60.0,
        logger_override: logging.Logger | None = None,
    ) -> None:
        self._quote_stream = quote_stream_service
        self._rules_repo = monitor_rule_repository
        self._alerts_repo = monitor_alert_repository
        self._watchlist_repo = watchlist_repository
        self._session_repo = debug_session_repository
        self._span_repo = debug_session_span_repository
        self._assistant_service = assistant_service
        self._timezone = timezone
        self._sweep_interval = sweep_interval_seconds
        self._log = logger_override or logger

        self._state = IntradayStateStore()
        self._dedup = DedupGate()
        self._counters = _Counters()

        # rebuilt by reload_rules()
        self._symbol_to_rules: dict[str, list[Any]] = {}
        self._condition_names: dict[str, str] = {}
        self._display_names: dict[str, str | None] = {}
        self._monitored_symbols: set[str] = set()

        self._sweep_task: asyncio.Task | None = None
        self._current_day: str | None = None
        self._started = False
        self._reload_lock = asyncio.Lock()

    # ----- lifecycle ---------------------------------------------------

    async def start(self) -> None:
        if self._rules_repo is None:
            self._log.info("monitor_daemon: no rule repository; daemon inert")
            return
        self._started = True
        await self.reload_rules()
        await self._rehydrate_dedup()
        if self._quote_stream is not None:
            self._quote_stream.add_snapshot_observer(self._on_snapshot)
        self._sweep_task = asyncio.create_task(self._sweep_loop(), name="monitor_daemon_sweep")
        self._log.info(
            "monitor_daemon started rules_symbols=%d", len(self._monitored_symbols)
        )

    async def stop(self) -> None:
        self._started = False
        if self._quote_stream is not None:
            self._quote_stream.remove_snapshot_observer(self._on_snapshot)
            try:
                await self._quote_stream.set_monitored_symbols(set())
            except Exception as exc:  # noqa: BLE001 — visible
                self._log.warning("monitor_daemon: clear monitored symbols failed: %s", exc)
        task = self._sweep_task
        self._sweep_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._log.info("monitor_daemon stopped")

    # ----- rule index --------------------------------------------------

    async def reload_rules(self) -> None:
        """(Re)load active rules, rebuild the symbol index, re-pin the stream.

        Called at start and by the rule-CRUD API after any change — never polled
        per tick.
        """
        async with self._reload_lock:
            rules = await self._rules_repo.list_active()
            symbol_to_rules: dict[str, list[Any]] = {}
            condition_names: dict[str, str] = {}
            display: dict[str, str | None] = {}
            all_symbols: set[str] = set()
            for rule in rules:
                condition_names[rule.id] = _condition_label(rule.condition_json)
                symbols, names = await self._resolve_scope(rule)
                for sym in symbols:
                    symbol_to_rules.setdefault(sym, []).append(rule)
                    all_symbols.add(sym)
                for sym, name in names.items():
                    display.setdefault(sym, name)
            self._symbol_to_rules = symbol_to_rules
            self._condition_names = condition_names
            self._display_names = display
            self._monitored_symbols = all_symbols

        if self._quote_stream is not None:
            await self._quote_stream.set_monitored_symbols(all_symbols)
        self._state.forget(all_symbols)
        self._log.info(
            "monitor_daemon reloaded rules=%d symbols=%d", len(self._condition_names), len(all_symbols)
        )

    async def _resolve_scope(self, rule) -> tuple[set[str], dict[str, str | None]]:
        """Resolve a rule's scope to (symbols, {symbol: display_name})."""
        scope = rule.scope_json or {}
        if rule.scope_kind == "symbols":
            syms = {str(s) for s in (scope.get("symbols") or [])}
            return syms, {}
        if rule.scope_kind == "watchlist_tag":
            if self._watchlist_repo is None:
                return set(), {}
            tag = scope.get("tag")
            tag_arg = None if tag in (None, "", "*") else str(tag)
            entries = await self._watchlist_repo.list_entries(tag_arg)
            syms: set[str] = set()
            names: dict[str, str | None] = {}
            for entry in entries:
                sym = entry.get("symbol")
                if not sym:
                    continue
                syms.add(str(sym))
                names[str(sym)] = entry.get("display_name")
            return syms, names
        return set(), {}

    async def _rehydrate_dedup(self) -> None:
        if self._alerts_repo is None:
            return
        try:
            latest = await self._alerts_repo.list_latest_per_dedup_key()
        except Exception as exc:  # noqa: BLE001 — visible, non-fatal
            self._log.warning("monitor_daemon: dedup rehydrate query failed: %s", exc)
            return
        last_fired = {
            (a.monitor_rule_id, a.symbol, a.condition_name): a.triggered_at for a in latest
        }
        count = self._dedup.rehydrate(last_fired)
        if count:
            self._log.info("monitor_dedup_rehydrated count=%d", count)

    # ----- per-tick evaluation ----------------------------------------

    async def _on_snapshot(self, symbol: str, snapshot) -> None:
        """Observer entry: evaluate every rule interested in ``symbol``.

        Raising is caught by the quote stream's observer isolation, but we also
        guard here so one symbol's failure never drops the others.
        """
        if not self._started:
            return
        rules = self._symbol_to_rules.get(symbol)
        if not rules:
            return
        now = datetime.now(timezone.utc)
        if not is_ashare_continuous_trading(now, timezone=self._timezone):
            self._counters.out_of_session += 1
            return
        trading_day = trading_day_for(now, timezone=self._timezone)
        state = self._state.get_or_reset(symbol, trading_day)
        ctx = EvalContext(snapshot=snapshot, state=state, now=now)

        for rule in rules:
            try:
                triggered, leaves = evaluate_tree(rule.condition_json, ctx)
            except MonitorEvalError as exc:
                self._counters.eval_errors += 1
                self._log.warning(
                    "monitor_condition_tree_invalid rule=%s symbol=%s reason=%s msg=%s",
                    rule.id,
                    symbol,
                    exc.reason,
                    exc,
                )
                continue
            self._counters.evaluated += 1
            # Surface a seal-data gap (a shrink leaf that could not judge) — counted
            # for the sweep summary so it is visible without per-tick spans.
            if any(
                leaf.diagnostics.get("skipped_reason") == "seal_vol_missing" for leaf in leaves
            ):
                self._counters.seal_missing += 1
            condition_name = self._condition_names.get(rule.id, "composite")
            key = (rule.id, symbol, condition_name)
            cooldown = int(getattr(rule, "cooldown_seconds", 0) or _NO_COOLDOWN)
            fire, suppressed = self._dedup.should_fire(
                key, triggered=triggered, now=now, cooldown_seconds=cooldown
            )
            if fire:
                await self._fire(rule, symbol, snapshot, leaves, now, condition_name, trading_day)
            elif suppressed == "edge_not_rising":
                self._counters.suppressed_edge += 1
            elif suppressed == "within_cooldown":
                self._counters.suppressed_cooldown += 1
                remaining = self._dedup.remaining_cooldown(key, now=now, cooldown_seconds=cooldown)
                self._log.info(
                    "monitor_alert_suppressed_cooldown rule=%s symbol=%s condition=%s remaining_s=%.0f",
                    rule.id,
                    symbol,
                    condition_name,
                    remaining,
                )

        # Fold this tick into per-symbol state AFTER evaluation so 大减 measures a
        # drop from the prior peak and 打开 is a true seal→unseal transition.
        state.fold_snapshot(snapshot)

    # ----- fire path (run_id + debug session + spans + deliver) -------

    async def _fire(self, rule, symbol, snapshot, leaves, now, condition_name, trading_day) -> None:
        run_id = new_run_id()
        session_id = f"sess-mon-{uuid.uuid4().hex}"
        now_naive = _to_naive_utc(now)
        last_price = getattr(snapshot, "price", None)
        leaf_payload = [
            {
                "kind": leaf.kind,
                "label": leaf.label,
                "triggered": leaf.triggered,
                "diagnostics": leaf.diagnostics,
            }
            for leaf in leaves
        ]
        limit_price = next(
            (
                leaf.diagnostics.get("limit_price")
                for leaf in leaves
                if leaf.triggered and leaf.diagnostics.get("limit_price") is not None
            ),
            None,
        )
        self._counters.fired += 1
        self._log.info(
            "monitor_alert_fired rule=%s symbol=%s condition=%s run_id=%s price=%s",
            rule.id,
            symbol,
            condition_name,
            run_id,
            last_price,
        )

        # Best-effort debug session so the fire is visible in the debug viewer.
        if self._session_repo is not None:
            try:
                await self._session_repo.create_session(
                    session_id=session_id,
                    task_id=rule.id,
                    config_overrides=None,
                    input_overrides=None,
                    session_type="monitor",
                )
            except Exception as exc:  # noqa: BLE001 — visible, non-fatal
                self._log.warning(
                    "monitor_debug_session_create_failed rule=%s run_id=%s err=%s",
                    rule.id,
                    run_id,
                    exc,
                )

        export_ctx = (
            debug_span_export_for_session(session_id, "monitor")
            if self._span_repo is not None
            else nullcontext()
        )
        alert_id: int | None = None
        delivery_status = "pending"
        with detached_trace_root():
            with export_ctx:
                with tracer.start_as_current_span(SPAN_CONDITION) as span:
                    span.set_attribute("monitor.rule_id", rule.id)
                    span.set_attribute("monitor.symbol", symbol)
                    span.set_attribute("monitor.condition_name", condition_name)
                    span.set_attribute("monitor.triggered", True)
                    span.set_attribute("monitor.run_id", run_id)
                    span.set_attribute("monitor.status", "fired")
                    await emit_debug_event(
                        EVENT_ALERT_FIRED,
                        {
                            "run_id": run_id,
                            "monitor_rule_id": rule.id,
                            "symbol": symbol,
                            "condition_name": condition_name,
                            "triggered": True,
                            "last_price": last_price,
                            "limit_price": limit_price,
                            "leaf_diagnostics": leaf_payload,
                            "hint": "condition tree fired; pushing alert to the rule's channel",
                        },
                    )
                    for leaf in leaves:
                        if leaf.diagnostics.get("skipped_reason") == "seal_vol_missing":
                            await emit_debug_event(
                                EVENT_SEAL_DATA_UNAVAILABLE,
                                {
                                    "symbol": symbol,
                                    "preset": leaf.label,
                                    "reason": "seal_vol_missing",
                                    "hint": "order-book seal volume absent; 大减 leaf could not judge",
                                },
                            )

                    if self._alerts_repo is not None:
                        try:
                            alert = await self._alerts_repo.insert_alert(
                                monitor_rule_id=rule.id,
                                symbol=symbol,
                                condition_name=condition_name,
                                transition_key=trading_day,
                                triggered_at=now_naive,
                                last_price=last_price,
                                limit_price=limit_price,
                                diagnostics_json={"leaves": leaf_payload},
                                run_id=run_id,
                                delivery_status="pending",
                            )
                            alert_id = alert.id
                        except Exception as exc:  # noqa: BLE001 — visible, non-fatal
                            self._log.warning(
                                "monitor_alert_persist_failed rule=%s run_id=%s err=%s",
                                rule.id,
                                run_id,
                                exc,
                            )

                    delivery_status = await self._deliver(
                        rule,
                        symbol=symbol,
                        condition_name=condition_name,
                        diagnostics={"leaves": leaf_payload},
                        triggered_at=now,
                        last_price=last_price,
                        limit_price=limit_price,
                        run_id=run_id,
                    )
                    span.set_attribute("monitor.delivery.status", delivery_status)

        # Record the fire for cooldown BEFORE awaiting the post-span bookkeeping.
        self._dedup.record_fired((rule.id, symbol, condition_name), now=now)

        await drain_debug_span_persist_queue()
        if self._session_repo is not None:
            try:
                await self._session_repo.attach_run_id(session_id, run_id)
                await self._session_repo.mark_finished(
                    session_id, status="finished", error_message=""
                )
            except Exception as exc:  # noqa: BLE001 — visible, non-fatal
                self._log.warning(
                    "monitor_debug_session_finalize_failed run_id=%s err=%s", run_id, exc
                )
        if alert_id is not None and delivery_status != "pending" and self._alerts_repo is not None:
            delivered_at = now_naive if delivery_status == "forwarded" else None
            try:
                await self._alerts_repo.mark_delivered(
                    alert_id, delivery_status=delivery_status, delivered_at=delivered_at
                )
            except Exception as exc:  # noqa: BLE001 — visible, non-fatal
                self._log.warning(
                    "monitor_alert_mark_delivered_failed alert_id=%s err=%s", alert_id, exc
                )
        if delivery_status == "forwarded":
            self._counters.delivered += 1
        elif delivery_status in ("forward_failed",):
            self._counters.delivery_failed += 1

    async def _deliver(
        self,
        rule,
        *,
        symbol: str,
        condition_name: str,
        diagnostics: dict,
        triggered_at: datetime,
        last_price: float | None,
        limit_price: float | None,
        run_id: str,
    ) -> str:
        if self._assistant_service is None or not rule.delivery_json:
            return "skipped"
        # Lazy import keeps the heavy assistant.channels import off the module
        # load path and avoids any import cycle.
        from doyoutrade.runtime.monitor_delivery import deliver_monitor_alert

        try:
            return await deliver_monitor_alert(
                self._assistant_service,
                rule=rule,
                symbol=symbol,
                display_name=self._display_names.get(symbol),
                condition_name=condition_name,
                diagnostics=diagnostics,
                triggered_at=triggered_at,
                last_price=last_price,
                limit_price=limit_price,
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001 — visible, best-effort
            self._log.exception(
                "monitor_delivery_failed rule=%s symbol=%s run_id=%s err=%s",
                rule.id,
                symbol,
                run_id,
                exc,
            )
            return "forward_failed"

    # ----- periodic sweep ----------------------------------------------

    async def _sweep_loop(self) -> None:
        while self._started:
            try:
                await asyncio.sleep(self._sweep_interval)
            except asyncio.CancelledError:
                raise
            if not self._started:
                return
            try:
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — visible, loop survives
                self._log.warning("monitor_daemon sweep failed: %s", exc)

    async def _sweep_once(self) -> None:
        now = datetime.now(timezone.utc)
        trading_day = trading_day_for(now, timezone=self._timezone)
        day_reset = 0
        if self._current_day is not None and self._current_day != trading_day:
            day_reset = self._state.reset_day(trading_day)
            self._dedup.reset_edges()
        self._current_day = trading_day

        counts = self._counters.as_dict()
        self._counters.reset()
        with detached_trace_root():
            with tracer.start_as_current_span(SPAN_SWEEP) as span:
                span.set_attribute("monitor.active_rules", len(self._condition_names))
                span.set_attribute("monitor.monitored_symbols", len(self._monitored_symbols))
                span.set_attribute("monitor.state_symbols", self._state.size())
                span.set_attribute("monitor.day_reset_count", day_reset)
                await emit_debug_event(
                    EVENT_SWEEP_SUMMARY,
                    {
                        "trading_day": trading_day,
                        "active_rules": len(self._condition_names),
                        "monitored_symbols": len(self._monitored_symbols),
                        "state_symbols": self._state.size(),
                        "day_reset_count": day_reset,
                        "counters": counts,
                        "hint": "per-sweep aggregate of monitor evaluations / suppressions / skips",
                    },
                )
        if any(counts.values()) or day_reset:
            self._log.info(
                "monitor_sweep day=%s rules=%d symbols=%d counters=%s day_reset=%d",
                trading_day,
                len(self._condition_names),
                len(self._monitored_symbols),
                counts,
                day_reset,
            )
