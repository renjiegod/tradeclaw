"""The 6 built-in preset condition detectors + registry.

Each detector is ``detect(ctx: EvalContext, params: dict) -> (triggered, diagnostics)``.
Detectors are PURE functions of the current snapshot + PRIOR-tick state (the
daemon folds the current tick into state *after* evaluation), so 打开 is a true
seal→unseal transition and 大减 measures a drop from the intraday peak.

Realtime limit prices come from ``snapshot.limit_up_price`` /
``limit_down_price`` (computed in the data layer from ``a_share_limit_pct`` ×
``prev_close``). A leaf may pass ``params.limit_pct`` to override the inferred
board pct (e.g. an ST/*ST 5% name that ``a_share_limit_pct`` cannot detect from
the code prefix). NOTE: we deliberately do NOT use ``limit_up_approx`` /
``limit_down_approx`` — those are daily-bar Series approximations (require
close==high/low) and are wrong for a realtime tick.

Missing seal volume (``bid_vol1`` is None while at limit-up) makes the shrink
detector SKIP with ``skipped_reason='seal_vol_missing'`` — it is never treated as
a zero seal, which would false-fire 大减 (CLAUDE.md §错误可见性 tolerant-fallback
ban).
"""

from __future__ import annotations

from typing import Any, Callable

from doyoutrade.monitoring.evaluator import EvalContext
from doyoutrade.monitoring.state import at_limit_down, at_limit_up
from doyoutrade.strategy_sdk.indicators import a_share_limit_pct

Detector = Callable[[EvalContext, dict], "tuple[bool, dict]"]

DEFAULT_SHRINK_PCT = 0.5
DEFAULT_OPEN_EPS = 0.01


def _validate_pct(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"limit_pct must be a number in (0, 1), got {value!r}")
    pct = float(value)
    if pct <= 0.0 or pct >= 1.0:
        raise ValueError(f"limit_pct must be in (0, 1), got {pct!r}")
    return pct


def _resolve_limits(snapshot, params: dict) -> tuple[float | None, float | None]:
    """Resolve (limit_up_price, limit_down_price) for the snapshot.

    Priority: explicit ``params.limit_pct`` override > snapshot-precomputed >
    recompute from ``a_share_limit_pct(symbol)`` × ``prev_close``.
    """
    prev = getattr(snapshot, "prev_close", None)
    override = params.get("limit_pct")
    if override is not None:
        pct = _validate_pct(override)
        if prev is None or prev <= 0:
            return None, None
        return round(prev * (1.0 + pct), 2), round(prev * (1.0 - pct), 2)

    lu = getattr(snapshot, "limit_up_price", None)
    ld = getattr(snapshot, "limit_down_price", None)
    if lu is not None or ld is not None:
        return lu, ld

    symbol = getattr(snapshot, "symbol", "") or ""
    if prev is None or prev <= 0:
        return None, None
    pct = a_share_limit_pct(symbol)
    return round(prev * (1.0 + pct), 2), round(prev * (1.0 - pct), 2)


def detect_limit_up(ctx: EvalContext, params: dict) -> tuple[bool, dict]:
    snap = ctx.snapshot
    price = getattr(snap, "price", None)
    lu, _ = _resolve_limits(snap, params)
    if price is None or lu is None:
        return False, {
            "skipped_reason": "price_or_limit_unavailable",
            "hint": "need last price and prev_close to compute limit-up",
        }
    return at_limit_up(price, lu), {"price": price, "limit_price": lu}


def detect_limit_down(ctx: EvalContext, params: dict) -> tuple[bool, dict]:
    snap = ctx.snapshot
    price = getattr(snap, "price", None)
    _, ld = _resolve_limits(snap, params)
    if price is None or ld is None:
        return False, {
            "skipped_reason": "price_or_limit_unavailable",
            "hint": "need last price and prev_close to compute limit-down",
        }
    return at_limit_down(price, ld), {"price": price, "limit_price": ld}


