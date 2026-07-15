"""Write-time bar continuity validation.

Any path that persists historical bars into the local DB (gap backfill,
force-refresh, batch sync) must guarantee the rows are *continuous* — every
trading day in the window is present, except days the symbol was suspended
(停牌) or the exchange was closed (节假日/周末). A missing trading day that is
not a suspension is a data defect; persisting it produces silent holes that
later feed wrong indicator values. Per the user's invariant, such a payload
must fail the whole write rather than land partial/dirty data.

This module is the pure, side-effect-free judge. It does NOT decide what to do
on a violation and it does NOT touch the DB or emit events — it returns a
structured :class:`ContinuityReport` with a ``classification`` and the caller
(``LocalHistoricalBarsDataProvider``) applies the task's
:class:`doyoutrade.data.cache_policy.DataCachePolicy` (``on_unverifiable_gap``)
and emits the debug events. Keeping the judgement pure makes every branch unit
testable without a provider/DB fixture.

Calendar inputs (``expected_trading_days`` / ``suspended_days``) are
``YYYY-MM-DD`` strings. ``authoritative`` must be ``True`` only when the
trading calendar was produced by the SAME provider that served the bars and
that provider declares ``capabilities.authoritative_calendar`` — a cross-source
calendar (e.g. qmt calendar judging baostock bars) would manufacture false
gaps and is explicitly downgraded by the caller instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from doyoutrade.data.bar_timestamp import normalize_bar_timestamp

CONTINUITY_VIOLATION_ERROR_CODE = "market_data_continuity_violation"

#: How many offending missing days to surface in the report / error before
#: truncating — keeps a long-history defect from producing a megabyte event.
_MAX_REPORTED_MISSING = 40


@dataclass(frozen=True)
class ContinuityReport:
    """Structured verdict for a candidate bar payload.

    ``classification`` drives the caller's action:

    * ``ok`` — continuous; persist freely.
    * ``calendar_violation`` — authoritative calendar + suspension source
      available; the remaining missing days are confirmed defects → always
      reject the write.
    * ``calendar_unverifiable`` — authoritative calendar but no usable
      suspension source; missing days cannot be proven to be halts →
      caller applies ``on_unverifiable_gap`` (``fail`` rejects, ``degrade``
      persists + warns).
    * ``internal_gap_violation`` — degraded mode (non-authoritative calendar)
      and the returned bars themselves contain a gap larger than the allowed
      internal gap → always reject (a real hole, regardless of calendar).
    * ``degraded_ok`` — degraded mode, internal gap acceptable; persist but the
      caller should emit ``continuity_degraded`` (we could not run the
      authoritative check).
    """

    classification: str
    mode: str  # "calendar" | "internal_gap"
    authoritative: bool
    covered_count: int
    expected_count: int | None
    suspended_count: int
    missing_days: list[str] = field(default_factory=list)
    largest_internal_gap_days: int = 0
    reason: str | None = None
    hint: str | None = None

    @property
    def ok(self) -> bool:
        return self.classification in ("ok", "degraded_ok")

    @property
    def is_hard_violation(self) -> bool:
        """Classifications that must reject the write regardless of policy."""
        return self.classification in ("calendar_violation", "internal_gap_violation")

    def as_payload(self) -> dict[str, Any]:
        return {
            "continuity_classification": self.classification,
            "continuity_mode": self.mode,
            "authoritative_calendar": self.authoritative,
            "covered_count": self.covered_count,
            "expected_count": self.expected_count,
            "suspended_count": self.suspended_count,
            "missing_days_count": len(self.missing_days),
            "missing_days_sample": self.missing_days[:_MAX_REPORTED_MISSING],
            "largest_internal_gap_days": self.largest_internal_gap_days,
            "reason": self.reason,
            "hint": self.hint,
        }


class ContinuityError(ValueError):
    """Raised by the caller to reject a non-continuous write.

    Carries ``error_code`` so the structured failure event / envelope can use a
    stable token (``market_data_continuity_violation``) rather than parsing the
    free-text message.
    """

    error_code = CONTINUITY_VIOLATION_ERROR_CODE

    def __init__(self, message: str, *, report: ContinuityReport) -> None:
        super().__init__(message)
        self.report = report


def covered_days_from_payloads(
    bars: list[dict[str, Any]], *, interval: str
) -> set[date]:
    """Extract the set of distinct trading days present in ``bars``.

    Raises ``ValueError`` (never silently skips) on a bar with an empty or
    unparseable timestamp — a malformed timestamp must surface as a schema
    violation, not get dropped and shrink the covered set (§错误可见性).
    """
    covered: set[date] = set()
    for bar in bars:
        timestamp = str(bar.get("timestamp") or "").strip()
        if not timestamp:
            raise ValueError(
                "market_data_continuity_bar_timestamp_invalid: empty bar timestamp "
                f"in payload {bar!r}"
            )
        normalized = normalize_bar_timestamp(timestamp)
        if not normalized or len(normalized) < 10:
            raise ValueError(
                "market_data_continuity_bar_timestamp_invalid: cannot normalize "
                f"bar timestamp {timestamp!r}"
            )
        try:
            day = date.fromisoformat(normalized[:10])
        except ValueError as exc:
            raise ValueError(
                "market_data_continuity_bar_timestamp_invalid: invalid bar day "
                f"{normalized[:10]!r}"
            ) from exc
        covered.add(day)
    return covered


def _largest_internal_gap_days(covered: set[date]) -> int:
    if len(covered) < 2:
        return 0
    ordered = sorted(covered)
    return max((b - a).days for a, b in zip(ordered, ordered[1:]))


def _to_day_set(days: set[str] | frozenset[str] | None) -> set[date]:
    out: set[date] = set()
    for raw in days or ():
        text = str(raw).strip()[:10]
        if not text:
            continue
        try:
            out.add(date.fromisoformat(text))
        except ValueError:
            # An unparseable calendar/suspension entry is dropped here but the
            # caller is responsible for sourcing clean calendar data; we never
            # let it shrink the *covered* set (that path raises above).
            continue
    return out


def validate_continuity(
    *,
    bars: list[dict[str, Any]],
    interval: str,
    expected_trading_days: set[str] | frozenset[str] | None,
    suspended_days: set[str] | frozenset[str] | None,
    authoritative: bool,
    suspension_source_available: bool,
    max_internal_gap_days: int,
) -> ContinuityReport:
    """Judge whether ``bars`` are continuous enough to persist.

    Parameters mirror the data actually available at the write site:

    * ``expected_trading_days`` — authoritative calendar days for the requested
      window (``None`` when the served provider has no authoritative calendar).
    * ``suspended_days`` — days the symbol is known to be suspended, to subtract
      from the calendar before judging gaps.
    * ``authoritative`` — calendar is authoritative AND same-source as the bars.
    * ``suspension_source_available`` — the served provider gave a usable
      per-date suspension signal (only baostock today). Distinguishes a
      "confirmed defect" from an "unverifiable" missing day.
    """
    covered = covered_days_from_payloads(bars, interval=interval)
    largest_gap = _largest_internal_gap_days(covered)

    if not authoritative or expected_trading_days is None:
        # Degraded path: no authoritative calendar to compare against. Fall back
        # to the legacy "no large internal gap" heuristic and let the caller
        # announce the degradation.
        if largest_gap > max_internal_gap_days:
            return ContinuityReport(
                classification="internal_gap_violation",
                mode="internal_gap",
                authoritative=False,
                covered_count=len(covered),
                expected_count=None,
                suspended_count=0,
                largest_internal_gap_days=largest_gap,
                reason="internal_gap_too_large",
                hint=(
                    f"returned bars contain a {largest_gap}-day internal gap "
                    f"(> {max_internal_gap_days}d); upstream data is incomplete — "
                    "the whole write is rejected to avoid persisting a hole"
                ),
            )
        return ContinuityReport(
            classification="degraded_ok",
            mode="internal_gap",
            authoritative=False,
            covered_count=len(covered),
            expected_count=None,
            suspended_count=0,
            largest_internal_gap_days=largest_gap,
            reason="non_authoritative_calendar",
            hint=(
                "served provider has no authoritative trading calendar; only the "
                "internal-gap check ran. Configure a qmt/baostock source for a "
                "calendar-level continuity guarantee"
            ),
        )

    expected = _to_day_set(expected_trading_days)
    suspended = _to_day_set(suspended_days)
    # Validate INTERNAL continuity only: restrict the expected calendar to the
    # span actually covered by the bars, ``[min(covered), max(covered)]``. A
    # trading day strictly inside that span that is neither covered nor a
    # suspension is a real hole. Days before the first / after the last covered
    # bar are listing boundaries (IPO not yet traded, delisting, requested
    # window wider than the symbol's life) — not gaps — and must not be flagged,
    # or every IPO backfill would falsely fail.
    if covered:
        span_lo, span_hi = min(covered), max(covered)
        expected_in_span = {d for d in expected if span_lo <= d <= span_hi}
    else:
        expected_in_span = set()
    missing = sorted(expected_in_span - suspended - covered)
    missing_str = [d.isoformat() for d in missing]
    expected_count = len(expected_in_span)

    if not missing:
        return ContinuityReport(
            classification="ok",
            mode="calendar",
            authoritative=True,
            covered_count=len(covered),
            expected_count=expected_count,
            suspended_count=len(suspended),
            largest_internal_gap_days=largest_gap,
        )

    if suspension_source_available:
        # Suspensions already subtracted from a trustworthy per-date source, so
        # the remainder are genuine defects — always reject.
        return ContinuityReport(
            classification="calendar_violation",
            mode="calendar",
            authoritative=True,
            covered_count=len(covered),
            expected_count=expected_count,
            suspended_count=len(suspended),
            missing_days=missing_str,
            largest_internal_gap_days=largest_gap,
            reason="calendar_gap_confirmed",
            hint=(
                f"{len(missing)} trading day(s) missing from upstream after "
                "subtracting known suspensions; the whole write is rejected to "
                "avoid persisting a discontinuous series"
            ),
        )

    # Authoritative calendar but no per-date suspension source: cannot prove the
    # missing days are halts. Hand the decision to the caller's policy.
    return ContinuityReport(
        classification="calendar_unverifiable",
        mode="calendar",
        authoritative=True,
        covered_count=len(covered),
        expected_count=expected_count,
        suspended_count=len(suspended),
        missing_days=missing_str,
        largest_internal_gap_days=largest_gap,
        reason="calendar_gap_unverifiable",
        hint=(
            f"{len(missing)} calendar trading day(s) missing and the served "
            "provider exposes no per-date suspension signal, so a halt cannot be "
            "ruled out; on_unverifiable_gap decides fail vs degrade"
        ),
    )


__all__ = [
    "ContinuityReport",
    "ContinuityError",
    "CONTINUITY_VIOLATION_ERROR_CODE",
    "covered_days_from_payloads",
    "validate_continuity",
]
