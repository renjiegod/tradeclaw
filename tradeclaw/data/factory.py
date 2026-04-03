from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tradeclaw.config import DataSettings
from tradeclaw.data.mock_provider import MockTradingDataProvider, StaticUniverseProvider
from tradeclaw.data.qmt_proxy import QmtLiveDataProvider
from tradeclaw.data.qmt_proxy_client import QmtProxyRestClient

# Built-in provider ids (also used in config / API).
PROVIDER_AUTO = "auto"
PROVIDER_MOCK = "mock"
PROVIDER_QMT = "qmt"

_CUSTOM_BUILDERS: dict[str, Callable[[DataSettings, list[str]], tuple[Any, Any]]] = {}


def register_trading_data_provider(
    name: str,
    builder: Callable[[DataSettings, list[str]], tuple[Any, Any]],
) -> None:
    """Register an extra channel; `name` must be lowercase and not reserved."""
    key = name.strip().lower()
    if key in (PROVIDER_AUTO, PROVIDER_MOCK, PROVIDER_QMT, "demo"):
        raise ValueError(f"reserved data provider id: {key}")
    if not key:
        raise ValueError("data provider name must be non-empty")
    _CUSTOM_BUILDERS[key] = builder


def normalize_provider_id(value: str | None) -> str:
    if value is None:
        return PROVIDER_AUTO
    key = str(value).strip().lower()
    if key == "demo":
        return PROVIDER_MOCK
    return key or PROVIDER_AUTO


def resolve_effective_provider(requested: str | None, global_default: str) -> str:
    return normalize_provider_id(requested or global_default)


def _qmt_configured(data_cfg: DataSettings) -> bool:
    url = data_cfg.qmt.base_url
    return bool(url and str(url).strip())


def _build_qmt_stack(data_cfg: DataSettings, symbols: list[str]) -> tuple[Any, Any]:
    if not _qmt_configured(data_cfg):
        raise ValueError(
            "data provider 'qmt' requires data.qmt.base_url; "
            "set it in config or use provider 'mock' or 'auto'."
        )
    client = QmtProxyRestClient(
        base_url=data_cfg.qmt.base_url or "",
        token=data_cfg.qmt.token,
        session_id=data_cfg.qmt.session_id,
        timeout_seconds=data_cfg.qmt.timeout_seconds,
    )
    return QmtLiveDataProvider(client=client, symbols=symbols), StaticUniverseProvider(symbols)


def _build_mock_stack(_data_cfg: DataSettings, symbols: list[str]) -> tuple[Any, Any]:
    return MockTradingDataProvider(), StaticUniverseProvider(symbols)


def build_trading_data_stack(
    provider_id: str,
    data_cfg: DataSettings,
    symbols: list[str] | None = None,
) -> tuple[Any, Any]:
    """
    Returns (data_provider, universe_provider) for a worker.

    `provider_id`: auto | mock | qmt | a name registered via register_trading_data_provider.
    """
    sym = list(symbols if symbols is not None else data_cfg.symbols)
    key = normalize_provider_id(provider_id)

    if key == PROVIDER_AUTO:
        key = PROVIDER_QMT if _qmt_configured(data_cfg) else PROVIDER_MOCK

    if key == PROVIDER_MOCK:
        return _build_mock_stack(data_cfg, sym)
    if key == PROVIDER_QMT:
        return _build_qmt_stack(data_cfg, sym)

    builder = _CUSTOM_BUILDERS.get(key)
    if builder is None:
        raise ValueError(
            f"unknown data provider {provider_id!r}; "
            f"expected auto, mock, qmt, or a registered provider."
        )
    return builder(data_cfg, sym)
