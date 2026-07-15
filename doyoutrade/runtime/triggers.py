"""Trigger input validation, schedule due-ness, and execution-intent mapping.

Lives in the runtime layer (above persistence, below API/assistant) so it can use
the neutral cron helpers and be called from the API to validate user/LLM input
*before* a malformed trigger enters the run link (CLAUDE.md §错误可见性: structured
validation, not runtime "try it and see"). Error codes are stable tokens the API
surfaces and skill docs may reference:

- ``invalid_schedule_json``  — schedule fields missing/ill-typed for the kind
- ``schedule_kind_unknown``  — schedule_kind not in the allowed set
- ``invalid_cron_expression`` — cron failed APScheduler validation
- ``invalid_delivery_json``  — delivery_json present but not a dict / bad enum
- ``delivery_channel_unresolved`` — delivery wants to push but no resolvable target
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from doyoutrade.runtime.cron_timing import (
    next_cron_fire_after,
    rewrite_unix_weekday_dow,
    validate_cron_expression,
)

SCHEDULE_KINDS = ("interval", "cron", "at", "backtest_range")
EXECUTION_INTENTS = ("trade", "signal_only")
DELIVERY_MODES = ("none", "card", "prose")
NO_SIGNAL_MODES = ("silent", "brief", "full")
DELIVERY_TARGET_KINDS = ("session", "channel")


class TriggerValidationError(ValueError):
    """Structured trigger validation failure carrying a stable ``error_code``."""

    def __init__(self, error_code: str, message: str, *, field: str | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.field = field

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.error_code, "message": str(self)}
        if self.field:
            payload["field"] = self.field
        return payload


def _to_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_at_iso(at_iso: Any) -> datetime:
    if not isinstance(at_iso, str) or not at_iso.strip():
        raise TriggerValidationError(
            "invalid_schedule_json", "at_iso is required for schedule_kind='at'", field="at_iso"
        )
    try:
        parsed = datetime.fromisoformat(at_iso.strip())
    except ValueError as exc:
        raise TriggerValidationError(
            "invalid_schedule_json",
            f"at_iso must be ISO-8601 (with offset), got {at_iso!r}: {exc}",
            field="at_iso",
        ) from exc
    return parsed


def run_mode_for_intent(execution_intent: str, base_run_mode: str) -> str:
    """The effective worker run_mode for a fire of this trigger.

    ``signal_only`` forces the post-generate_signals short-circuit regardless of the
    task's bound mode (this is the per-fire override that dissolves the old
    3-condition readiness gate); ``trade`` runs under the task's own run_mode.
    """
    if execution_intent == "signal_only":
        return "signal_only"
    return base_run_mode


def validate_trigger_input(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalize a trigger create/update payload.

    Returns a dict of normalized column values (cron weekday rewritten, defaults
    applied). Raises :class:`TriggerValidationError` on any bad input. Only keys
    relevant to the declared ``schedule_kind`` are returned for schedule columns;
    the caller (API) merges these onto the repo create/update kwargs.
    """
    out: dict[str, Any] = {}

    schedule_kind = fields.get("schedule_kind")
    if schedule_kind not in SCHEDULE_KINDS:
        raise TriggerValidationError(
            "schedule_kind_unknown",
            f"schedule_kind must be one of {SCHEDULE_KINDS}, got {schedule_kind!r}",
            field="schedule_kind",
        )
    out["schedule_kind"] = schedule_kind

    timezone_str = fields.get("timezone") or "UTC"
    out["timezone"] = timezone_str

    if schedule_kind == "interval":
        secs = fields.get("interval_seconds")
        if not isinstance(secs, int) or isinstance(secs, bool) or secs <= 0:
            raise TriggerValidationError(
                "invalid_schedule_json",
                f"interval_seconds must be a positive int for schedule_kind='interval', got {secs!r}",
                field="interval_seconds",
            )
        out["interval_seconds"] = secs
    elif schedule_kind == "cron":
        expr = fields.get("cron_expression")
        rewritten, _notice = rewrite_unix_weekday_dow(expr)
        try:
            validate_cron_expression(rewritten, timezone_str)
        except ValueError as exc:
            raise TriggerValidationError(
                "invalid_cron_expression", str(exc), field="cron_expression"
            ) from exc
        out["cron_expression"] = rewritten
    elif schedule_kind == "at":
        _parse_at_iso(fields.get("at_iso"))
        out["at_iso"] = str(fields.get("at_iso")).strip()
    elif schedule_kind == "backtest_range":
        for key in ("range_start", "range_end"):
            if not isinstance(fields.get(key), str) or not fields.get(key).strip():
                raise TriggerValidationError(
                    "invalid_schedule_json",
                    f"{key} is required for schedule_kind='backtest_range'",
                    field=key,
                )
            out[key] = fields[key].strip()
        if fields.get("bar_interval"):
            out["bar_interval"] = str(fields["bar_interval"])

    ts = fields.get("trading_session")
    if ts is not None:
        out["trading_session"] = str(ts)
    if "delete_after_run" in fields and fields["delete_after_run"] is not None:
        out["delete_after_run"] = bool(fields["delete_after_run"])
    elif schedule_kind in ("at", "backtest_range"):
        out["delete_after_run"] = True

    execution_intent = fields.get("execution_intent", "signal_only")
    if execution_intent not in EXECUTION_INTENTS:
        raise TriggerValidationError(
            "invalid_schedule_json",
            f"execution_intent must be one of {EXECUTION_INTENTS}, got {execution_intent!r}",
            field="execution_intent",
        )
    out["execution_intent"] = execution_intent

    if "delivery_json" in fields:
        out["delivery_json"] = _validate_delivery(fields.get("delivery_json"))

    return out


