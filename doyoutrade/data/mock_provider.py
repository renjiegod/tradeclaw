from __future__ import annotations

import datetime
import logging
import math
from collections.abc import Iterable
from dataclasses import replace
from decimal import Decimal
from typing import Any, Dict, List, Optional

from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_MOCK, ProviderCapabilities
from doyoutrade.data.providers import InMemoryHistoricalDataProvider
from doyoutrade.core.models import AccountSnapshot, Bar, FillRecord, MarketContext, OrderIntent, PositionSnapshot
from doyoutrade.core.share_math import floor_whole_share_count
from doyoutrade.execution.settlement import SettlementMode, should_run_settlement_trigger_b
from doyoutrade.money.decimal_helpers import decimal_from_number, decimal_to_json_str

logger = logging.getLogger(__name__)


def _decimal_price_map(raw: dict[str, float] | dict[str, Decimal]) -> dict[str, Decimal]:
    return {str(k): decimal_from_number(v) for k, v in raw.items()}


def _fill_quantity_decimal(qty_f: float) -> Decimal:
    """Whole-share fills: avoid ``Decimal(repr(3356.0))`` style trailing zeros skewing notional."""
    if math.isfinite(qty_f) and float(qty_f).is_integer():
        return Decimal(int(qty_f))
    return decimal_from_number(qty_f)


def _position_available_int(pos: PositionSnapshot) -> int:
    if pos.available is None:
        return 0
    return floor_whole_share_count(float(pos.available))


