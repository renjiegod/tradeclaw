"""Locate and launch the bundled qmt-proxy server in the DoYouTrade process.

qmt-proxy is a sibling FastAPI (+ optional gRPC) service that wraps the
Windows-only ``xtquant`` SDK as an authenticated REST API. Historically it was
deployed and started separately. ``doyoutrade --mode both`` (the Windows
default) now runs it *in the same process* so a single install / single command
gives real QMT quotes with zero extra configuration.

Two launch shapes are exposed:

- :func:`start_embedded_qmt_proxy_thread` — used by ``--mode both``. Runs the
  REST server (and optional gRPC) in **daemon threads** so DoYouTrade keeps the
  main thread / asyncio loop. Uvicorn skips signal-handler install off the main
  thread, so this is safe.
- :func:`run_qmt_proxy_blocking` — used by ``--mode qmt-proxy``. Runs the REST
  server in the **main thread** (blocking) plus optional gRPC in a daemon
  thread — a drop-in equivalent of ``qmt-proxy/run.py`` driven by DoYouTrade
  config.

The qmt-proxy ``app.main:app`` object is a module-level singleton with import
side effects (``sys.path`` insert, swagger-CDN monkeypatch); we import it as-is
rather than refactor it into a factory. All ``xtquant`` imports inside it are
lazy + guarded, so importing works off-Windows (it degrades to mock).
"""

from __future__ import annotations

import importlib
import importlib.resources
import os
import sys
import threading
from pathlib import Path

from doyoutrade.config import QmtProxySettings, default_base_dir
from doyoutrade.observability import get_logger

logger = get_logger(__name__)


def _qmt_proxy_root() -> Path:
    """Directory that contains the qmt-proxy ``app`` (and ``generated``) package.

    Source checkout: ``<repo>/qmt-proxy``. Installed wheel: the copy bundled at
    ``doyoutrade/_qmt_proxy`` by the custom hatch build hook. Mirrors
    :func:`doyoutrade.api.server._frontend_dist_dir`'s two-location lookup.

    Raises RuntimeError (not a silent None) when neither exists — a missing
    bundle in ``both`` / ``qmt-proxy`` mode is a hard, operator-visible error,
    not a degrade-to-nothing.
    """

    repo_root = Path(__file__).resolve().parents[2]
    source = repo_root / "qmt-proxy"
    if (source / "app" / "main.py").is_file():
        return source
    packaged = Path(str(importlib.resources.files("doyoutrade"))) / "_qmt_proxy"
    if (packaged / "app" / "main.py").is_file():
        return packaged
    raise RuntimeError(
        "qmt-proxy server bundle not found. It ships with the 'qmt-proxy' extra: "
        "install with `uv tool install \"doyoutrade[qmt-proxy] @ <source>\"` "
        "(or `pip install 'doyoutrade[qmt-proxy]'`). On non-Windows hosts it runs "
        "in mock/degraded mode; real QMT quotes require Windows + miniQMT + xtquant."
    )


def load_qmt_proxy_app(mode: str):
    """Import and return the qmt-proxy FastAPI ``app`` singleton.

    ``mode`` maps to qmt-proxy's ``APP_MODE`` env (mock | dev | prod) and is set
    with ``setdefault`` so an explicit ``APP_MODE`` in the environment still
    wins. Import failures (missing extra / missing xtquant on a real-mode host)
    are re-raised as a RuntimeError with an actionable hint — never swallowed.
    """

    root = _qmt_proxy_root()
    os.environ.setdefault("APP_MODE", mode)
    # Point the embedded proxy at the same ~/.doyoutrade home the Web UI writes to
    # (~/.doyoutrade/qmt-proxy.yml), so a single config location drives both. An
    # explicit QMT_PROXY_CONFIG in the environment still wins (setdefault).
    os.environ.setdefault(
        "QMT_PROXY_CONFIG", str(default_base_dir() / "qmt-proxy.yml")
    )
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    try:
        app_main = importlib.import_module("app.main")
    except Exception as exc:  # pragma: no cover - exercised via logs on failure
        logger.exception(
            "failed to import embedded qmt-proxy app (%s: %s); root=%s APP_MODE=%s",
            type(exc).__name__,
            exc,
            root_str,
            os.environ.get("APP_MODE"),
        )
        raise RuntimeError(
            f"could not import embedded qmt-proxy ({type(exc).__name__}: {exc}). "
            "Ensure the 'qmt-proxy' extra is installed "
            "(`doyoutrade[qmt-proxy]`); on Windows real modes also require xtquant + miniQMT."
        ) from exc
    return app_main.app


