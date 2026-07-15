"""Launch-mode selection, embedded qmt-proxy wiring, and the first-run
qmt-proxy address prompt.

Covers the ``doyoutrade --mode {doyoutrade,qmt-proxy,both}`` entry:
- OS-derived default + flag + ``DOYOUTRADE_LAUNCH_MODE`` env resolution.
- ``both``-mode auto-wiring of the default account's base_url to the embedded
  proxy (create / patch / leave-as-is).
- Importability of the bundled qmt-proxy app off-Windows (mock mode).
- The doyoutrade-only wizard's remote-qmt-proxy prompt.
"""

import os
import unittest
from unittest.mock import patch

from doyoutrade import onboarding
from doyoutrade.api import server
from doyoutrade.config import QmtProxySettings


class _FakeAccountRepo:
    """Minimal stand-in for SqlAlchemyAccountRepository used by auto-wire /
    the wizard. Tracks upserts so tests can assert on the written payload."""

    def __init__(self, accounts=None):
        self.accounts = list(accounts or [])
        self.upserts: list[dict] = []

    async def get_default_account(self):
        for a in self.accounts:
            if a.get("is_default"):
                return dict(a)
        return None

    async def list_accounts(self):
        return [dict(a) for a in self.accounts]

    async def upsert_account(self, data: dict):
        self.upserts.append(dict(data))
        if data.get("id"):
            for a in self.accounts:
                if a.get("id") == data["id"]:
                    a.update(data)
                    return dict(a)
        rec = {"id": data.get("id") or f"acct-{len(self.accounts) + 1}", **data}
        self.accounts.append(rec)
        return dict(rec)


