"""Detect 复权因子漂移 (adjustment-factor drift) between cached and fresh bars.

Front-adjusted (qfq) prices are NOT immutable: every 除权/除息 event rescales
the entire price history. Any store that persists qfq bars as absolute values
(``market_bars`` warehouse, ``cached_bars`` backtest/live cache) therefore
holds data that silently goes stale the day an ex-rights event lands — old
bars keep the previous factor while newly synced bars carry the new one,
producing a price cliff (e.g. 000636.SZ 2025-06-11, ~130 → ~13).

The cure is anchor-overlap revalidation: whenever fresh bars are fetched from
upstream, compare them against locally stored bars on the *same trading days*.
A relative close-price deviation beyond ``DEFAULT_DRIFT_TOLERANCE`` means the
cumulative adjust factor changed and the local history for that symbol must be
refreshed/invalidated wholesale. Callers own the refresh; this module only
renders the verdict, deterministically and side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from doyoutrade.data.bar_timestamp import normalize_bar_timestamp

# Providers round CNY prices to 0.01; on low-priced stocks that is <0.1%
# relative noise. Real ex-rights events move the factor by ≫1%, so 0.5%
# separates rounding noise from factor changes with a wide margin on both
# sides.
DEFAULT_DRIFT_TOLERANCE = 0.005

# How many calendar days a fetch window is widened backwards into
# already-covered territory so the upstream response contains anchor days to
# compare against. ~10 calendar days ≥ 5 A-share trading days even across
# golden-week holidays.
ANCHOR_OVERLAP_CALENDAR_DAYS = 10


@dataclass(frozen=True)
class AdjustDriftSample:
    """One mismatched trading day, kept for debug-event payloads."""

    timestamp: str
    cached_close: float
    fresh_close: float
    rel_deviation: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "cached_close": self.cached_close,
            "fresh_close": self.fresh_close,
            "rel_deviation": round(self.rel_deviation, 6),
        }


@dataclass(frozen=True)
class AdjustDriftReport:
    """Outcome of comparing cached vs freshly fetched bars.

    ``drifted`` is only meaningful when ``overlap_count > 0``; with no
    overlapping trading days the verdict is "cannot judge" and callers must
    not treat it as "clean".
    """

    drifted: bool
    overlap_count: int
    max_rel_deviation: float
    samples: tuple[AdjustDriftSample, ...] = field(default_factory=tuple)
    tolerance: float = DEFAULT_DRIFT_TOLERANCE

    def as_payload(self) -> dict[str, Any]:
        return {
            "drifted": self.drifted,
            "overlap_count": self.overlap_count,
            "max_rel_deviation": round(self.max_rel_deviation, 6),
            "tolerance": self.tolerance,
            "samples": [sample.as_payload() for sample in self.samples],
        }


def _close_by_day(bars: Sequence[Any]) -> dict[str, float]:
    """Index close prices by normalized calendar day.

    Accepts either ``Bar`` dataclasses or repository row mappings. A bar whose
    close cannot be interpreted as a finite float is a schema violation —
    raise with type and value rather than skipping it (§错误可见性).
    """

    closes: dict[str, float] = {}
    for bar in bars:
        if isinstance(bar, Mapping):
            raw_ts, raw_close = bar.get("timestamp"), bar.get("close")
        else:
            raw_ts, raw_close = getattr(bar, "timestamp", None), getattr(bar, "close", None)
        ts = normalize_bar_timestamp(str(raw_ts))[:10] if raw_ts is not None else ""
        if not ts:
            raise ValueError(
                f"adjust_drift_bar_timestamp_invalid: bar timestamp must normalize to a day, "
                f"got {type(raw_ts).__name__}: {raw_ts!r}"
            )
        try:
            close = float(raw_close)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"adjust_drift_bar_close_invalid: close must be numeric, "
                f"got {type(raw_close).__name__}: {raw_close!r} on {ts}"
            ) from exc
        if close != close:  # NaN guard
            raise ValueError(f"adjust_drift_bar_close_invalid: close is NaN on {ts}")
        closes[ts] = close
    return closes


def detect_adjust_drift(
    cached_bars: Sequence[Any],
    fresh_bars: Sequence[Any],
    *,
    tolerance: float = DEFAULT_DRIFT_TOLERANCE,
    max_samples: int = 3,
) -> AdjustDriftReport:
    """Compare cached vs fresh bars on overlapping trading days.

    Daily granularity by design: intraday bars normalize to their calendar
    day and the LAST bar of each day wins, which is exact for 1d data and a
    sufficient factor-change detector for 5m data (an ex-rights event scales
    every bar of the day equally).
    """

    cached_closes = _close_by_day(cached_bars)
    fresh_closes = _close_by_day(fresh_bars)
    overlap_days = sorted(set(cached_closes) & set(fresh_closes))

    max_dev = 0.0
    samples: list[AdjustDriftSample] = []
    for day in overlap_days:
        cached_close = cached_closes[day]
        fresh_close = fresh_closes[day]
        baseline = max(abs(cached_close), abs(fresh_close))
        if baseline == 0:
            # Both zero → no deviation; one zero with nonzero peer is an
            # infinite relative move and definitely drift.
            deviation = 0.0 if cached_close == fresh_close else float("inf")
        else:
            deviation = abs(fresh_close - cached_close) / baseline
        if deviation > max_dev:
            max_dev = deviation
        if deviation > tolerance and len(samples) < max_samples:
            samples.append(
                AdjustDriftSample(
                    timestamp=day,
                    cached_close=cached_close,
                    fresh_close=fresh_close,
                    rel_deviation=deviation,
                )
            )

    return AdjustDriftReport(
        drifted=bool(overlap_days) and max_dev > tolerance,
        overlap_count=len(overlap_days),
        max_rel_deviation=max_dev,
        samples=tuple(samples),
        tolerance=tolerance,
    )


__all__ = [
    "ANCHOR_OVERLAP_CALENDAR_DAYS",
    "DEFAULT_DRIFT_TOLERANCE",
    "AdjustDriftReport",
    "AdjustDriftSample",
    "detect_adjust_drift",
]