class MockTradingDataProvider:
    """Deterministic in-process quotes, bars, calendar, and in-memory account state.

    Implements :class:`~doyoutrade.data.protocol.TradingDataProvider` for market APIs.
    Account methods exist for :class:`~doyoutrade.account.store_reader.StoreBackedAccountReader`
    to delegate to the same store (they are not part of the market protocol).
    """

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_MOCK,
        # Mock store serves whatever bars the test seeded; declare the
        # superset of intervals other providers offer so it can stand in
        # for any of them in unit tests.
        supported_intervals=frozenset(
            {"1d", "1w", "1mo", "weekly", "monthly", "1m", "5m", "15m", "30m", "60m"}
        ),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=False,
        is_realtime_capable=False,
    )

    _FACTORY_DEFAULT_PRICES: Dict[str, float] = {"600000.SH": 10.0, "601318.SH": 50.0}
    _FACTORY_DEFAULT_CASH: float = 100_000.0

    def __init__(
        self,
        symbol_to_price: dict[str, float] | None = None,
        cash: float = 100_000.0,
        equity: float = 100_000.0,
        positions: List[PositionSnapshot] | None = None,
        bars_by_symbol: Optional[Dict[str, Iterable[Bar]]] = None,
        *,
        ledger_settlement_mode: SettlementMode = "t1",
    ):
        self._symbol_to_price = _decimal_price_map(dict(symbol_to_price or dict(self._FACTORY_DEFAULT_PRICES)))
        self._cash = decimal_from_number(cash)
        self._equity = decimal_from_number(equity)
        self._positions = list(
            positions
            if positions is not None
            else [PositionSnapshot(symbol="600000.SH", quantity=0, cost_price=0, available=0.0)]
        )
        self._bars_store = InMemoryHistoricalDataProvider(
            {sym: list(seq) for sym, seq in (bars_by_symbol or {}).items()}
        )
        self.ledger_settlement_mode: SettlementMode = ledger_settlement_mode
        self._last_settlement_trading_day: str | None = None
        #: Optional A-share fee model (doyoutrade.execution.fees.AShareFeeModel).
        #: ``None`` → no transaction cost, ledger math unchanged (default).
        #: Set at runtime by the worker (mirrors ``ledger_settlement_mode``)
        #: from the task's ``fee_config``.
        self.fee_model: Any = None

    def reset_ledger_to_factory_defaults(self) -> None:
        """Reset cash, positions, and seed marks to :meth:`__init__` defaults.

        Preserves :attr:`_bars_store` so in-process bar history (mock+backtest) is unchanged.
        """
        self._symbol_to_price = _decimal_price_map(dict(self._FACTORY_DEFAULT_PRICES))
        self._cash = decimal_from_number(self._FACTORY_DEFAULT_CASH)
        self._positions = [PositionSnapshot(symbol="600000.SH", quantity=0.0, cost_price=0, available=0.0)]
        self._last_settlement_trading_day = None
        self._mark_equity_from_positions()

    async def get_market_context(self) -> MarketContext:
        with data_span("mock", "get_market_context"):
            return MarketContext(symbol_to_price={k: float(v) for k, v in self._symbol_to_price.items()})

    async def get_account_snapshot(self) -> AccountSnapshot:
        with data_span("mock", "get_account_snapshot"):
            self._mark_equity_from_positions()
            return AccountSnapshot(cash=self._cash, equity=self._equity)

    async def get_positions(self) -> List[PositionSnapshot]:
        with data_span("mock", "get_positions"):
            return list(self._positions)

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> List[Bar]:
        with data_span("mock", "get_bars"):
            del interval, adjust  # in-memory store is not keyed by interval/adjust
            raw = self._bars_store.get_bars(symbol, start_time, end_time)
            return [replace(b, timestamp=normalize_bar_timestamp(b.timestamp)) for b in raw]

    async def is_trading_day(self, date: str) -> bool:
        with data_span("mock", "is_trading_day"):
            d = datetime.date.fromisoformat(date)
            return d.weekday() < 5  # Mon-Fri

    async def get_trading_dates(self, start: str, end: str) -> list[str]:
        with data_span("mock", "get_trading_dates"):
            result: list[str] = []
            d = datetime.date.fromisoformat(start)
            end_d = datetime.date.fromisoformat(end)
            while d <= end_d:
                if d.weekday() < 5:
                    result.append(d.isoformat())
                d += datetime.timedelta(days=1)
            return result

    def _mark_equity_from_positions(self) -> None:
        mtm = self._cash
        for pos in self._positions:
            px_raw = self._symbol_to_price.get(pos.symbol)
            if px_raw is None:
                px = pos.cost_price
            else:
                px = decimal_from_number(px_raw)
            mtm = mtm + decimal_from_number(pos.quantity) * px
        self._equity = mtm

    def get_last_settlement_trading_day(self) -> str | None:
        return self._last_settlement_trading_day

    def settle_all_positions(self) -> int:
        """Unlock sellable quantity for a new trading session (T+1 day boundary)."""
        unlocked = 0
        updated: list[PositionSnapshot] = []
        for pos in self._positions:
            qty_i = floor_whole_share_count(float(pos.quantity))
            if qty_i <= 0:
                updated.append(pos)
                continue
            avail_i = _position_available_int(pos)
            if avail_i < qty_i:
                unlocked += 1
            updated.append(replace(pos, available=float(qty_i)))
        self._positions = updated
        return unlocked

    def apply_settlement_trigger_b(self, current_trading_day: datetime.date) -> bool:
        """Trigger B: first cycle on a new trading day unlocks prior holdings."""
        if self.ledger_settlement_mode != "t1":
            return False
        run_unlock, new_day = should_run_settlement_trigger_b(
            self._last_settlement_trading_day,
            current_trading_day,
        )
        if new_day is not None:
            self._last_settlement_trading_day = new_day
        if run_unlock:
            count = self.settle_all_positions()
            logger.info(
                "mock ledger settlement trigger B trading_day=%s unlocked_positions=%s",
                current_trading_day.isoformat(),
                count,
            )
            return True
        return False

    def apply_synthetic_fill(self, intent: OrderIntent, fill: FillRecord) -> None:
        """Update cash and positions after a simulated fill (paper / backtest)."""
        qty = _fill_quantity_decimal(float(fill.quantity))
        price = decimal_from_number(fill.price)
        if qty <= 0 or price <= 0:
            self._mark_equity_from_positions()
            return
        notional = qty * price
        symbol = str(fill.symbol)
        mode = self.ledger_settlement_mode
        # A-share transaction fee (default-off: fee_model is None → fee=0 →
        # cash math byte-for-byte unchanged). Computed once and written back
        # onto the FillRecord so the worker persists it and the backtest
        # summary can reconcile per-trade PnL against this same cost.
        fee = Decimal("0")
        if self.fee_model is not None:
            # 场内 ETF 卖出免征印花税；股票照常。按 symbol 分类判定。
            from doyoutrade.data.instrument_catalog.a_share_equity import (
                is_cn_a_share_etf_symbol,
            )

            fee = self.fee_model.compute_fee(
                intent.action,
                qty,
                price,
                stamp_tax_exempt=is_cn_a_share_etf_symbol(symbol),
            )
        if intent.action == "buy":
            self._cash -= notional + fee
            self._apply_buy_position(symbol, qty, price, mode=mode)
        elif intent.action == "sell":
            if not self._apply_sell_position(symbol, qty, mode=mode):
                logger.warning(
                    "mock ledger rejected sell fill symbol=%s qty=%s mode=%s",
                    symbol,
                    qty,
                    mode,
                )
                self._mark_equity_from_positions()
                return
            self._cash += notional - fee
        # Surface the fee on the fill (0.0 when no model → no change to
        # downstream persistence vs. the historic default).
        if fee > 0:
            fill.fee = float(fee)
        self._mark_equity_from_positions()

    def _position_index(self, symbol: str) -> int:
        for i, p in enumerate(self._positions):
            if p.symbol == symbol:
                return i
        empty_avail = 0.0 if self.ledger_settlement_mode == "t1" else 0.0
        self._positions.append(
            PositionSnapshot(symbol=symbol, quantity=0.0, cost_price=0, available=empty_avail)
        )
        return len(self._positions) - 1

    def _apply_buy_position(
        self,
        symbol: str,
        qty: Decimal,
        price: Decimal,
        *,
        mode: SettlementMode,
    ) -> None:
        i = self._position_index(symbol)
        cur = self._positions[i]
        old_q = decimal_from_number(cur.quantity)
        new_q = old_q + qty
        if new_q <= 0:
            self._positions[i] = replace(
                cur,
                quantity=0.0,
                cost_price=decimal_from_number(0),
                available=0.0,
            )
            return
        if old_q <= 0:
            new_cost = price
        else:
            new_cost = (old_q * cur.cost_price + qty * price) / new_q
        new_avail = _position_available_int(cur)
        if mode == "t0":
            new_avail = floor_whole_share_count(float(new_q))
        self._positions[i] = replace(
            cur,
            quantity=float(new_q),
            cost_price=new_cost,
            available=float(new_avail),
        )

    def _apply_sell_position(self, symbol: str, qty: Decimal, *, mode: SettlementMode) -> bool:
        i = self._position_index(symbol)
        cur = self._positions[i]
        old_q = decimal_from_number(cur.quantity)
        sell_q = qty
        if mode == "t0":
            if sell_q > old_q:
                return False
            avail = old_q
        else:
            avail = decimal_from_number(_position_available_int(cur))
            if sell_q > avail:
                return False
        new_q = max(Decimal(0), old_q - sell_q)
        new_avail = max(Decimal(0), avail - sell_q)
        if new_q <= 0:
            self._positions[i] = replace(
                cur,
                quantity=0.0,
                cost_price=decimal_from_number(0),
                available=0.0,
            )
        else:
            self._positions[i] = replace(
                cur,
                quantity=float(new_q),
                cost_price=cur.cost_price,
                available=float(floor_whole_share_count(float(new_avail))),
            )
        return True

    def ledger_checkpoint(self) -> dict[str, Any]:
        """Serializable mock ledger state for backtest resume across process restarts."""
        return {
            "symbol_to_price": {str(k): decimal_to_json_str(v) for k, v in self._symbol_to_price.items()},
            "cash": decimal_to_json_str(self._cash),
            "ledger_settlement_mode": self.ledger_settlement_mode,
            "last_settlement_trading_day": self._last_settlement_trading_day,
            "positions": [
                {
                    "symbol": p.symbol,
                    "quantity": float(p.quantity),
                    "cost_price": decimal_to_json_str(p.cost_price),
                    "available": (
                        float(p.available) if p.available is not None else None
                    ),
                }
                for p in self._positions
            ],
        }

    def restore_ledger_checkpoint(self, payload: dict[str, Any] | None) -> None:
        """Restore ledger from :meth:`ledger_checkpoint` output (no-op if *payload* is empty)."""
        if not payload:
            return
        stp = payload.get("symbol_to_price")
        if isinstance(stp, dict) and stp:
            self._symbol_to_price = {str(k): decimal_from_number(v) for k, v in stp.items()}
        if "cash" in payload:
            self._cash = decimal_from_number(payload["cash"])
        mode_raw = payload.get("ledger_settlement_mode")
        if isinstance(mode_raw, str) and mode_raw in ("t0", "t1", "broker"):
            self.ledger_settlement_mode = mode_raw  # type: ignore[assignment]
        last_day = payload.get("last_settlement_trading_day")
        self._last_settlement_trading_day = str(last_day) if last_day else None
        raw_pos = payload.get("positions")
        if isinstance(raw_pos, list):
            restored: list[PositionSnapshot] = []
            for p in raw_pos:
                if not isinstance(p, dict):
                    continue
                qty = float(p.get("quantity", 0.0))
                avail_raw = p.get("available")
                if avail_raw is None and qty > 0 and self.ledger_settlement_mode == "t1":
                    # Legacy checkpoint: treat as unsellable until next trigger B.
                    avail_f: float | None = 0.0
                elif avail_raw is None:
                    avail_f = None
                else:
                    avail_f = float(avail_raw)
                restored.append(
                    PositionSnapshot(
                        symbol=str(p.get("symbol", "")),
                        quantity=qty,
                        cost_price=decimal_from_number(p.get("cost_price", 0)),
                        available=avail_f,
                    )
                )
            self._positions = restored
        self._mark_equity_from_positions()


class StaticUniverseProvider:
    """Universe is exactly the configured symbol list (same idea as QmtUniverseProvider)."""

    def __init__(self, symbols: List[str]):
        self._symbols = list(symbols)

    async def build_universe(self, market_context, account_snapshot, positions, *, cycle_state=None):
        with data_span("mock", "build_universe"):
            return list(self._symbols)
