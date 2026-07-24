from __future__ import annotations

from collections.abc import Callable
from typing import Any

from doyoutrade.account import QmtAccountReader, StoreBackedAccountReader
from doyoutrade.config import DataSettings
from doyoutrade.data.account_resolution import ResolvedAccount
from doyoutrade.data.akshare_provider import AkshareDataProvider
from doyoutrade.data.baostock_provider import BaostockDataProvider
from doyoutrade.data.fallback_provider import FallbackHistoricalDataProvider
from doyoutrade.data.mock_provider import MockTradingDataProvider, StaticUniverseProvider
from doyoutrade.data.qmt_proxy import QmtLiveDataProvider
from doyoutrade.core.models import PositionSnapshot
from doyoutrade.infra import create_qmt_proxy_rest_client

# Built-in provider ids (also used in config / API).
PROVIDER_AUTO = "auto"
PROVIDER_MOCK = "mock"
PROVIDER_QMT = "qmt"
PROVIDER_AKSHARE = "akshare"
PROVIDER_BAOSTOCK = "baostock"
PROVIDER_TUSHARE = "tushare"
PROVIDER_MOOTDX = "mootdx"

# Priority for ``provider_id == "auto"`` selection. QMT stays first WHEN a
# connected account supplies it (source of truth for the live broker account);
# when QMT is absent / banned the chain naturally leads with baostock
# (authoritative exchange calendar + reliable qfq daily history), then mootdx
# (通达信 socket: minute bars + same-day intraday + self-computed qfq, verified
# to match baostock within ~0.08% on dividend/split names), then akshare
# (token-free scrape fallback), then tushare (high-quality, skipped without a
# token). The chain is filtered per-interval in :func:`_resolve_auto_chain` /
# ``FallbackHistoricalDataProvider``, so a minute-bar request skips baostock
# (no 1m) and lands on mootdx's socket feed ahead of akshare's fragile scrape.
_AUTO_PRIORITY: tuple[str, ...] = (
    PROVIDER_QMT,
    PROVIDER_BAOSTOCK,
    PROVIDER_MOOTDX,
    PROVIDER_AKSHARE,
    PROVIDER_TUSHARE,
)

_CUSTOM_BUILDERS: dict[str, Callable[[DataSettings, list[str]], tuple[Any, Any, Any]]] = {}


def list_data_provider_ids() -> list[str]:
    """Return built-in provider ids plus any registered custom ids (sorted after core).

    Used by HTTP API for console dropdowns. Order: auto, mock, qmt, akshare, tushare, baostock, mootdx, then custom A–Z.
    """
    core = [
        PROVIDER_AUTO,
        PROVIDER_MOCK,
        PROVIDER_QMT,
        PROVIDER_AKSHARE,
        PROVIDER_TUSHARE,
        PROVIDER_BAOSTOCK,
        PROVIDER_MOOTDX,
    ]
    custom = sorted(_CUSTOM_BUILDERS.keys())
    return [*core, *custom]


def register_trading_data_provider(
    name: str,
    builder: Callable[[DataSettings, list[str]], tuple[Any, Any, Any]],
) -> None:
    """Register an extra channel; `name` must be lowercase and not reserved.

    *builder* must return ``(data_provider, universe_provider, account_reader)`` aligned with
    built-in stacks.
    """
    key = name.strip().lower()
    if key in (
        PROVIDER_AUTO,
        PROVIDER_MOCK,
        PROVIDER_QMT,
        PROVIDER_AKSHARE,
        PROVIDER_BAOSTOCK,
        PROVIDER_TUSHARE,
        PROVIDER_MOOTDX,
        "demo",
    ):
        raise ValueError(f"reserved data provider id: {key}")
    if not key:
        raise ValueError("data provider name must be non-empty")
    _CUSTOM_BUILDERS[key] = builder


def normalize_provider_id(value: str | None) -> str:
    if value is None:
        return PROVIDER_AUTO
    key = str(value).strip().lower()
    if key == "demo":
        return PROVIDER_MOCK
    return key or PROVIDER_AUTO