def _build_uvicorn_server(app, host: str, port: int, log_level: str):
    from uvicorn import Config, Server

    config = Config(
        app,
        host=host,
        port=port,
        log_level=(log_level or "info").lower(),
        log_config=None,
    )
    return Server(config)


def _start_grpc_thread(mode: str) -> threading.Thread:
    """Start qmt-proxy's blocking gRPC ``serve()`` in a daemon thread.

    Mirrors ``qmt-proxy/run.py``: gRPC runs alongside REST. Returns the thread
    so the caller can log it. Import/boot failures inside the thread are logged
    at ERROR (the thread dies, REST keeps serving) — never silently dropped.
    """

    def _serve() -> None:
        try:
            root = _qmt_proxy_root()
            os.environ.setdefault("APP_MODE", mode)
            root_str = str(root)
            if root_str not in sys.path:
                sys.path.insert(0, root_str)
            grpc_server = importlib.import_module("app.grpc_server")
            logger.info("embedded qmt-proxy gRPC server starting")
            grpc_server.serve()
        except Exception as exc:
            logger.exception(
                "embedded qmt-proxy gRPC server failed (%s: %s); REST unaffected",
                type(exc).__name__,
                exc,
            )

    thread = threading.Thread(target=_serve, name="qmt-proxy-grpc", daemon=True)
    thread.start()
    return thread


def start_embedded_qmt_proxy_thread(
    settings: QmtProxySettings, *, log_level: str = "info"
) -> threading.Thread:
    """Run the embedded qmt-proxy REST server in a daemon thread (``--mode both``).

    Uvicorn's ``Server.run()`` starts its own event loop and skips signal
    handlers when not on the main thread, so it coexists with DoYouTrade's main
    loop. Returns the REST server thread. Raises before spawning if the bundle /
    deps are missing (see :func:`load_qmt_proxy_app`)."""

    app = load_qmt_proxy_app(settings.mode)
    server = _build_uvicorn_server(app, settings.host, settings.port, log_level)

    def _run() -> None:
        try:
            server.run()
        except Exception as exc:
            logger.exception(
                "embedded qmt-proxy REST server crashed (%s: %s)",
                type(exc).__name__,
                exc,
            )

    thread = threading.Thread(target=_run, name="qmt-proxy-rest", daemon=True)
    thread.start()
    logger.info(
        "embedded qmt-proxy started (mode=both): http://%s:%s APP_MODE=%s grpc=%s",
        settings.host,
        settings.port,
        os.environ.get("APP_MODE"),
        settings.grpc_enabled,
    )
    if settings.grpc_enabled:
        _start_grpc_thread(settings.mode)
    return thread


def run_qmt_proxy_blocking(
    settings: QmtProxySettings, *, log_level: str = "info"
) -> None:
    """Run qmt-proxy REST in the main thread (blocking) — ``--mode qmt-proxy``.

    Optional gRPC runs in a daemon thread first (as in ``qmt-proxy/run.py``)."""

    app = load_qmt_proxy_app(settings.mode)
    if settings.grpc_enabled:
        _start_grpc_thread(settings.mode)
    logger.info(
        "qmt-proxy standalone starting: http://%s:%s APP_MODE=%s grpc=%s",
        settings.host,
        settings.port,
        os.environ.get("APP_MODE"),
        settings.grpc_enabled,
    )
    server = _build_uvicorn_server(app, settings.host, settings.port, log_level)
    server.run()
