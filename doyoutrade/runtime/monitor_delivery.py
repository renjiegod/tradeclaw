"""Deliver a fired monitor alert to the rule's channel (盯盘告警投递).

Reuses the SAME outbound pipeline as Task-Trigger delivery
(``runtime.trigger_delivery``): the ``delivery_json`` target shape
({mode, target:{kind∈{session,channel}, channel_id, chat_id, session_id}}) and
the proven ``deliver_assistant_message_to_session`` / ``channel.send`` calls. A
Feishu channel gets a rich ``build_stock_alert_card``; everything else (and the
card-build fallback) gets a plain text alert. Best-effort: a delivery failure is
visible (``monitor.delivery`` span error + ERROR log + returned
``forward_failed`` status) but never propagates into the daemon's eval loop —
the alert is already persisted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from doyoutrade.assistant.cron_executors._deliver import deliver_assistant_message_to_session
from doyoutrade.monitoring.presets import PRESET_LABELS
from doyoutrade.observability import get_logger, get_tracer
from opentelemetry.trace import Status, StatusCode

logger = get_logger(__name__)
tracer = get_tracer(__name__)

_BEIJING_TZ = timezone(timedelta(hours=8))


def _beijing_now_str() -> str:
    return datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _first_leaf_field(diagnostics: dict, field: str) -> Any:
    """Pull a field from the first triggered leaf that carries it."""
    for leaf in (diagnostics or {}).get("leaves", []) or []:
        if not leaf.get("triggered"):
            continue
        value = (leaf.get("diagnostics") or {}).get(field)
        if value is not None:
            return value
    # fall back to any leaf
    for leaf in (diagnostics or {}).get("leaves", []) or []:
        value = (leaf.get("diagnostics") or {}).get(field)
        if value is not None:
            return value
    return None


def _alert_text(
    *,
    symbol: str,
    display_name: str | None,
    condition_label: str,
    triggered_at: str,
    last_price: float | None,
    limit_price: float | None,
) -> str:
    name = f"{display_name}（{symbol}）" if display_name else symbol
    lines = [f"📡 盯盘告警 · {condition_label}", f"标的：{name}", f"触发时间：{triggered_at}（北京时间）"]
    if last_price is not None:
        lines.append(f"现价：{last_price}")
    if limit_price is not None:
        lines.append(f"涨跌停价：{limit_price}")
    return "\n".join(lines)


async def deliver_monitor_alert(
    assistant_service: Any,
    *,
    rule: Any,
    symbol: str,
    display_name: str | None,
    condition_name: str,
    diagnostics: dict,
    triggered_at: datetime,
    last_price: float | None,
    limit_price: float | None,
    run_id: str | None,
) -> str:
    """Push one fired alert. Returns a status string:

    ``skipped`` (no delivery configured) / ``forwarded`` / ``forward_failed`` /
    ``channel_disabled`` / ``no_channel_target``.
    """
    delivery = getattr(rule, "delivery_json", None)
    if not isinstance(delivery, dict):
        return "skipped"
    mode = delivery.get("mode") or "none"
    if mode == "none":
        return "skipped"
    if assistant_service is None:
        logger.warning("monitor delivery skipped (no assistant_service) rule=%s", rule.id)
        return "skipped"

    condition_label = PRESET_LABELS.get(condition_name, condition_name)
    triggered_str = _beijing_now_str()
    content = _alert_text(
        symbol=symbol,
        display_name=display_name,
        condition_label=condition_label,
        triggered_at=triggered_str,
        last_price=last_price,
        limit_price=limit_price,
    )
    target = delivery.get("target") or {}
    kind = target.get("kind")

    with tracer.start_as_current_span("monitor.delivery") as span:
        span.set_attribute("monitor.rule_id", rule.id)
        span.set_attribute("monitor.symbol", symbol)
        span.set_attribute("monitor.condition_name", condition_name)
        span.set_attribute("monitor.delivery.target_kind", str(kind))
        if run_id:
            span.set_attribute("monitor.run_id", run_id)

        if kind == "session":
            session_id = target.get("session_id")
            if not session_id:
                span.set_attribute("monitor.delivery.status", "no_channel_target")
                logger.warning("monitor delivery: session target without session_id rule=%s", rule.id)
                return "no_channel_target"
            try:
                status, _info = await deliver_assistant_message_to_session(
                    assistant_service,
                    target_session_id=session_id,
                    content=content,
                    cron_job_id=rule.id,
                    cron_job_run_id=run_id or "",
                    cron_task_kind="monitor",
                    extra_metadata={
                        "monitor_rule_id": rule.id,
                        "symbol": symbol,
                        "condition_name": condition_name,
                        "run_id": run_id,
                    },
                    source="monitor",
                )
            except Exception as exc:  # noqa: BLE001 — visible, best-effort
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, "monitor session delivery failed"))
                span.set_attribute("monitor.delivery.status", "forward_failed")
                logger.exception(
                    "monitor_delivery_failed rule=%s symbol=%s run_id=%s target=session err=%s",
                    rule.id, symbol, run_id, exc,
                )
                return "forward_failed"
            span.set_attribute("monitor.delivery.status", status)
            logger.info(
                "monitor delivery session status=%s rule=%s symbol=%s run_id=%s",
                status, rule.id, symbol, run_id,
            )
            return status

        if kind == "channel":
            channel_id = target.get("channel_id")
            chat_id = target.get("chat_id")
            if not chat_id:
                span.set_attribute("monitor.delivery.status", "no_channel_target")
                logger.warning(
                    "monitor delivery: channel target missing chat_id channel_id=%s rule=%s",
                    channel_id, rule.id,
                )
                return "no_channel_target"
            manager = getattr(assistant_service, "channel_manager", None)
            channel = manager.get(channel_id) if (manager is not None and channel_id) else None
            if channel is None:
                span.set_attribute("monitor.delivery.status", "channel_disabled")
                logger.warning(
                    "monitor delivery: channel unavailable channel_id=%s rule=%s",
                    channel_id, rule.id,
                )
                return "channel_disabled"
            from doyoutrade.assistant.channels.base import CardContent, TextContent

            outgoing: Any = TextContent(text=content)
            if getattr(channel, "channel_type", "") == "feishu":
                try:
                    from doyoutrade.assistant.channels.feishu.card.builder import build_stock_alert_card

                    outgoing = CardContent(
                        card=build_stock_alert_card(
                            symbol=symbol,
                            display_name=display_name,
                            condition_label=condition_label,
                            condition_name=condition_name,
                            triggered_at=triggered_str,
                            last_price=last_price,
                            limit_price=limit_price,
                            seal_peak=_first_leaf_field(diagnostics, "seal_peak"),
                            seal_now=_first_leaf_field(diagnostics, "seal_now"),
                            drop_pct=_first_leaf_field(diagnostics, "drop_pct"),
                            diagnostics=diagnostics,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 — visible, text fallback
                    logger.warning(
                        "monitor alert card build failed rule=%s err=%s; text fallback",
                        rule.id, exc,
                    )
                    outgoing = TextContent(text=content)
            send_meta: dict[str, Any] = {}
            if isinstance(target.get("meta"), dict):
                send_meta.update(target["meta"])
            send_meta.setdefault("feishu_chat_id", chat_id)
            send_meta.setdefault("feishu_chat_type", "group")
            try:
                await channel.send(f"monitor-{rule.id}-{symbol}", outgoing, send_meta)
            except Exception as exc:  # noqa: BLE001 — visible, best-effort
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, "monitor channel delivery failed"))
                span.set_attribute("monitor.delivery.status", "forward_failed")
                logger.exception(
                    "monitor_delivery_failed rule=%s symbol=%s channel_id=%s chat_id=%s run_id=%s err=%s",
                    rule.id, symbol, channel_id, chat_id, run_id, exc,
                )
                return "forward_failed"
            span.set_attribute("monitor.delivery.status", "forwarded")
            logger.info(
                "monitor delivery channel forwarded channel_id=%s chat_id=%s rule=%s symbol=%s run_id=%s",
                channel_id, chat_id, rule.id, symbol, run_id,
            )
            return "forwarded"

        span.set_attribute("monitor.delivery.status", "no_channel_target")
        logger.warning("monitor delivery: unknown target kind=%s rule=%s", kind, rule.id)
        return "no_channel_target"
