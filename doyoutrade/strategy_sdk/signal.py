"""Signal — the value returned by :meth:`Strategy.on_bar`.

A signal expresses what the strategy *wants for this symbol after this bar*:

- :meth:`Signal.buy` — want long. ``tag`` mandatory; it identifies which
  factor(s) triggered the decision and is persisted onto
  ``trade_fills.entry_tag`` / debug session events for post-hoc analysis.
- :meth:`Signal.sell` — want flat. ``tag`` mandatory.
- :meth:`Signal.target_exposure` — rebalance toward an explicit post-cycle
  long exposure as a fraction of account equity. ``tag`` mandatory.
- :meth:`Signal.target_quantity` — target an explicit post-cycle share
  inventory. ``tag`` mandatory.
- :meth:`Signal.hold` — no opinion this cycle; the runner preserves the
  current position. ``tag`` optional.

The runner converts these target-state directions into ``OrderIntent`` rows
by diffing against current positions (see :mod:`doyoutrade.strategy_sdk.runner`).

``tag`` is the multi-factor breadcrumb. For composite signals use the
canonical ``"factorA+factorB"`` form (``+`` joined, sorted) so analytical
queries can `GROUP BY entry_tag` meaningfully.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping

from doyoutrade.strategy_sdk.errors import (
    INVALID_ARGUMENT,
    INVALID_EXIT_REASON,
    INVALID_SIGNAL_FRACTION,
    INVALID_TARGET_EXPOSURE,
    INVALID_TARGET_QUANTITY,
    MISSING_SIGNAL_TAG,
    StrategyValidationError,
)


class Direction(str, enum.Enum):
    """Target-state direction expressed by a :class:`Signal`."""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    TARGET_EXPOSURE = "target_exposure"
    TARGET_QUANTITY = "target_quantity"


class ExitReason(str, enum.Enum):
    """Why a position is being exited — orthogonal to ``tag`` (the factor).

    ``tag`` answers *which factor* triggered the decision; ``exit_reason``
    answers *what kind of exit* it is. Optional: a strategy SELL with no
    reason leaves ``Signal.exit_reason = None`` (serializes identically to a
    pre-feature signal). When set it rides signal → intent → fill and powers
    the backtest summary's ``by_exit_reason`` attribution block.

    - ``SIGNAL`` — the strategy's own SELL decision (explicit, when an author
      wants the round-trip attributed as a discretionary signal exit).
    - ``STOP_LOSS`` / ``TAKE_PROFIT`` — strategy-authored protective exits.
    - ``TRAILING_STOP`` / ``ROI`` — reserved for the task-level exit engine
      (set by the worker, not strategy code).
    - ``CIRCUIT_BREAKER`` — reserved for a forced portfolio-protection exit.
    """

    SIGNAL = "signal"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    ROI = "roi"
    CIRCUIT_BREAKER = "circuit_breaker"


def _validate_fraction(value: float) -> float:
    """Validate a partial-exit fraction is in ``(0, 1]``.

    ``1.0`` (the default) means a full exit. Rejects out-of-range values
    (including 0, >1, negative, NaN) rather than clamping — an LLM typo must
    fail at construction time, not silently change the trade size.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        f = float("nan")
    if not (f > 0.0) or f > 1.0 or f != f:  # f != f catches NaN
        raise StrategyValidationError(
            f"Signal.sell() got invalid fraction {value!r}",
            error_code=INVALID_SIGNAL_FRACTION,
            hint="fraction is the portion of the held position to sell; it must be in (0, 1] (1.0 = full exit).",
        )
    return f


def _validate_exit_reason(value: "str | ExitReason | None") -> str | None:
    """Normalize an exit_reason to its canonical string, or ``None``.

    Rejects unknown values with a stable ``error_code`` rather than silently
    coercing — a typo'd reason must fail at construction, not pollute the
    ``by_exit_reason`` attribution downstream (per §错误可见性).
    """
    if value is None:
        return None
    if isinstance(value, ExitReason):
        return value.value
    if isinstance(value, str):
        try:
            return ExitReason(value.strip().lower()).value
        except ValueError:
            pass
    allowed = ", ".join(r.value for r in ExitReason)
    raise StrategyValidationError(
        f"Signal.sell() got unknown exit_reason {value!r}",
        error_code=INVALID_EXIT_REASON,
        hint=f"exit_reason must be one of: {allowed} (or omit it).",
    )


_MAX_TAG_LENGTH = 255


