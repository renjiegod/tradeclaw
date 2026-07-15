"""Internal market-data helper used by ``data_run``.

This module no longer exposes a chat / CLI tool — the ``data ohlcv``
command and the ``get_market_data`` operation registration were removed
when ``data run`` became the single market-data entry point. What
remains here is a stateful helper class plus the typed exception /
filename / artifacts-root helpers other operations import.

Public surface (kept):

* :class:`MarketDataFetcher` — instantiated per call; reuses the data
  factory's auto / single-provider chain via
  :meth:`_fetch_ohlcv` and resolves ``period`` / ``start_date`` /
  ``end_date`` windows via :meth:`_resolve_window`.
* :class:`_InvalidPeriod` / :class:`_InvalidDate` /
  :class:`_ConflictingRange` — typed exceptions consumed by
  ``data_run`` for the per-window failure modes.
* :func:`_get_artifacts_root` / :func:`_safe_code` — artifacts dir
  conventions, also imported by ``pattern`` / ``indicators_compute``.
* ``ARTIFACTS_ROOT`` — import-time snapshot for legacy callers that
  hold a reference rather than calling the function each time.

If you need a fresh chat-facing market-data tool, build it on top of
``MarketDataFetcher`` — don't re-introduce an ``OperationHandler``
subclass here.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Note: ``data_source`` enum and the data_factory provider chain are
# documented in :mod:`doyoutrade.data.factory`. Callers carry their own
# whitelist (see ``data_run._SUPPORTED_DATA_SOURCES`` /
# ``stock_screen._SUPPORTED_DATA_SOURCES``) — this helper accepts whatever
# the factory accepts and lets the upstream raise on unknown ids.


_PERIOD_PATTERN = re.compile(r"^\s*(\d+)\s*(d|w|m|mo|y)\s*$", re.IGNORECASE)
# Unit -> approximate calendar-day length. `m` and `mo` both mean "month" so
# the historical `1m`/`6m` callers keep working while `1mo`/`3mo` are also
# accepted; `y` follows the existing 365-day convention.
_PERIOD_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "mo": 30, "y": 365}


class _InvalidPeriod(ValueError):
    """Raised when the ``period`` kwarg can't be parsed."""


class _InvalidDate(ValueError):
    """Raised when a ``start_date`` / ``end_date`` kwarg can't be parsed.

    Callers surface this as ``invalid_date`` so agents get a stable
    ``error_code`` token rather than a free-form prose message.
    """


class _ConflictingRange(ValueError):
    """Raised when both ``period`` and ``start_date`` / ``end_date`` are given.

    Per CLAUDE.md's "no silent fallback" rule we refuse to guess which one
    the caller meant — the agent (or operator) gets a structured
    ``conflicting_range_args`` envelope and must drop one of the inputs.
    """


def _get_artifacts_root() -> Path:
    return Path.home() / ".doyoutrade" / "assistant" / "artifacts"


# Backward-compatible module-level constant (computed at import time for
# consumers who import it directly rather than calling the function).
ARTIFACTS_ROOT = _get_artifacts_root()


def _safe_code(code: str) -> str:
    """Sanitize a symbol code so it can be used inside a filename."""
    return code.replace("/", "_").replace("\\", "_").replace(":", "_")


