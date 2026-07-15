"""Pure-function backtest verification for persisted decision signals.

Ported (in spirit) from DSA's ``BacktestEngine.evaluate_decision_signal`` /
``infer_direction_expected`` / ``_evaluate_targets``, re-fed with doyoutrade
``cached_bars`` rows. No DB access here — callers (platform service, API
evaluate endpoint) fetch bars via
``SqlAlchemyCachedBarsRepository.bars_in_range`` and persist results via
``SqlAlchemyDecisionSignalRepository.upsert_outcome``.

Bar shape: the dicts returned by ``bars_in_range`` /
:class:`doyoutrade.core.models.Bar` kwargs — keys ``timestamp`` (str,
``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM:SS``), ``open`` / ``high`` / ``low`` /
``close`` (float).

Conventions (documented, deterministic):

- ``anchor_date`` is exclusive: evaluation bars are strictly AFTER the anchor
  day (the signal fired on/using the anchor day's data; you could only act the
  next session).
- ``entry_price`` is the first post-anchor bar's **open** (falls back to that
  bar's close when open is missing/zero — e.g. a synthetic bar).
- ``exit_price`` is the close of the last bar inside the ``horizon_days``
  window; ``return_pct`` / ``max_gain_pct`` / ``max_drawdown_pct`` are all
  relative to ``entry_price`` in percent (drawdown is <= 0).
- Target evaluation walks bars chronologically and uses intraday extremes
  (high/low) to decide which of target_price / stop_loss was touched first;
  same-bar ties resolve pessimistically (stop first).
- Without targets, the direction verdict uses a ±1% neutral band on
  ``return_pct``.
- Fewer than ``horizon_days`` bars after the anchor →
  ``{"outcome": None, "reason": "data_insufficient", ...}``; the caller must
  skip AND surface this (debug event / structured response), never silently.
"""

from __future__ import annotations

from typing import Any

ENGINE_VERSION_DEFAULT = "v1"

#: ±band (percent) inside which a directionless / no-target verdict is neutral.
NEUTRAL_BAND_PCT = 1.0

_DIRECTION_BY_ACTION = {
    "buy": "up",
    "add": "up",
    "hold": "flat",
    "watch": "flat",
    "sell": "down",
    "reduce": "down",
    "take_profit": "down",
    "stop_loss": "down",
}


def infer_direction_expected(action: str) -> str:
    """Map a decision-signal action (八态) to the expected price direction."""
    key = str(action or "").strip().lower()
    direction = _DIRECTION_BY_ACTION.get(key)
    if direction is None:
        raise ValueError(
            f"unknown decision signal action: {action!r} "
            f"(expected one of {sorted(_DIRECTION_BY_ACTION)})"
        )
    return direction


def _bar_day(bar: dict[str, Any]) -> str:
    return str(bar.get("timestamp") or "")[:10]


def _price(bar: dict[str, Any], field: str) -> float | None:
    value = bar.get(field)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"bar[{field!r}] must be numeric, got {type(value).__name__}: {value!r}"
        ) from None
    return number if number > 0 else None