def resolve_effective_provider(requested: str | None, global_default: str) -> str:
    return normalize_provider_id(requested or global_default)


def _qmt_configured(account: ResolvedAccount | None) -> bool:
    """QMT is reachable when a resolved account supplies a proxy base_url.

    With accounts persisted in the DB (no more ``config.data.qmt``), the
    market-data connection comes from the default account; ``account`` is the
    resolved default (or task) account, or ``None`` when no account exists.
    """
    return bool(account is not None and account.has_connection)


def _tushare_configured(data_cfg: DataSettings) -> bool:
    """Tushare is reachable when an API token is configured (env or YAML).

    ``TushareSettings`` is added by the Tushare provider PR; until that
    lands this helper looks for the optional ``tushare`` attribute and
    returns False when absent. Keeping the check defensive avoids
    forcing every test fixture to construct a ``DataSettings`` with a
    full Tushare block when none of the tests touch Tushare.
    """
    tushare = getattr(data_cfg, "tushare", None)
    if tushare is None:
        return False
    token = getattr(tushare, "token", None)
    return bool(token and str(token).strip())


def _resolve_auto_chain(
    data_cfg: DataSettings,
    account: ResolvedAccount | None,
    *,
    source_priority: tuple[str, ...] | None = None,
) -> list[str]:
    """Return the ordered provider ids for ``data_source=auto`` after dropping unconfigured auth providers.

    The chain is *not* filtered by interval here — that decision is
    deferred to runtime inside
    :class:`doyoutrade.data.fallback_provider.FallbackHistoricalDataProvider`,
    which inspects each provider's ``capabilities.supported_intervals``
    per call and skips with a debug event. That keeps the factory
    pure (no interval guesses) and lets per-symbol calls degrade
    gracefully when the requested interval doesn't survive the chain.

    ``source_priority`` (from the task's ``data_cache.source_priority``)
    overrides the default :data:`_AUTO_PRIORITY` order when supplied — already
    validated against the known provider set by
    :func:`doyoutrade.data.cache_policy.parse_data_cache_policy`. The same
    auth-config filtering (QMT needs a connection, tushare needs a token) is
    applied to whichever order is used, so an explicit priority never resurrects
    an unconfigured provider.
    """
    order = source_priority if source_priority else _AUTO_PRIORITY
    chain: list[str] = []
    for name in order:
        if name == PROVIDER_QMT and not _qmt_configured(account):
            continue
        if name == PROVIDER_TUSHARE and not _tushare_configured(data_cfg):
            continue
        chain.append(name)
    if not chain:
        # Nothing configured (no QMT URL, no Tushare token). Surface the
        # mock provider so callers don't get a None data_provider — the
        # mock answers with deterministic in-process bars + empty market
        # context, which is the legacy behaviour for unconfigured envs.
        chain = [PROVIDER_MOCK]
    return chain


def _qmt_mock_portfolio(account: ResolvedAccount) -> MockTradingDataProvider:
    positions = [
        PositionSnapshot(symbol=p.symbol, quantity=p.quantity, cost_price=p.cost_price)
        for p in account.mock_positions
    ]
    return MockTradingDataProvider(
        cash=account.mock_cash, equity=account.mock_equity, positions=positions
    )


def _build_qmt_stack(
    account: ResolvedAccount | None,
    symbols: list[str],
    session_persist=None,
) -> tuple[Any, Any, Any]:
    if not _qmt_configured(account):
        raise ValueError(
            "data provider 'qmt' requires an account with base_url; "
            "create a default account (or bind one to the task), "
            "or use provider 'mock' / 'auto'."
        )
    assert account is not None  # _qmt_configured guarantees this
    # ``account.mode`` carries live/mock (each account is one or the other).
    # mock => StoreBackedAccountReader over the account's mock portfolio and
    # no live trading-terminal session.
    client = create_qmt_proxy_rest_client(account, session_persist=session_persist)
    dp = QmtLiveDataProvider(client=client, symbols=symbols)
    if account.mode == "mock":
        mock_pf = _qmt_mock_portfolio(account)
        return dp, StaticUniverseProvider(symbols), StoreBackedAccountReader(mock_pf)
    return dp, StaticUniverseProvider(symbols), QmtAccountReader(client)