def _validate_delivery(delivery: Any) -> dict[str, Any] | None:
    if delivery is None:
        return None
    if not isinstance(delivery, dict):
        raise TriggerValidationError(
            "invalid_delivery_json",
            f"delivery must be an object or null, got {type(delivery).__name__}",
            field="delivery",
        )
    mode = delivery.get("mode", "card")
    if mode not in DELIVERY_MODES:
        raise TriggerValidationError(
            "invalid_delivery_json",
            f"delivery.mode must be one of {DELIVERY_MODES}, got {mode!r}",
            field="delivery.mode",
        )
    normalized: dict[str, Any] = {"mode": mode}

    no_signal = delivery.get("no_signal_mode")
    if no_signal is not None:
        if no_signal not in NO_SIGNAL_MODES:
            raise TriggerValidationError(
                "invalid_delivery_json",
                f"delivery.no_signal_mode must be one of {NO_SIGNAL_MODES}, got {no_signal!r}",
                field="delivery.no_signal_mode",
            )
        normalized["no_signal_mode"] = no_signal

    if mode == "none":
        return normalized

    target = delivery.get("target")
    if not isinstance(target, dict):
        raise TriggerValidationError(
            "delivery_channel_unresolved",
            "delivery.target is required when delivery.mode is 'card' or 'prose'",
            field="delivery.target",
        )
    kind = target.get("kind")
    if kind not in DELIVERY_TARGET_KINDS:
        raise TriggerValidationError(
            "invalid_delivery_json",
            f"delivery.target.kind must be one of {DELIVERY_TARGET_KINDS}, got {kind!r}",
            field="delivery.target.kind",
        )
    norm_target: dict[str, Any] = {"kind": kind}
    if kind == "session":
        # Either an explicit session_id, or origin=true (API auto-fills the creating
        # session id for parity with today's "push to originating chat" UX).
        session_id = target.get("session_id")
        if target.get("origin") is True:
            norm_target["origin"] = True
            if isinstance(session_id, str) and session_id:
                norm_target["session_id"] = session_id
        elif isinstance(session_id, str) and session_id:
            norm_target["session_id"] = session_id
        else:
            raise TriggerValidationError(
                "delivery_channel_unresolved",
                "delivery.target.session_id or origin=true is required for kind='session'",
                field="delivery.target.session_id",
            )
    else:  # channel
        # A channel push needs BOTH a bot (registered channel record id) and a
        # concrete chat (Feishu group oc_… chat_id). channel_id alone selects the
        # bot, not where the message lands, so chat_id is required too.
        channel_id = target.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id:
            raise TriggerValidationError(
                "delivery_channel_unresolved",
                "delivery.target.channel_id (registered channel record id) is required for kind='channel'",
                field="delivery.target.channel_id",
            )
        norm_target["channel_id"] = channel_id
        chat_id = target.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            raise TriggerValidationError(
                "delivery_channel_unresolved",
                "delivery.target.chat_id (Feishu group 'oc_…' chat id) is required for kind='channel'",
                field="delivery.target.chat_id",
            )
        norm_target["chat_id"] = chat_id
        if target.get("chat_name"):
            norm_target["chat_name"] = str(target["chat_name"])
        if target.get("channel_type"):
            norm_target["channel_type"] = str(target["channel_type"])
    normalized["target"] = norm_target

    composer = delivery.get("composer_agent_id")
    if isinstance(composer, str) and composer:
        normalized["composer_agent_id"] = composer
    return normalized


def compute_next_fire(
    *,
    schedule_kind: str,
    interval_seconds: int | None = None,
    cron_expression: str | None = None,
    timezone_str: str = "UTC",
    at_iso: str | None = None,
    last_fired_at: datetime | None = None,
    now: datetime,
) -> datetime | None:
    """Naive-UTC next fire for a trigger. ``now`` and ``last_fired_at`` are naive UTC.

    Returns None for backtest_range (never wall-clock polled) or when a cron pattern
    has no future match.
    """
    if schedule_kind == "interval":
        base = last_fired_at or now
        return base + timedelta(seconds=int(interval_seconds or 0))
    if schedule_kind == "cron":
        after = (last_fired_at or now).replace(tzinfo=timezone.utc)
        return _to_naive_utc(next_cron_fire_after(cron_expression or "", timezone_str, after))
    if schedule_kind == "at":
        return _to_naive_utc(_parse_at_iso(at_iso))
    return None


def is_due(next_fire_at: datetime | None, *, now: datetime) -> bool:
    """A trigger is due when its (naive-UTC) next_fire_at has arrived."""
    return next_fire_at is not None and now >= next_fire_at
