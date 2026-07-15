"""Realtime quote fan-out service for the watchlist WebSocket endpoint.

``QuoteStreamService`` sits between qmt-proxy's streaming quote feed and the
set of connected frontend WebSocket clients. It is the **highest-risk**
component in the realtime path, so every state transition that can hide a
failure is made visible per CLAUDE.md §错误可见性:

* one background task subscribes to qmt for the *union* of all clients'
  symbols and fans each frame out only to the clients that asked for that
  symbol;
* the union is rebuilt (old task cancelled, new task started) whenever a
  client connects / disconnects / changes its subscription;
* upstream disconnects / errors are logged + emitted as structured events and
  retried with a bounded backoff (never a bare ``except: pass``);
* a single client whose ``send`` raises is logged and queued for removal so
  one dead socket cannot stall the broadcast to everyone else;
* when qmt is not connected (``has_connection=False``) no background task is
  started — new clients just get a ``qmt_disconnected`` status frame and
  ``fetch_once`` returns ``status="qmt_disconnected"`` placeholders.

**Dynamic reconnection (default-account changes at runtime)**: pass an
``account_resolver`` + ``connection_factory`` to the constructor. The service
then re-resolves the default account on every register / subscription change,
on a slow background poll (``refresh_interval_seconds``), and on demand via
``refresh()`` (called by the account CRUD API). When the resolved connection
signature changes (``base_url`` / ``token`` / ``qmt_terminal_id`` /
``timeout_seconds``) the old provider + ws_client + stream task are torn down
and rebuilt, and connected WS clients get a fresh ``snapshot`` (or
``qmt_disconnected`` status) frame so the frontend banner clears without a
page reload.

Wire shapes (the contract Phase B's WS handler / REST layer and the frontend
depend on):

* client → server (handled by the WS endpoint, not here): ``{"action":
  "subscribe", "symbols": [...]}``.
* server → client frames produced by this service:
  - ``{"type": "snapshot", "quotes": [QuoteSnapshot.to_dict(), ...]}``
    — sent once on register / on subscription change, from the cache.
  - ``{"type": "quote", "quote": QuoteSnapshot.to_dict()}``
    — per streamed update, fanned out to subscribers of that symbol.
  - ``{"type": "status", "status": "qmt_disconnected"}``
    — sent on register when qmt is not connected, and on dynamic state flip.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from datetime import date
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

from doyoutrade.core.models import QuoteSnapshot
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.qmt_proxy import quote_snapshot_from_tick
from doyoutrade.data.protocols import RealtimeQuoteProvider
from doyoutrade.debug import emit_debug_event

logger = logging.getLogger(__name__)

SendFn = Callable[[dict], Awaitable[None]]
WsSubscribe = Callable[[List[str]], AsyncIterator[Any]]
# An in-process observer notified of every freshly-cached snapshot. Used by the
# monitoring daemon to evaluate conditions tick-by-tick without being a WS client.
SnapshotObserver = Callable[[str, "QuoteSnapshot"], Awaitable[None]]
# Dynamic connection factory: takes the freshly resolved market-only account
# and returns ``(provider, ws_subscribe, aclose)``. ``aclose`` is an optional
# async callable the service invokes when tearing this connection down (so the
# underlying qmt-proxy clients release their sockets).
AccountResolver = Callable[[], Awaitable[Optional[dict]]]
ConnectionFactory = Callable[[Any], Tuple[Optional[RealtimeQuoteProvider], Optional[WsSubscribe], Optional[Callable[[], Awaitable[None]]]]]
# Suspension (停牌) set provider: given the subscribed symbol union + an asof
# date (YYYY-MM-DD), returns the subset that is halted today. Used to overlay
# ``status="suspended"`` onto served snapshots so a halted name shows 停牌 even
# when qmt streams a flat ``last_price == prev_close`` tick (which alone reads
# as a benign 0% move). It is a NETWORK lookup (akshare) — the service calls it
# only from the slow background refresh, never on the per-tick hot path.
SuspensionProvider = Callable[[List[str], Optional[str]], Awaitable["frozenset[str]"]]

# Bounded exponential backoff (seconds) used when the upstream subscription
# drops or errors. Deterministic — no Date / random per CLAUDE.md.
_BACKOFF_SCHEDULE_SECONDS = (1.0, 2.0, 5.0, 10.0, 15.0)

# Default cadence for the dynamic-connection background refresh. Long enough
# to stay out of the hot path, short enough to clear the disconnected banner
# within a user-perceptible delay after the default account is configured.
_DEFAULT_REFRESH_INTERVAL_SECONDS = 15.0

# Connection signature for the polling fallback (mootdx) — a stable 1-tuple that
# never collides with a real qmt signature (a 4-tuple of base_url/token/...), so
# refresh idempotency holds while on the fallback and it rebuilds only when the
# account flips to/from a real qmt connection.
_FALLBACK_SIG: tuple = ("__fallback_polling__",)


class _ClientHandle:
    """Per-client registration: its send callback and its symbol set.

    A plain object so callers hold an opaque ``handle`` (identity-based) they
    pass back to ``update_subscription`` / ``unregister``.
    """

    __slots__ = ("send", "symbols")

    def __init__(self, send: SendFn, symbols: List[str]) -> None:
        self.send = send
        # Preserve order-insensitive membership; dedup at registration time.
        self.symbols: set[str] = {str(s) for s in symbols}


class QuoteStreamService:
    """Fan-out realtime qmt quotes to WebSocket clients (qmt-only).

    Construction (Phase B wires it like this from bootstrap)::

        # Dynamic mode (preferred): resolver + factory let the service rebuild
        # the connection when the default account changes at runtime.
        QuoteStreamService(
            account_resolver=account_repository.get_default_account,
            connection_factory=lambda acct: (
                QmtRealtimeQuoteProvider(create_qmt_proxy_rest_client(acct)),
                lambda syms: QmtProxyWsClient(acct.base_url, acct.token).subscribe_quotes(syms),
                None,
            ),
        )

        # Static mode (tests / no account repository): one-shot build.
        QuoteStreamService(
            quote_provider=qmt_realtime_quote_provider,   # one-shot snapshots
            ws_subscribe=lambda syms: qmt_ws_client.subscribe_quotes(syms),
            has_connection=account.has_connection,
        )

    * ``quote_provider`` — used for one-shot snapshots (REST + the initial
      frame to a freshly-registered client). May be ``None`` (then snapshots
      come only from the streaming cache).
    * ``ws_subscribe(symbols)`` — returns an **async iterator** of qmt
      ``QuoteData`` for the given symbol union. May be ``None`` (no streaming).
    * ``has_connection`` — ``False`` means qmt is not connected; no background
      task runs and clients receive a ``qmt_disconnected`` status frame.
      Ignored in dynamic mode (derived from the resolver each refresh).
    * ``account_resolver`` / ``connection_factory`` — when both are supplied
      the service runs in dynamic mode: the default account is re-resolved on
      register / subscription change / on a slow background poll / via the
      public ``refresh()`` method, and the connection is rebuilt when its
      signature (``base_url`` / ``token`` / ``qmt_terminal_id`` /
      ``timeout_seconds``) changes.
    """

    def __init__(
        self,
        *,
        quote_provider: Optional[RealtimeQuoteProvider] = None,
        ws_subscribe: Optional[WsSubscribe] = None,
        has_connection: bool = False,
        logger: Optional[logging.Logger] = None,
        account_resolver: Optional[AccountResolver] = None,
        connection_factory: Optional[ConnectionFactory] = None,
        refresh_interval_seconds: float = _DEFAULT_REFRESH_INTERVAL_SECONDS,
        suspension_provider: Optional[SuspensionProvider] = None,
        fallback_provider: Optional[RealtimeQuoteProvider] = None,
    ) -> None:
        self._log = logger or logging.getLogger(__name__)

        # Polling-only snapshot source used when no qmt account is connected
        # (e.g. mootdx after QMT is banned). Backs ``fetch_once`` + the REST
        # ``/market/quotes`` path and register-time snapshots; it carries NO
        # ws_subscribe, so no continuous WS push is started for it (the frontend
        # watchlist's REST poll + WS initial snapshot still resolve real quotes
        # instead of ``qmt_disconnected`` placeholders).
        self._fallback_provider = fallback_provider

        # Dynamic-mode hooks. When both are set, the service periodically (and
        # on demand) re-resolves the default account and rebuilds the provider
        # + ws_client via ``connection_factory`` if the connection signature
        # changed. Static mode is preserved for tests that build directly.
        self._account_resolver = account_resolver
        self._connection_factory = connection_factory
        self._refresh_interval = float(refresh_interval_seconds)
        self._dynamic_mode = account_resolver is not None and connection_factory is not None

        # Currently active connection state. In static mode these are whatever
        # the caller passed; in dynamic mode they are (re)built lazily by
        # ``_refresh_connection_locked``. ``_connection_signature`` tracks the
        # account fingerprint so a no-op refresh is cheap (no provider rebuild).
        self._quote_provider = quote_provider
        self._ws_subscribe = ws_subscribe
        self._has_connection = bool(has_connection)
        self._connection_signature: Optional[tuple] = None
        # Per-connection teardown callback (set by the factory). Awaited once
        # when the connection is replaced or the service closes.
        self._connection_aclose: Optional[Callable[[], Awaitable[None]]] = None

        # handle -> registration. Identity-keyed.
        self._clients: Dict[_ClientHandle, _ClientHandle] = {}
        # symbol -> latest snapshot (most recent streamed frame).
        self._cache: Dict[str, QuoteSnapshot] = {}
        # The symbol union currently driving the background task.
        self._subscribed_union: frozenset[str] = frozenset()
        # In-process snapshot observers (e.g. the MonitorDaemon). Each receives
        # every freshly-cached snapshot. Kept separate from WS clients so the
        # monitor keeps the upstream subscription alive even with no browser open.
        self._snapshot_observers: list[SnapshotObserver] = []
        # Symbols the monitor daemon needs streamed regardless of WS clients.
        self._monitored: frozenset[str] = frozenset()

        # Suspension (停牌) overlay. ``_suspended`` is the in-memory set read on
        # the hot path (O(1), no network); it is (re)populated by
        # ``_refresh_suspensions`` off the slow background loop via
        # ``_suspension_provider``. ``_suspended_asof`` / ``_suspended_union``
        # throttle the network lookup to once per trading day + on union growth.
        self._suspension_provider = suspension_provider
        self._suspended: frozenset[str] = frozenset()
        self._suspended_asof: Optional[str] = None
        self._suspended_union: frozenset[str] = frozenset()

        self._stream_task: Optional[asyncio.Task] = None
        self._refresh_task: Optional[asyncio.Task] = None
        # Serialize registry mutations + task (re)builds so concurrent
        # register / update / unregister calls don't race the union.
        self._lock = asyncio.Lock()
        self._started = False
        self._closing = False

    # ----- lifecycle ---------------------------------------------------

    async def start(self) -> None:
        """Mark the service started and emit the start event.

        The background subscription task is created lazily once a client
        registers with symbols (so we never open an upstream stream for an
        empty union). When qmt is not connected, no task is ever created.

        In dynamic mode this also performs the first synchronous refresh (so
        ``has_connection`` reflects the current default account immediately,
        not after the first poll interval) and starts the slow background
        refresh loop that clears the disconnected banner after the operator
        configures the default account at runtime.
        """
        self._started = True
        self._closing = False
        with data_span("quote_stream", "start"):
            await emit_debug_event(
                "quote_stream_started",
                {
                    "has_connection": self._has_connection,
                    "dynamic_mode": self._dynamic_mode,
                    "hint": "qmt quote fan-out service started; clients register via /ws/market/quotes",
                },
            )
        self._log.info(
            "quote_stream started has_connection=%s dynamic_mode=%s",
            self._has_connection,
            self._dynamic_mode,
        )
        if self._dynamic_mode:
            # First refresh is synchronous so the initial state is correct.
            await self.refresh()
            self._refresh_task = asyncio.create_task(
                self._run_refresh_loop(), name="quote_stream_refresh"
            )

    async def aclose(self) -> None:
        """Stop the background tasks and drop all client registrations."""
        self._closing = True
        async with self._lock:
            await self._cancel_stream_task_locked()
            await self._cancel_refresh_task_locked()
            await self._teardown_connection_locked()
            self._clients.clear()
            self._subscribed_union = frozenset()
        self._started = False
        self._log.info("quote_stream closed")

    # ----- one-shot snapshot ------------------------------------------

    async def fetch_once(self, symbols: List[str]) -> dict[str, QuoteSnapshot]:
        """Return a one-shot snapshot for ``symbols``.

        When qmt is not connected, returns a ``qmt_disconnected`` placeholder
        for every requested symbol (never raises, never empty). Otherwise
        delegates to the configured ``quote_provider``; if none is configured
        the result is built from the streaming cache (missing → ``no_data``).
        """
        requested = [str(s) for s in symbols]
        if not self._has_connection:
            return {
                symbol: QuoteSnapshot(symbol=symbol, status="qmt_disconnected")
                for symbol in requested
            }
        if self._quote_provider is not None:
            fetched = await self._quote_provider.fetch_quotes(requested)
            return {s: self._overlay_suspension(q) for s, q in fetched.items()}
        # No provider: serve whatever the stream cache has, no_data otherwise.
        return {
            symbol: self._overlay_suspension(
                self._cache.get(symbol, QuoteSnapshot(symbol=symbol, status="no_data"))
            )
            for symbol in requested
        }

    # ----- client registry --------------------------------------------

    async def register(self, send: SendFn, symbols: List[str]) -> _ClientHandle:
        """Register a WebSocket client and return an opaque handle.

        Immediately sends the client one ``snapshot`` frame built from the
        cache (and a one-shot provider fetch for any uncached symbols when qmt
        is connected). When qmt is not connected, also sends a
        ``qmt_disconnected`` status frame.
        """
        handle = _ClientHandle(send, symbols)
        async with self._lock:
            self._clients[handle] = handle
            client_symbols = sorted(handle.symbols)
            await self._rebuild_stream_task_locked(reason="client_registered")
        with data_span("quote_stream", "register"):
            await emit_debug_event(
                "quote_stream_client_registered",
                {
                    "symbol_count": len(client_symbols),
                    "client_count": len(self._clients),
                    "has_connection": self._has_connection,
                    "hint": "new WS client subscribed; sent initial snapshot frame",
                },
            )
        self._log.info(
            "quote_stream client registered symbol_count=%d client_count=%d",
            len(client_symbols),
            len(self._clients),
        )

        # Build and send the initial snapshot frame from cache; for connected
        # qmt, fill any cache misses via a one-shot provider fetch so the
        # client isn't blank until the first streamed tick. A client whose
        # very first send fails is unregistered immediately (don't leave a
        # dead socket in the registry until the next tick).
        snapshot = await self._initial_snapshot(client_symbols)
        ok = await self._safe_send(
            handle, {"type": "snapshot", "quotes": [q.to_dict() for q in snapshot]}
        )
        if ok and not self._has_connection:
            ok = await self._safe_send(
                handle, {"type": "status", "status": "qmt_disconnected"}
            )
        if not ok:
            await self.unregister(handle)
        return handle

    async def update_subscription(
        self, handle: _ClientHandle, symbols: List[str]
    ) -> None:
        """Replace a client's subscribed symbol set and resend its snapshot."""
        async with self._lock:
            if handle not in self._clients:
                self._log.info(
                    "quote_stream update_subscription on unknown handle; ignoring"
                )
                return
            handle.symbols = {str(s) for s in symbols}
            client_symbols = sorted(handle.symbols)
            await self._rebuild_stream_task_locked(reason="subscription_changed")
        snapshot = await self._initial_snapshot(client_symbols)
        ok = await self._safe_send(
            handle, {"type": "snapshot", "quotes": [q.to_dict() for q in snapshot]}
        )
        if ok and not self._has_connection:
            ok = await self._safe_send(
                handle, {"type": "status", "status": "qmt_disconnected"}
            )
        if not ok:
            await self.unregister(handle)

    async def unregister(self, handle: _ClientHandle) -> None:
        """Drop a client registration and rebuild the union if it shrank."""
        async with self._lock:
            if self._clients.pop(handle, None) is None:
                return
            await self._rebuild_stream_task_locked(reason="client_unregistered")
            remaining = len(self._clients)
        with data_span("quote_stream", "unregister"):
            await emit_debug_event(
                "quote_stream_client_unregistered",
                {
                    "client_count": remaining,
                    "hint": "WS client disconnected; symbol union rebuilt",
                },
            )
        self._log.info("quote_stream client unregistered client_count=%d", remaining)

    # ----- dynamic-mode refresh (public + internal) -------------------

    async def refresh(self) -> bool:
        """Re-resolve the default account and rebuild the connection if its
        signature changed.

        Returns ``True`` when the connection state changed AND was pushed to
        already-connected WS clients (so the frontend banner clears without a
        page reload). Returns ``False`` in static mode or when nothing changed.

        Safe to call from the account CRUD API (post-mutation) and from the
        background poll loop; concurrent calls serialize on ``self._lock``.
        """
        if not self._dynamic_mode:
            return False
        changed: bool = False
        async with self._lock:
            changed = await self._refresh_connection_locked()
            if changed:
                # The union-driven stream task is rebuilt by the inner refresh
                # when ``has_connection`` flips; here we only re-push frames
                # to existing clients so they recover without re-registering.
                pass
        if changed:
            await self._broadcast_state_change()
        return changed

    async def _run_refresh_loop(self) -> None:
        """Slow background poll: re-resolve the default account on a fixed
        cadence. Exits on cancellation / service close. Each iteration is
        wrapped so a single resolver error cannot kill the loop."""
        while not self._closing:
            try:
                await asyncio.sleep(self._refresh_interval)
                if self._closing:
                    return
                await self.refresh()
                # Re-query the 停牌 set (self-throttled to daily + on union
                # growth). When it changes, re-push snapshots so halted names —
                # which never stream a tick — flip to 停牌 without a reload.
                if await self._refresh_suspensions():
                    await self._repush_snapshots_to_clients()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — visible, keeps loop alive
                self._log.warning(
                    "quote_stream refresh loop iteration failed (%s): %s",
                    type(exc).__name__,
                    exc,
                )
                await emit_debug_event(
                    "quote_stream_refresh_failed",
                    {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "hint": "background refresh iteration raised; loop continues",
                    },
                )

    async def _cancel_refresh_task_locked(self) -> None:
        task = self._refresh_task
        self._refresh_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _connection_signature_for(self, record: Optional[dict]) -> Optional[tuple]:
        """Fingerprint the resolved default account.

        Returns ``None`` when there is no default account or it carries no
        ``base_url`` (i.e. qmt-proxy unreachable) — the disconnected state.
        Otherwise a tuple of the fields that, if changed, require rebuilding
        the provider + ws_client.
        """
        if not record:
            return _FALLBACK_SIG if self._fallback_provider is not None else None
        base_url = str(record.get("base_url") or "").strip()
        if not base_url:
            # No qmt connection. Fall back to the polling provider when one is
            # wired (mootdx); otherwise stay disconnected as before.
            return _FALLBACK_SIG if self._fallback_provider is not None else None
        return (
            base_url,
            record.get("token"),
            record.get("qmt_terminal_id"),
            float(record.get("timeout_seconds") or 30.0),
        )

    async def _refresh_connection_locked(self) -> bool:
        """Re-resolve the default account and rebuild the provider + ws_client
        when the connection signature changed.

        Must be called with ``self._lock`` held. Returns ``True`` when the
        connection state changed (caller should re-push frames to clients).
        No-op in static mode.
        """
        if not self._dynamic_mode:
            return False
        try:
            record = await self._account_resolver()  # type: ignore[misc]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — visible, keeps service alive
            self._log.warning(
                "quote_stream account resolver raised (%s): %s; keeping current connection",
                type(exc).__name__,
                exc,
            )
            await emit_debug_event(
                "quote_stream_account_resolve_failed",
                {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "hint": "default-account resolver raised; quote stream keeps its current connection",
                },
            )
            return False

        new_sig = self._connection_signature_for(record)
        if new_sig == self._connection_signature:
            return False

        prev_connected = self._has_connection
        # Tear down whatever is currently built (cancels the stream task,
        # clears the cache, closes the old provider/ws_client).
        await self._teardown_connection_locked()

        if new_sig is None:
            self._has_connection = False
            self._quote_provider = None
            self._ws_subscribe = None
            self._connection_signature = None
            await emit_debug_event(
                "quote_stream_disconnected",
                {
                    "reason": "default_account_unavailable",
                    "prev_connected": prev_connected,
                    "hint": "default account missing or carries no base_url; "
                    "realtime quotes unavailable, clients see qmt_disconnected",
                },
            )
            self._log.info(
                "quote_stream disconnected (default account unavailable) prev=%s",
                prev_connected,
            )
            return True

        if new_sig == _FALLBACK_SIG:
            # No qmt account, but a polling fallback (mootdx) is wired: serve its
            # L1 snapshots via fetch_once / REST + register-time snapshots. No
            # ws_subscribe → the stream task starts no continuous push, and (per
            # ``_rebuild_stream_task_locked``) emits no misleading qmt_unavailable
            # event because ``has_connection`` is True.
            self._quote_provider = self._fallback_provider
            self._ws_subscribe = None
            self._connection_aclose = None
            self._has_connection = True
            self._connection_signature = new_sig
            await emit_debug_event(
                "quote_stream_connected",
                {
                    "mode": "fallback_polling",
                    "prev_connected": prev_connected,
                    "hint": "no qmt account; serving realtime L1 snapshots via the "
                    "polling fallback provider (mootdx) — REST /market/quotes + "
                    "register snapshots resolve real quotes, no WS push",
                },
            )
            self._log.info(
                "quote_stream connected via polling fallback (no qmt) prev=%s",
                prev_connected,
            )
            return True

        # Build new provider + ws_subscribe via the factory.
        from doyoutrade.data.account_resolution import (
            resolved_account_from_record,
        )

        account = resolved_account_from_record(record).market_only()
        try:
            provider, ws_subscribe, aclose = self._connection_factory(account)  # type: ignore[misc]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — visible
            self._log.warning(
                "quote_stream connection_factory raised (%s): %s; staying disconnected",
                type(exc).__name__,
                exc,
            )
            await emit_debug_event(
                "quote_stream_connection_build_failed",
                {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "base_url": account.base_url,
                    "hint": "connection_factory raised; quote stream stays disconnected",
                },
            )
            self._has_connection = False
            self._quote_provider = None
            self._ws_subscribe = None
            self._connection_signature = None
            self._connection_aclose = None
            return True

        self._quote_provider = provider
        self._ws_subscribe = ws_subscribe
        self._connection_aclose = aclose
        self._has_connection = True
        self._connection_signature = new_sig
        await emit_debug_event(
            "quote_stream_connected",
            {
                "base_url": account.base_url,
                "qmt_terminal_id": account.qmt_terminal_id,
                "prev_connected": prev_connected,
                "hint": "default account resolved; quote stream connected via qmt-proxy",
            },
        )
        self._log.info(
            "quote_stream connected base_url=%s terminal=%s prev=%s",
            account.base_url,
            account.qmt_terminal_id,
            prev_connected,
        )
        return True

    async def _teardown_connection_locked(self) -> None:
        """Tear down the current stream task, clear the cache, and close any
        owned provider/ws_client built by the factory.

        Must be called with ``self._lock`` held. Leaves ``_has_connection``
        alone (caller decides the next state) but does flip the stream-side
        bookkeeping so the next ``_rebuild_stream_task_locked`` starts clean.
        """
        await self._cancel_stream_task_locked()
        self._cache = {}
        aclose = self._connection_aclose
        self._connection_aclose = None
        if aclose is None:
            return
        try:
            await aclose()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — visible, non-fatal
            self._log.warning(
                "quote_stream connection aclose raised (%s): %s",
                type(exc).__name__,
                exc,
            )

    async def _broadcast_state_change(self) -> None:
        """After a connected ⇄ disconnected flip, push a fresh frame to every
        already-connected client so the frontend banner clears (or shows)
        without a page reload. Outside the lock (sends may block on slow
        sockets) but uses ``_safe_send`` so a dead client is unregistered."""
        clients = list(self._clients)
        if not clients:
            return
        if self._has_connection:
            await self._repush_snapshots_to_clients(clients)
        else:
            for handle in clients:
                await self._safe_send(
                    handle, {"type": "status", "status": "qmt_disconnected"}
                )

    async def _repush_snapshots_to_clients(
        self, clients: Optional[List["_ClientHandle"]] = None
    ) -> None:
        """Push a fresh (suspension-overlaid) ``snapshot`` frame to connected
        clients. Used after a connected flip AND after the suspension set
        changes — halted names never stream a tick, so they need an explicit
        re-push to flip to 停牌. Best-effort per client; ``_safe_send`` drops a
        dead socket. Built from ``_initial_snapshot`` so the overlay is applied
        uniformly."""
        for handle in list(self._clients) if clients is None else clients:
            symbols = sorted(handle.symbols)
            if not symbols:
                continue
            snapshot = await self._initial_snapshot(symbols)
            await self._safe_send(
                handle,
                {"type": "snapshot", "quotes": [q.to_dict() for q in snapshot]},
            )

    # ----- suspension (停牌) overlay -----------------------------------

    @staticmethod
    def _today() -> str:
        """Today's date (YYYY-MM-DD), the asof key for the suspension lookup.

        Server runs in CN market time (Asia/Shanghai); this matches how
        ``data events`` defaults its asof. Used only as the daily throttle key —
        not for stream timing (which stays deterministic per backoff schedule).
        """
        return date.today().isoformat()

    def _overlay_suspension(self, snapshot: QuoteSnapshot) -> QuoteSnapshot:
        """Overlay ``status="suspended"`` onto a snapshot for a halted symbol.

        Hot-path safe: a pure in-memory ``frozenset`` membership test, no
        network. For a halted name we drop ``price`` / ``change`` / ``change_pct``
        (a flat ``last_price == prev_close`` tick would otherwise read as a
        benign 0% move) while keeping ``prev_close`` + limit prices. We never
        mask a genuine ``qmt_disconnected`` / ``no_data`` placeholder.
        """
        if not self._suspended or snapshot.symbol not in self._suspended:
            return snapshot
        if snapshot.status in ("qmt_disconnected", "no_data"):
            return snapshot
        if snapshot.status == "suspended" and snapshot.price is None:
            return snapshot
        return dataclasses.replace(
            snapshot,
            status="suspended",
            price=None,
            change=None,
            change_pct=None,
        )

    async def _refresh_suspensions(self) -> bool:
        """Re-query the 停牌 set for the subscribed union and update the overlay.

        NETWORK call (akshare via ``_suspension_provider``) — deliberately kept
        off the per-tick hot path: invoked only from the slow background loop,
        self-throttled to once per trading day + on union growth. Returns
        ``True`` when the set changed so the caller re-pushes snapshots (halted
        names never stream a tick, so they need an explicit re-push to flip to
        停牌). Failures are visible (logged + debug event) and non-fatal — the
        previous set is kept and quotes keep flowing without the overlay.
        """
        provider = self._suspension_provider
        if provider is None:
            return False
        union = self._current_union()
        if not union:
            return False
        asof = self._today()
        if asof == self._suspended_asof and union <= self._suspended_union:
            return False
        symbols = sorted(union)
        try:
            suspended = await provider(symbols, asof)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — visible, non-fatal
            self._log.warning(
                "quote_stream suspension refresh failed (%s): %s symbol_count=%d asof=%s",
                type(exc).__name__,
                exc,
                len(symbols),
                asof,
            )
            await emit_debug_event(
                "quote_stream_suspension_refresh_failed",
                {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "symbol_count": len(symbols),
                    "asof": asof,
                    "hint": "suspension event lookup raised; quotes keep flowing without the "
                    "停牌 overlay; check akshare reachability / the data events source",
                },
            )
            return False

        new_set = frozenset(suspended) & frozenset(union)
        changed = new_set != self._suspended
        self._suspended = new_set
        self._suspended_asof = asof
        self._suspended_union = frozenset(union)
        await emit_debug_event(
            "quote_stream_suspension_refreshed",
            {
                "suspended_count": len(new_set),
                "union_size": len(union),
                "asof": asof,
                "changed": changed,
                "hint": "halted symbols overlaid with status=suspended on served snapshots",
            },
        )
        self._log.info(
            "quote_stream suspension set refreshed asof=%s suspended=%d union=%d changed=%s",
            asof,
            len(new_set),
            len(union),
            changed,
        )
        return changed

    # ----- internal: snapshot helpers ---------------------------------

    async def _initial_snapshot(self, symbols: List[str]) -> List[QuoteSnapshot]:
        """Cache-first snapshot for a client's symbols.

        For connected qmt, cache misses are filled with a one-shot provider
        fetch (best-effort; provider failure is logged but does not block the
        register so the client still gets cached/placeholder rows).
        """
        if not self._has_connection:
            return [QuoteSnapshot(symbol=s, status="qmt_disconnected") for s in symbols]

        out: Dict[str, QuoteSnapshot] = {}
        missing: List[str] = []
        for symbol in symbols:
            cached = self._cache.get(symbol)
            if cached is not None:
                out[symbol] = cached
            else:
                missing.append(symbol)

        if missing and self._quote_provider is not None:
            try:
                fetched = await self._quote_provider.fetch_quotes(missing)
            except Exception as exc:  # noqa: BLE001 — visible, non-fatal
                self._log.warning(
                    "quote_stream initial snapshot fetch failed (%s): %s symbol_count=%d",
                    type(exc).__name__,
                    exc,
                    len(missing),
                )
                await emit_debug_event(
                    "quote_stream_snapshot_fetch_failed",
                    {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "symbol_count": len(missing),
                        "hint": "one-shot provider fetch for initial snapshot failed; "
                        "client gets cached/placeholder rows, stream will fill in",
                    },
                )
                fetched = {}
            for symbol in missing:
                snap = fetched.get(symbol)
                out[symbol] = snap if snap is not None else QuoteSnapshot(
                    symbol=symbol, status="no_data"
                )
        else:
            for symbol in missing:
                out[symbol] = QuoteSnapshot(symbol=symbol, status="no_data")

        return [self._overlay_suspension(out[symbol]) for symbol in symbols]

    # ----- internal: background subscription --------------------------

    def _current_union(self) -> frozenset[str]:
        union: set[str] = set(self._monitored)
        for handle in self._clients:
            union |= handle.symbols
        return frozenset(union)

    # ----- in-process observers (monitoring daemon) -------------------

    def add_snapshot_observer(self, observer: SnapshotObserver) -> None:
        """Register an in-process observer notified of every cached snapshot."""
        if observer not in self._snapshot_observers:
            self._snapshot_observers.append(observer)

    def remove_snapshot_observer(self, observer: SnapshotObserver) -> None:
        try:
            self._snapshot_observers.remove(observer)
        except ValueError:
            pass

    async def set_monitored_symbols(self, symbols: set[str]) -> None:
        """Pin a symbol set into the upstream subscription (monitor daemon).

        These symbols stay subscribed regardless of WS clients, so a monitor
        keeps receiving ticks with the browser closed. Rebuilds the upstream
        task when the resulting union changes.
        """
        new_monitored = frozenset(str(s) for s in symbols)
        async with self._lock:
            if new_monitored == self._monitored:
                return
            self._monitored = new_monitored
            await self._rebuild_stream_task_locked(reason="monitor_symbols_changed")

    async def _rebuild_stream_task_locked(self, *, reason: str) -> None:
        """(Re)build the background task when the symbol union changes.

        Must be called with ``self._lock`` held. In dynamic mode the default
        account is re-resolved first so an account change is picked up by any
        caller (register / update / monitor) without waiting for the poll.
        No-op when qmt is not connected (no streaming) or the union is unchanged.
        """
        if self._dynamic_mode:
            await self._refresh_connection_locked()
        union = self._current_union()
        if union == self._subscribed_union and self._stream_task is not None:
            return
        if not self._has_connection or self._ws_subscribe is None:
            # No streaming path; just track the union so register-time logic
            # stays consistent. qmt_disconnected frames are sent on register.
            self._subscribed_union = union
            if not self._has_connection:
                await emit_debug_event(
                    "quote_stream_qmt_unavailable",
                    {
                        "reason": reason,
                        "symbol_count": len(union),
                        "hint": "qmt not connected; no upstream subscription started, "
                        "clients receive qmt_disconnected status frames",
                    },
                )
            return

        await self._cancel_stream_task_locked()
        self._subscribed_union = union
        if not union:
            # Nothing to subscribe to; leave the task absent until a client
            # subscribes to at least one symbol.
            return

        await emit_debug_event(
            "quote_stream_resubscribe",
            {
                "reason": reason,
                "symbol_count": len(union),
                "hint": "symbol union changed; rebuilding upstream qmt subscription",
            },
        )
        self._log.info(
            "quote_stream resubscribe reason=%s symbol_count=%d", reason, len(union)
        )
        self._stream_task = asyncio.create_task(
            self._run_stream(sorted(union)), name="quote_stream_subscribe"
        )

    async def _cancel_stream_task_locked(self) -> None:
        task = self._stream_task
        self._stream_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run_stream(self, symbols: List[str]) -> None:
        """Background loop: subscribe, consume frames, fan-out, retry on drop.

        Runs until cancelled (union change / aclose). Each upstream
        disconnect / error is surfaced as a structured event and retried with
        a bounded backoff. The loop exits without retry only on cancellation
        or when the service is closing.
        """
        attempt = 0
        while not self._closing:
            try:
                with data_span("quote_stream", "subscribe"):
                    await emit_debug_event(
                        "quote_stream_subscribe_open",
                        {
                            "symbol_count": len(symbols),
                            "hint": "opening upstream qmt quote subscription",
                        },
                    )
                    stream = self._ws_subscribe(symbols)  # type: ignore[misc]
                    async for quote_data in stream:
                        attempt = 0  # any frame resets the backoff
                        await self._on_quote_data(quote_data)
                # Iterator ended cleanly (upstream closed the stream).
                if self._closing:
                    return
                attempt += 1
                await self._handle_upstream_drop(
                    symbols, attempt, reason="stream_ended", exc=None
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — visible, retried
                if self._closing:
                    return
                attempt += 1
                await self._handle_upstream_drop(
                    symbols, attempt, reason="stream_error", exc=exc
                )

    async def _handle_upstream_drop(
        self,
        symbols: List[str],
        attempt: int,
        *,
        reason: str,
        exc: Optional[BaseException],
    ) -> None:
        delay = _BACKOFF_SCHEDULE_SECONDS[
            min(attempt - 1, len(_BACKOFF_SCHEDULE_SECONDS) - 1)
        ]
        error_type = type(exc).__name__ if exc is not None else None
        await emit_debug_event(
            "quote_stream_upstream_disconnected",
            {
                "reason": reason,
                "error_type": error_type,
                "error": str(exc) if exc is not None else None,
                "symbol_count": len(symbols),
                "attempt": attempt,
                "retry_in_seconds": delay,
                "hint": "qmt upstream quote stream dropped; backing off and reconnecting",
            },
        )
        if exc is not None:
            self._log.warning(
                "quote_stream upstream disconnected reason=%s (%s): %s "
                "symbol_count=%d attempt=%d retry_in=%.1fs",
                reason,
                error_type,
                exc,
                len(symbols),
                attempt,
                delay,
            )
        else:
            self._log.info(
                "quote_stream upstream stream ended reason=%s symbol_count=%d "
                "attempt=%d retry_in=%.1fs",
                reason,
                len(symbols),
                attempt,
                delay,
            )
        await asyncio.sleep(delay)

    async def _on_quote_data(self, quote_data: Any) -> None:
        """Map one upstream ``QuoteData`` to a snapshot, cache it, fan it out."""
        symbol = getattr(quote_data, "stock_code", None)
        if symbol is None:
            await emit_debug_event(
                "quote_stream_frame_dropped",
                {
                    "reason": "missing_stock_code",
                    "hint": "upstream QuoteData had no stock_code; cannot route to clients",
                },
            )
            self._log.info("quote_stream dropped frame reason=missing_stock_code")
            return
        tick = _quote_data_to_tick(quote_data)
        snapshot = self._overlay_suspension(
            quote_snapshot_from_tick(
                symbol, tick, timestamp=getattr(quote_data, "timestamp", None)
            )
        )
        self._cache[symbol] = snapshot
        await self._notify_observers(symbol, snapshot)
        await self._fanout(symbol, snapshot)

    async def _notify_observers(self, symbol: str, snapshot: QuoteSnapshot) -> None:
        """Notify in-process observers (monitor daemon). One raising observer is
        isolated (logged + structured event) so it cannot stall the stream or the
        WS broadcast — mirrors the ``_safe_send`` discipline for WS clients."""
        for observer in list(self._snapshot_observers):
            try:
                await observer(symbol, snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — visible, isolates bad observer
                self._log.warning(
                    "monitor_observer_failed (%s): %s symbol=%s",
                    type(exc).__name__,
                    exc,
                    symbol,
                )
                await emit_debug_event(
                    "monitor_observer_failed",
                    {
                        "symbol": symbol,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "hint": "snapshot observer raised; isolated so the stream keeps running",
                    },
                )

    async def _fanout(self, symbol: str, snapshot: QuoteSnapshot) -> None:
        """Send a quote frame to every client subscribed to ``symbol``.

        A client whose ``send`` raises is logged and queued for removal so a
        single dead socket cannot stall the broadcast.
        """
        frame = {"type": "quote", "quote": snapshot.to_dict()}
        dead: List[_ClientHandle] = []
        for handle in list(self._clients):
            if symbol not in handle.symbols:
                continue
            ok = await self._safe_send(handle, frame)
            if not ok:
                dead.append(handle)
        for handle in dead:
            await self.unregister(handle)

    async def _safe_send(self, handle: _ClientHandle, frame: dict) -> bool:
        """Send one frame to one client; return False (and log) on failure."""
        try:
            await handle.send(frame)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — visible, isolates bad client
            self._log.warning(
                "quote_stream client send failed (%s): %s frame_type=%s",
                type(exc).__name__,
                exc,
                frame.get("type"),
            )
            await emit_debug_event(
                "quote_stream_client_send_failed",
                {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "frame_type": frame.get("type"),
                    "hint": "WS client send raised; client will be unregistered",
                },
            )
            return False


def _first_level(seq: Any, *, field: str, symbol: str) -> int | None:
    """Project the level-1 entry of a qmt order-book list (买一/卖一量).

    Returns ``seq[0]`` for a non-empty list/tuple, ``None`` when absent. A
    present-but-malformed value (not a list) is a schema violation: we log a
    WARNING with the type and return ``None`` rather than fabricating a 0 seal
    (CLAUDE.md §错误可见性 — a fabricated seal would false-fire 涨停大减).
    """
    if seq is None:
        return None
    if isinstance(seq, (list, tuple)):
        if not seq:
            return None
        first = seq[0]
        if first is None or isinstance(first, bool):
            return None
        try:
            return int(float(first))
        except (TypeError, ValueError):
            logger.warning(
                "monitor_seal_field_malformed symbol=%s field=%s type=%s value=%r "
                "hint=order-book level-1 not numeric; seal volume forwarded as None",
                symbol,
                field,
                type(first).__name__,
                first,
            )
            return None
    logger.warning(
        "monitor_seal_field_malformed symbol=%s field=%s type=%s "
        "hint=order-book field is not a list; seal volume forwarded as None",
        symbol,
        field,
        type(seq).__name__,
    )
    return None


def _quote_data_to_tick(quote_data: Any) -> dict:
    """Project a qmt ``QuoteData`` onto the tick-dict shape the mapper reads.

    The streaming ``QuoteData`` carries ``pre_close`` (昨收) where the REST
    ``TickData`` carries ``last_close``; ``quote_snapshot_from_tick`` accepts
    both. We forward ``pre_close`` straight through, plus the level-1 seal
    volumes (``bid_vol[0]`` / ``ask_vol[0]``) so realtime monitoring can judge
    涨停/跌停封单 strength — these were historically dropped here.
    """
    symbol = getattr(quote_data, "stock_code", "") or ""
    return {
        "last_price": getattr(quote_data, "last_price", None),
        "pre_close": getattr(quote_data, "pre_close", None),
        "open": getattr(quote_data, "open", None),
        "high": getattr(quote_data, "high", None),
        "low": getattr(quote_data, "low", None),
        "volume": getattr(quote_data, "volume", None),
        "amount": getattr(quote_data, "amount", None),
        "bid_vol1": _first_level(getattr(quote_data, "bid_vol", None), field="bid_vol", symbol=symbol),
        "ask_vol1": _first_level(getattr(quote_data, "ask_vol", None), field="ask_vol", symbol=symbol),
    }


__all__ = ["QuoteStreamService"]