class MarketDataFetcher:
    """Internal helper: fetch OHLCV bars through the data-factory provider chain.

    Each instance records the **last** provider that actually answered
    a request on ``_last_used_source``; callers that pass
    ``data_source="auto"`` read this back so the per-symbol envelope can
    report the real source instead of just "auto".
    """

    def __init__(self) -> None:
        self._last_used_source: str = "unknown"

    # ------------------------------------------------------------------
    # Window resolution
    # ------------------------------------------------------------------

    def _period_to_start_date(self, period: str, end_dt: date) -> date:
        """Parse ``<N><unit>`` (d/w/m/mo/y) into a start date.

        Raises :class:`_InvalidPeriod` on unrecognized input — callers
        must surface this as a structured ``invalid_period`` error rather
        than silently falling back to a default window (a 1y fallback
        previously caused ``period='1mo'`` to fetch ~242 daily bars).
        """

        if not isinstance(period, str):
            raise _InvalidPeriod(f"period must be a string, got {type(period).__name__}")
        match = _PERIOD_PATTERN.match(period)
        if not match:
            raise _InvalidPeriod(
                f"invalid period {period!r}; expected <N><unit> with unit in "
                "d/w/m/mo/y (e.g. '20d', '3w', '1mo', '6m', '1y')"
            )
        count = int(match.group(1))
        if count <= 0:
            raise _InvalidPeriod(
                f"invalid period {period!r}; count must be a positive integer"
            )
        unit = match.group(2).lower()
        days = _PERIOD_UNIT_DAYS[unit] * count
        return end_dt - timedelta(days=days)

    def _parse_iso_date(self, label: str, value: Any) -> date:
        """Parse ``YYYY-MM-DD`` into :class:`date`.

        Raises :class:`_InvalidDate` for non-strings, malformed strings,
        or impossible calendar dates. We refuse to silently coerce —
        skipping a typo would let a backtest fetch the wrong window and
        the operator would never know.
        """

        if not isinstance(value, str):
            raise _InvalidDate(
                f"{label} must be a YYYY-MM-DD string, got "
                f"{type(value).__name__}({value!r})"
            )
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise _InvalidDate(
                f"{label}={value!r} is not a valid YYYY-MM-DD date: {exc}"
            ) from exc

    def _resolve_window(
        self,
        *,
        period: str | None,
        start_date: str | None,
        end_date: str | None,
    ) -> tuple[date, date, str]:
        """Resolve the (start_dt, end_dt, label) window from caller inputs.

        Three valid input shapes:

        * ``period`` only — relative window ending today (default 1y when
          period is omitted entirely).
        * ``start_date`` and/or ``end_date`` — absolute window. If only
          one side is given, the other defaults to ``today`` (for missing
          ``end_date``) or ``end_date - 1y`` (for missing ``start_date``).
        * neither — fall back to ``period='1y'``.

        Mixing ``period`` with ``start_date`` / ``end_date`` raises
        :class:`_ConflictingRange`. Returns the label string for logging.
        """

        period_provided = period is not None
        absolute_provided = start_date is not None or end_date is not None

        if period_provided and absolute_provided:
            given = []
            if start_date is not None:
                given.append("start_date")
            if end_date is not None:
                given.append("end_date")
            raise _ConflictingRange(
                f"period={period!r} cannot be combined with "
                f"{', '.join(given)}"
            )

        today = date.today()

        if absolute_provided:
            if start_date is not None and end_date is not None:
                start_dt = self._parse_iso_date("start_date", start_date)
                end_dt = self._parse_iso_date("end_date", end_date)
            elif start_date is not None:
                start_dt = self._parse_iso_date("start_date", start_date)
                end_dt = today
            else:
                # only end_date given
                end_dt = self._parse_iso_date("end_date", end_date)
                start_dt = end_dt - timedelta(days=365)
            if start_dt > end_dt:
                raise _InvalidDate(
                    f"start_date={start_dt.isoformat()} is after "
                    f"end_date={end_dt.isoformat()}"
                )
            return start_dt, end_dt, f"{start_dt.isoformat()}..{end_dt.isoformat()}"

        # Relative-only path. ``period`` may be None (default 1y) or a
        # caller-supplied string we still need to validate.
        period_value = period if period is not None else "1y"
        end_dt = today
        start_dt = self._period_to_start_date(period_value, end_dt)
        return start_dt, end_dt, period_value

    # ------------------------------------------------------------------
    # Provider fetch
    # ------------------------------------------------------------------

    async def _fetch_ohlcv(
        self,
        code: str,
        *,
        start_dt: date,
        end_dt: date,
        period_label: str,
        interval: str,
        data_source: str,
    ):
        """Fetch OHLCV bars via the factory and translate to a DataFrame.

        * ``data_source != "auto"`` builds a single-provider stack and
          surfaces upstream failures by raising — callers map it to
          ``data_fetch_failed`` in their envelopes.
        * ``data_source == "auto"`` builds the factory's auto chain,
          which fires ``market_data_provider_skipped`` debug events on
          each fallback so the operator can see which source actually
          answered.
        """
        from doyoutrade.config import get_config
        from doyoutrade.core.models import Bar
        from doyoutrade.data.account_resolution import resolve_default_market_account
        from doyoutrade.data.factory import build_trading_data_stack
        from doyoutrade.data.fallback_provider import FallbackHistoricalDataProvider

        start_iso = start_dt.isoformat()
        end_iso = end_dt.isoformat()
        data_cfg = get_config().data
        account = await resolve_default_market_account()

        logger.info(
            "market_data: fetching %s window=%s interval=%s data_source=%s",
            code, period_label, interval, data_source,
        )

        try:
            provider, _u, _a = build_trading_data_stack(
                data_source, data_cfg, [code], account=account
            )
        except Exception as exc:
            logger.error(
                "market_data: factory build failed for data_source=%s: %s",
                data_source, exc,
            )
            raise RuntimeError(
                f"data_source={data_source!r} is not available: {exc}"
            ) from exc

        try:
            bars: list[Bar] = list(
                await provider.get_bars(
                    code, start_iso, end_iso, interval=interval
                )
            )
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 — close failures are not fatal
                    logger.warning(
                        "market_data: provider.aclose() raised for data_source=%s",
                        data_source, exc_info=True,
                    )

        # Resolve which provider actually answered. Single-provider stacks
        # take their capabilities name directly; the fallback wrapper
        # records ``last_used_provider`` on the first non-empty return so
        # ``data_source=auto`` reports the real source.
        if isinstance(provider, FallbackHistoricalDataProvider):
            self._last_used_source = (
                provider.last_used_provider or data_source
            )
        else:
            caps = getattr(provider, "capabilities", None)
            self._last_used_source = (
                getattr(caps, "name", None) if caps is not None else None
            ) or data_source

        if not bars:
            raise RuntimeError(
                f"no bars returned for {code} via data_source={data_source!r} "
                f"({start_iso}..{end_iso}, interval={interval})"
            )

        return self._bars_to_dataframe(bars)

    def _bars_to_dataframe(self, bars: list[Any]) -> Any:
        """Translate ``list[Bar]`` to the DataFrame shape downstream expects.

        ``amount`` (成交额 / turnover) is carried through when the provider supplies
        it, so downstream consumers (avg-amount screening, an agent reading the
        OHLCV CSV) can use turnover rather than only ``volume`` (traded quantity).
        It is omitted entirely when no bar reports it, so providers without a
        turnover field keep the legacy ``open/high/low/close/volume`` column set
        (no all-NaN column, no change for existing callers).
        """
        import pandas as pd

        include_amount = any(getattr(bar, "amount", None) is not None for bar in bars)
        rows = [
            {
                "date": bar.timestamp,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                **({"amount": bar.amount} if include_amount else {}),
            }
            for bar in bars
        ]
        df = pd.DataFrame(rows)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
        return df


__all__ = [
    "ARTIFACTS_ROOT",
    "MarketDataFetcher",
    "_ConflictingRange",
    "_InvalidDate",
    "_InvalidPeriod",
    "_get_artifacts_root",
    "_safe_code",
]
