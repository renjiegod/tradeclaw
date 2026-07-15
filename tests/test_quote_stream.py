"""Dynamic-reconnection tests for :class:`QuoteStreamService`.

These lock in the behavior added to make the watchlist banner
"行情未连接（需配置默认 QMT 账户）" clear automatically once the operator
configures the default QMT account at runtime — i.e. the service must not be
frozen into the disconnected state captured at server startup.

Coverage:
* static-mode backward compatibility (no resolver/factory → behaves as before,
  ``refresh()`` is a no-op).
* dynamic mode starts disconnected when the resolver returns ``None``.
* configuring a default account with a ``base_url`` flips the service to
  connected and rebuilds the provider via the factory.
* already-connected WS clients receive a fresh ``snapshot`` frame on the
  connected-flip (so the frontend banner clears without a page reload).
* a subsequent account change (different terminal / base_url) tears down the
  old connection (``aclose`` invoked) and rebuilds.
* dropping the default account flips back to disconnected and pushes a
  ``qmt_disconnected`` status frame to existing clients.
* signature is fingerprint-stable: identical record → no rebuild, no frame.
* resolver exceptions are swallowed (logged) and do not mutate state.
"""

from __future__ import annotations

import asyncio
import unittest

from doyoutrade.core.models import QuoteSnapshot
from doyoutrade.data.quote_stream import _FALLBACK_SIG, QuoteStreamService


def _record(*, base_url="http://qmt-proxy-host:8000", token="t", terminal="T1", timeout=30.0):
    return {
        "id": "acct-x",
        "name": "test",
        "mode": "live",
        "base_url": base_url,
        "token": token,
        "qmt_terminal_id": terminal,
        "timeout_seconds": timeout,
        "qmt_account_id": "A1",
        "session_id": "s1",
        "mock_cash": 0.0,
        "mock_equity": 0.0,
        "mock_positions": [],
    }


class _Conn:
    """Captures factory calls + provides an aclose that records its invocation."""

    def __init__(self):
        self.built = []
        self.closed = []

    def factory(self, account):
        self.built.append((account.base_url, account.qmt_terminal_id))

        async def aclose():
            self.closed.append((account.base_url, account.qmt_terminal_id))

        # No real provider/ws_subscribe — these tests don't drive ticks.
        return None, None, aclose


def _send_spy(bucket: list):
    async def _send(frame):
        bucket.append(frame)

    return _send


class StaticModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_static_build(self):
        svc = QuoteStreamService(
            quote_provider=None, ws_subscribe=None, has_connection=False
        )
        await svc.start()
        self.assertFalse(svc._has_connection)

        received = []
        h = await svc.register(_send_spy(received), ["000001.SZ"])
        # snapshot + qmt_disconnected status frame, in that order.
        self.assertEqual([f["type"] for f in received], ["snapshot", "status"])
        self.assertEqual(received[-1]["status"], "qmt_disconnected")
        await svc.unregister(h)
        await svc.aclose()

    async def test_refresh_is_noop_in_static_mode(self):
        svc = QuoteStreamService(
            quote_provider=None, ws_subscribe=None, has_connection=False
        )
        await svc.start()
        self.assertFalse(await svc.refresh())
        await svc.aclose()


