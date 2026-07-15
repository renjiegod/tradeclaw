"""Neutral cron-timing helpers shared by trigger validation and the TriggerScheduler.

This module deliberately depends only on APScheduler so it can be imported from
both the validation path (``doyoutrade.runtime.triggers``) and the scheduler
without dragging in the assistant layer. It is the single source of truth for:

- the Unix-vs-APScheduler weekday rewrite (``1-5`` → ``mon-fri``) — the dominant
  "工作日" mistake; APScheduler uses ``0=Mon..6=Sun`` so bare ``1-5`` means Tue-Sat,
- cron-expression validation (the same check APScheduler runs at fire time, run at
  write time so bad rows never reach the DB),
- next-fire computation via ``CronTrigger.get_next_fire_time``.

``cron_manager`` keeps its own copies for now; Phase 2/3 migrates it onto this
module. Keeping the behavior byte-identical here avoids the TZ-drift bug class
(see MEMORY project_cron_pending_followups) by never hand-rolling cron math.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from apscheduler.triggers.cron import CronTrigger


def rewrite_unix_weekday_dow(expr: Any) -> tuple[str, str | None]:
    """Rewrite Unix/Vixie weekday DOW tokens for APScheduler (``1-5`` → ``mon-fri``).

    Returns ``(expr, notice_or_None)``. Mirrors
    ``AgentCronManager._rewrite_unix_weekday_dow`` exactly.
    """
    if not isinstance(expr, str) or not expr.strip():
        return (expr if isinstance(expr, str) else ""), None
    parts = expr.strip().split()
    if len(parts) != 5:
        return expr, None
    dow = parts[4].replace(" ", "")
    unix_weekday_forms = frozenset({"1-5", "1,2,3,4,5"})
    if dow not in unix_weekday_forms:
        return expr, None
    rewritten = " ".join([*parts[:4], "mon-fri"])
    notice = (
        f"Auto-rewrote weekday field: day-of-week {parts[4]!r} follows "
        "Unix/Vixie numbering (0=Sun, 1=Mon..5=Fri), but DoYouTrade schedules via "
        "APScheduler where 0=Mon..6=Sun — so it means Tue-Sat and skips Monday. "
        "Stored as 'mon-fri'. For weekdays use `mon-fri` (or APScheduler `0-4`)."
    )
    return rewritten, notice


def validate_cron_expression(expr: Any, tz: Any) -> CronTrigger:
    """Build the ``CronTrigger`` APScheduler will fire from, raising on bad input.

    The expression is the only thing a caller can get wrong; an unknown timezone is
    expected to have been validated upstream and falls back to ``"UTC"``. Returns the
    built trigger so callers can reuse it without re-parsing.
    """
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError("cron_expression is required")
    validate_tz = tz if isinstance(tz, str) and tz.strip() else "UTC"
    try:
        return CronTrigger.from_crontab(expr, timezone=validate_tz)
    except (ValueError, LookupError, TypeError) as exc:
        raise ValueError(
            f"invalid cron_expression {expr!r} (timezone={tz!r}): {exc}"
        ) from exc


def next_cron_fire_after(
    cron_expression: str,
    tz: str,
    after: datetime | None = None,
) -> datetime | None:
    """Next fire strictly after ``after`` (UTC-aware; defaults to now) for a cron expr.

    Applies the weekday rewrite first so due-ness matches what would be stored.
    Returns an aware datetime in the trigger's timezone, or ``None`` if the cron
    pattern has no future match.
    """
    expr, _ = rewrite_unix_weekday_dow(cron_expression)
    trigger = validate_cron_expression(expr, tz)
    now_aware = after if (after is not None and after.tzinfo is not None) else datetime.now(timezone.utc)
    return trigger.get_next_fire_time(None, now_aware)
