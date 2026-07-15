"""Tagged-union ``schedule`` normalizer for the assistant cron tools.

The legacy interface forced the model to hand-write a 5-field cron string
even for trivially-relative intents like "30 seconds later". The model
routinely confused the seconds component with the minute field (see
``tmp/error_request.json`` for the 22:52:26 → ``'56 22 16 5 *'`` regression).

This module accepts a structured dict::

    {"kind": "once_at", "at": "2026-05-16T22:53:00+08:00"}
    {"kind": "once_at", "delay_seconds": 30}
    {"kind": "every",   "every_seconds": 300}
    {"kind": "cron",    "expr": "0 9 * * 1-5"}

and translates it into the existing storage shape (a cron expression
interpreted in ``Asia/Shanghai``). The model no longer has to do clock
arithmetic on cron fields; the failure modes that produced the regression
are structurally impossible — there is no minute field for the model to
mis-fill.

Why cron remains the underlying storage:

* APScheduler ``CronTrigger`` powers ``AgentCronManager``; the persistence
  layer columns are ``cron_expression`` + ``timezone``. Translating in
  this helper keeps the schema migration out of the critical path.
* ``kind=once_at`` becomes ``"M H D Mo *"`` (a single matching instant
  per year). The prompt instructs the agent to call ``delete_cron_job``
  from inside its own ``input_template`` for a true one-shot.
* Sub-minute precision is impossible under cron — sub-minute targets
  round UP to the next whole minute and emit a note so the agent can
  truthfully tell the user "scheduled for HH:MM:00".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

CRON_TOOL_TIMEZONE = "Asia/Shanghai"
_TZ = ZoneInfo(CRON_TOOL_TIMEZONE)

_VALID_KINDS = ("once_at", "every", "cron")

# Sub-hour every_seconds is expressed as ``*/N * * * *`` where N=minutes.
# For the firing pattern to be regular across hour boundaries, N must
# divide 60. ``1`` is supported via the ``"* * * * *"`` form.
_SUB_HOUR_OK_MINUTES = frozenset({1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30})

# Hour-aligned every_seconds is expressed as ``0 */H * * *`` where H=hours.
# For the firing pattern to be regular across the day, H must divide 24.
_HOUR_OK_HOURS = frozenset({1, 2, 3, 4, 6, 8, 12, 24})


@dataclass(frozen=True)
class NormalizedSchedule:
    """Output of :func:`normalize_schedule`.

    ``cron_expression`` is always a 5-field cron string interpreted in
    ``Asia/Shanghai`` — safe to forward to the manager / DB unchanged.
    ``notes`` carries human-readable adjustments (e.g. "rounded up to
    22:53:00"); surfaced in the tool success text so the agent can
    truthfully report the actual fire time.
    ``source_kind`` echoes the caller-supplied ``kind`` for debug events.
    """

    cron_expression: str
    source_kind: str
    notes: tuple[str, ...] = field(default_factory=tuple)


class ScheduleValidationError(ValueError):
    """Structured failure surfaced by :func:`normalize_schedule`.

    Carries a stable ``error_code`` token so skill docs and the tool
    wrapper can refer to it without scraping the message.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_schedule(
    value: Any, *, now: datetime | None = None
) -> NormalizedSchedule:
    """Translate a schedule tagged-union dict into a cron expression.

    Raises :class:`ScheduleValidationError` for any structurally-invalid
    input. ``now`` may be injected for deterministic tests; defaults to
    the current ``Asia/Shanghai`` wall-clock time.
    """

    if not isinstance(value, dict):
        raise ScheduleValidationError(
            "invalid_schedule",
            "schedule must be an object with a 'kind' field "
            f"(one of {list(_VALID_KINDS)})",
        )

    kind = value.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise ScheduleValidationError(
            "missing_schedule_kind",
            "schedule.kind is required — use 'once_at' for one-shot delays, "
            "'every' for sub-day repeats, or 'cron' for calendar patterns",
        )
    kind = kind.strip()

    if kind == "cron":
        return _normalize_cron(value)
    if kind == "once_at":
        return _normalize_once_at(value, now=now)
    if kind == "every":
        return _normalize_every(value)

    raise ScheduleValidationError(
        "invalid_schedule_kind",
        f"schedule.kind={kind!r} is not supported; "
        f"use one of {list(_VALID_KINDS)}",
    )


# ── kind=cron ──────────────────────────────────────────────────────────


def _normalize_cron(value: dict[str, Any]) -> NormalizedSchedule:
    expr = value.get("expr")
    if not isinstance(expr, str) or not expr.strip():
        raise ScheduleValidationError(
            "missing_cron_expr",
            "schedule.expr is required when kind='cron' (a 5-field cron "
            "expression like '0 9 * * 1-5', interpreted in Asia/Shanghai)",
        )
    extras = _extras(value, allowed={"kind", "expr"})
    if extras:
        raise ScheduleValidationError(
            "unexpected_schedule_fields",
            f"kind='cron' only accepts 'expr'; got unexpected field(s) "
            f"{sorted(extras)}",
        )
    return NormalizedSchedule(
        cron_expression=expr.strip(),
        source_kind="cron",
    )


# ── kind=once_at ────────────────────────────────────────────────────────


def _normalize_once_at(
    value: dict[str, Any], *, now: datetime | None
) -> NormalizedSchedule:
    extras = _extras(value, allowed={"kind", "at", "delay_seconds"})
    if extras:
        raise ScheduleValidationError(
            "unexpected_schedule_fields",
            f"kind='once_at' only accepts 'at' or 'delay_seconds'; got "
            f"unexpected field(s) {sorted(extras)}",
        )
    at_raw = value.get("at")
    delay_raw = value.get("delay_seconds")
    if at_raw is None and delay_raw is None:
        raise ScheduleValidationError(
            "missing_once_at_target",
            "kind='once_at' requires either 'at' (ISO-8601 timestamp) or "
            "'delay_seconds' (integer seconds from currentTime)",
        )
    if at_raw is not None and delay_raw is not None:
        raise ScheduleValidationError(
            "conflicting_once_at_target",
            "kind='once_at' accepts 'at' OR 'delay_seconds', not both",
        )

    now_local = (now or datetime.now(_TZ)).astimezone(_TZ)

    if delay_raw is not None:
        try:
            delay = int(delay_raw)
        except (TypeError, ValueError) as exc:
            raise ScheduleValidationError(
                "invalid_delay_seconds",
                "schedule.delay_seconds must be a non-negative integer",
            ) from exc
        if delay < 0:
            raise ScheduleValidationError(
                "invalid_delay_seconds",
                f"schedule.delay_seconds must be >= 0 (got {delay})",
            )
        target = now_local + timedelta(seconds=delay)
    else:
        if not isinstance(at_raw, str) or not at_raw.strip():
            raise ScheduleValidationError(
                "invalid_once_at",
                "schedule.at must be an ISO-8601 string",
            )
        try:
            target = datetime.fromisoformat(at_raw.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ScheduleValidationError(
                "invalid_once_at",
                f"schedule.at is not a valid ISO-8601 timestamp: {at_raw!r}",
            ) from exc
        if target.tzinfo is None:
            target = target.replace(tzinfo=_TZ)
        else:
            target = target.astimezone(_TZ)

    notes: list[str] = []
    if target.second or target.microsecond:
        rounded = target.replace(second=0, microsecond=0) + timedelta(minutes=1)
        notes.append(
            f"rounded up from {target.strftime('%Y-%m-%d %H:%M:%S %z')} "
            f"to {rounded.strftime('%Y-%m-%d %H:%M:00 %z')} "
            "(cron resolution is one minute)"
        )
        target = rounded

    if target <= now_local:
        raise ScheduleValidationError(
            "once_at_in_past",
            f"schedule target {target.isoformat()} is not in the future "
            f"(currentTime={now_local.isoformat()})",
        )

    expr = f"{target.minute} {target.hour} {target.day} {target.month} *"
    return NormalizedSchedule(
        cron_expression=expr,
        source_kind="once_at",
        notes=tuple(notes),
    )


# ── kind=every ──────────────────────────────────────────────────────────


def _normalize_every(value: dict[str, Any]) -> NormalizedSchedule:
    extras = _extras(value, allowed={"kind", "every_seconds"})
    if extras:
        raise ScheduleValidationError(
            "unexpected_schedule_fields",
            f"kind='every' only accepts 'every_seconds'; got unexpected "
            f"field(s) {sorted(extras)}",
        )
    raw = value.get("every_seconds")
    if raw is None:
        raise ScheduleValidationError(
            "missing_every_seconds",
            "kind='every' requires 'every_seconds' (integer, minimum 60)",
        )
    try:
        secs = int(raw)
    except (TypeError, ValueError) as exc:
        raise ScheduleValidationError(
            "invalid_every_seconds",
            "schedule.every_seconds must be an integer",
        ) from exc

    if secs < 60:
        raise ScheduleValidationError(
            "every_seconds_below_minimum",
            "schedule.every_seconds must be >= 60 (cron has minute "
            "resolution). For a sub-minute one-shot delay, use "
            "kind='once_at' with delay_seconds=<n> — the server will round "
            "up to the next whole minute.",
        )
    if secs % 60 != 0:
        raise ScheduleValidationError(
            "every_seconds_not_minute_aligned",
            f"schedule.every_seconds={secs} is not a whole-minute interval. "
            "Pass a multiple of 60 (60, 120, 180, 300, …) — cron cannot "
            "fire on sub-minute boundaries.",
        )

    minutes = secs // 60

    if minutes < 60:
        if minutes not in _SUB_HOUR_OK_MINUTES:
            raise ScheduleValidationError(
                "every_seconds_uneven",
                f"schedule.every_seconds={secs} ({minutes} minutes) does "
                "not produce a regular cron pattern. Supported sub-hour "
                f"minute values: {sorted(_SUB_HOUR_OK_MINUTES)}. "
                "For other intervals, use kind='cron' with an explicit expr.",
            )
        expr = "* * * * *" if minutes == 1 else f"*/{minutes} * * * *"
        return NormalizedSchedule(
            cron_expression=expr, source_kind="every"
        )

    if minutes % 60 != 0:
        raise ScheduleValidationError(
            "every_seconds_uneven",
            f"schedule.every_seconds={secs} ({minutes} minutes) is "
            "between 1 hour and 1 day but not hour-aligned. Pick an "
            "hour-aligned interval (3600, 7200, 10800, …) or use kind='cron'.",
        )
    hours = minutes // 60
    if hours not in _HOUR_OK_HOURS:
        raise ScheduleValidationError(
            "every_seconds_uneven",
            f"schedule.every_seconds={secs} ({hours} hours) does not "
            "divide a day evenly. Supported hour values: "
            f"{sorted(_HOUR_OK_HOURS)}. For other intervals, use kind='cron'.",
        )
    if hours == 24:
        expr = "0 0 * * *"
    elif hours == 1:
        expr = "0 * * * *"
    else:
        expr = f"0 */{hours} * * *"
    return NormalizedSchedule(cron_expression=expr, source_kind="every")


# ── helpers ─────────────────────────────────────────────────────────────


def _extras(value: dict[str, Any], *, allowed: set[str]) -> set[str]:
    return {k for k in value.keys() if k not in allowed}


__all__ = [
    "CRON_TOOL_TIMEZONE",
    "NormalizedSchedule",
    "ScheduleValidationError",
    "normalize_schedule",
]