class DynamicModeTests(unittest.IsolatedAsyncioTestCase):
    async def _build(self, initial_record=None):
        state = {"record": initial_record}

        async def resolver():
            return state["record"]

        conn = _Conn()
        svc = QuoteStreamService(
            account_resolver=resolver,
            connection_factory=conn.factory,
            refresh_interval_seconds=9999,  # disable the poll loop
        )
        await svc.start()
        return svc, state, conn

    async def test_starts_disconnected_when_no_default_account(self):
        svc, _state, conn = await self._build(initial_record=None)
        self.assertFalse(svc._has_connection)
        self.assertIsNone(svc._connection_signature)
        self.assertEqual(conn.built, [])
        await svc.aclose()

    async def test_configuring_account_connects_and_pushes_snapshot(self):
        svc, state, conn = await self._build(initial_record=None)
        received = []
        h = await svc.register(_send_spy(received), ["000001.SZ"])
        # Initial register: disconnected snapshot + status.
        self.assertEqual(
            [f["type"] for f in received], ["snapshot", "status"]
        )
        self.assertEqual(received[-1]["status"], "qmt_disconnected")

        # Configure the default account at runtime.
        state["record"] = _record()
        changed = await svc.refresh()
        self.assertTrue(changed)
        self.assertTrue(svc._has_connection)
        self.assertIsNotNone(svc._connection_signature)
        # Factory was invoked once with the new account.
        self.assertEqual(conn.built, [("http://qmt-proxy-host:8000", "T1")])
        # Existing client got a fresh snapshot frame (clears the banner).
        self.assertEqual(
            [f["type"] for f in received],
            ["snapshot", "status", "snapshot"],
        )

        await svc.unregister(h)
        await svc.aclose()

    async def test_signature_change_rebuilds_and_invokes_aclose(self):
        svc, state, conn = await self._build(initial_record=_record())

        # Flip the terminal id — same base_url, different signature.
        state["record"] = _record(terminal="T2")
        changed = await svc.refresh()
        self.assertTrue(changed)
        # Old connection was torn down (aclose invoked) then new one built.
        self.assertEqual(conn.built, [("http://qmt-proxy-host:8000", "T1"), ("http://qmt-proxy-host:8000", "T2")])
        self.assertEqual(conn.closed, [("http://qmt-proxy-host:8000", "T1")])

        await svc.aclose()
        # aclose on service teardown also closes the active connection.
        self.assertEqual(conn.closed, [("http://qmt-proxy-host:8000", "T1"), ("http://qmt-proxy-host:8000", "T2")])

    async def test_dropping_account_disconnects_and_pushes_status(self):
        svc, state, conn = await self._build(initial_record=_record())
        received = []
        h = await svc.register(_send_spy(received), ["000001.SZ"])
        # Initial connected register → snapshot only.
        self.assertEqual([f["type"] for f in received], ["snapshot"])

        state["record"] = None
        changed = await svc.refresh()
        self.assertTrue(changed)
        self.assertFalse(svc._has_connection)
        # Existing client got a qmt_disconnected status frame.
        self.assertEqual(
            [f["type"] for f in received], ["snapshot", "status"]
        )
        self.assertEqual(received[-1]["status"], "qmt_disconnected")

        await svc.unregister(h)
        await svc.aclose()

    async def test_identical_record_is_idempotent(self):
        svc, state, conn = await self._build(initial_record=_record())
        received = []
        h = await svc.register(_send_spy(received), ["000001.SZ"])
        self.assertEqual(conn.built, [("http://qmt-proxy-host:8000", "T1")])

        # Same record again → no rebuild, no broadcast.
        changed = await svc.refresh()
        self.assertFalse(changed)
        self.assertEqual(conn.built, [("http://qmt-proxy-host:8000", "T1")])  # unchanged
        # No additional frames pushed.
        self.assertEqual([f["type"] for f in received], ["snapshot"])

        await svc.unregister(h)
        await svc.aclose()

    async def test_resolver_exception_does_not_mutate_state(self):
        svc, state, conn = await self._build(initial_record=_record())
        self.assertTrue(svc._has_connection)

        async def boom():
            raise RuntimeError("db down")

        svc._account_resolver = boom
        changed = await svc.refresh()
        self.assertFalse(changed)
        # State preserved — still connected with the prior signature.
        self.assertTrue(svc._has_connection)
        self.assertEqual(conn.closed, [])  # nothing torn down

        await svc.aclose()

    async def test_factory_exception_stays_disconnected_visible(self):
        # Factory raises → service flips to disconnected and emits the structured
        # event (visible failure, never silently swallowed).
        state = {"record": _record()}

        async def resolver():
            return state["record"]

        def factory(account):
            raise RuntimeError("ws client build failed")

        svc = QuoteStreamService(
            account_resolver=resolver,
            connection_factory=factory,
            refresh_interval_seconds=9999,
        )
        await svc.start()
        # First refresh inside start() flipped it to disconnected (factory raised).
        self.assertFalse(svc._has_connection)
        await svc.aclose()


