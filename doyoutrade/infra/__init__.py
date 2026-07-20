"""Infrastructure clients (HTTP/SDK) shared across data and account stacks.

Heavy QMT / OpenTelemetry imports are lazy so lightweight helpers such as
``doyoutrade.infra.release_artifacts`` can be imported without installing the
full runtime dependency set (needed by Windows installer CI, which runs bare
``python -m unittest``).
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "QmtProxyRestClient",
    "QmtProxyWsClient",
    "create_qmt_proxy_rest_client",
]

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "QmtProxyRestClient": ("doyoutrade.infra.qmt_proxy_client", "QmtProxyRestClient"),
    "QmtProxyWsClient": ("doyoutrade.infra.qmt_proxy_client", "QmtProxyWsClient"),
    "create_qmt_proxy_rest_client": (
        "doyoutrade.infra.qmt",
        "create_qmt_proxy_rest_client",
    ),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    value = getattr(import_module(module_name), attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
