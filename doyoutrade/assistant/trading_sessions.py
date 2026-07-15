"""Trading-session helpers for cron and assistant scheduling."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

_A_SHARE_MORNING_OPEN = time(9, 15)
_A_SHARE_MORNING_CLOSE = time(11, 30)
_A_SHARE_AFTERNOON_OPEN = time(13, 0)
_A_SHARE_AFTERNOON_CLOSE = time(15, 0)


def is_ashare_continuous_trading(
    instant: datetime,
    *,
    timezone: str = "Asia/Shanghai",
) -> bool:
    """Return True when ``instant`` falls in A-share continuous auction hours.

  Mon–Fri 09:15–11:30 and 13:00–15:00 in the given IANA timezone
  (inclusive at minute resolution).
    """
    local = instant.astimezone(ZoneInfo(timezone))
    if local.weekday() >= 5:
        return False
    clock = local.time().replace(second=0, microsecond=0)
    morning = _A_SHARE_MORNING_OPEN <= clock <= _A_SHARE_MORNING_CLOSE
    afternoon = _A_SHARE_AFTERNOON_OPEN <= clock <= _A_SHARE_AFTERNOON_CLOSE
    return morning or afternoon


def ashare_continuous_trading_skip_reason(
    instant: datetime,
    *,
    timezone: str,
    trading_session: str | None,
    manual: bool,
) -> dict[str, str] | None:
    """When a scheduled cron should not run, return structured skip metadata."""
    if manual:
        return None
    if trading_session != "ashare":
        return None
    if is_ashare_continuous_trading(instant, timezone=timezone):
        return None
    return {
        "reason": "outside_trading_session",
        "hint": (
            "A-share continuous auction is Mon–Fri 09:15–11:30 and "
            "13:00–15:00 in the job timezone; scheduled fires outside "
            "that window are skipped (manual cron trigger is not gated)."
        ),
    }