class _FakeQuoteProvider:
    """Minimal RealtimeQuoteProvider returning preconfigured snapshots."""

    def __init__(self, snapshots):
        self._snaps = dict(snapshots)

    async def fetch_quotes(self, symbols):
        return {s: self._snaps[s] for s in symbols if s in self._snaps}


def _flat_quote(symbol="688146.SH"):
    # The 停牌 representation B: qmt streams last_price == prev_close with zero
    # volume, which reads as a benign 0% move unless overlaid as suspended.
    return QuoteSnapshot(
        symbol=symbol,
        price=389.99,
        prev_close=389.99,
        change=0.0,
        change_pct=0.0,
        volume=0.0,
        status="ok",
    )


class SuspensionOverlayTests(unittest.IsolatedAsyncioTestCase):
    """The 停牌 overlay: a suspension-event set sourced off the slow loop is
    applied (in-memory, hot-path-safe) to served snapshots so a halted name
    shows 停牌 even when qmt streams a flat last_price == prev_close tick."""

    async def test_overlay_via_fetch_once(self):
        provider = _FakeQuoteProvider({"688146.SH": _flat_quote()})

        async def susp(symbols, asof):
            return frozenset({"688146.SH"})

        svc = QuoteStreamService(
            quote_provider=provider, has_connection=True, suspension_provider=susp
        )
        svc._monitored = frozenset({"688146.SH"})  # populate the union
        self.assertTrue(await svc._refresh_suspensions())
        self.assertIn("688146.SH", svc._suspended)

        result = await svc.fetch_once(["688146.SH"])
        q = result["688146.SH"]
        self.assertEqual(q.status, "suspended")
        self.assertIsNone(q.price)
        self.assertIsNone(q.change)
        self.assertIsNone(q.change_pct)
        self.assertEqual(q.prev_close, 389.99)  #昨收保留

    async def test_refresh_throttled_same_day_same_union(self):
        calls = {"n": 0}

        async def susp(symbols, asof):
            calls["n"] += 1
            return frozenset({"688146.SH"})

        svc = QuoteStreamService(
            quote_provider=_FakeQuoteProvider({}), has_connection=True, suspension_provider=susp
        )
        svc._monitored = frozenset({"688146.SH"})
        self.assertTrue(await svc._refresh_suspensions())   # queries
        self.assertFalse(await svc._refresh_suspensions())  # throttled, no re-query
        self.assertEqual(calls["n"], 1)

        # Union growth re-queries even on the same day.
        svc._monitored = frozenset({"688146.SH", "600519.SH"})
        await svc._refresh_suspensions()
        self.assertEqual(calls["n"], 2)

    async def test_refresh_failure_is_visible_and_nonfatal(self):
        async def boom(symbols, asof):
            raise RuntimeError("akshare down")

        svc = QuoteStreamService(
            quote_provider=_FakeQuoteProvider({}), has_connection=True, suspension_provider=boom
        )
        svc._monitored = frozenset({"688146.SH"})
        # Does not raise; set stays empty; quotes keep flowing without overlay.
        self.assertFalse(await svc._refresh_suspensions())
        self.assertEqual(svc._suspended, frozenset())

    async def test_overlay_does_not_mask_placeholders(self):
        svc = QuoteStreamService(quote_provider=_FakeQuoteProvider({}), has_connection=True)
        svc._suspended = frozenset({"X.SH"})
        for placeholder in ("no_data", "qmt_disconnected"):
            snap = QuoteSnapshot(symbol="X.SH", status=placeholder)
            self.assertEqual(svc._overlay_suspension(snap).status, placeholder)

    async def test_overlay_noop_when_not_suspended(self):
        svc = QuoteStreamService(quote_provider=_FakeQuoteProvider({}), has_connection=True)
        svc._suspended = frozenset({"688146.SH"})
        other = _flat_quote("600519.SH")
        self.assertIs(svc._overlay_suspension(other), other)  # untouched

    async def test_no_provider_means_no_overlay(self):
        # Backward-compat: without a suspension provider the set stays empty and
        # refresh is a no-op, so served snapshots are unchanged.
        svc = QuoteStreamService(
            quote_provider=_FakeQuoteProvider({"688146.SH": _flat_quote()}),
            has_connection=True,
        )
        svc._monitored = frozenset({"688146.SH"})
        self.assertFalse(await svc._refresh_suspensions())
        q = (await svc.fetch_once(["688146.SH"]))["688146.SH"]
        self.assertEqual(q.status, "ok")
        self.assertEqual(q.change_pct, 0.0)


