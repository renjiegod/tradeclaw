"""Resolved-account value object shared across the trading-data stack.

A :class:`ResolvedAccount` is the runtime view of one persisted ``accounts``
row (see :class:`doyoutrade.persistence.models.AccountRecord`). It carries the
QMT proxy connection (``base_url`` / ``token`` / ``timeout_seconds``), the
trading identity (``qmt_account_id`` / ``session_id``), the ``mode``
(``live`` | ``mock``) and the mock portfolio. The data factory consumes this
object instead of reading the old ``config.data.qmt`` block.

There are two construction paths:

* :func:`resolved_account_from_record` — full account for a worker cycle.
* :meth:`ResolvedAccount.market_only` — strip the trading identity so a
  pure market-data path (backtest / ``data run`` / screening / sector /
  fundamentals) reuses the connection without ever connecting a trading
  terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class QmtMockPositionSettings:
    """A single mock-portfolio holding. Migrated from ``config.py`` so the
    mock account can live entirely in the accounts table."""

    symbol: str
    quantity: float
    cost_price: float


@dataclass(frozen=True)
class ResolvedAccount:
    account_id: str  # acct-...; "" means "no account / market-data only"
    name: str
    mode: str  # "live" | "mock"
    base_url: str
    token: str | None
    timeout_seconds: float
    qmt_account_id: str | None  # broker trading account; None for mock / market-only
    session_id: str | None
    mock_cash: float
    mock_equity: float
    mock_positions: tuple[QmtMockPositionSettings, ...] = field(default_factory=tuple)
    # Which QMT terminal (client) on a multi-terminal qmt-proxy this account
    # routes to (sent as ``X-QMT-Terminal``). None → proxy default terminal.
    qmt_terminal_id: str | None = None

    @property
    def has_connection(self) -> bool:
        """True when a QMT proxy base_url is present (market data reachable)."""
        return bool(self.base_url and self.base_url.strip())

    def market_only(self) -> "ResolvedAccount":
        """A copy with the trading identity cleared and mode forced to mock so
        the QMT client never opens a trading-terminal session — used by pure
        market-data paths that only need the connection (base_url/token)."""
        return ResolvedAccount(
            account_id="",
            name=self.name,
            mode="mock",
            base_url=self.base_url,
            token=self.token,
            timeout_seconds=self.timeout_seconds,
            qmt_account_id=None,
            session_id=None,
            mock_cash=self.mock_cash,
            mock_equity=self.mock_equity,
            mock_positions=self.mock_positions,
            # Market-data paths still hit the proxy on this account's terminal
            # (data is broker-agnostic but the connection/datadir is the
            # terminal's); preserve the routing hint.
            qmt_terminal_id=self.qmt_terminal_id,
        )


def _coerce_positions(raw: Any) -> tuple[QmtMockPositionSettings, ...]:
    if not raw:
        return ()
    out: list[QmtMockPositionSettings] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(
                f"mock_positions entries must be dicts, got {type(item).__name__}: {item!r}"
            )
        out.append(
            QmtMockPositionSettings(
                symbol=str(item["symbol"]),
                quantity=float(item.get("quantity", 0) or 0),
                cost_price=float(item.get("cost_price", 0) or 0),
            )
        )
    return tuple(out)


def resolved_account_from_record(record: dict[str, Any]) -> ResolvedAccount:
    """Build a :class:`ResolvedAccount` from a serialized account dict
    (the shape returned by ``SqlAlchemyAccountRepository``)."""
    mode = str(record.get("mode") or "live").strip().lower() or "live"
    return ResolvedAccount(
        account_id=str(record.get("id") or ""),
        name=str(record.get("name") or ""),
        mode=mode,
        base_url=str(record.get("base_url") or ""),
        token=record.get("token"),
        timeout_seconds=float(record.get("timeout_seconds") or 30.0),
        qmt_account_id=(record.get("qmt_account_id") or None),
        session_id=(record.get("session_id") or None),
        mock_cash=float(record.get("mock_cash") or 0.0),
        mock_equity=float(record.get("mock_equity") or 0.0),
        mock_positions=_coerce_positions(record.get("mock_positions")),
        qmt_terminal_id=(record.get("qmt_terminal_id") or None),
    )


def market_only_from_record(record: dict[str, Any] | None) -> ResolvedAccount | None:
    """Convenience for pure market-data paths: resolve a default-account dict
    (or None) into a market-only :class:`ResolvedAccount` (or None)."""
    if not record:
        return None
    return resolved_account_from_record(record).market_only()


# --- default market-account resolver (mirrors the get_config() global) -------
# Stateless data tools (data run / screen / sector / fundamentals) have no
# repository handle, so bootstrap registers an async ``() -> dict | None``
# resolver bound to ``account_repository.get_default_account``. Tools call
# ``resolve_default_market_account()`` to get a market-only ResolvedAccount.
_default_account_resolver = None


def register_default_account_resolver(resolver) -> None:
    global _default_account_resolver
    _default_account_resolver = resolver


async def resolve_default_market_account() -> ResolvedAccount | None:
    """Market-only default account for stateless data paths, or None when no
    resolver is registered / no default account exists."""
    if _default_account_resolver is None:
        return None
    record = await _default_account_resolver()
    return market_only_from_record(record)