def _build_mock_stack(_data_cfg: DataSettings, symbols: list[str]) -> tuple[Any, Any, Any]:
    store = MockTradingDataProvider()
    return store, StaticUniverseProvider(symbols), StoreBackedAccountReader(store)


def _build_akshare_stack(_data_cfg: DataSettings, symbols: list[str]) -> tuple[Any, Any, Any]:
    mock_pf = MockTradingDataProvider()
    return (
        AkshareDataProvider(symbols=symbols),
        StaticUniverseProvider(symbols),
        StoreBackedAccountReader(mock_pf),
    )


def _build_baostock_stack(_data_cfg: DataSettings, symbols: list[str]) -> tuple[Any, Any, Any]:
    mock_pf = MockTradingDataProvider()
    return (
        BaostockDataProvider(symbols=symbols),
        StaticUniverseProvider(symbols),
        StoreBackedAccountReader(mock_pf),
    )


def _build_mootdx_stack(_data_cfg: DataSettings, symbols: list[str]) -> tuple[Any, Any, Any]:
    """Build a mootdx-backed stack. Imports lazily so the dep stays optional."""
    from doyoutrade.data.mootdx_provider import MootdxDataProvider

    mock_pf = MockTradingDataProvider()
    return (
        MootdxDataProvider(symbols=symbols),
        StaticUniverseProvider(symbols),
        StoreBackedAccountReader(mock_pf),
    )


def _build_tushare_stack(data_cfg: DataSettings, symbols: list[str]) -> tuple[Any, Any, Any]:
    """Build a Tushare-backed stack. Imports lazily so the dep stays optional."""
    if not _tushare_configured(data_cfg):
        raise ValueError(
            "data provider 'tushare' requires data.tushare.token; "
            "set it in config or via TUSHARE_TOKEN env var."
        )
    from doyoutrade.data.tushare_provider import TushareDataProvider

    token = data_cfg.tushare.token  # type: ignore[attr-defined]
    url = data_cfg.tushare.url  # type: ignore[attr-defined]
    mock_pf = MockTradingDataProvider()
    return (
        TushareDataProvider(symbols=symbols, token=str(token), url=url),
        StaticUniverseProvider(symbols),
        StoreBackedAccountReader(mock_pf),
    )


def _build_single_provider_stack(
    name: str,
    data_cfg: DataSettings,
    symbols: list[str],
    account: ResolvedAccount | None = None,
    session_persist=None,
) -> tuple[Any, Any, Any]:
    """Dispatch a single named provider (no auto / chain logic)."""
    if name == PROVIDER_MOCK:
        return _build_mock_stack(data_cfg, symbols)
    if name == PROVIDER_QMT:
        return _build_qmt_stack(account, symbols, session_persist=session_persist)
    if name == PROVIDER_AKSHARE:
        return _build_akshare_stack(data_cfg, symbols)
    if name == PROVIDER_BAOSTOCK:
        return _build_baostock_stack(data_cfg, symbols)
    if name == PROVIDER_TUSHARE:
        return _build_tushare_stack(data_cfg, symbols)
    if name == PROVIDER_MOOTDX:
        return _build_mootdx_stack(data_cfg, symbols)
    builder = _CUSTOM_BUILDERS.get(name)
    if builder is None:
        raise ValueError(
            f"unknown data provider {name!r}; expected one of "
            f"auto, mock, qmt, akshare, tushare, baostock, mootdx, or a registered provider."
        )
    return builder(data_cfg, symbols)


