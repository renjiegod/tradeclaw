"""Canonical bar timestamps aligned with TradingAgents-style OHLCV dates."""

from __future__ import annotations

from datetime import datetime, timezone


def normalize_bar_timestamp(raw: str | int | float | None) -> str:
    """Normalize bar timestamps for LLM-facing OHLCV (TradingAgents / yfinance style).

    - **Calendar day** (midnight): ``YYYY-MM-DD`` — same idea as stockstats
      ``Date.dt.strftime('%Y-%m-%d')`` on daily bars.
    - **Intraday**: ``YYYY-MM-DDTHH:MM:SS`` without timezone suffix (naive).

    Accepts compact ``YYYYMMDD``, ISO strings, and trailing ``Z`` (treated as UTC
    then converted to naive UTC wall time).
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"

    s_iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(s_iso)
    except ValueError:
        return s

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

    if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%dT%H:%M:%S")
