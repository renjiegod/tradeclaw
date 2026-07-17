"""Optional Windows system tray icon for a double-click-launched DoYouTrade.

A user who double-clicks ``启动DoYouTrade.bat`` gets a bare console window with
no way to reopen the browser tab or quit cleanly short of closing the window.
:func:`maybe_start_tray_icon` adds a minimal ``pystray`` tray icon with two
menu items ("打开控制台" / "退出 DoYouTrade") when — and only when — both:

- ``sys.platform == "win32"`` (this is a Windows-only convenience; other
  platforms have a normal terminal the user is expected to use), and
- the environment variable ``DOYOUTRADE_TRAY == "1"`` (set by the launcher
  script, not by this process — see ``doyoutrade/api/server.py``).

Everything else is a no-op *return*, not a raised error: this mirrors the
``doyoutrade`` extras story (``pystray`` / ``Pillow`` ship only in the
Windows-only ``qmt-proxy`` extra, see ``pyproject.toml``), so a Linux/macOS
dev box or a Windows box that skipped the extra must boot exactly as if this
module didn't exist. Any failure while trying to actually start the icon
(missing dependency, no tray/display environment, pystray internals raising)
is caught, logged at WARNING with the exception type + message, and degrades
to "no tray icon" — it must never take the rest of the server down (AGENTS.md
error-visibility discipline: a convenience feature failing must be visible in
logs, not silently swallowed *and* not fatal).
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser
from typing import Any

from doyoutrade.observability import get_logger

logger = get_logger(__name__)

_TRAY_ICON_SIZE = 64
_TRAY_ACCENT_RGB = (201, 133, 54)  # matches the web console's shell-accent color


def maybe_start_tray_icon(server: Any, host: str, port: int) -> None:
    """Start a Windows system tray icon for *server*, if enabled.

    No-op (returns immediately, no exception) unless running on Windows with
    ``DOYOUTRADE_TRAY=1`` set. When enabled, runs pystray's icon loop in its
    own thread (or via ``run_detached()`` when available) so it never blocks
    the caller's asyncio event loop running ``await server.serve()``.

    ``server`` is the uvicorn ``Server`` instance already constructed by
    ``_serve_doyoutrade``; "退出 DoYouTrade" sets ``server.should_exit = True``,
    the same graceful-shutdown trigger the self-updater's restart hook uses
    (see ``doyoutrade/api/server.py``), so quitting from the tray drains
    in-flight requests instead of killing the process.
    """

    if sys.platform != "win32":
        return
    if os.environ.get("DOYOUTRADE_TRAY") != "1":
        return

    try:
        _start_tray_icon(server, host, port)
    except Exception as exc:  # noqa: BLE001 — convenience feature, must not kill startup
        logger.warning(
            "tray icon failed to start (%s: %s); continuing without a tray icon",
            type(exc).__name__,
            exc,
        )


def _start_tray_icon(server: Any, host: str, port: int) -> None:
    import pystray

    console_url = f"http://{_display_host(host)}:{port}"
    image = _build_icon_image()

    def _open_console(icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        try:
            webbrowser.open(console_url)
        except Exception as exc:  # noqa: BLE001 — browser launch is best-effort
            logger.warning(
                "tray icon: failed to open browser at %s (%s: %s)",
                console_url,
                type(exc).__name__,
                exc,
            )

    def _quit(icon: "pystray.Icon", item: "pystray.MenuItem") -> None:
        logger.info("tray icon: quit requested, stopping DoYouTrade")
        server.should_exit = True
        icon.stop()

    icon = pystray.Icon(
        "doyoutrade",
        image,
        "DoYouTrade",
        menu=pystray.Menu(
            pystray.MenuItem("打开控制台", _open_console, default=True),
            pystray.MenuItem("退出 DoYouTrade", _quit),
        ),
    )

    run_detached = getattr(icon, "run_detached", None)
    if callable(run_detached):
        run_detached()
        logger.info("tray icon started (run_detached) console_url=%s", console_url)
        return

    thread = threading.Thread(target=icon.run, name="doyoutrade-tray", daemon=True)
    thread.start()
    logger.info("tray icon started (thread) console_url=%s", console_url)


def _display_host(host: str) -> str:
    """``0.0.0.0`` / ``::`` bind hosts aren't browsable; show localhost instead."""

    if host in ("0.0.0.0", "::", ""):
        return "127.0.0.1"
    return host


def _build_icon_image():
    """Draw a minimal filled-circle tray icon with PIL — no bundled image asset.

    The repo ships no icon binary; drawing one at runtime keeps the tray
    feature self-contained (no new binary resource to package/track).
    """

    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (_TRAY_ICON_SIZE, _TRAY_ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = 4
    draw.ellipse(
        (margin, margin, _TRAY_ICON_SIZE - margin, _TRAY_ICON_SIZE - margin),
        fill=(*_TRAY_ACCENT_RGB, 255),
    )
    return image
