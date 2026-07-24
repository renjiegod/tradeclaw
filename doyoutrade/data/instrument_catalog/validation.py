"""Catalog membership checks for instance and backtest configuration."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class _CatalogRepo(Protocol):
    async def find_missing_symbols(self, symbols: list[str]) -> list[str]: ...
    async def find_non_tradable_symbols(self, symbols: list[str]) -> list[str]: ...


class CatalogError(ValueError):
    """Base class for instrument-catalog validation failures.

    Catch this (not ``ValueError``) when you want to surface catalog problems
    as a structured 400 instead of a generic bad-request.
    """


class CatalogValidationError(CatalogError):
    """Raised when one or more symbols are not present in ``instrument_catalog``.

    A missing symbol is ambiguous on its own — it can mean "typo'd the code"
    or "this deployment's instrument_catalog was never seeded" (a brand-new
    environment starts with an empty table; see
    ``doyoutrade-cli instruments catalog sync``). ``hint`` disambiguates so
    callers don't have to guess which one it is.
    """

    hint = (
        "symbol not found, or this environment's instrument_catalog is empty/unseeded — "
        "run `doyoutrade-cli instruments catalog sync` (or POST /instruments/catalog/sync) "
        "to populate it, then retry"
    )

    def __init__(self, missing_symbols: list[str]):
        self.missing_symbols = list(missing_symbols)
        super().__init__(
            f"symbols not in instrument catalog: {sorted(missing_symbols)}. {self.hint}",
        )


class CatalogNotTradableError(CatalogError):
    """Raised when ``tradable_only=True`` rejects symbols that exist in the
    catalog but are marked ``is_tradable=False``.

    Indices and other non-tradable instruments live in the catalog for
    watchlist / charting but must not enter order-generating universes
    (live tasks / backtests): 生产稳定性, 指数不可下单.
    """

    def __init__(self, non_tradable_symbols: list[str]):
        self.non_tradable_symbols = list(non_tradable_symbols)
        super().__init__(
            f"symbols are not tradable: {sorted(non_tradable_symbols)}",
        )


async def ensure_symbols_in_catalog(
    catalog_repo: _CatalogRepo | None,
    symbols: list[str],
    *,
    tradable_only: bool = False,
) -> None:
    """Validate ``symbols`` against the instrument catalog.

    Always raises :class:`CatalogValidationError` if any non-empty symbol is
    missing from the catalog. When ``tradable_only=True`` (task / backtest
    universes that feed order generation), additionally raises
    :class:`CatalogNotTradableError` for symbols present in the catalog but
    marked ``is_tradable=False`` (e.g. indices). ``tradable_only`` is
    keyword-only by design — callers must opt into the stricter contract.
    """
    if catalog_repo is None:
        return
    merged = list(dict.fromkeys(s.strip() for s in symbols if s and str(s).strip()))
    if not merged:
        return
    missing = await catalog_repo.find_missing_symbols(merged)
    if missing:
        raise CatalogValidationError(sorted(missing))
    if tradable_only:
        non_tradable = await catalog_repo.find_non_tradable_symbols(merged)
        if non_tradable:
            raise CatalogNotTradableError(sorted(non_tradable))
