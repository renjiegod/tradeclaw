"""Doyoutrade MVP core package."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("doyoutrade")
except PackageNotFoundError:  # pragma: no cover  — not installed
    __version__ = "0.0.0+unknown"


def engine_version() -> str:
    """Stable identifier for the runtime engine version persisted onto
    ``runs.engine_version`` at backtest start.

    Currently just the package version; future builds can extend with a
    git sha or build timestamp (e.g. baked in via env var at CI time)
    without breaking callers.
    """
    return f"doyoutrade-{__version__}"


__all__ = ["__version__", "engine_version"]
