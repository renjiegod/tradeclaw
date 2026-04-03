from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    tick_seconds: float


@dataclass(frozen=True)
class QmtSettings:
    base_url: Optional[str]
    token: Optional[str]
    session_id: Optional[str]
    timeout_seconds: float


@dataclass(frozen=True)
class DataSettings:
    symbols: list[str]
    qmt: QmtSettings
    # auto | mock | qmt — per-instance can override via AgentInstanceConfig.data_provider.
    default_provider: str


@dataclass(frozen=True)
class RiskSettings:
    max_single_order_amount: float
    max_position_ratio: float


@dataclass(frozen=True)
class ApprovalSettings:
    min_notional_for_approval: float
    timeout_seconds: int


@dataclass(frozen=True)
class ObservabilitySettings:
    service_name: str
    log_level: str
    console_enabled: bool
    tracing_enabled: bool


@dataclass(frozen=True)
class AnthropicModelSettings:
    api_key: Optional[str]
    base_url: Optional[str]


@dataclass(frozen=True)
class OpenAICompatibleModelSettings:
    api_key: Optional[str]
    base_url: Optional[str]


@dataclass(frozen=True)
class ModelSettings:
    provider: str
    model: str
    temperature: float
    max_tokens: int
    timeout_seconds: float
    anthropic: AnthropicModelSettings
    openai_compatible: OpenAICompatibleModelSettings


@dataclass(frozen=True)
class AppConfig:
    server: ServerSettings
    data: DataSettings
    risk: RiskSettings
    approval: ApprovalSettings
    observability: ObservabilitySettings
    model: ModelSettings


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _default_dict() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "default_config.yaml"
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _parse(data: dict[str, Any]) -> AppConfig:
    server = data["server"]
    data_block = data["data"]
    qmt = data_block["qmt"]
    risk = data["risk"]
    approval = data["approval"]
    observability = data.get("observability", {})
    model = data["model"]
    anthropic = model.get("anthropic", {})
    openai_compatible = model.get("openai_compatible", {})
    return AppConfig(
        server=ServerSettings(
            host=str(server["host"]),
            port=int(server["port"]),
            tick_seconds=float(server["tick_seconds"]),
        ),
        data=DataSettings(
            symbols=[str(s) for s in data_block["symbols"]],
            qmt=QmtSettings(
                base_url=qmt.get("base_url"),
                token=_resolve_secret(qmt.get("token")),
                session_id=_resolve_secret(qmt.get("session_id")),
                timeout_seconds=float(qmt["timeout_seconds"]),
            ),
            default_provider=str(data_block.get("default_provider", "auto")).strip().lower() or "auto",
        ),
        risk=RiskSettings(
            max_single_order_amount=float(risk["max_single_order_amount"]),
            max_position_ratio=float(risk["max_position_ratio"]),
        ),
        approval=ApprovalSettings(
            min_notional_for_approval=float(approval["min_notional_for_approval"]),
            timeout_seconds=int(approval["timeout_seconds"]),
        ),
        observability=ObservabilitySettings(
            service_name=str(observability.get("service_name", "tradeclaw")).strip() or "tradeclaw",
            log_level=str(observability.get("log_level", "INFO")).strip().upper() or "INFO",
            console_enabled=bool(observability.get("console_enabled", True)),
            tracing_enabled=bool(observability.get("tracing_enabled", True)),
        ),
        model=ModelSettings(
            provider=str(model["provider"]),
            model=str(model["model"]),
            temperature=float(model["temperature"]),
            max_tokens=int(model["max_tokens"]),
            timeout_seconds=float(model["timeout_seconds"]),
            anthropic=AnthropicModelSettings(
                api_key=_resolve_secret(anthropic.get("api_key")),
                base_url=_maybe_str(anthropic.get("base_url")),
            ),
            openai_compatible=OpenAICompatibleModelSettings(
                api_key=_resolve_secret(openai_compatible.get("api_key")),
                base_url=_maybe_str(openai_compatible.get("base_url")),
            ),
        ),
    )


def _maybe_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _resolve_secret(value: Any) -> Optional[str]:
    text = _maybe_str(value)
    if text is None:
        return None
    if text.startswith("${") and text.endswith("}") and len(text) > 3:
        env_name = text[2:-1].strip()
        return _maybe_str(os.environ.get(env_name))
    return text


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("TRADECLAW_CONFIG")
    if env:
        paths.append(Path(env).expanduser().resolve())
    pkg = Path(__file__).resolve().parent
    repo_root = pkg.parent
    paths.extend(
        [
            Path.cwd() / "config.yaml",
            repo_root / "config.yaml",
            pkg / "default_config.yaml",
        ]
    )
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(p)
    return unique


def resolve_config_path() -> Path:
    for path in _candidate_paths():
        if path.is_file():
            return path
    raise FileNotFoundError(
        "No config file found. Set TRADECLAW_CONFIG or add config.yaml (see tradeclaw/default_config.yaml)."
    )


def load_config(path: Optional[Path] = None) -> AppConfig:
    base = _default_dict()
    if path is None:
        path = resolve_config_path()
    with path.open(encoding="utf-8") as handle:
        merged = _deep_merge(base, yaml.safe_load(handle) or {})
    return _parse(merged)


_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    global _config
    _config = None
