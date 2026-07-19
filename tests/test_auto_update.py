"""Tests for the release-based self-update service (doyoutrade/infra/updater.py).

Covers: version tag parsing, install-kind detection, check_now outcomes
(update available / up-to-date / no releases / API failure visibility), apply
guards (dev checkout, no update, missing uv, missing restart hook, double
apply), the staged restart handoff, and the background loop honoring the hot
``auto_update.enabled`` toggle.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from unittest import mock

from doyoutrade.config import AutoUpdateSettings
from doyoutrade.infra import updater
from doyoutrade.infra.updater import (
    StagedUpdate,
    UpdateError,
    UpdateService,
    build_restart_command,
    detect_install_kind,
    parse_version_tag,
)


def _release_payload(tag: str = "v0.2.0") -> dict:
    return {
        "tag_name": tag,
        "name": tag,
        "published_at": "2026-07-01T00:00:00Z",
        "html_url": f"https://github.com/renjiegod/doyoutrade/releases/tag/{tag}",
        "body": "notes",
    }


def _service(
    payload: dict | None | Exception = None,
    *,
    version: str = "0.1.0",
    install_kind: str = "package",
    which=lambda name: f"/usr/bin/{name}",
    **kwargs,
) -> UpdateService:
    async def fetch(repo: str):
        if isinstance(payload, Exception):
            raise payload
        return payload

    return UpdateService(
        fetch_latest_release=fetch,
        install_kind=install_kind,
        version=version,
        which=which,
        **kwargs,
    )


def _run(coro):
    return asyncio.run(coro)


class VersionParsingTests(unittest.TestCase):
    def test_parse_plain_and_v_prefixed(self):
        self.assertEqual(parse_version_tag("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version_tag("1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version_tag("V0.10.0"), (0, 10, 0))
        self.assertEqual(parse_version_tag("2.0"), (2, 0))

    def test_reject_non_numeric_tags(self):
        for bad in ("v1.2-rc1", "release-2026", "", "v", "1.2.3.dev0", None):
            self.assertIsNone(parse_version_tag(bad), msg=repr(bad))

    def test_tuple_comparison_orders_versions(self):
        self.assertGreater(parse_version_tag("v0.10.0"), parse_version_tag("v0.9.9"))
        self.assertGreater(parse_version_tag("v1.0"), parse_version_tag("v0.99.99"))


class InstallKindTests(unittest.TestCase):
    def test_repo_checkout_is_source(self):
        # This test suite always runs from the git checkout.
        self.assertEqual(detect_install_kind(), "source")


class CheckNowTests(unittest.TestCase):
    def test_newer_release_flags_update_available(self):
        svc = _service(_release_payload("v0.2.0"), version="0.1.0")
        status = _run(svc.check_now())
        self.assertTrue(status["update_available"])
        self.assertEqual(status["latest"]["tag"], "v0.2.0")
        self.assertEqual(status["latest"]["version"], "0.2.0")
        self.assertIsNone(status["last_error"])
        self.assertIsNotNone(status["last_checked_at"])
        self.assertEqual(status["state"], "idle")

    def test_same_version_is_up_to_date(self):
        svc = _service(_release_payload("v0.1.0"), version="0.1.0")
        status = _run(svc.check_now())
        self.assertFalse(status["update_available"])
        self.assertEqual(status["latest"]["tag"], "v0.1.0")
        self.assertIsNone(status["last_error"])

    def test_older_release_is_not_an_update(self):
        svc = _service(_release_payload("v0.0.9"), version="0.1.0")
        status = _run(svc.check_now())
        self.assertFalse(status["update_available"])

    def test_no_releases_is_a_normal_state(self):
        svc = _service(None)
        status = _run(svc.check_now())
        self.assertFalse(status["update_available"])
        self.assertIsNone(status["latest"])
        self.assertIsNone(status["last_error"])
        # The returned snapshot reflects the FINISHED check (regression: the
        # early-return path used to report state="checking").
        self.assertEqual(status["state"], "idle")
        self.assertIsNotNone(status["last_checked_at"])

    def test_api_failure_lands_in_last_error_with_code(self):
        svc = _service(UpdateError("github_unreachable", "boom", hint="check network"))
        status = _run(svc.check_now())
        self.assertFalse(status["update_available"])
        self.assertEqual(status["last_error"]["error_code"], "github_unreachable")
        self.assertIn("boom", status["last_error"]["message"])
        self.assertEqual(status["last_error"]["hint"], "check network")

    def test_uncomparable_release_tag_is_a_visible_error(self):
        svc = _service(_release_payload("nightly-2026"))
        status = _run(svc.check_now())
        self.assertFalse(status["update_available"])
        self.assertEqual(status["last_error"]["error_code"], "invalid_release_tag")

    def test_unexpected_exception_is_wrapped_not_swallowed(self):
        svc = _service(RuntimeError("kaboom"))
        status = _run(svc.check_now())
        self.assertEqual(status["last_error"]["error_code"], "update_check_failed")
        self.assertIn("kaboom", status["last_error"]["message"])


class ApplyTests(unittest.TestCase):
    def _checked_service(self, **kwargs) -> UpdateService:
        svc = _service(_release_payload("v0.2.0"), version="0.1.0", **kwargs)
        _run(svc.check_now())
        return svc

    def test_source_checkout_is_refused(self):
        svc = self._checked_service(install_kind="source")
        svc.bind_restart_requester(lambda: None)
        with self.assertRaises(UpdateError) as ctx:
            _run(svc.apply())
        self.assertEqual(ctx.exception.error_code, "dev_checkout_unsupported")

    def test_no_update_available_is_refused(self):
        svc = _service(_release_payload("v0.1.0"), version="0.1.0")
        _run(svc.check_now())
        svc.bind_restart_requester(lambda: None)
        with self.assertRaises(UpdateError) as ctx:
            _run(svc.apply())
        self.assertEqual(ctx.exception.error_code, "no_update_available")

    def test_missing_uv_is_refused(self):
        svc = self._checked_service(which=lambda name: None)
        svc.bind_restart_requester(lambda: None)
        with self.assertRaises(UpdateError) as ctx:
            _run(svc.apply())
        self.assertEqual(ctx.exception.error_code, "uv_not_found")

    def test_unbound_restart_is_refused(self):
        svc = self._checked_service()
        with self.assertRaises(UpdateError) as ctx:
            _run(svc.apply())
        self.assertEqual(ctx.exception.error_code, "restart_unsupported")

    def test_apply_stages_update_and_requests_restart(self):
        async def scenario():
            svc = _service(_release_payload("v0.2.0"), version="0.1.0")
            await svc.check_now()
            restart_requested = asyncio.Event()
            svc.bind_restart_requester(restart_requested.set)
            status = await svc.apply()
            self.assertEqual(status["state"], "restarting")
            staged = svc.staged_update
            self.assertIsNotNone(staged)
            self.assertEqual(staged.tag, "v0.2.0")
            self.assertEqual(staged.uv_path, "/usr/bin/uv")
            # The restart is scheduled ~0.8s out so the HTTP response flushes.
            await asyncio.wait_for(restart_requested.wait(), timeout=5.0)
            # A second apply while staged is refused.
            with self.assertRaises(UpdateError) as ctx:
                await svc.apply()
            self.assertEqual(ctx.exception.error_code, "update_already_staged")

        _run(scenario())

    def test_apply_refusals_are_recorded_in_last_error(self):
        svc = self._checked_service()
        with self.assertRaises(UpdateError):
            _run(svc.apply())
        self.assertEqual(
            svc.status()["last_error"]["error_code"], "restart_unsupported"
        )


class RestartCommandTests(unittest.TestCase):
    def _staged(self) -> StagedUpdate:
        return StagedUpdate(
            repo="renjiegod/doyoutrade",
            tag="v0.2.0",
            version="0.2.0",
            uv_path="/usr/bin/uv",
            argv=("/home/x/.local/bin/doyoutrade", "--mode", "both"),
        )

    @unittest.skipIf(sys.platform == "win32", "posix shell shape")
    def test_posix_command_installs_then_relaunches(self):
        with mock.patch.object(updater.shutil, "which", return_value="/usr/bin/git"):
            command = build_restart_command(self._staged())
        self.assertEqual(command[:2], ["/bin/sh", "-c"])
        script = command[2]
        # PEP 508 direct reference (uv's --from rejects name[extra] args).
        self.assertIn(
            "tool install --force "
            "'doyoutrade @ git+https://github.com/renjiegod/doyoutrade.git@v0.2.0'",
            script,
        )
        # Relaunches whether or not the install succeeded (old version stays
        # intact on failure).
        self.assertEqual(script.count("exec /home/x/.local/bin/doyoutrade"), 2)
        self.assertIn("&&", script)
        self.assertIn("||", script)

    def test_requirement_uses_git_source_when_git_available(self):
        req = updater._install_requirement(
            self._staged(), platform="linux", which=lambda name: "/usr/bin/git"
        )
        self.assertEqual(
            req, "doyoutrade @ git+https://github.com/renjiegod/doyoutrade.git@v0.2.0"
        )

    def test_requirement_falls_back_to_tag_archive_without_git(self):
        # GUI-installed Windows machines usually have no git; uv would die
        # with "Git executable not found" on a git+ source.
        req = updater._install_requirement(
            self._staged(), platform="linux", which=lambda name: None
        )
        self.assertEqual(
            req,
            "doyoutrade @ "
            "https://github.com/renjiegod/doyoutrade/archive/refs/tags/v0.2.0.zip",
        )

    def test_requirement_keeps_qmt_proxy_extra_on_windows(self):
        # --force replaces the tool venv with exactly what is requested;
        # dropping the extra would strip the embedded qmt-proxy on update.
        req = updater._install_requirement(
            self._staged(), platform="win32", which=lambda name: "C:\\git\\git.exe"
        )
        self.assertEqual(
            req,
            "doyoutrade[qmt-proxy] @ "
            "git+https://github.com/renjiegod/doyoutrade.git@v0.2.0",
        )

    def test_requirement_windows_without_git_uses_archive_with_extra(self):
        req = updater._install_requirement(
            self._staged(), platform="win32", which=lambda name: None
        )
        self.assertEqual(
            req,
            "doyoutrade[qmt-proxy] @ "
            "https://github.com/renjiegod/doyoutrade/archive/refs/tags/v0.2.0.zip",
        )

    def test_relaunch_argv_falls_back_to_path_lookup(self):
        with mock.patch.object(sys, "argv", ["-"]):
            argv = updater._relaunch_argv(which=lambda name: "/usr/local/bin/doyoutrade")
        self.assertEqual(argv, ["/usr/local/bin/doyoutrade"])

    def test_relaunch_argv_keeps_launcher_argv(self):
        with mock.patch.object(sys, "argv", ["/usr/local/bin/doyoutrade", "--mode", "both"]):
            with mock.patch("os.path.exists", return_value=True):
                argv = updater._relaunch_argv(which=lambda name: None)
        self.assertEqual(argv, ["/usr/local/bin/doyoutrade", "--mode", "both"])


class BackgroundLoopTests(unittest.TestCase):
    def _cfg(self, enabled: bool) -> mock.Mock:
        cfg = mock.Mock()
        cfg.auto_update = AutoUpdateSettings(
            enabled=enabled, check_interval_hours=6.0, repo="renjiegod/doyoutrade"
        )
        return cfg

    def test_loop_checks_when_enabled_and_skips_when_disabled(self):
        async def scenario():
            fetched = asyncio.Event()

            async def fetch(repo: str):
                fetched.set()
                return _release_payload("v0.2.0")

            svc = UpdateService(
                fetch_latest_release=fetch,
                install_kind="package",
                version="0.1.0",
                tick_seconds=1.0,
                initial_delay_seconds=0.0,
            )
            with mock.patch.object(updater, "get_config", return_value=self._cfg(False)):
                svc.start()
                # Cover one full tick: disabled → the loop must not fetch.
                await asyncio.sleep(1.3)
                self.assertFalse(fetched.is_set())
                await svc.stop()

            with mock.patch.object(updater, "get_config", return_value=self._cfg(True)):
                svc = UpdateService(
                    fetch_latest_release=fetch,
                    install_kind="package",
                    version="0.1.0",
                    tick_seconds=1.0,
                    initial_delay_seconds=0.0,
                )
                svc.start()
                await asyncio.wait_for(fetched.wait(), timeout=5.0)
                await svc.stop()
                self.assertTrue(svc.status()["update_available"])

        _run(scenario())


if __name__ == "__main__":
    unittest.main()