def build_trading_data_stack(
    provider_id: str,
    data_cfg: DataSettings,
    symbols: list[str],
    *,
    account: ResolvedAccount | None = None,
    session_persist=None,
    source_priority: tuple[str, ...] | None = None,
) -> tuple[Any, Any, Any]:
    """
    Returns ``(data_provider, universe_provider, account_reader)`` for a worker.

    `provider_id`: auto | mock | qmt | akshare | tushare | baostock | mootdx | a name registered via register_trading_data_provider.

    For ``auto``, the resolved order is :data:`_AUTO_PRIORITY` filtered by
    config (QMT requires ``data.qmt.base_url``; Tushare requires
    ``data.tushare.token``). When more than one provider survives the
    filter, the returned ``data_provider`` is a
    :class:`FallbackHistoricalDataProvider` that fires
    ``market_data_provider_skipped`` debug events on each miss so the
    cycle's debug session shows which source actually answered.

    ``universe_provider`` and ``account_reader`` always come from the
    primary (first chain entry) — universes don't generally roll over
    between providers, and broker accounts are inherently
    provider-specific.

    ``account`` (optional :class:`ResolvedAccount`) supplies the QMT proxy
    connection (base_url/token) and the live/mock trading identity. ``None``
    (no account configured) means QMT is unavailable: the ``auto`` chain drops
    QMT and falls back to akshare/baostock, and an explicit ``qmt`` provider
    raises. ``session_persist`` is an async ``(account_id, session_id)``
    callback used to write a refreshed trading session id back to the account
    row (only the live qmt path fires it).
    """
    sym = list(symbols)
    key = normalize_provider_id(provider_id)

    if key != PROVIDER_AUTO:
        return _build_single_provider_stack(
            key, data_cfg, sym, account=account, session_persist=session_persist
        )

    chain = _resolve_auto_chain(data_cfg, account, source_priority=source_priority)
    primary_dp, universe, account_reader = _build_single_provider_stack(
        chain[0], data_cfg, sym, account=account, session_persist=session_persist
    )
    if len(chain) == 1:
        return primary_dp, universe, account_reader

    fallback_dps: list[Any] = [primary_dp]
    for name in chain[1:]:
        try:
            secondary_dp, _u, _a = _build_single_provider_stack(name, data_cfg, sym)
        except Exception as exc:
            # Construction failure for a secondary (e.g. missing
            # akshare dep, baostock login failure) is non-fatal — the
            # primary still works. Log + continue so the chain stays
            # usable; per CLAUDE.md the warning carries the provider id
            # and exception type so an operator can see *which*
            # backup dropped out without grepping the cycle log.
            import logging
            logging.getLogger(__name__).warning(
                "auto-chain: %s provider construction skipped due to %s: %s",
                name, type(exc).__name__, exc,
            )
            continue
        fallback_dps.append(secondary_dp)
    if len(fallback_dps) == 1:
        return primary_dp, universe, account_reader
    return FallbackHistoricalDataProvider(fallback_dps), universe, account_reader


# ---------------------------------------------------------------------------
# Sector axis (board / industry / concept membership)
# ---------------------------------------------------------------------------

# Sector providers supported by ``data_source`` on the sector axis. Distinct
# from the OHLCV set: tushare / baostock have no sector membership endpoint.
_SECTOR_SOURCES: tuple[str, ...] = (PROVIDER_AKSHARE, PROVIDER_QMT)


