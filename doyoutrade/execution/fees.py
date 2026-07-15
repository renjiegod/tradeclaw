"""A-share transaction-fee model (佣金 / 印花税 / 过户费).

Borrowed from freqtrade's fee-aware backtest and TurtleTrace's 做T fee
calculator, adapted to A-share conventions. The problem it solves: doyoutrade's
backtest previously recorded fills at the raw execution price with **zero**
transaction cost, so equity curves and realized PnL were optimistically
inflated — short-holding / high-turnover strategies look profitable in
backtest but lose money live. Baseline survey flagged this as the #1 gap.

A-share retail cost structure (defaults below, all configurable):

* **佣金 (commission)** — charged on BOTH buy and sell, ``commission_rate``
  of notional, floored at ``min_commission`` (5 元 at most brokers).
* **印花税 (stamp tax)** — charged on the SELL leg only, ``stamp_tax_rate``
  of notional (0.05% since 2023-08).
* **过户费 (transfer fee)** — charged on BOTH legs, ``transfer_fee_rate`` of
  notional (0.001%; historically 沪市-only, now both exchanges).

Strictly opt-in: there is no fee unless a ``fee_config`` is supplied.
:func:`fee_model_from_config` returns ``None`` for an absent / empty config so
the backtest ledger and FIFO PnL stay byte-for-byte unchanged by default
(preserving every existing golden-number test).

All math is :class:`~decimal.Decimal` (via
:mod:`doyoutrade.money.decimal_helpers`) to avoid IEEE-754 dust; the per-fill
fee is quantized to 0.01 元 (ROUND_HALF_UP), matching broker statements.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from doyoutrade.money.decimal_helpers import decimal_from_number

# A-share retail defaults (2024). Rates are fractions of notional.
_DEFAULT_COMMISSION_RATE = Decimal("0.00025")  # 万2.5
_DEFAULT_MIN_COMMISSION = Decimal("5")  # 元
_DEFAULT_STAMP_TAX_RATE = Decimal("0.0005")  # 0.05%, sell only
_DEFAULT_TRANSFER_FEE_RATE = Decimal("0.00001")  # 0.001%
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class AShareFeeModel:
    """Frozen, deterministic A-share per-fill fee calculator.

    Construct via :func:`fee_model_from_config` so callers get the
    None-when-off contract; the dataclass itself is always "on".
    """

    commission_rate: Decimal = _DEFAULT_COMMISSION_RATE
    min_commission: Decimal = _DEFAULT_MIN_COMMISSION
    stamp_tax_rate: Decimal = _DEFAULT_STAMP_TAX_RATE
    transfer_fee_rate: Decimal = _DEFAULT_TRANSFER_FEE_RATE

    def compute_fee(self, side: str, quantity: Any, price: Any) -> Decimal:
        """Total fee (元, quantized to 0.01) for one fill.

        ``side`` is ``"buy"`` or ``"sell"``; 印花税 applies to sells only.
        Zero / non-positive notional yields ``Decimal("0")`` (no fee, and no
        minimum-commission floor — a no-op fill is not a trade).
        """

        qty = decimal_from_number(quantity)
        px = decimal_from_number(price)
        notional = qty * px
        if notional <= 0:
            return Decimal("0")

        commission = notional * self.commission_rate
        if commission < self.min_commission:
            commission = self.min_commission
        transfer = notional * self.transfer_fee_rate
        stamp = notional * self.stamp_tax_rate if str(side).lower() == "sell" else Decimal("0")

        total = commission + transfer + stamp
        return total.quantize(_CENT, rounding=ROUND_HALF_UP)

    def to_dict(self) -> dict[str, str]:
        """Serialize the rates (for debug events / report provenance)."""

        return {
            "commission_rate": str(self.commission_rate),
            "min_commission": str(self.min_commission),
            "stamp_tax_rate": str(self.stamp_tax_rate),
            "transfer_fee_rate": str(self.transfer_fee_rate),
        }


def _rate(cfg: dict[str, Any], key: str, default: Decimal) -> Decimal:
    if key not in cfg or cfg[key] is None:
        return default
    value = decimal_from_number(cfg[key])
    if value < 0:
        raise ValueError(f"fee_config.{key} must be >= 0, got {cfg[key]!r}")
    return value


def fee_model_from_config(cfg: Any) -> AShareFeeModel | None:
    """Build an :class:`AShareFeeModel` from a config dict, or ``None`` when off.

    Returns ``None`` (fees disabled, ledger/PnL unchanged) when:

    * ``cfg`` is ``None`` or not a dict;
    * ``cfg`` is an empty dict;
    * ``cfg`` carries an explicit ``{"enabled": false}``.

    Otherwise the provided rate keys override the A-share defaults. Unknown
    keys are ignored on purpose — fee config is operator-facing and additive.
    """

    if not isinstance(cfg, dict) or not cfg:
        return None
    if cfg.get("enabled") is False:
        return None
    return AShareFeeModel(
        commission_rate=_rate(cfg, "commission_rate", _DEFAULT_COMMISSION_RATE),
        min_commission=_rate(cfg, "min_commission", _DEFAULT_MIN_COMMISSION),
        stamp_tax_rate=_rate(cfg, "stamp_tax_rate", _DEFAULT_STAMP_TAX_RATE),
        transfer_fee_rate=_rate(cfg, "transfer_fee_rate", _DEFAULT_TRANSFER_FEE_RATE),
    )


__all__ = ["AShareFeeModel", "fee_model_from_config"]