class FallbackProviderTests(unittest.IsolatedAsyncioTestCase):
    """A polling fallback (e.g. mootdx) serves quotes when no qmt account exists."""

    async def _build(self, *, record, fallback):
        state = {"record": record}

        async def resolver():
            return state["record"]

        def factory(account):
            raise AssertionError("connection_factory must not run on the fallback path")

        svc = QuoteStreamService(
            account_resolver=resolver,
            connection_factory=factory,
            refresh_interval_seconds=9999,
            fallback_provider=fallback,
        )
        await svc.start()
        return svc, state

    async def test_no_account_uses_fallback_polling(self):
        fallback = _FakeQuoteProvider(
            {"600036.SH": QuoteSnapshot(symbol="600036.SH", price=10.0, status="ok")}
        )
        svc, _state = await self._build(record=None, fallback=fallback)
        await svc.refresh()
        # Connected via fallback (not qmt): has_connection True, no ws push.
        self.assertTrue(svc._has_connection)
        self.assertIsNone(svc._ws_subscribe)
        quotes = await svc.fetch_once(["600036.SH"])
        self.assertEqual(quotes["600036.SH"].status, "ok")
        self.assertAlmostEqual(quotes["600036.SH"].price, 10.0)
        await svc.aclose()

    async def test_account_without_base_url_uses_fallback(self):
        fallback = _FakeQuoteProvider(
            {"600036.SH": QuoteSnapshot(symbol="600036.SH", price=11.0, status="ok")}
        )
        svc, _state = await self._build(record=_record(base_url=""), fallback=fallback)
        await svc.refresh()
        self.assertTrue(svc._has_connection)
        quotes = await svc.fetch_once(["600036.SH"])
        self.assertAlmostEqual(quotes["600036.SH"].price, 11.0)
        await svc.aclose()

    async def test_flip_to_qmt_account_leaves_fallback(self):
        fallback = _FakeQuoteProvider(
            {"600036.SH": QuoteSnapshot(symbol="600036.SH", price=10.0, status="ok")}
        )
        conn = _Conn()
        state = {"record": None}

        async def resolver():
            return state["record"]

        svc = QuoteStreamService(
            account_resolver=resolver,
            connection_factory=conn.factory,
            refresh_interval_seconds=9999,
            fallback_provider=fallback,
        )
        await svc.start()
        await svc.refresh()
        # On the fallback path the factory is never called.
        self.assertEqual(svc._connection_signature, _FALLBACK_SIG)
        self.assertIs(svc._quote_provider, fallback)
        self.assertEqual(conn.built, [])
        # A real qmt account appears → leaves the fallback, builds via factory.
        state["record"] = _record()
        await svc.refresh()
        self.assertTrue(svc._has_connection)
        self.assertNotEqual(svc._connection_signature, _FALLBACK_SIG)
        self.assertEqual(conn.built, [("http://qmt-proxy-host:8000", "T1")])
        await svc.aclose()


if __name__ == "__main__":
    unittest.main()