def _to_float_or_none(value: Any, name: str) -> float | None:
    """Strict numeric parse for signal price fields (decimal strings or numbers)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"signal field {name!r} must be a decimal string or number, "
            f"got {type(value).__name__}: {value!r}"
        ) from None


def _evaluate_targets(
    *,
    direction: str,
    entry_price: float,
    window: list[dict[str, Any]],
    target_price: float | None,
    stop_loss: float | None,
) -> str | None:
    """First-touch outcome vs explicit price targets; None when undecidable.

    For an ``up`` signal: touching ``target_price`` (bar high >= target) first
    is a hit; touching ``stop_loss`` (bar low <= stop) first is a miss. For a
    ``down`` signal the roles invert (falling to target = hit, rising to stop
    = miss). A bar touching both resolves pessimistically (miss). ``None``
    when neither was touched inside the window (caller falls back to the
    direction/return verdict).
    """
    if target_price is None and stop_loss is None:
        return None
    for bar in window:
        high = _price(bar, "high") or _price(bar, "close") or 0.0
        low = _price(bar, "low") or _price(bar, "close") or 0.0
        if direction == "down":
            hit = target_price is not None and low <= target_price
            stopped = stop_loss is not None and high >= stop_loss
        else:
            hit = target_price is not None and high >= target_price
            stopped = stop_loss is not None and low <= stop_loss
        if stopped:
            return "miss"
        if hit:
            return "hit"
    return None


def _direction_verdict(direction: str, return_pct: float) -> tuple[str, bool]:
    """(outcome, direction_correct) from the horizon return alone."""
    if direction == "up":
        correct = return_pct > 0
        if return_pct > NEUTRAL_BAND_PCT:
            return "hit", correct
        if return_pct < -NEUTRAL_BAND_PCT:
            return "miss", correct
        return "neutral", correct
    if direction == "down":
        correct = return_pct < 0
        if return_pct < -NEUTRAL_BAND_PCT:
            return "hit", correct
        if return_pct > NEUTRAL_BAND_PCT:
            return "miss", correct
        return "neutral", correct
    # flat: staying inside the band is the prediction coming true.
    correct = abs(return_pct) <= NEUTRAL_BAND_PCT
    return ("hit" if correct else "miss"), correct


def evaluate_decision_signal(
    signal_fields: dict[str, Any],
    bars: list[dict[str, Any]],
    *,
    horizon_days: int,
    engine_version: str = ENGINE_VERSION_DEFAULT,
) -> dict[str, Any]:
    """Evaluate one decision signal against daily bars.

    ``signal_fields`` needs ``action`` and ``anchor_date`` (``YYYY-MM-DD``);
    ``target_price`` / ``stop_loss`` are optional decimal strings or numbers.
    ``bars`` are chronological daily bars (any bars on/before ``anchor_date``
    are ignored). Returns a dict directly consumable by
    ``SqlAlchemyDecisionSignalRepository.upsert_outcome`` — or, when fewer
    than ``horizon_days`` post-anchor bars exist,
    ``{"outcome": None, "reason": "data_insufficient", ...}``.
    """
    if not isinstance(horizon_days, int) or horizon_days < 1:
        raise ValueError(
            f"horizon_days must be a positive int, got {type(horizon_days).__name__}: {horizon_days!r}"
        )
    anchor_date = str(signal_fields.get("anchor_date") or "")[:10]
    if len(anchor_date) != 10:
        raise ValueError(
            f"signal_fields['anchor_date'] must be YYYY-MM-DD, got {signal_fields.get('anchor_date')!r}"
        )
    direction = infer_direction_expected(str(signal_fields.get("action") or ""))
    target_price = _to_float_or_none(signal_fields.get("target_price"), "target_price")
    stop_loss = _to_float_or_none(signal_fields.get("stop_loss"), "stop_loss")

    post = sorted(
        (bar for bar in bars if _bar_day(bar) > anchor_date),
        key=_bar_day,
    )
    horizon_label = str(signal_fields.get("horizon") or f"{horizon_days}d")
    base: dict[str, Any] = {
        "horizon": horizon_label,
        "engine_version": engine_version,
        "direction_expected": direction,
        "anchor_date": anchor_date,
        "eval_window_days": horizon_days,
    }
    if len(post) < horizon_days:
        return {
            **base,
            "outcome": None,
            "reason": "data_insufficient",
            "bars_available": len(post),
            "bars_required": horizon_days,
        }

    window = post[:horizon_days]
    entry_bar = window[0]
    entry_price = _price(entry_bar, "open") or _price(entry_bar, "close")
    if entry_price is None:
        return {
            **base,
            "outcome": None,
            "reason": "data_insufficient",
            "bars_available": len(post),
            "bars_required": horizon_days,
            "detail": f"entry bar has no usable open/close: {entry_bar!r}",
        }
    exit_price = _price(window[-1], "close") or entry_price
    highs = [h for h in (_price(b, "high") for b in window) if h is not None]
    lows = [low for low in (_price(b, "low") for b in window) if low is not None]
    max_gain_pct = ((max(highs) - entry_price) / entry_price * 100.0) if highs else 0.0
    max_drawdown_pct = ((min(lows) - entry_price) / entry_price * 100.0) if lows else 0.0
    return_pct = (exit_price - entry_price) / entry_price * 100.0

    fallback_outcome, direction_correct = _direction_verdict(direction, return_pct)
    target_outcome = _evaluate_targets(
        direction=direction,
        entry_price=entry_price,
        window=window,
        target_price=target_price,
        stop_loss=stop_loss,
    )
    outcome = target_outcome or fallback_outcome

    return {
        **base,
        "outcome": outcome,
        "direction_correct": direction_correct,
        "entry_price": round(entry_price, 6),
        "exit_price": round(exit_price, 6),
        "max_gain_pct": round(max_gain_pct, 4),
        "max_drawdown_pct": round(min(max_drawdown_pct, 0.0), 4),
        "return_pct": round(return_pct, 4),
    }


def parse_horizon_days(horizon: str) -> int:
    """``"5d"`` → 5. Strict: anything not ``<int>d`` (or bare int) raises."""
    text = str(horizon or "").strip().lower()
    if text.endswith("d"):
        text = text[:-1]
    if not text.isdigit() or int(text) < 1:
        raise ValueError(
            f"horizon must look like '5d' (positive day count), got {horizon!r}"
        )
    return int(text)
