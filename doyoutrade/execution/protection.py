"""Portfolio-level protection (circuit breaker) for the trading loop.

Borrowed from freqtrade's ``protections`` plugin and adapted to doyoutrade's
per-cycle worker. The problem it solves: doyoutrade has no portfolio-level
halt — a runaway strategy keeps opening new positions through a deep account
drawdown until an operator manually hits the global kill switch. This adds an
opt-in circuit breaker that stops NEW entries once the account's peak-to-trough
drawdown breaches a threshold, while still allowing exits so existing positions
can be unwound (A-share long-only).

v1 ships the single highest-value guard — **MaxDrawdown** — with the engine
shaped so more guards (stoploss-guard, cooldown) can be added later. The guard
tracks the equity peak across cycles on the (long-lived) worker instance, so it
works the same in a multi-cycle backtest and a long-running live/paper task.

Strictly opt-in: :func:`protection_engine_from_config` returns ``None`` for an
absent / empty / disabled config, so by default there is no new phase behaviour
and existing runs are unchanged. The halt only ever VETOES buys — it never
mutates positions or auto-sells.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from doyoutrade.money.decimal_helpers import decimal_from_number


@dataclass(frozen=True)
class ProtectionConfig:
    """Thresholds for the portfolio circuit breaker. ``None`` field = that
    guard is off."""

    #: Peak-to-trough account drawdown fraction that halts new entries, e.g.
    #: ``0.2`` for 20%. ``None`` disables the max-drawdown guard.
    max_drawdown_pct: float | None = None


@dataclass(frozen=True)
class ProtectionDecision:
    """Outcome of one ``ProtectionEngine.evaluate`` call."""

    halted: bool
    reason: str | None = None
    peak_equity: Decimal | None = None
    current_equity: Decimal | None = None
    drawdown_pct: float | None = None


def protection_config_from_config(cfg: Any) -> ProtectionConfig | None:
    """Build a :class:`ProtectionConfig` from a dict, or ``None`` when off.

    Returns ``None`` (no protection, loop unchanged) when ``cfg`` is not a
    non-empty dict, carries ``{"enabled": false}``, or declares no usable
    guard threshold. Otherwise validates the thresholds and returns the config.
    """

    if not isinstance(cfg, dict) or not cfg:
        return None
    if cfg.get("enabled") is False:
        return None

    mdd_raw = cfg.get("max_drawdown_pct")
    max_drawdown_pct: float | None = None
    if mdd_raw is not None:
        if isinstance(mdd_raw, bool) or not isinstance(mdd_raw, (int, float)):
            raise ValueError(
                f"protection.max_drawdown_pct must be a number in (0, 1), got {mdd_raw!r}"
            )
        v = float(mdd_raw)
        if not (0.0 < v < 1.0):
            raise ValueError(
                f"protection.max_drawdown_pct must be in (0, 1), got {v}"
            )
        max_drawdown_pct = v

    if max_drawdown_pct is None:
        # Config present but no usable guard → treat as off (visible: caller
        # gets None, not a silently-inert engine).
        return None
    return ProtectionConfig(max_drawdown_pct=max_drawdown_pct)


class ProtectionEngine:
    """Stateful portfolio circuit breaker (one instance per worker / run).

    Holds the running equity peak across cycles. ``evaluate`` is pure w.r.t.
    its inputs except for updating that peak; it never raises on normal input.
    """

    def __init__(self, config: ProtectionConfig) -> None:
        self.config = config
        self._peak_equity: Decimal | None = None

    def evaluate(self, current_equity: Any) -> ProtectionDecision:
        eq = decimal_from_number(current_equity)
        if self._peak_equity is None or eq > self._peak_equity:
            self._peak_equity = eq
        peak = self._peak_equity

        drawdown_pct: float | None = None
        if peak is not None and peak > 0:
            drawdown_pct = float((peak - eq) / peak)

        if (
            self.config.max_drawdown_pct is not None
            and drawdown_pct is not None
            and drawdown_pct > self.config.max_drawdown_pct
        ):
            return ProtectionDecision(
                halted=True,
                reason="max_drawdown_exceeded",
                peak_equity=peak,
                current_equity=eq,
                drawdown_pct=drawdown_pct,
            )
        return ProtectionDecision(
            halted=False,
            peak_equity=peak,
            current_equity=eq,
            drawdown_pct=drawdown_pct,
        )


def protection_engine_from_config(cfg: Any) -> ProtectionEngine | None:
    """Convenience: config dict → :class:`ProtectionEngine` or ``None`` (off)."""

    config = protection_config_from_config(cfg)
    return ProtectionEngine(config) if config is not None else None


__all__ = [
    "ProtectionConfig",
    "ProtectionDecision",
    "ProtectionEngine",
    "protection_config_from_config",
    "protection_engine_from_config",
]
