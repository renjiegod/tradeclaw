"""Tests for doyoutrade/infra/tray_icon.py.

The dev/CI machine running these tests is macOS/Linux, so ``pystray`` /
``Pillow`` are not installed here (they ship only in the Windows-only
``qmt-proxy`` extra guarded by ``sys_platform == 'win32'`` — see
pyproject.toml). This file therefore covers two things:

1. The gating logic (non-win32 / DOYOUTRADE_TRAY unset / exceptions) fully,
   for real — no mocking of platform or env needed beyond monkeypatching
   ``sys.platform`` and the environment, which is honest coverage of the
   actual no-op contract.
2. The internal wiring of ``_start_tray_icon`` (menu construction, "打开控制台"
   opening the browser at the right URL, "退出 DoYouTrade" setting
   ``server.should_exit`` and stopping the icon, run_detached vs thread
   fallback) using hand-built fake ``pystray`` / ``PIL`` modules injected via
   ``sys.modules`` — this exercises the real code paths in
   ``doyoutrade/infra/tray_icon.py`` without needing a real Windows tray.

What this file CANNOT verify (no Windows GUI environment available here):
whether ``pystray.Icon`` actually renders/behaves correctly against a real
Windows shell, whether the drawn PIL image displays legibly at real tray
icon sizes, or any real user interaction with the tray menu. That remains
unverified until run on an actual Windows machine.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from doyoutrade.infra import tray_icon


class _FakeServer:
    def __init__(self):
        self.should_exit = False


class TrayIconGatingTests(unittest.TestCase):
    """No-op contract: only win32 + DOYOUTRADE_TRAY=1 actually starts anything."""

    def test_noop_on_non_windows_regardless_of_env(self):
        server = _FakeServer()
        with patch.object(tray_icon.sys, "platform", "darwin"), \
             patch.dict(tray_icon.os.environ, {"DOYOUTRADE_TRAY": "1"}), \
             patch.object(tray_icon, "_start_tray_icon") as start_mock:
            tray_icon.maybe_start_tray_icon(server, "127.0.0.1", 8000)
        start_mock.assert_not_called()

    def test_noop_on_windows_without_env_var(self):
        server = _FakeServer()
        with patch.object(tray_icon.sys, "platform", "win32"), \
             patch.dict(tray_icon.os.environ, {}, clear=False), \
             patch.object(tray_icon, "_start_tray_icon") as start_mock:
            tray_icon.os.environ.pop("DOYOUTRADE_TRAY", None)
            tray_icon.maybe_start_tray_icon(server, "127.0.0.1", 8000)
        start_mock.assert_not_called()

    def test_noop_on_windows_with_env_var_not_exactly_one(self):
        server = _FakeServer()
        with patch.object(tray_icon.sys, "platform", "win32"), \
             patch.dict(tray_icon.os.environ, {"DOYOUTRADE_TRAY": "true"}), \
             patch.object(tray_icon, "_start_tray_icon") as start_mock:
            tray_icon.maybe_start_tray_icon(server, "127.0.0.1", 8000)
        start_mock.assert_not_called()

    def test_starts_on_windows_with_env_var_set(self):
        server = _FakeServer()
        with patch.object(tray_icon.sys, "platform", "win32"), \
             patch.dict(tray_icon.os.environ, {"DOYOUTRADE_TRAY": "1"}), \
             patch.object(tray_icon, "_start_tray_icon") as start_mock:
            tray_icon.maybe_start_tray_icon(server, "127.0.0.1", 8000)
        start_mock.assert_called_once_with(server, "127.0.0.1", 8000)

    def test_start_failure_is_caught_and_logged_not_raised(self):
        server = _FakeServer()
        with patch.object(tray_icon.sys, "platform", "win32"), \
             patch.dict(tray_icon.os.environ, {"DOYOUTRADE_TRAY": "1"}), \
             patch.object(
                 tray_icon, "_start_tray_icon", side_effect=RuntimeError("no display")
             ), \
             patch.object(tray_icon.logger, "warning") as warn_mock:
            # Must not raise — a broken tray must never take startup down.
            tray_icon.maybe_start_tray_icon(server, "127.0.0.1", 8000)
        warn_mock.assert_called_once()
        args = warn_mock.call_args[0]
        self.assertIn("tray icon failed to start", args[0])
        self.assertIn("RuntimeError", args)


class _FakeMenuItem:
    def __init__(self, text, action, default=False):
        self.text = text
        self.action = action
        self.default = default


class _FakeMenu:
    def __init__(self, *items):
        self.items = list(items)


class _FakeIcon:
    instances: list["_FakeIcon"] = []

    def __init__(self, name, image, title, menu=None):
        self.name = name
        self.image = image
        self.title = title
        self.menu = menu
        self.stopped = False
        self.ran = False
        self.run_detached_called = False
        _FakeIcon.instances.append(self)

    def run(self):
        self.ran = True

    def run_detached(self):
        self.run_detached_called = True

    def stop(self):
        self.stopped = True


def _install_fake_pystray_and_pil():
    fake_pystray = types.ModuleType("pystray")
    fake_pystray.Icon = _FakeIcon
    fake_pystray.Menu = _FakeMenu
    fake_pystray.MenuItem = _FakeMenuItem

    fake_image_module = types.ModuleType("PIL.Image")

    class _FakeImage:
        def __init__(self, mode, size, color):
            self.mode = mode
            self.size = size
            self.color = color

    fake_image_module.new = lambda mode, size, color: _FakeImage(mode, size, color)

    fake_imagedraw_module = types.ModuleType("PIL.ImageDraw")

    class _FakeDraw:
        def __init__(self, image):
            self.image = image
            self.ellipse_calls: list[tuple] = []

        def ellipse(self, box, fill=None):
            self.ellipse_calls.append((box, fill))

    fake_imagedraw_module.Draw = lambda image: _FakeDraw(image)

    fake_pil = types.ModuleType("PIL")
    fake_pil.Image = fake_image_module
    fake_pil.ImageDraw = fake_imagedraw_module

    return {
        "pystray": fake_pystray,
        "PIL": fake_pil,
        "PIL.Image": fake_image_module,
        "PIL.ImageDraw": fake_imagedraw_module,
    }


class TrayIconWiringTests(unittest.TestCase):
    """Exercise _start_tray_icon's real logic against hand-built fake
    pystray/PIL modules (no real pystray/PIL installed on this dev machine)."""

    def setUp(self):
        _FakeIcon.instances.clear()

    def test_menu_open_console_opens_browser_at_expected_url(self):
        server = _FakeServer()
        fakes = _install_fake_pystray_and_pil()
        with patch.dict(sys.modules, fakes), \
             patch.object(tray_icon.webbrowser, "open") as open_mock:
            tray_icon._start_tray_icon(server, "127.0.0.1", 8000)
            icon = _FakeIcon.instances[-1]
            open_item = next(i for i in icon.menu.items if i.text == "打开控制台")
            open_item.action(icon, open_item)
            open_mock.assert_called_once_with("http://127.0.0.1:8000")

    def test_menu_quit_sets_should_exit_and_stops_icon(self):
        server = _FakeServer()
        fakes = _install_fake_pystray_and_pil()
        with patch.dict(sys.modules, fakes):
            tray_icon._start_tray_icon(server, "127.0.0.1", 8000)

        icon = _FakeIcon.instances[-1]
        quit_item = next(i for i in icon.menu.items if i.text == "退出 DoYouTrade")
        self.assertFalse(server.should_exit)
        quit_item.action(icon, quit_item)
        self.assertTrue(server.should_exit)
        self.assertTrue(icon.stopped)

    def test_prefers_run_detached_when_available(self):
        server = _FakeServer()
        fakes = _install_fake_pystray_and_pil()
        with patch.dict(sys.modules, fakes):
            tray_icon._start_tray_icon(server, "127.0.0.1", 8000)

        icon = _FakeIcon.instances[-1]
        self.assertTrue(icon.run_detached_called)
        self.assertFalse(icon.ran)

    def test_falls_back_to_thread_when_run_detached_missing(self):
        server = _FakeServer()
        fakes = _install_fake_pystray_and_pil()

        # A separate class (not subclassing _FakeIcon) that genuinely has no
        # run_detached attribute at all — getattr(icon, "run_detached", None)
        # must return None so tray_icon._start_tray_icon falls back to a thread.
        class _IconNoRunDetached:
            instances: list["_IconNoRunDetached"] = []

            def __init__(self, name, image, title, menu=None):
                self.name = name
                self.image = image
                self.title = title
                self.menu = menu
                self.stopped = False
                self.ran = False
                _IconNoRunDetached.instances.append(self)

            def run(self):
                self.ran = True

            def stop(self):
                self.stopped = True

        fakes["pystray"].Icon = _IconNoRunDetached
        with patch.dict(sys.modules, fakes):
            tray_icon._start_tray_icon(server, "127.0.0.1", 8000)

        icon = _IconNoRunDetached.instances[-1]
        self.assertFalse(hasattr(icon, "run_detached"))
        # Thread is a daemon thread; give it a brief moment to run.
        import time

        for _ in range(50):
            if icon.ran:
                break
            time.sleep(0.01)
        self.assertTrue(icon.ran)

    def test_wildcard_bind_host_displays_as_localhost(self):
        server = _FakeServer()
        fakes = _install_fake_pystray_and_pil()
        with patch.dict(sys.modules, fakes), \
             patch.object(tray_icon.webbrowser, "open") as open_mock:
            tray_icon._start_tray_icon(server, "0.0.0.0", 8000)
            icon = _FakeIcon.instances[-1]
            open_item = next(i for i in icon.menu.items if i.text == "打开控制台")
            open_item.action(icon, open_item)
            open_mock.assert_called_once_with("http://127.0.0.1:8000")


if __name__ == "__main__":
    unittest.main()