def _validate_tag(tag: str, *, direction: Direction) -> str:
    if direction is Direction.HOLD:
        return tag or ""
    if not isinstance(tag, str) or not tag.strip():
        raise StrategyValidationError(
            f"Signal.{direction.value}() requires a non-empty tag",
            error_code=MISSING_SIGNAL_TAG,
            hint=(
                "Every BUY/SELL signal must carry a factor tag (e.g. "
                'tag="ma_cross+rsi_ok") so trade_fills.entry_tag and debug '
                "events can attribute the decision back to its factors."
            ),
        )
    cleaned = tag.strip()
    if len(cleaned) > _MAX_TAG_LENGTH:
        raise StrategyValidationError(
            f"Signal tag exceeds {_MAX_TAG_LENGTH} chars: {cleaned[:80]!r}...",
            error_code=INVALID_ARGUMENT,
            hint="Tags persist to trade_fills.entry_tag (VARCHAR 255).",
        )
    return cleaned


def _validate_target_exposure(value: float) -> float:
    """Validate a desired long exposure fraction is in ``[0, 1]``.

    ``0.0`` means fully flat; ``1.0`` means fully allocated to the symbol.
    Rejects out-of-range values rather than clamping so a strategy typo does
    not silently change the inventory target.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        f = float("nan")
    if f != f or f < 0.0 or f > 1.0:
        raise StrategyValidationError(
            f"Signal.target_exposure() got invalid target {value!r}",
            error_code=INVALID_TARGET_EXPOSURE,
            hint="target_exposure must be in [0, 1] where 0=flat and 1=fully allocated.",
        )
    return f


def _validate_target_quantity(value: float) -> float:
    """Validate a desired post-cycle share inventory is non-negative."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        f = float("nan")
    if f != f or f < 0.0:
        raise StrategyValidationError(
            f"Signal.target_quantity() got invalid quantity {value!r}",
            error_code=INVALID_TARGET_QUANTITY,
            hint="target_quantity must be >= 0 and represents the desired post-cycle share inventory.",
        )
    return f