class ResolveLaunchModeTests(unittest.TestCase):
    def setUp(self):
        # Isolate the env var each test touches.
        self._saved = os.environ.pop("DOYOUTRADE_LAUNCH_MODE", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["DOYOUTRADE_LAUNCH_MODE"] = self._saved
        else:
            os.environ.pop("DOYOUTRADE_LAUNCH_MODE", None)

    def test_default_is_both_on_windows(self):
        with patch.object(server.sys, "platform", "win32"):
            mode, port = server._resolve_launch_mode([])
        self.assertEqual(mode, "both")
        self.assertIsNone(port)

    def test_default_is_doyoutrade_off_windows(self):
        with patch.object(server.sys, "platform", "darwin"):
            mode, _ = server._resolve_launch_mode([])
        self.assertEqual(mode, "doyoutrade")

    def test_flag_overrides_os_default(self):
        with patch.object(server.sys, "platform", "darwin"):
            mode, _ = server._resolve_launch_mode(["--mode", "both"])
        self.assertEqual(mode, "both")

    def test_qmt_port_override_parsed(self):
        mode, port = server._resolve_launch_mode(["--mode", "qmt-proxy", "--qmt-port", "9001"])
        self.assertEqual(mode, "qmt-proxy")
        self.assertEqual(port, 9001)

    def test_env_var_used_when_no_flag(self):
        os.environ["DOYOUTRADE_LAUNCH_MODE"] = "qmt-proxy"
        with patch.object(server.sys, "platform", "win32"):
            mode, _ = server._resolve_launch_mode([])
        self.assertEqual(mode, "qmt-proxy")

    def test_flag_beats_env_var(self):
        os.environ["DOYOUTRADE_LAUNCH_MODE"] = "qmt-proxy"
        mode, _ = server._resolve_launch_mode(["--mode", "doyoutrade"])
        self.assertEqual(mode, "doyoutrade")

    def test_bad_env_var_rejected(self):
        os.environ["DOYOUTRADE_LAUNCH_MODE"] = "bogus"
        with self.assertRaises(ValueError):
            server._resolve_launch_mode([])


class EmbeddedBaseUrlTests(unittest.TestCase):
    def test_wildcard_bind_maps_to_loopback(self):
        self.assertEqual(server._embedded_base_url("0.0.0.0", 8001), "http://127.0.0.1:8001")
        self.assertEqual(server._embedded_base_url("::", 8001), "http://127.0.0.1:8001")
        self.assertEqual(server._embedded_base_url("", 8001), "http://127.0.0.1:8001")

    def test_concrete_host_preserved(self):
        self.assertEqual(server._embedded_base_url("192.168.1.5", 8001), "http://192.168.1.5:8001")


class AutoWireQmtBaseUrlTests(unittest.IsolatedAsyncioTestCase):
    def _settings(self):
        return QmtProxySettings(host="127.0.0.1", port=8001, local_token="embedded-local")

    async def test_creates_default_account_when_none(self):
        repo = _FakeAccountRepo(accounts=[])
        await server._auto_wire_qmt_base_url({"account_repository": repo}, self._settings())
        self.assertEqual(len(repo.upserts), 1)
        payload = repo.upserts[0]
        self.assertEqual(payload["base_url"], "http://127.0.0.1:8001")
        self.assertEqual(payload["token"], "embedded-local")
        self.assertEqual(payload["mode"], "mock")
        self.assertTrue(payload["is_default"])

    async def test_patches_default_account_missing_base_url(self):
        repo = _FakeAccountRepo(
            accounts=[{"id": "acct-x", "is_default": True, "base_url": "", "token": "keep-me"}]
        )
        await server._auto_wire_qmt_base_url({"account_repository": repo}, self._settings())
        self.assertEqual(len(repo.upserts), 1)
        payload = repo.upserts[0]
        self.assertEqual(payload["id"], "acct-x")
        self.assertEqual(payload["base_url"], "http://127.0.0.1:8001")
        # Existing token is preserved rather than clobbered.
        self.assertEqual(payload["token"], "keep-me")

    async def test_leaves_account_with_existing_base_url(self):
        repo = _FakeAccountRepo(
            accounts=[{"id": "acct-x", "is_default": True, "base_url": "http://remote:8001"}]
        )
        await server._auto_wire_qmt_base_url({"account_repository": repo}, self._settings())
        self.assertEqual(repo.upserts, [])

    async def test_missing_repo_is_non_fatal(self):
        # No account_repository in runtime → logs a warning, does not raise.
        await server._auto_wire_qmt_base_url({}, self._settings())


class EmbeddedImportTests(unittest.TestCase):
    def test_qmt_proxy_app_imports_in_mock_mode(self):
        # Validates the bundle is locatable from a source checkout and that the
        # qmt-proxy app imports off-Windows (xtquant absent → mock/degraded).
        os.environ["APP_MODE"] = "mock"
        from doyoutrade.infra.qmt_proxy_server import load_qmt_proxy_app

        app = load_qmt_proxy_app("mock")
        paths = {getattr(r, "path", None) for r in app.routes}
        self.assertIn("/health/", paths)
        self.assertTrue(any(str(p).startswith("/api/v1/data") for p in paths if p))


class QmtProxyPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_registers_account_from_prompt(self):
        repo = _FakeAccountRepo(accounts=[])
        runtime = {"account_repository": repo}
        # base_url answer, then token answer.
        inputs = iter(["http://192.168.1.10:8001", "tok-123"])
        with patch.object(onboarding, "_ask", lambda *a, **k: next(inputs)):
            await onboarding._maybe_prompt_qmt_proxy(runtime)
        self.assertEqual(len(repo.upserts), 1)
        payload = repo.upserts[0]
        self.assertEqual(payload["base_url"], "http://192.168.1.10:8001")
        self.assertEqual(payload["token"], "tok-123")
        self.assertTrue(payload["is_default"])

    async def test_blank_answer_skips(self):
        repo = _FakeAccountRepo(accounts=[])
        with patch.object(onboarding, "_ask", lambda *a, **k: ""):
            await onboarding._maybe_prompt_qmt_proxy({"account_repository": repo})
        self.assertEqual(repo.upserts, [])

    async def test_bare_host_gets_http_scheme(self):
        repo = _FakeAccountRepo(accounts=[])
        inputs = iter(["192.168.1.10:8001", ""])
        with patch.object(onboarding, "_ask", lambda *a, **k: next(inputs)):
            await onboarding._maybe_prompt_qmt_proxy({"account_repository": repo})
        self.assertEqual(repo.upserts[0]["base_url"], "http://192.168.1.10:8001")
        self.assertIsNone(repo.upserts[0]["token"])

    async def test_does_not_nag_when_base_url_account_exists(self):
        repo = _FakeAccountRepo(accounts=[{"id": "a", "base_url": "http://x:8001"}])
        with patch.object(onboarding, "_ask", lambda *a, **k: "should-not-be-asked"):
            await onboarding._maybe_prompt_qmt_proxy({"account_repository": repo})
        self.assertEqual(repo.upserts, [])


if __name__ == "__main__":
    unittest.main()
