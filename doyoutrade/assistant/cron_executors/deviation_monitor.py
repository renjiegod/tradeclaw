"""``deviation_monitor`` cron task executor — 交易纪律提醒.

Use case: the user buys a stock with a plan ("连阳、不破5日线，跌破止损就提醒我")
and wants the agent to watch it intraday (~14:50, before the daily bar seals)
and remind them — recalling the *original* reasoning — only when the price
**deviates from that plan**, staying silent otherwise.

The deviation logic itself is a user-authored **Strategy SDK** strategy
(``sd-…``): full numpy/pandas expressiveness for 破5日线 / 大阴线 / 连阳被破坏 /
放量下跌 / 跌破成本, gated by the same AST/compile safety net as ``sdk validate``.
This executor is the glue that the SDK never gets on its own at fire time:

  1. Compile the strategy from its ``sd-`` definition (``strategy_loader``).
  2. Gather the live account statement (cost basis + which symbols are held)
     via ``statement_provider`` so 跌破成本 / current_profit are real, not flat.
  3. Fetch the live ~14:50 quote per symbol (``quote_fetcher``) and splice it
     onto warehouse history as a forming day-bar
     (:class:`~doyoutrade.strategy_sdk.live_overlay.LiveBarOverlayHistoryFetcher`)
     so ``on_bar`` sees today's price.
  4. Evaluate the strategy per symbol with the *real* position injected
     (:meth:`StrategyRunner.evaluate_one_signal`).
  5. A ``Signal.sell`` is read as "plan violated"; ``hold`` / ``buy`` as "still
     on plan". Deviations are composed into a reminder that recalls the stored
     thesis and nudges the user to act per plan; **no deviations → ``[SILENT]``**
     so the fire is suppressed.

Every skip is structured, never silent (CLAUDE.md §错误可见性): a symbol that
is not held (``require_position``), whose live quote is unavailable, or whose
rule raises emits a ``deviation_monitor_*`` debug event + a ``logger`` line and
is excluded from the reminder — it never silently disappears.

Params (validated by :meth:`validate_params`):

  - ``strategy_definition_id`` (str, required) — the ``sd-…`` deviation rule.
  - ``symbols`` (list[str], required) — symbols to watch.
  - ``target_session_id`` (str | None) — session to push into (null →
    ``delivery_status='skipped'``).
  - ``thesis`` (str | dict[str,str] | None) — the original buy reasoning,
    recalled verbatim in the reminder. A dict maps symbol → thesis; a string
    applies to all watched symbols.
  - ``account_id`` (str | None) — which account to read positions from.
  - ``parameter_overrides`` (dict | None) — strategy parameter overrides.
  - ``require_position`` (bool, default True) — only watch symbols currently
    held; a symbol no longer held is skipped (self-suppressing teardown after a
    sell, until the job is deleted).
  - ``data_source`` (str, default "auto") — market-data source for history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, ClassVar, Optional
from zoneinfo import ZoneInfo

from doyoutrade.core.models import QuoteSnapshot
from doyoutrade.debug import emit_debug_event
from doyoutrade.execution.position_manager import PositionManager
from doyoutrade.observability import get_logger, get_tracer
from doyoutrade.strategy_sdk.context import AccountView, PositionView
from doyoutrade.strategy_sdk.live_overlay import LiveBarOverlayHistoryFetcher
from doyoutrade.strategy_sdk.runner import StrategyRunner
from doyoutrade.strategy_sdk.signal import Signal

from ._deliver import SILENT_SENTINEL, deliver_assistant_message_to_session
from .base import JobRunContext, TaskResult

logger = get_logger(__name__)
tracer = get_tracer(__name__)

KIND = "deviation_monitor"

_A_SHARE_TZ = ZoneInfo("Asia/Shanghai")

# A large synthetic account is fine when the statement can't supply cash/equity:
# the deviation rules only read price/position, not account sizing.
_SYNTHETIC_EQUITY = Decimal("100000000")


@dataclass(frozen=True)
class LoadedStrategy:
    """A compiled, smoke-tested strategy ready to instantiate."""

    strategy_class: type
    class_name: str


@dataclass(frozen=True)
class _Holding:
    symbol: str
    quantity: float
    cost_price: Decimal
    market_price: float | None
    name: str | None


#: ``strategy_loader(sd_id) -> LoadedStrategy`` — compiles a strategy definition.
StrategyLoader = Callable[[str], Awaitable[LoadedStrategy]]
#: ``statement_provider(account_id, asof, captured_at) -> statement dict``.
StatementProvider = Callable[
    [Optional[str], date, datetime], Awaitable[dict[str, Any]]
]
#: ``quote_fetcher(symbols) -> {symbol: QuoteSnapshot}``.
QuoteFetcher = Callable[[list[str]], Awaitable[dict[str, QuoteSnapshot]]]
#: ``history_fetcher_factory(symbols, data_source) -> HistoryFetcher`` (base
#: warehouse fetcher; the executor wraps it with the live-bar overlay).
HistoryFetcherFactory = Callable[[list[str], str], Awaitable[Any]]


def _asof_from_fired_at(fired_at: datetime) -> tuple[datetime, date]:
    """Return ``(fire_time_in_shanghai, trading_day)`` for an A-share fire."""
    if fired_at.tzinfo is None:
        fired_at = fired_at.replace(tzinfo=timezone.utc)
    local = fired_at.astimezone(_A_SHARE_TZ)
    return local, local.date()


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def parse_holdings(statement: dict[str, Any]) -> dict[str, _Holding]:
    """Project a statement's ``account.positions`` rows into held lots by symbol.

    ``build_post_cycle_account`` already drops zero-quantity rows, so presence
    in the map means "currently held". Malformed numeric fields degrade to
    ``None`` / ``0`` rather than raising — the caller decides what to skip.
    """
    out: dict[str, _Holding] = {}
    account = statement.get("account") if isinstance(statement, dict) else None
    if not isinstance(account, dict):
        return out
    rows = account.get("positions")
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        out[symbol] = _Holding(
            symbol=symbol,
            quantity=_float_or_none(row.get("quantity")) or 0.0,
            cost_price=_decimal_or_none(row.get("cost_price")) or Decimal("0"),
            market_price=_float_or_none(row.get("last_price")),
            name=row.get("name") if isinstance(row.get("name"), str) else None,
        )
    return out


def parse_account_view(statement: dict[str, Any]) -> AccountView:
    """Build an :class:`AccountView` from the statement, or a synthetic one."""
    account = statement.get("account") if isinstance(statement, dict) else None
    if isinstance(account, dict):
        inner = account.get("account")
        if isinstance(inner, dict):
            cash = _decimal_or_none(inner.get("cash"))
            equity = _decimal_or_none(inner.get("equity"))
            if cash is not None and equity is not None:
                return AccountView(cash=cash, equity=equity)
    return AccountView(cash=_SYNTHETIC_EQUITY, equity=_SYNTHETIC_EQUITY)


def _resolve_thesis(thesis: Any, symbol: str) -> str:
    if isinstance(thesis, dict):
        value = thesis.get(symbol)
        return str(value).strip() if isinstance(value, str) else ""
    if isinstance(thesis, str):
        return thesis.strip()
    return ""


@dataclass
class _Deviation:
    symbol: str
    name: str | None
    signal: Signal
    holding: _Holding | None
    quote: QuoteSnapshot
    thesis: str


def compose_reminder(
    *,
    asof_local: datetime,
    deviations: list[_Deviation],
) -> str:
    """Compose the user-facing discipline reminder (Chinese, deterministic).

    Recalls each symbol's original thesis, states the deviation the strategy
    detected (its ``tag`` / ``rationale`` / ``diagnostics``), shows the live
    price vs cost, and nudges the user to act per plan. Returns the
    ``[SILENT]`` sentinel when there is nothing to report.
    """
    if not deviations:
        return SILENT_SENTINEL

    when = asof_local.strftime("%Y-%m-%d %H:%M")
    lines: list[str] = ["⚠️ 交易纪律提醒", ""]
    for dev in deviations:
        label = f"{dev.symbol}" + (f" {dev.name}" if dev.name else "")
        lines.append(f"【{label}】")
        if dev.thesis:
            lines.append(f"你的原始买入计划：{dev.thesis}")
        sig = dev.signal
        deviation_desc = sig.rationale or sig.tag or "已偏离你的持有计划"
        lines.append(f"但今天（{when}）触发了偏离信号：{deviation_desc}")
        if sig.tag:
            lines.append(f"  · 触发因子：{sig.tag}")
        if sig.diagnostics:
            diag = "，".join(f"{k}={v}" for k, v in sig.diagnostics.items())
            if diag:
                lines.append(f"  · 关键数据：{diag}")
        price = dev.quote.price
        if price is not None:
            cost_line = f"  · 当前价 {price}"
            if dev.holding is not None and dev.holding.cost_price > 0:
                cost = float(dev.holding.cost_price)
                pnl_pct = (price - cost) / cost * 100.0
                cost_line += f"（成本 {cost:g}，浮动 {pnl_pct:+.2f}%）"
            lines.append(cost_line)
        lines.append("")
    lines.append("不及预期。请按你当初的计划行事，不要临时改变主意。")
    return "\n".join(lines).strip()


class DeviationMonitorExecutor:
    """Task executor for the ``deviation_monitor`` kind."""

    kind: ClassVar[str] = KIND

    def __init__(
        self,
        *,
        assistant_service: Any,
        cron_job_repository: Any,
        strategy_loader: StrategyLoader,
        statement_provider: StatementProvider,
        quote_fetcher: QuoteFetcher,
        history_fetcher_factory: HistoryFetcherFactory,
    ):
        self._svc = assistant_service
        self._cron_repo = cron_job_repository
        self._strategy_loader = strategy_loader
        self._statement_provider = statement_provider
        self._quote_fetcher = quote_fetcher
        self._history_fetcher_factory = history_fetcher_factory

    # --- contract validation ----------------------------------------------

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(params, dict):
            return {
                "error_code": "invalid_task_params",
                "error": "params must be an object",
            }
        sd_id = params.get("strategy_definition_id")
        if not isinstance(sd_id, str) or not sd_id.strip():
            return {
                "error_code": "missing_strategy_definition_id",
                "error": "deviation_monitor.params.strategy_definition_id is required",
                "field": "strategy_definition_id",
            }
        symbols = params.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            return {
                "error_code": "missing_symbols",
                "error": "deviation_monitor.params.symbols must be a non-empty array",
                "field": "symbols",
            }
        if not all(isinstance(s, str) and s.strip() for s in symbols):
            return {
                "error_code": "invalid_symbols",
                "error": "deviation_monitor.params.symbols must all be non-empty strings",
                "field": "symbols",
            }
        target = params.get("target_session_id")
        if target is not None and (not isinstance(target, str) or not target.strip()):
            return {
                "error_code": "invalid_target_session_id",
                "error": "target_session_id must be a string or null",
                "field": "target_session_id",
            }
        thesis = params.get("thesis")
        if thesis is not None and not isinstance(thesis, (str, dict)):
            return {
                "error_code": "invalid_thesis",
                "error": "thesis must be a string, an object {symbol: text}, or null",
                "field": "thesis",
            }
        account_id = params.get("account_id")
        if account_id is not None and (
            not isinstance(account_id, str) or not account_id.strip()
        ):
            return {
                "error_code": "invalid_account_id",
                "error": "account_id must be a string or null",
                "field": "account_id",
            }
        overrides = params.get("parameter_overrides")
        if overrides is not None and not isinstance(overrides, dict):
            return {
                "error_code": "invalid_parameter_overrides",
                "error": "parameter_overrides must be an object or null",
                "field": "parameter_overrides",
            }
        require_position = params.get("require_position")
        if require_position is not None and not isinstance(require_position, bool):
            return {
                "error_code": "invalid_require_position",
                "error": "require_position must be a boolean or null",
                "field": "require_position",
            }
        return None

    # --- runtime ----------------------------------------------------------

    async def run(self, params: dict[str, Any], ctx: JobRunContext) -> TaskResult:
        with tracer.start_as_current_span("cron.task.run") as span:
            span.set_attribute("cron.task.kind", self.kind)
            span.set_attribute("cron.job_id", ctx.job_id)
            span.set_attribute("cron.job_run_id", ctx.cron_job_run_id)

            sd_id = str(params.get("strategy_definition_id") or "").strip()
            symbols = [str(s).strip() for s in (params.get("symbols") or []) if str(s).strip()]
            target_session_id = params.get("target_session_id")
            if target_session_id is not None:
                target_session_id = str(target_session_id).strip() or None
            account_id = params.get("account_id")
            account_id = str(account_id).strip() if account_id else None
            thesis = params.get("thesis")
            parameter_overrides = params.get("parameter_overrides") or {}
            require_position = params.get("require_position")
            require_position = True if require_position is None else bool(require_position)
            data_source = str(params.get("data_source") or "auto")

            local_now, asof = _asof_from_fired_at(ctx.fired_at)
            span.set_attribute("deviation_monitor.strategy_definition_id", sd_id)
            span.set_attribute("deviation_monitor.symbol_count", len(symbols))
            span.set_attribute("deviation_monitor.asof", asof.isoformat())

            job = await self._cron_repo.get_job(ctx.job_id)
            if not job:
                span.set_attribute("cron.task.status", "failed")
                return TaskResult(status="failed", error=f"cron job not found: {ctx.job_id}")

            # 1) Compile the deviation strategy.
            try:
                loaded = await self._strategy_loader(sd_id)
            except Exception as exc:  # noqa: BLE001 — distinct failure mode
                logger.exception(
                    "deviation_monitor strategy load failed job_id=%s sd=%s",
                    ctx.job_id, sd_id,
                )
                await emit_debug_event(
                    "deviation_monitor_strategy_unavailable",
                    {
                        "job_id": ctx.job_id,
                        "strategy_definition_id": sd_id,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "hint": "could not compile the deviation strategy; fix the "
                        "sd- source (see strategy-definition-authoring) — error "
                        "mirrors `sdk validate`",
                    },
                )
                span.set_attribute("cron.task.status", "failed")
                span.set_attribute("cron.task.error", f"{type(exc).__name__}: {exc}")
                return TaskResult(
                    status="failed",
                    error=f"strategy_unavailable: {type(exc).__name__}: {exc}",
                )

            # 2) Gather positions (cost basis + which symbols are held).
            try:
                statement = await self._statement_provider(account_id, asof, ctx.fired_at)
            except Exception as exc:  # noqa: BLE001 — distinct failure mode
                logger.exception(
                    "deviation_monitor statement gather failed job_id=%s asof=%s",
                    ctx.job_id, asof,
                )
                await emit_debug_event(
                    "deviation_monitor_data_unavailable",
                    {
                        "job_id": ctx.job_id,
                        "asof": asof.isoformat(),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "hint": "could not read the account statement (account "
                        "resolution / QMT connection); cost-basis checks would be "
                        "wrong, so the fire is failed rather than half-evaluated",
                    },
                )
                span.set_attribute("cron.task.status", "failed")
                span.set_attribute("cron.task.error", f"{type(exc).__name__}: {exc}")
                return TaskResult(
                    status="failed",
                    error=f"data_unavailable: {type(exc).__name__}: {exc}",
                )

            holdings = parse_holdings(statement)
            account_view = parse_account_view(statement)

            # 3) Fetch live quotes for the watched symbols.
            try:
                quotes = await self._quote_fetcher(symbols)
            except Exception as exc:  # noqa: BLE001 — distinct failure mode
                logger.exception(
                    "deviation_monitor quote fetch failed job_id=%s symbols=%s",
                    ctx.job_id, symbols,
                )
                await emit_debug_event(
                    "deviation_monitor_data_unavailable",
                    {
                        "job_id": ctx.job_id,
                        "asof": asof.isoformat(),
                        "stage": "quote_fetch",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "hint": "live quote fetch raised; cannot evaluate the 14:50 "
                        "forming bar — check the quote stream / qmt connection",
                    },
                )
                span.set_attribute("cron.task.status", "failed")
                span.set_attribute("cron.task.error", f"{type(exc).__name__}: {exc}")
                return TaskResult(
                    status="failed",
                    error=f"data_unavailable: {type(exc).__name__}: {exc}",
                )

            # 4) Build the runner with a live-bar-overlay history fetcher.
            base_fetcher = await self._history_fetcher_factory(symbols, data_source)
            overlay = LiveBarOverlayHistoryFetcher(inner=base_fetcher, quotes=quotes)
            runner = StrategyRunner(
                strategy=loaded.strategy_class(),
                position_manager=PositionManager(),
                history_fetcher=overlay,
                parameters=dict(parameter_overrides),
            )

            deviations: list[_Deviation] = []
            skipped: list[dict[str, str]] = []
            evaluated = 0
            try:
                for symbol in symbols:
                    holding = holdings.get(symbol)
                    if require_position and (holding is None or holding.quantity <= 0):
                        skipped.append({"symbol": symbol, "reason": "position_closed"})
                        await emit_debug_event(
                            "deviation_monitor_skipped",
                            {
                                "job_id": ctx.job_id,
                                "symbol": symbol,
                                "reason": "position_closed",
                                "hint": "symbol not currently held (likely sold); "
                                "skipped this fire. Delete the cron job to stop "
                                "monitoring entirely.",
                            },
                        )
                        logger.info(
                            "deviation_monitor skipped symbol=%s reason=position_closed job_id=%s",
                            symbol, ctx.job_id,
                        )
                        continue

                    quote = quotes.get(symbol) or QuoteSnapshot(symbol=symbol, status="no_data")
                    if quote.status != "ok" or quote.price is None:
                        skipped.append({"symbol": symbol, "reason": f"quote_{quote.status}"})
                        await emit_debug_event(
                            "deviation_monitor_quote_unavailable",
                            {
                                "job_id": ctx.job_id,
                                "symbol": symbol,
                                "quote_status": quote.status,
                                "reason": "quote_unavailable",
                                "hint": "no usable live price (qmt_disconnected / "
                                "no_data); skipped so the rule never runs on a stale "
                                "bar. Check the quote stream during trading hours.",
                            },
                        )
                        logger.warning(
                            "deviation_monitor quote unavailable symbol=%s status=%s job_id=%s",
                            symbol, quote.status, ctx.job_id,
                        )
                        continue

                    position_view = PositionView(
                        symbol=symbol,
                        quantity=holding.quantity if holding else 0.0,
                        cost_price=holding.cost_price if holding else Decimal("0"),
                        market_price=quote.price,
                    )
                    try:
                        signal = await runner.evaluate_one_signal(
                            symbol,
                            as_of=local_now,
                            account_view=account_view,
                            position_view=position_view,
                            is_backtest=False,
                            run_id=ctx.cron_job_run_id,
                            trace_id="",
                            universe=tuple(symbols),
                        )
                    except Exception as exc:  # noqa: BLE001 — isolate per symbol
                        skipped.append({"symbol": symbol, "reason": "rule_failed"})
                        await emit_debug_event(
                            "deviation_monitor_rule_failed",
                            {
                                "job_id": ctx.job_id,
                                "symbol": symbol,
                                "strategy_definition_id": sd_id,
                                "error_type": type(exc).__name__,
                                "message": str(exc),
                                "hint": "the deviation strategy raised for this "
                                "symbol; excluded from the reminder. Inspect via "
                                "debug get-run-view; fix the sd- source.",
                            },
                        )
                        logger.warning(
                            "deviation_monitor rule failed symbol=%s job_id=%s %s: %s",
                            symbol, ctx.job_id, type(exc).__name__, exc,
                        )
                        continue

                    evaluated += 1
                    if signal.is_sell:
                        deviations.append(
                            _Deviation(
                                symbol=symbol,
                                name=holding.name if holding else None,
                                signal=signal,
                                holding=holding,
                                quote=quote,
                                thesis=_resolve_thesis(thesis, symbol),
                            )
                        )
            finally:
                provider = getattr(base_fetcher, "data_provider", None)
                close = getattr(provider, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception as exc:  # noqa: BLE001 — cleanup; surfaced
                        logger.warning(
                            "deviation_monitor provider.aclose() raised job_id=%s %s: %s",
                            ctx.job_id, type(exc).__name__, exc,
                        )

            span.set_attribute("deviation_monitor.evaluated", evaluated)
            span.set_attribute("deviation_monitor.deviation_count", len(deviations))
            span.set_attribute("deviation_monitor.skipped_count", len(skipped))

            await emit_debug_event(
                "deviation_monitor_evaluated",
                {
                    "job_id": ctx.job_id,
                    "asof": asof.isoformat(),
                    "strategy_definition_id": sd_id,
                    "symbol_count": len(symbols),
                    "evaluated": evaluated,
                    "deviation_count": len(deviations),
                    "deviation_symbols": [d.symbol for d in deviations],
                    "skipped": skipped,
                    "hint": "deviation_monitor fire complete; "
                    + (
                        "reminder composed for the listed symbols"
                        if deviations
                        else "no plan violation — staying silent"
                    ),
                },
            )

            # 5) Compose + deliver (or stay silent).
            content = compose_reminder(asof_local=local_now, deviations=deviations)
            delivery_status, delivery_info = await deliver_assistant_message_to_session(
                self._svc,
                target_session_id=target_session_id,
                content=content,
                cron_job_id=ctx.job_id,
                cron_job_run_id=ctx.cron_job_run_id,
                cron_task_kind=self.kind,
            )
            span.set_attribute("cron.delivery.status", delivery_status)
            span.set_attribute("cron.task.status", "ok")
            delivery_error: str | None = None
            if delivery_status == "failed" and isinstance(delivery_info, dict):
                delivery_error = str(delivery_info.get("error") or "")

            return TaskResult(
                status="ok",
                delivery_status=delivery_status,
                delivery_error=delivery_error,
                data={
                    "asof": asof.isoformat(),
                    "strategy_definition_id": sd_id,
                    "evaluated": evaluated,
                    "deviation_count": len(deviations),
                    "deviation_symbols": [d.symbol for d in deviations],
                    "skipped": skipped,
                    "target_session_id": target_session_id,
                },
            )


__all__ = [
    "DeviationMonitorExecutor",
    "LoadedStrategy",
    "compose_reminder",
    "parse_holdings",
    "parse_account_view",
]