def _detect_seal_shrink(
    ctx: EvalContext,
    params: dict,
    *,
    side: str,
) -> tuple[bool, dict]:
    snap = ctx.snapshot
    st = ctx.state
    price = getattr(snap, "price", None)
    lu, ld = _resolve_limits(snap, params)
    if side == "up":
        at_limit = at_limit_up(price, lu)
        limit_price = lu
        seal_now = getattr(snap, "bid_vol1", None)
        peak = st.seal_peak_bid
    else:
        at_limit = at_limit_down(price, ld)
        limit_price = ld
        seal_now = getattr(snap, "ask_vol1", None)
        peak = st.seal_peak_ask

    if not at_limit:
        return False, {"reason": "not_at_limit", "price": price, "limit_price": limit_price}
    if seal_now is None:
        return False, {
            "skipped_reason": "seal_vol_missing",
            "hint": "order-book seal volume not in snapshot; cannot judge 大减",
            "limit_price": limit_price,
        }
    if peak is None or peak <= 0:
        return False, {"reason": "no_peak_yet", "seal_now": seal_now, "limit_price": limit_price}

    shrink_pct = float(params.get("shrink_pct", DEFAULT_SHRINK_PCT))
    min_peak_vol = int(params.get("min_peak_vol", 0))
    if peak < min_peak_vol:
        return False, {
            "reason": "peak_below_min",
            "seal_peak": peak,
            "min_peak_vol": min_peak_vol,
            "limit_price": limit_price,
        }
    triggered = seal_now <= peak * (1.0 - shrink_pct)
    drop_pct = round((peak - seal_now) / peak, 4) if peak > 0 else None
    return triggered, {
        "seal_peak": peak,
        "seal_now": seal_now,
        "drop_pct": drop_pct,
        "shrink_pct": shrink_pct,
        "limit_price": limit_price,
    }


def detect_limit_up_seal_shrink(ctx: EvalContext, params: dict) -> tuple[bool, dict]:
    return _detect_seal_shrink(ctx, params, side="up")


def detect_limit_down_seal_shrink(ctx: EvalContext, params: dict) -> tuple[bool, dict]:
    return _detect_seal_shrink(ctx, params, side="down")


def detect_limit_up_open(ctx: EvalContext, params: dict) -> tuple[bool, dict]:
    snap = ctx.snapshot
    st = ctx.state
    price = getattr(snap, "price", None)
    lu, _ = _resolve_limits(snap, params)
    if price is None or lu is None:
        return False, {
            "skipped_reason": "price_or_limit_unavailable",
            "hint": "need last price and prev_close to detect limit-up board open",
        }
    open_eps = float(params.get("open_eps", DEFAULT_OPEN_EPS))
    if st.last_sealed_up and price < lu - open_eps:
        return True, {"price": price, "limit_price": lu, "was_sealed": True}
    return False, {"price": price, "limit_price": lu, "was_sealed": bool(st.last_sealed_up)}


def detect_limit_down_open(ctx: EvalContext, params: dict) -> tuple[bool, dict]:
    snap = ctx.snapshot
    st = ctx.state
    price = getattr(snap, "price", None)
    _, ld = _resolve_limits(snap, params)
    if price is None or ld is None:
        return False, {
            "skipped_reason": "price_or_limit_unavailable",
            "hint": "need last price and prev_close to detect limit-down board open",
        }
    open_eps = float(params.get("open_eps", DEFAULT_OPEN_EPS))
    if st.last_sealed_down and price > ld + open_eps:
        return True, {"price": price, "limit_price": ld, "was_sealed": True}
    return False, {"price": price, "limit_price": ld, "was_sealed": bool(st.last_sealed_down)}


PRESET_DETECTORS: dict[str, Detector] = {
    "limit_up": detect_limit_up,
    "limit_down": detect_limit_down,
    "limit_up_seal_shrink": detect_limit_up_seal_shrink,
    "limit_down_seal_shrink": detect_limit_down_seal_shrink,
    "limit_up_open": detect_limit_up_open,
    "limit_down_open": detect_limit_down_open,
}

PRESET_NAMES = frozenset(PRESET_DETECTORS)

# Human-readable labels for alert cards / CLI.
PRESET_LABELS: dict[str, str] = {
    "limit_up": "涨停",
    "limit_down": "跌停",
    "limit_up_seal_shrink": "涨停大减",
    "limit_down_seal_shrink": "跌停大减",
    "limit_up_open": "涨停打开",
    "limit_down_open": "跌停打开",
}


def get_detector(name: str) -> Detector:
    detector = PRESET_DETECTORS.get(name)
    if detector is None:
        raise KeyError(f"unknown preset detector: {name!r}")
    return detector
