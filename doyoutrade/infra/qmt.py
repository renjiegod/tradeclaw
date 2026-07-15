"""Construct shared QMT proxy HTTP clients for market and account stacks."""

from __future__ import annotations

from doyoutrade.data._sdk_instrumentation import instrument_sdk
from doyoutrade.data.account_resolution import ResolvedAccount
from doyoutrade.infra.qmt_proxy_client import QmtProxyRestClient


def create_qmt_proxy_rest_client(
    account: ResolvedAccount, session_persist=None
) -> QmtProxyRestClient:
    """Build one :class:`~doyoutrade.infra.qmt_proxy_client.QmtProxyRestClient`
    from a resolved account.

    When ``account.mode == "mock"`` (or the account is market-only), the
    trading ``account_id`` is cleared so the proxy never opens a trading
    session — account snapshots come from the in-memory mock reader instead.

    ``session_persist`` is an optional async ``(account_id, session_id)``
    callback used to write a refreshed trading session id back to the
    ``accounts`` row when a live connect rotates the session.
    """
    connect_account_id = None if account.mode == "mock" else account.qmt_account_id
    client = QmtProxyRestClient(
        base_url=account.base_url or "",
        token=account.token,
        session_id=account.session_id,
        timeout_seconds=account.timeout_seconds,
        account_id=connect_account_id,
        account_pk=account.account_id or None,
        terminal_id=account.qmt_terminal_id,
        session_persist=session_persist,
    )
    instrument_sdk(client._client)
    return client
