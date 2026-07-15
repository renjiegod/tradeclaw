"""Alert dedup / cooldown gate (MANDATORY — no alert spam).

Two gates, both must pass to fire (CLAUDE.md: a sealed limit-up board must not
re-alert every tick):

1. Rising-edge: fire only on a False→True transition of the condition for a
   ``(rule, symbol, condition)`` key. While it stays True, no re-fire; True→False
   re-arms. One-shot presets (涨停/跌停) fire once/day naturally — the price stays
   at the limit and the day reset re-arms the next session.
2. Cooldown floor: even on a genuinely re-armed edge, suppress if
   ``now - last_fired_at < cooldown_seconds``.

Edge state is in-process (a restart conservatively re-arms the edge). The
durable ``last_fired_at`` is the ``monitor_alerts`` row timestamp, rehydrated at
daemon start so a restart inside a cooldown still suppresses an immediate
duplicate.
"""

from __future__ import annotations

from datetime import datetime, timezone

DEFAULT_COOLDOWN_SECONDS = 300

DedupKey = tuple[str, str, str]  # (monitor_rule_id, symbol, condition_name)


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


class DedupGate:
    def __init__(self) -> None:
        # key -> last evaluated triggered state (for rising-edge detection)
        self._edge: dict[DedupKey, bool] = {}
        # key -> last fired timestamp (naive UTC) for the cooldown floor
        self._last_fired: dict[DedupKey, datetime] = {}

    def should_fire(
        self,
        key: DedupKey,
        *,
        triggered: bool,
        now: datetime,
        cooldown_seconds: int,
    ) -> tuple[bool, str | None]:
        """Decide whether to fire. Returns (fire, suppressed_reason).

        ``suppressed_reason`` ∈ {None, 'edge_not_rising', 'within_cooldown'}.
        Always updates the stored edge state to ``triggered``.
        """
        prev = self._edge.get(key, False)
        self._edge[key] = triggered
        if not triggered:
            return False, None
        if prev:  # was already True last tick — not a rising edge
            return False, "edge_not_rising"
        now_naive = _to_naive_utc(now)
        last = self._last_fired.get(key)
        if last is not None and cooldown_seconds > 0:
            elapsed = (now_naive - last).total_seconds()
            if elapsed < cooldown_seconds:
                return False, "within_cooldown"
        return True, None

    def remaining_cooldown(self, key: DedupKey, *, now: datetime, cooldown_seconds: int) -> float:
        last = self._last_fired.get(key)
        if last is None or cooldown_seconds <= 0:
            return 0.0
        elapsed = (_to_naive_utc(now) - last).total_seconds()
        return max(0.0, cooldown_seconds - elapsed)

    def record_fired(self, key: DedupKey, *, now: datetime) -> None:
        self._last_fired[key] = _to_naive_utc(now)

    def rehydrate(self, last_fired: dict[DedupKey, datetime]) -> int:
        """Seed the cooldown floor from persisted alert timestamps. Returns count."""
        count = 0
        for key, ts in last_fired.items():
            if ts is not None:
                self._last_fired[key] = _to_naive_utc(ts)
                count += 1
        return count

    def reset_edges(self) -> None:
        """Clear rising-edge state (called on day rollover). Cooldown floor kept."""
        self._edge.clear()