@dataclass(frozen=True)
class Signal:
    """One symbol's target-state decision for one bar.

    Construct only via the :meth:`buy` / :meth:`sell` / :meth:`hold` factory
    classmethods — direct construction skips tag validation.
    """

    direction: Direction
    tag: str = ""
    rationale: str = ""
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    #: Optional exit categorization (see :class:`ExitReason`). ``None`` for
    #: BUY/HOLD and for SELL signals that don't categorize the exit — in
    #: which case it is omitted from ``to_dict`` so the wire shape is
    #: byte-identical to a pre-feature signal.
    exit_reason: str | None = None
    #: Portion of the held position to sell on a SELL signal, in ``(0, 1]``.
    #: ``1.0`` (default) = full exit (unchanged behavior). A smaller value
    #: scales the sell quantity (``floor(sellable * fraction)``); omitted from
    #: ``to_dict`` when ``1.0`` so full exits stay byte-identical.
    fraction: float = 1.0
    #: Desired post-cycle long exposure as a fraction of account equity in
    #: ``[0, 1]``. ``None`` means the signal uses the legacy 0/1 target-state
    #: semantics (BUY / SELL / HOLD). When set, PositionManager rebalances the
    #: symbol toward this explicit exposure target.
    target_exposure_value: float | None = None
    #: Desired post-cycle share inventory for the symbol. ``None`` means the
    #: signal does not use strict inventory semantics. When set,
    #: PositionManager compares the current quantity to this target and buys /
    #: sells the delta shares only — no within-band notional rebalancing.
    target_quantity_value: float | None = None

    @classmethod
    def buy(
        cls,
        *,
        tag: str,
        rationale: str = "",
        diagnostics: Mapping[str, Any] | None = None,
    ) -> "Signal":
        """Return a BUY signal (target-state long).

        ``tag`` is mandatory — it records which factor(s) triggered the
        decision. Use ``"+".join(sorted([...]))`` for multi-factor entries
        so the same combination always produces the same tag string.
        """
        return cls(
            direction=Direction.BUY,
            tag=_validate_tag(tag, direction=Direction.BUY),
            rationale=rationale,
            diagnostics=dict(diagnostics) if diagnostics else {},
        )

    @classmethod
    def sell(
        cls,
        *,
        tag: str,
        rationale: str = "",
        diagnostics: Mapping[str, Any] | None = None,
        exit_reason: "str | ExitReason | None" = None,
        fraction: float = 1.0,
    ) -> "Signal":
        """Return a SELL signal (target-state flat). ``tag`` mandatory.

        ``exit_reason`` optionally categorizes *why* the position is exiting
        (see :class:`ExitReason`); it is independent of ``tag`` (the factor)
        and powers the backtest summary's ``by_exit_reason`` block. Unknown
        values are rejected with ``error_code=invalid_exit_reason``.

        ``fraction`` is the portion of the held position to sell, in
        ``(0, 1]`` — ``1.0`` (default) is a full exit; ``0.5`` sells half.
        Out-of-range values raise ``error_code=invalid_signal_fraction``.
        """
        return cls(
            direction=Direction.SELL,
            tag=_validate_tag(tag, direction=Direction.SELL),
            rationale=rationale,
            diagnostics=dict(diagnostics) if diagnostics else {},
            exit_reason=_validate_exit_reason(exit_reason),
            fraction=_validate_fraction(fraction),
        )

    @classmethod
    def hold(
        cls,
        *,
        tag: str = "",
        rationale: str = "",
        diagnostics: Mapping[str, Any] | None = None,
    ) -> "Signal":
        """Return a HOLD signal — no opinion, position unchanged.

        ``tag`` is optional; provide one only if you want to record *why*
        the strategy chose not to act (e.g. "data_insufficient",
        "outside_trading_hours") — useful in debug sessions.
        """
        return cls(
            direction=Direction.HOLD,
            tag=tag,
            rationale=rationale,
            diagnostics=dict(diagnostics) if diagnostics else {},
        )

    @classmethod
    def target_exposure(
        cls,
        *,
        target: float,
        tag: str,
        rationale: str = "",
        diagnostics: Mapping[str, Any] | None = None,
    ) -> "Signal":
        """Return an explicit target long exposure for this symbol.

        ``target`` is the desired post-cycle long exposure as a fraction of
        account equity in ``[0, 1]``. Unlike BUY/SELL, this is declarative:
        PositionManager compares the current exposure to ``target`` and sizes a
        delta order to rebalance toward it.
        """
        return cls(
            direction=Direction.TARGET_EXPOSURE,
            tag=_validate_tag(tag, direction=Direction.TARGET_EXPOSURE),
            rationale=rationale,
            diagnostics=dict(diagnostics) if diagnostics else {},
            target_exposure_value=_validate_target_exposure(target),
        )

    @classmethod
    def target_quantity(
        cls,
        *,
        quantity: float,
        tag: str,
        rationale: str = "",
        diagnostics: Mapping[str, Any] | None = None,
    ) -> "Signal":
        """Return an explicit post-cycle share inventory for this symbol.

        ``quantity`` is the desired total shares held after this cycle. Unlike
        ``target_exposure``, this is share-count based: if the strategy emits
        the same target quantity on the next bar, PositionManager does nothing
        as long as the current share inventory already matches it.
        """
        return cls(
            direction=Direction.TARGET_QUANTITY,
            tag=_validate_tag(tag, direction=Direction.TARGET_QUANTITY),
            rationale=rationale,
            diagnostics=dict(diagnostics) if diagnostics else {},
            target_quantity_value=_validate_target_quantity(quantity),
        )

    @property
    def is_buy(self) -> bool:
        return self.direction is Direction.BUY

    @property
    def is_sell(self) -> bool:
        return self.direction is Direction.SELL

    @property
    def is_hold(self) -> bool:
        return self.direction is Direction.HOLD

    @property
    def is_target_exposure(self) -> bool:
        return self.direction is Direction.TARGET_EXPOSURE

    @property
    def is_target_quantity(self) -> bool:
        return self.direction is Direction.TARGET_QUANTITY

    def to_target_state(self) -> int | None:
        """Project this signal onto the legacy 0/1 target-state semantics.

        - BUY → 1 (long)
        - SELL → 0 (flat)
        - TARGET_EXPOSURE / TARGET_QUANTITY → None (must be handled by
          explicit-target-aware callers such as
          :func:`doyoutrade.strategy_sdk.runner._build_legacy_signals`)
        - HOLD → None (omit from the runner's target-state map so current
          position is preserved)

        Used by :class:`doyoutrade.strategy_sdk.runner.StrategyRunner` to feed
        :class:`PositionManager`, which still consumes ``dict[str, int]``.
        """
        if self.direction is Direction.BUY:
            return 1
        if self.direction is Direction.SELL:
            return 0
        return None

    def to_dict(self) -> dict[str, Any]:
        """Stable JSON-serializable form for persistence / debug events.

        ``exit_reason`` is emitted **only when set** so signals that don't use
        it serialize byte-identically to the pre-feature shape (golden
        cycle_runs / debug-event snapshots stay unchanged).
        """
        out: dict[str, Any] = {
            "direction": self.direction.value,
            "tag": self.tag,
            "rationale": self.rationale,
            "diagnostics": dict(self.diagnostics),
        }
        if self.exit_reason is not None:
            out["exit_reason"] = self.exit_reason
        if self.fraction != 1.0:
            out["fraction"] = self.fraction
        if self.target_exposure_value is not None:
            out["target_exposure"] = self.target_exposure_value
        if self.target_quantity_value is not None:
            out["target_quantity"] = self.target_quantity_value
        return out


__all__ = ["Direction", "ExitReason", "Signal"]
