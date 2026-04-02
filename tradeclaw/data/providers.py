from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Callable, Dict, Iterable, List, Optional

from tradeclaw.domain.models import Bar, Quote


class InMemoryHistoricalDataProvider:
    def __init__(self, bars_by_symbol: Optional[Dict[str, Iterable[Bar]]] = None):
        self._bars_by_symbol: Dict[str, List[Bar]] = {}
        for symbol, bars in (bars_by_symbol or {}).items():
            self._bars_by_symbol[symbol] = sorted(list(bars), key=lambda item: item.timestamp)

    def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        as_of_time: Optional[str] = None,
    ) -> List[Bar]:
        source = self._bars_by_symbol.get(symbol, [])
        result: List[Bar] = []
        for bar in source:
            if bar.timestamp < start_time or bar.timestamp > end_time:
                continue
            if as_of_time is not None and bar.timestamp > as_of_time:
                continue
            result.append(bar)
        return result


class MarketDataNormalizer:
    def __init__(self, default_market: str = "SH"):
        self.default_market = default_market.upper()

    def normalize_symbol(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        if "." in normalized:
            return normalized
        return f"{normalized}.{self.default_market}"

    def normalize_bar(self, bar: Bar) -> Bar:
        return replace(bar, symbol=self.normalize_symbol(bar.symbol))


class RealtimeMarketFeed:
    def __init__(self):
        self._subscribers: Dict[str, List[Callable[[Quote], None]]] = defaultdict(list)

    def subscribe(self, symbol: str, callback: Callable[[Quote], None]):
        self._subscribers[symbol].append(callback)

    def unsubscribe(self, symbol: str, callback: Callable[[Quote], None]):
        callbacks = self._subscribers.get(symbol, [])
        self._subscribers[symbol] = [item for item in callbacks if item != callback]

    def publish_quote(self, symbol: str, price: float, timestamp: str):
        quote = Quote(symbol=symbol, price=price, timestamp=timestamp)
        for callback in self._subscribers.get(symbol, []):
            callback(quote)
