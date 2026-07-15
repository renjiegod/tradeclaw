from __future__ import annotations

from dataclasses import asdict
from typing import Any

from doyoutrade.core.models import Bar, MarketContext
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST


class PatchedDataProvider:
    def __init__(self, inner: Any, input_overrides: dict[str, Any] | None):
        self._inner = inner
        self._input_overrides = dict(input_overrides or {})

    async def get_market_context(self):
        market = await self._inner.get_market_context()
        prices = dict(getattr(market, "symbol_to_price", {}) or {})
        ticks = dict(getattr(market, "symbol_to_tick", {}) or {})
        raw_prices = self._input_overrides.get("market_prices")
        if isinstance(raw_prices, dict):
            for symbol, value in raw_prices.items():
                try:
                    prices[str(symbol)] = float(value)
                except (TypeError, ValueError):
                    continue
        raw_ticks = self._input_overrides.get("ticks")
        if isinstance(raw_ticks, dict):
            for symbol, value in raw_ticks.items():
                if isinstance(value, dict):
                    ticks[str(symbol)] = dict(value)
        return MarketContext(symbol_to_price=prices, symbol_to_tick=ticks)

    async def get_bars(self, symbol: str, start_time: str, end_time: str, *, interval: str = "1d", adjust: str = DEFAULT_BAR_ADJUST):
        override = _match_bar_override(
            self._input_overrides.get("bars_requests"),
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
            interval=interval,
        )
        if override is not None:
            return override
        return await self._inner.get_bars(symbol, start_time, end_time, interval=interval, adjust=adjust)

    async def is_trading_day(self, date: str) -> bool:
        return await self._inner.is_trading_day(date)

    async def get_trading_dates(self, start: str, end: str) -> list[str]:
        return await self._inner.get_trading_dates(start, end)


class OverriddenUniverseProvider:
    def __init__(self, inner: Any, input_overrides: dict[str, Any] | None):
        self._inner = inner
        self._input_overrides = dict(input_overrides or {})

    async def build_universe(self, market_context, account_snapshot, positions, *, cycle_state=None):
        raw = self._input_overrides.get("universe")
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        return await self._inner.build_universe(
            market_context,
            account_snapshot,
            positions,
            cycle_state=cycle_state,
        )


def bar_to_dict(bar: Any) -> dict[str, Any]:
    if hasattr(bar, "__dataclass_fields__"):
        return asdict(bar)
    if isinstance(bar, dict):
        return dict(bar)
    return {"value": str(bar)}


def _match_bar_override(raw_items: Any, *, symbol: str, start_time: str, end_time: str, interval: str) -> list[Bar] | None:
    if not isinstance(raw_items, list):
        return None
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("symbol", "")).strip() != symbol:
            continue
        if str(item.get("start_time", "")).strip() != start_time:
            continue
        if str(item.get("end_time", "")).strip() != end_time:
            continue
        item_interval = str(item.get("interval", "1d")).strip() or "1d"
        if item_interval != interval:
            continue
        raw_bars = item.get("bars")
        if not isinstance(raw_bars, list):
            return []
        bars: list[Bar] = []
        for raw_bar in raw_bars:
            if not isinstance(raw_bar, dict):
                continue
            try:
                amount_raw = raw_bar.get("amount")
                amount: float | None
                try:
                    amount = float(amount_raw) if amount_raw is not None else None
                except (TypeError, ValueError):
                    amount = None
                bars.append(
                    Bar(
                        symbol=str(raw_bar["symbol"]),
                        timestamp=str(raw_bar["timestamp"]),
                        open=float(raw_bar["open"]),
                        high=float(raw_bar["high"]),
                        low=float(raw_bar["low"]),
                        close=float(raw_bar["close"]),
                        volume=float(raw_bar["volume"]),
                        amount=amount,
                        adjust_type=str(raw_bar.get("adjust_type", DEFAULT_BAR_ADJUST)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return bars
    return None
