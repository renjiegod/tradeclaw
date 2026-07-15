"""WatchlistSnapshot — an immutable, IO-free view of the watchlist for strategies.

This is the data structure backing ``ctx.dp.watchlist_symbols(tag=...)``. It is
deliberately:

- **Frozen / immutable** — a strategy sandbox can hold a reference for the whole
  cycle without any risk of it mutating mid-evaluation, and the same snapshot
  yields the same answer for every symbol in the cycle (per-cycle determinism).
- **Zero IO** — built once at worker assembly time from
  ``WatchlistRepository.snapshot()`` (Phase B wiring), then passed down into the
  per-symbol :class:`~doyoutrade.strategy_sdk.data_provider.DataProvider`. No live
  DB read happens inside strategy code, so it never breaks the prefetch contract.

The internal representation is a ``symbol → tags`` mapping. ``all_symbols`` and
the tag-filtered views return results in a **stable order** (symbol insertion
order, tags as declared) so backtests are reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class WatchlistSnapshot:
    """Frozen ``symbol → tags`` view of the watchlist for one cycle.

    Construct via :meth:`from_mapping`; the dataclass field holds a read-only
    mapping so the snapshot stays immutable even though it lives behind a
    ``ctx.dp`` reference inside strategy code.
    """

    # symbol → (tag, ...). MappingProxyType keeps it read-only; the dataclass is
    # frozen so the field itself cannot be rebound.
    _by_symbol: Mapping[str, tuple[str, ...]]

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, list[str]]) -> "WatchlistSnapshot":
        """Build a snapshot from a ``{symbol: [tag, ...]}`` mapping.

        Symbol insertion order is preserved for stable iteration. Tags are
        copied into tuples (deduplicated while preserving first-seen order) so
        callers cannot mutate the snapshot after construction.
        """
        if not isinstance(mapping, Mapping):
            raise TypeError(
                f"WatchlistSnapshot.from_mapping expects a mapping, got "
                f"{type(mapping).__name__}: {mapping!r}"
            )
        frozen: dict[str, tuple[str, ...]] = {}
        for symbol, tags in mapping.items():
            if not isinstance(symbol, str):
                raise TypeError(
                    f"watchlist symbol must be str, got {type(symbol).__name__}: {symbol!r}"
                )
            if not isinstance(tags, (list, tuple)):
                raise TypeError(
                    f"watchlist tags for {symbol!r} must be a list/tuple, got "
                    f"{type(tags).__name__}: {tags!r}"
                )
            seen: list[str] = []
            for tag in tags:
                if not isinstance(tag, str):
                    raise TypeError(
                        f"watchlist tag for {symbol!r} must be str, got "
                        f"{type(tag).__name__}: {tag!r}"
                    )
                if tag not in seen:
                    seen.append(tag)
            frozen[symbol] = tuple(seen)
        return cls(_by_symbol=MappingProxyType(frozen))

    def all_symbols(self) -> tuple[str, ...]:
        """Return every watchlist symbol in stable (insertion) order."""
        return tuple(self._by_symbol.keys())

    def symbols_for_tag(self, tag: str) -> tuple[str, ...]:
        """Return symbols carrying ``tag``, in stable (insertion) order."""
        return tuple(
            symbol for symbol, tags in self._by_symbol.items() if tag in tags
        )

    def tags_for(self, symbol: str) -> tuple[str, ...]:
        """Return the tags attached to ``symbol`` (empty tuple if absent)."""
        return self._by_symbol.get(symbol, ())


__all__ = ["WatchlistSnapshot"]