class _FallbackSectorProvider:
    """Try each sector provider in order; surface the last error if all fail.

    Mirrors :class:`FallbackHistoricalDataProvider` for the sector axis. A
    provider that raises is logged + skipped (not silently swallowed) and the
    next one is tried; if every provider raises, the last exception
    propagates so the ``data_sector`` tool reports ``sector_fetch_failed``.
    """

    def __init__(self, providers: list[Any]) -> None:
        if not providers:
            raise ValueError("_FallbackSectorProvider needs at least one provider")
        self._providers = providers
        # Expose the primary provider's capabilities for the SectorProvider
        # Protocol's ``capabilities`` attribute.
        self.capabilities = providers[0].capabilities

    async def list_sectors(self, *, sector_type: str | None = None) -> list[Any]:
        return await self._first_ok("list_sectors", sector_type=sector_type)

    async def get_sector_members(
        self, sector_name: str, *, sector_type: str | None = None
    ) -> list[Any]:
        return await self._first_ok(
            "get_sector_members", sector_name, sector_type=sector_type
        )

    async def _first_ok(self, method: str, *args: Any, **kwargs: Any) -> Any:
        import logging

        logger = logging.getLogger(__name__)
        last_exc: Exception | None = None
        for provider in self._providers:
            source = getattr(provider.capabilities, "name", type(provider).__name__)
            try:
                return await getattr(provider, method)(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — surfaced after the chain
                last_exc = exc
                logger.warning(
                    "sector auto-chain: %s.%s failed (%s: %s); trying next",
                    source, method, type(exc).__name__, exc,
                )
                await _emit_sector_fallback(source, method, exc)
        assert last_exc is not None
        raise last_exc


async def _emit_sector_fallback(source: str, method: str, exc: Exception) -> None:
    try:
        from doyoutrade.debug import emit_debug_event

        await emit_debug_event(
            "sector_provider_fallback",
            {
                "provider": source,
                "method": method,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "hint": "auto-chain fell through to the next sector provider",
            },
        )
    except Exception:  # noqa: BLE001 — observability must not break the chain
        pass


def _build_sector_provider_named(
    name: str, data_cfg: DataSettings, account: ResolvedAccount | None = None
) -> Any:
    if name == PROVIDER_AKSHARE:
        from doyoutrade.data.sector_akshare import AkshareSectorProvider

        return AkshareSectorProvider()
    if name == PROVIDER_QMT:
        if not _qmt_configured(account):
            raise ValueError(
                "sector provider 'qmt' requires a default account with base_url; "
                "create one or use 'akshare' / 'auto'."
            )
        from doyoutrade.data.sector_qmt import QmtSectorProvider

        return QmtSectorProvider(create_qmt_proxy_rest_client(account))
    raise ValueError(
        f"unknown sector data provider {name!r}; expected one of "
        f"auto, akshare, qmt."
    )


def build_sector_provider(
    provider_id: str | None,
    data_cfg: DataSettings,
    account: ResolvedAccount | None = None,
) -> Any:
    """Build a :class:`doyoutrade.data.protocols.SectorProvider`.

    ``provider_id``: ``auto`` | ``akshare`` | ``qmt``. For ``auto`` the chain
    is akshare-first (token-free, always reachable) then qmt when ``account``
    supplies a base_url, wrapped in :class:`_FallbackSectorProvider`. tushare /
    baostock are not on the sector axis and are rejected with a clear error.
    ``account`` is the market-only default account (or None) — callers resolve
    it via the account repository before calling this sync builder.
    """
    name = normalize_provider_id(provider_id)
    if name != PROVIDER_AUTO:
        if name not in _SECTOR_SOURCES:
            raise ValueError(
                f"data source {name!r} has no sector membership endpoint; "
                f"use one of {list(_SECTOR_SOURCES)} or 'auto'."
            )
        return _build_sector_provider_named(name, data_cfg, account)

    providers: list[Any] = [_build_sector_provider_named(PROVIDER_AKSHARE, data_cfg, account)]
    if _qmt_configured(account):
        providers.append(_build_sector_provider_named(PROVIDER_QMT, data_cfg, account))
    if len(providers) == 1:
        return providers[0]
    return _FallbackSectorProvider(providers)


# ---------------------------------------------------------------------------
# Fundamentals axis (valuation / market cap)
# ---------------------------------------------------------------------------

# tushare / baostock are not wired on the fundamentals axis yet.
_FUNDAMENTALS_SOURCES: tuple[str, ...] = (PROVIDER_AKSHARE, PROVIDER_QMT)


class _FallbackFundamentalsProvider:
    """Try each fundamentals provider in order; surface the last error if all fail.

    Fallback fires only when a provider *raises*; a provider that returns a
    partial map (some requested symbols absent) is a legitimate result, not a
    failure, so we do not chain on partial coverage.
    """

    def __init__(self, providers: list[Any]) -> None:
        if not providers:
            raise ValueError("_FallbackFundamentalsProvider needs at least one provider")
        self._providers = providers
        self.capabilities = providers[0].capabilities

    async def get_fundamentals_batch(
        self, symbols: list[str], *, asof: str | None = None
    ) -> dict[str, Any]:
        return await self._first_ok("get_fundamentals_batch", symbols, asof=asof)

    async def get_fundamentals(self, symbol: str, *, asof: str | None = None) -> Any:
        return await self._first_ok("get_fundamentals", symbol, asof=asof)

    async def _first_ok(self, method: str, *args: Any, **kwargs: Any) -> Any:
        import logging

        logger = logging.getLogger(__name__)
        last_exc: Exception | None = None
        for provider in self._providers:
            source = getattr(provider.capabilities, "name", type(provider).__name__)
            try:
                return await getattr(provider, method)(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — surfaced after the chain
                last_exc = exc
                logger.warning(
                    "fundamentals auto-chain: %s.%s failed (%s: %s); trying next",
                    source, method, type(exc).__name__, exc,
                )
                await _emit_sector_fallback(source, f"fundamentals.{method}", exc)
        assert last_exc is not None
        raise last_exc


def _build_fundamentals_provider_named(
    name: str, data_cfg: DataSettings, account: ResolvedAccount | None = None
) -> Any:
    if name == PROVIDER_AKSHARE:
        from doyoutrade.data.fundamentals_akshare import AkshareFundamentalsProvider

        return AkshareFundamentalsProvider()
    if name == PROVIDER_QMT:
        if not _qmt_configured(account):
            raise ValueError(
                "fundamentals provider 'qmt' requires a default account with base_url; "
                "create one or use 'akshare' / 'auto'."
            )
        from doyoutrade.data.fundamentals_qmt import QmtFundamentalsProvider

        return QmtFundamentalsProvider(create_qmt_proxy_rest_client(account))
    raise ValueError(
        f"unknown fundamentals data provider {name!r}; expected one of auto, akshare, qmt."
    )


def build_fundamentals_provider(
    provider_id: str | None,
    data_cfg: DataSettings,
    account: ResolvedAccount | None = None,
) -> Any:
    """Build a :class:`doyoutrade.data.protocols.FundamentalsProvider`.

    ``auto`` is akshare-first (one whole-market snapshot serves a full
    universe, and it carries PE/PB) then qmt (float-cap only) when ``account``
    supplies a base_url. ``account`` is the market-only default account (or
    None), resolved by the caller before this sync builder runs.
    """
    name = normalize_provider_id(provider_id)
    if name != PROVIDER_AUTO:
        if name not in _FUNDAMENTALS_SOURCES:
            raise ValueError(
                f"data source {name!r} has no fundamentals endpoint; "
                f"use one of {list(_FUNDAMENTALS_SOURCES)} or 'auto'."
            )
        return _build_fundamentals_provider_named(name, data_cfg, account)

    providers: list[Any] = [_build_fundamentals_provider_named(PROVIDER_AKSHARE, data_cfg, account)]
    if _qmt_configured(account):
        providers.append(_build_fundamentals_provider_named(PROVIDER_QMT, data_cfg, account))
    if len(providers) == 1:
        return providers[0]
    return _FallbackFundamentalsProvider(providers)


# ---------------------------------------------------------------------------
# Event axis (suspension / earnings calendar)
# ---------------------------------------------------------------------------

# Only akshare is wired on the event axis today (qmt's event surface — a tick
# status flag — is too thin to serve a calendar). ``auto`` resolves to it; the
# Protocol stays source-agnostic so a richer source can be added later.
_EVENT_SOURCES: tuple[str, ...] = (PROVIDER_AKSHARE,)


def build_event_provider(provider_id: str | None, data_cfg: DataSettings) -> Any:
    """Build a :class:`doyoutrade.data.protocols.EventProvider`.

    ``auto`` / ``akshare`` resolve to the akshare suspension provider.
    """
    name = normalize_provider_id(provider_id)
    if name in (PROVIDER_AUTO, PROVIDER_AKSHARE):
        from doyoutrade.data.event_akshare import AkshareEventProvider

        return AkshareEventProvider()
    raise ValueError(
        f"data source {name!r} has no event endpoint; use one of "
        f"{list(_EVENT_SOURCES)} or 'auto'."
    )
