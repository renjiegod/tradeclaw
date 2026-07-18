from __future__ import annotations

import logging
import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml
from sqlalchemy.engine.url import make_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int
    tick_seconds: float


@dataclass(frozen=True)
class TushareSettings:
    """Tushare Pro API credentials and per-call timeout.

    A non-empty :attr:`token` enables Tushare in the ``data_source=auto``
    fallback chain (see :func:`doyoutrade.data.factory._resolve_auto_chain`).
    When the token is None or empty, the factory drops Tushare from the
    chain — no upstream calls are issued, no errors are raised.
    """

    token: Optional[str]
    timeout_seconds: float = 10.0


def _parse_tushare(block: Any) -> TushareSettings:
    """Parse the ``data.tushare`` config block.

    Honors a ``TUSHARE_TOKEN`` env var as a fallback so contributors
    don't have to commit credentials. YAML values win over env when
    both are set.
    """

    if block in (None, {}):
        env_token = (os.environ.get("TUSHARE_TOKEN") or "").strip()
        return TushareSettings(token=env_token or None)
    if not isinstance(block, dict):
        raise ValueError("data.tushare must be a mapping or omitted")
    raw_token = block.get("token")
    token = _resolve_secret(raw_token) if raw_token is not None else None
    if not token:
        env_token = (os.environ.get("TUSHARE_TOKEN") or "").strip()
        token = env_token or None
    timeout = float(block.get("timeout_seconds", 10.0))
    return TushareSettings(token=token, timeout_seconds=timeout)


@dataclass(frozen=True)
class NewsWebSearchSettings:
    """API keys for the multi-engine web-search news provider (Tavily / Bocha).

    Each engine takes a list of keys (multi-key round-robin, mirroring the DSA
    ``BaseSearchProvider`` load-balancing). An engine with an empty key list is
    simply skipped by :class:`doyoutrade.data.news_websearch.NewsWebSearchProvider`
    — no upstream call, no error. Keys are secrets: they are read like
    ``data.tushare.token`` (``${ENV}`` refs resolved, with a comma-separated env
    fallback ``DOYOUTRADE_TAVILY_API_KEYS`` / ``DOYOUTRADE_BOCHA_API_KEYS``) and
    are never surfaced back through ``GET /config`` (config_store masks them to a
    ``*_set`` boolean).
    """

    tavily_api_keys: tuple[str, ...] = ()
    bocha_api_keys: tuple[str, ...] = ()
    #: Per-engine HTTP timeout (seconds) and default result cap per engine.
    timeout_seconds: float = 10.0
    max_results_per_engine: int = 10


@dataclass(frozen=True)
class NewsSettings:
    """News-axis configuration (separate from OHLCV ``default_provider``)."""

    websearch: NewsWebSearchSettings = NewsWebSearchSettings()


@dataclass(frozen=True)
class DataSettings:
    # auto | mock | qmt | akshare | tushare | baostock — per-instance can override via CycleTaskConfig.data_provider.
    # QMT connection / account config now lives in the ``accounts`` DB table
    # (see doyoutrade.persistence.models.AccountRecord), not here.
    default_provider: str
    # ``TushareSettings(token=None)`` keeps Tushare disabled by default
    # — set ``data.tushare.token`` in config.yaml or export
    # ``TUSHARE_TOKEN`` to opt in.
    tushare: TushareSettings = TushareSettings(token=None)
    #: Web-search news engine keys (``data.news.websearch.*``). Empty by default
    #: → the ``websearch`` news source is inert until keys are configured.
    news: NewsSettings = NewsSettings()


def _parse_api_key_list(value: Any, *, field_name: str, env_var: str) -> tuple[str, ...]:
    """Parse a list of secret API keys, reusing the ``${ENV}`` / secret pattern.

    Accepts a YAML list, a single string, or a comma-separated string. Each
    element is passed through :func:`_resolve_secret` so ``${ENV_NAME}`` refs are
    resolved and blanks dropped. When the config yields no keys, a
    comma-separated ``env_var`` env fallback is honoured (operator escape hatch,
    same convention as ``TUSHARE_TOKEN``). No silent coercion: a non-list /
    non-string element type raises with the offending field + type.
    """

    keys: list[str] = []
    if value is None:
        raw_items: list[Any] = []
    elif isinstance(value, str):
        raw_items = [part for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raise ValueError(
            f"{field_name} must be a list of strings or a comma-separated string, "
            f"got {type(value).__name__}"
        )

    for item in raw_items:
        if item is None:
            continue
        if not isinstance(item, str):
            raise ValueError(
                f"{field_name} entries must be strings, got {type(item).__name__}: {item!r}"
            )
        resolved = _resolve_secret(item)
        if resolved:
            keys.append(resolved)

    if not keys:
        env_raw = (os.environ.get(env_var) or "").strip()
        if env_raw:
            keys = [part.strip() for part in env_raw.split(",") if part.strip()]

    return tuple(keys)


def _parse_news_settings(block: Any) -> "NewsSettings":
    """Parse the ``data.news`` config block into :class:`NewsSettings`."""
    if block in (None, {}):
        block = {}
    if not isinstance(block, dict):
        raise ValueError("data.news must be a mapping or omitted")
    ws_block = block.get("websearch")
    if ws_block in (None, {}):
        ws_block = {}
    if not isinstance(ws_block, dict):
        raise ValueError("data.news.websearch must be a mapping or omitted")
    timeout = _parse_positive_float(
        ws_block.get("timeout_seconds", 10.0),
        field_name="data.news.websearch.timeout_seconds",
    )
    max_results = _parse_positive_int(
        ws_block.get("max_results_per_engine", 10),
        field_name="data.news.websearch.max_results_per_engine",
    )
    return NewsSettings(
        websearch=NewsWebSearchSettings(
            tavily_api_keys=_parse_api_key_list(
                ws_block.get("tavily_api_keys"),
                field_name="data.news.websearch.tavily_api_keys",
                env_var="DOYOUTRADE_TAVILY_API_KEYS",
            ),
            bocha_api_keys=_parse_api_key_list(
                ws_block.get("bocha_api_keys"),
                field_name="data.news.websearch.bocha_api_keys",
                env_var="DOYOUTRADE_BOCHA_API_KEYS",
            ),
            timeout_seconds=timeout,
            max_results_per_engine=max_results,
        )
    )


@dataclass(frozen=True)
class ObservabilitySettings:
    service_name: str
    log_level: str
    console_enabled: bool
    tracing_enabled: bool


@dataclass(frozen=True)
class RetentionSettings:
    """TTL pruning of the heavy observability / trace tables.

    Targets ``debug_sessions`` / ``debug_session_events`` / ``debug_session_spans``
    / ``model_invocations`` — rows older than :attr:`observability_ttl_days` are
    deleted. ``cycle_runs`` / ``trade_fills`` / ``runs`` are the durable record
    and are **never** pruned by this policy.

    A recurring background loop runs every :attr:`prune_interval_hours`; when
    :attr:`prune_on_startup` is True a one-shot sweep also runs at bootstrap.
    Set :attr:`enabled` to False to disable all pruning.

    Env overrides (operator escape hatch — these win over YAML/config.yaml):
    ``DOYOUTRADE_OBSERVABILITY_TTL_DAYS`` and
    ``DOYOUTRADE_RETENTION_PRUNE_INTERVAL_HOURS``.
    """

    enabled: bool = True
    observability_ttl_days: int = 7
    prune_interval_hours: int = 24
    prune_on_startup: bool = True


@dataclass(frozen=True)
class AutoUpdateSettings:
    """Release-based update notifications (设置页「自动更新」开关).

    When :attr:`enabled` (the default) a background loop polls the GitHub
    *releases* API of :attr:`repo` every :attr:`check_interval_hours` and
    compares the latest release tag against the installed package version.
    A newer release only surfaces a notification in the web UI — nothing is
    installed until the user explicitly triggers ``POST /update/apply``.

    All three fields are hot-reloadable: the loop re-reads ``get_config()``
    on every tick (see :class:`doyoutrade.infra.updater.UpdateService`).
    """

    enabled: bool = True
    check_interval_hours: float = 6.0
    repo: str = "renjiegod/doyoutrade"


@dataclass(frozen=True)
class ReviewSettings:
    #: ``default`` — no extra symbol filter; ``block_all`` — ``review_symbol_scope == []`` for the cycle.
    symbol_scope_mode: str


@dataclass(frozen=True)
class FeishuSettings:
    enabled: bool
    app_id: str
    app_secret: str
    encrypt_key: str
    verification_token: str
    domain: str  # "feishu" | "lark"


@dataclass(frozen=True)
class AssistantApprovalAllowlist:
    """Persistent (cross-session) remembered approval grants.

    Stored under ``assistant.approval_allowlist`` in ``~/.doyoutrade/config.yaml``.
    ``rule_keys`` remember whole :class:`ApprovalRule` keys; ``command_prefixes``
    are ClaudeCode-style command prefixes (``doyoutrade-cli task start:*``).
    """

    rule_keys: tuple[str, ...] = ()
    command_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssistantSettings:
    tool_result_max_chars: int = 50000
    approval_allowlist: AssistantApprovalAllowlist = AssistantApprovalAllowlist()


@dataclass(frozen=True)
class ApiSettings:
    # When set, ``doyoutrade-cli`` HTTP-mode commands (e.g. cron writes) talk to
    # this base URL instead of deriving one from ``server``. Env
    # ``DOYOUTRADE_API_URL`` still wins over this value.
    base_url: Optional[str] = None


@dataclass(frozen=True)
class QmtProxySettings:
    """Config for the embedded qmt-proxy server started by ``doyoutrade --mode
    both`` / ``--mode qmt-proxy``.

    qmt-proxy wraps the Windows-only ``xtquant`` SDK as an authenticated REST
    service. When DoYouTrade runs it in-process (``both`` mode), these values
    drive the embedded uvicorn (host/port) and, on ``both`` startup, auto-wire
    the default account's ``base_url`` to ``http://{host}:{port}`` with
    :attr:`local_token` so the box works with zero QMT configuration.

    - :attr:`mode` maps to qmt-proxy's ``APP_MODE`` (mock | dev | prod).
      ``dev`` connects xtquant read-only; ``prod`` allows real trading; ``mock``
      never touches xtquant (safe default off-Windows).
    - :attr:`grpc_enabled` starts qmt-proxy's optional gRPC server in a daemon
      thread. Off by default — DoYouTrade only consumes the REST surface.
    """

    host: str = "127.0.0.1"
    port: int = 8001
    mode: str = "dev"
    grpc_enabled: bool = False
    local_token: str = "embedded-local"


@dataclass(frozen=True)
class AnthropicModelSettings:
    api_key: Optional[str]
    base_url: Optional[str]
    # Messages API ``thinking`` (extended thinking); matches ThinkingConfigParam:
    # https://platform.claude.com/docs/en/api/messages/create
    thinking: dict[str, Any] | None = None
    # Messages API ``cache_control``; matches CacheControlParam:
    # https://platform.anthropic.com/docs/api/messages
    cache_control: dict[str, Any] | None = None


@dataclass(frozen=True)
class OpenAICompatibleModelSettings:
    api_key: Optional[str]
    base_url: Optional[str]
    # OpenAI Chat Completions ``tool_choice`` (e.g. ``"required"`` when tools are sent).
    tool_choice: Any = None
    # When set on the merged DB provider/route patch, overrides route-level ``max_tokens`` for OpenAI adapters.
    max_tokens: Optional[int] = None


@dataclass(frozen=True)
class LmStudioModelSettings:
    api_key: Optional[str]
    base_url: Optional[str]
    # OpenAI-compatible Chat Completions ``tool_choice`` (LM Studio uses the same wire shape).
    tool_choice: Any = None
    # When set on the merged DB provider/route patch, overrides route-level ``max_tokens`` for LM Studio adapters.
    max_tokens: Optional[int] = None
    # Deep-merged into LM Studio ``prediction`` ``config`` (camelCase keys), e.g. ``promptTemplate`` overrides.
    prediction_config_extra: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class ModelSettings:
    # Active ``model_routes.route_name`` (observability / recording label).
    provider: str
    # Adapter / API family: ``anthropic`` | ``openai_compatible`` | ``lmstudio`` (for factory, tools, recording).
    provider_kind: str
    # Resolved model id (route ``target_model``).
    model: str
    temperature: float
    # When null/omitted: OpenAI-compatible adapters omit ``max_tokens`` on the wire; Anthropic uses a built-in default.
    max_tokens: Optional[int]
    timeout_seconds: float
    # ``agent`` | ``factor`` — see :func:`_parse_signal_strategy`.
    signal_strategy: str
    anthropic: AnthropicModelSettings
    openai_compatible: OpenAICompatibleModelSettings
    lmstudio: LmStudioModelSettings


@dataclass(frozen=True)
class DatabaseSettings:
    url: str
    echo: bool
    pool_pre_ping: bool


@dataclass(frozen=True)
class MarketDataSettings:
    database_url: str
    enabled_intervals: tuple[str, ...]
    lookback_years: int
    default_provider: str
    sync_on_startup: bool
    sync_concurrency: int
    provider_rate_limit_per_second: float
    #: When True the background market-data sync covers the full A-share catalog
    #: instead of being scoped to the watchlist. Off by default so existing
    #: deployments keep their current (watchlist-scoped) sync load; turn it on to
    #: warm the local ``market_bars`` warehouse for full-market ``stock screen``.
    sync_full_market: bool = False


@dataclass(frozen=True)
class AppConfig:
    server: ServerSettings
    data: DataSettings
    observability: ObservabilitySettings
    review: ReviewSettings
    database: DatabaseSettings
    feishu: FeishuSettings
    market_data: MarketDataSettings
    assistant: AssistantSettings = AssistantSettings()
    api: ApiSettings = ApiSettings()
    retention: RetentionSettings = RetentionSettings()
    qmt_proxy: QmtProxySettings = QmtProxySettings()
    auto_update: AutoUpdateSettings = AutoUpdateSettings()


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


def _parse_openai_compatible_tool_choice(value: Any) -> Any:
    """Normalize ``model.openai_compatible.tool_choice`` for the OpenAI SDK."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text if text else None
    if isinstance(value, dict):
        return value
    raise ValueError("model.openai_compatible.tool_choice must be a string, a mapping, or omitted")


def _anthropic_settings_from_flat_mapping(block: dict[str, Any]) -> AnthropicModelSettings:
    """Build settings from a single Messages-API credential block (api_key, base_url, …)."""
    return AnthropicModelSettings(
        api_key=_resolve_secret(block.get("api_key")),
        base_url=_maybe_str(block.get("base_url")),
        thinking=_parse_anthropic_thinking(block.get("thinking")),
        cache_control=_parse_anthropic_cache_control(block.get("cache_control")),
    )


def _inactive_anthropic_settings() -> AnthropicModelSettings:
    return AnthropicModelSettings(api_key=None, base_url=None)


def _inactive_openai_compatible_settings() -> OpenAICompatibleModelSettings:
    return OpenAICompatibleModelSettings(api_key=None, base_url=None, max_tokens=None)


def _inactive_lmstudio_settings() -> LmStudioModelSettings:
    return LmStudioModelSettings(
        api_key=None,
        base_url=None,
        max_tokens=None,
        tool_choice=None,
        prediction_config_extra=None,
    )


def default_model_route_baseline() -> ModelSettings:
    """Scalar defaults merged with DB ``model_route`` / ``model_provider`` patches (no credentials)."""
    return ModelSettings(
        provider="",
        provider_kind="anthropic",
        model="",
        temperature=0.1,
        max_tokens=None,
        timeout_seconds=30.0,
        signal_strategy="agent",
        anthropic=_inactive_anthropic_settings(),
        openai_compatible=_inactive_openai_compatible_settings(),
        lmstudio=_inactive_lmstudio_settings(),
    )


def _lmstudio_settings_from_flat_mapping(
    block: dict[str, Any], *, provider_max_tokens: Optional[int] = None
) -> LmStudioModelSettings:
    block = dict(block)
    pred_extra = block.pop("prediction_config_extra", None)
    if pred_extra is not None and not isinstance(pred_extra, dict):
        raise ValueError("prediction_config_extra must be a JSON object when set")
    return LmStudioModelSettings(
        api_key=_resolve_secret(block.get("api_key")),
        base_url=_maybe_str(block.get("base_url")),
        tool_choice=_parse_openai_compatible_tool_choice(block.get("tool_choice")),
        max_tokens=provider_max_tokens,
        prediction_config_extra=pred_extra,
    )


def _openai_compatible_settings_from_flat_mapping(
    block: dict[str, Any], *, provider_max_tokens: Optional[int] = None
) -> OpenAICompatibleModelSettings:
    return OpenAICompatibleModelSettings(
        api_key=_resolve_secret(block.get("api_key")),
        base_url=_maybe_str(block.get("base_url")),
        tool_choice=_parse_openai_compatible_tool_choice(block.get("tool_choice")),
        max_tokens=provider_max_tokens,
    )


def _reject_yaml_model_and_providers(data: dict[str, Any]) -> None:
    """Model providers and routes are stored in the database, not in YAML."""
    prov = data.get("providers")
    if prov not in (None, []):
        raise ValueError(
            "top-level 'providers' in config YAML is no longer supported; "
            "configure model providers in the database or via POST /model-providers."
        )
    raw_model = data.get("model")
    if raw_model not in (None, {}):
        raise ValueError(
            "top-level 'model' in config YAML is no longer supported; "
            "configure model routes/providers in the database and set settings.model_route_name "
            "on each agent instance or backtest job."
        )


def _parse_optional_model_max_tokens(value: Any) -> Optional[int]:
    """Parse ``model.max_tokens``: null/omit → None; set → positive int."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"null", "none"}:
            return None
        try:
            parsed = int(text, 10)
        except ValueError as exc:
            raise ValueError("model.max_tokens must be an integer or null") from exc
        if parsed < 1:
            raise ValueError(f"model.max_tokens must be >= 1 when set, got {parsed}")
        return parsed
    if isinstance(value, bool):
        raise ValueError("model.max_tokens must be an integer or null")
    if isinstance(value, int):
        if value < 1:
            raise ValueError(f"model.max_tokens must be >= 1 when set, got {value}")
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("model.max_tokens must be an integer or null")
        n = int(value)
        if n < 1:
            raise ValueError(f"model.max_tokens must be >= 1 when set, got {n}")
        return n
    raise ValueError("model.max_tokens must be an integer or null")


_SUPPORTED_MARKET_INTERVALS = {"1d", "5m"}
_SUPPORTED_MARKET_DATA_DRIVERS = ("sqlite+aiosqlite", "postgresql+asyncpg")


def _parse_market_data_settings(block: Any) -> MarketDataSettings:
    if not isinstance(block, dict):
        raise ValueError("market_data must be a mapping")
    database_url = _parse_required_non_empty_str(
        _resolve_secret(block.get("database_url")), field_name="market_data.database_url"
    )
    try:
        parsed = make_url(database_url)
    except Exception as exc:
        raise ValueError(
            "market_data.database_url must be a valid SQLAlchemy database URL"
        ) from exc
    if parsed.drivername not in _SUPPORTED_MARKET_DATA_DRIVERS:
        raise ValueError(
            "market_data.database_url must use sqlite+aiosqlite (local file) or "
            "postgresql+asyncpg (TimescaleDB); "
            f"got {parsed.drivername!r}"
        )
    if parsed.drivername == "sqlite+aiosqlite":
        if not parsed.database or parsed.database == ":memory:":
            raise ValueError(
                "market_data.database_url with sqlite+aiosqlite must point to a "
                "file path (in-memory SQLite loses bars on every new connection)"
            )
    elif not parsed.database:
        raise ValueError("market_data.database_url must include a database name")
    raw_intervals = block.get("enabled_intervals", ["1d", "5m"])
    if not isinstance(raw_intervals, list) or not raw_intervals:
        raise ValueError("market_data.enabled_intervals must be a non-empty list")
    intervals = tuple(str(item).strip() for item in raw_intervals)
    if not intervals or any(not item for item in intervals):
        raise ValueError("market_data.enabled_intervals must contain non-empty interval strings")
    unsupported = [item for item in intervals if item not in _SUPPORTED_MARKET_INTERVALS]
    if unsupported:
        raise ValueError(
            "market_data.enabled_intervals only supports ['1d', '5m'] in this release; "
            f"got {unsupported!r}"
        )
    lookback_years = _parse_positive_int(
        block.get("lookback_years", 10), field_name="market_data.lookback_years"
    )
    sync_concurrency = _parse_positive_int(
        block.get("sync_concurrency", 4), field_name="market_data.sync_concurrency"
    )
    rate_limit = _parse_positive_float(
        block.get("provider_rate_limit_per_second", 2.0),
        field_name="market_data.provider_rate_limit_per_second",
    )
    default_provider = _parse_market_default_provider(
        block.get("default_provider"), field_name="market_data.default_provider"
    )
    return MarketDataSettings(
        database_url=database_url,
        enabled_intervals=intervals,
        lookback_years=lookback_years,
        default_provider=default_provider,
        sync_on_startup=_parse_bool(
            block.get("sync_on_startup", True), field_name="market_data.sync_on_startup"
        ),
        sync_concurrency=sync_concurrency,
        provider_rate_limit_per_second=rate_limit,
        sync_full_market=_parse_bool(
            block.get("sync_full_market", False),
            field_name="market_data.sync_full_market",
        ),
    )


def _parse_market_default_provider(value: Any, *, field_name: str) -> str:
    if value is None:
        return "auto"
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a non-empty string")
    provider = value.strip().lower()
    if not provider:
        raise ValueError(f"{field_name} must be a non-empty string")
    return provider


def _parse_positive_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{field_name} must be a positive integer")
        parsed = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name} must be a positive integer")
        try:
            parsed = int(text, 10)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a positive integer") from exc
        if str(parsed) != text and not (text.startswith("+") and str(parsed) == text[1:]):
            raise ValueError(f"{field_name} must be a positive integer")
    else:
        raise ValueError(f"{field_name} must be a positive integer")
    if parsed <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return parsed


def _parse_positive_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive number")
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name} must be a positive number")
        try:
            parsed = float(text)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a positive number") from exc
    else:
        raise ValueError(f"{field_name} must be a positive number")
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be finite")
    if parsed <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return parsed


def _parse(data: dict[str, Any]) -> AppConfig:
    _reject_yaml_model_and_providers(data)
    server = data["server"]
    data_block = data["data"]
    observability = data.get("observability", {})
    review_block = data.get("review") or {}
    if not isinstance(review_block, dict):
        raise ValueError("review must be a mapping or omitted")
    database = data.get("database", {})
    if not isinstance(database, dict):
        raise ValueError("database must be a mapping")
    market_data = data.get("market_data", {})
    return AppConfig(
        server=ServerSettings(
            host=str(server["host"]),
            port=int(server["port"]),
            tick_seconds=float(server["tick_seconds"]),
        ),
        data=DataSettings(
            default_provider=str(data_block.get("default_provider", "auto")).strip().lower() or "auto",
            tushare=_parse_tushare(data_block.get("tushare")),
            news=_parse_news_settings(data_block.get("news")),
        ),
        observability=ObservabilitySettings(
            service_name=str(observability.get("service_name", "doyoutrade")).strip() or "doyoutrade",
            log_level=str(observability.get("log_level", "INFO")).strip().upper() or "INFO",
            console_enabled=bool(observability.get("console_enabled", True)),
            tracing_enabled=bool(observability.get("tracing_enabled", True)),
        ),
        review=_parse_review_settings(review_block),
        feishu=_parse_feishu_settings(data.get("feishu") or {}),
        assistant=_parse_assistant_settings(data.get("assistant") or {}),
        api=_parse_api_settings(data.get("api") or {}),
        qmt_proxy=_parse_qmt_proxy_settings(data.get("qmt_proxy") or {}),
        retention=_parse_retention_settings(data.get("retention") or {}),
        auto_update=_parse_auto_update_settings(data.get("auto_update") or {}),
        database=DatabaseSettings(
            url=_parse_required_non_empty_str(
                database.get("url", "sqlite+aiosqlite:///./data/doyoutrade.db"),
                field_name="database.url",
            ),
            echo=_parse_bool(database.get("echo", False), field_name="database.echo"),
            pool_pre_ping=_parse_bool(
                database.get("pool_pre_ping", True),
                field_name="database.pool_pre_ping",
            ),
        ),
        market_data=_parse_market_data_settings(market_data),
    )


def _parse_required_non_empty_str(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _parse_signal_strategy(value: Any) -> str:
    raw = str(value if value is not None else "agent").strip().lower() or "agent"
    if raw == "demo":
        raise ValueError(
            "model.signal_strategy 'demo' is no longer supported; use 'agent' with a configured provider"
        )
    if raw not in ("agent", "factor"):
        raise ValueError("model.signal_strategy must be 'agent' or 'factor'")
    return raw


def _parse_review_settings(block: dict[str, Any]) -> ReviewSettings:
    if "strategy" in block:
        raw = str(block["strategy"] if block["strategy"] is not None else "").strip().lower()
        if raw == "agent":
            raise ValueError(
                "review.strategy: agent is no longer supported; proposal review is deterministic-only. "
                "Remove review.strategy from your config.yaml. "
                "Use review.symbol_scope_mode (default | block_all) to control symbol scope."
            )
        if raw == "deterministic":
            pass  # Accepted for backward compatibility; effective behavior is always deterministic.
        elif not raw:
            raise ValueError("review.strategy must not be empty when set; omit the key instead")
        else:
            raise ValueError(
                f"review.strategy {raw!r} is not supported; review is deterministic-only. "
                "Remove review.strategy from your config."
            )
    return ReviewSettings(
        symbol_scope_mode=_parse_review_symbol_scope_mode(block.get("symbol_scope_mode", "default")),
    )


def _parse_review_symbol_scope_mode(value: Any) -> str:
    raw = str(value if value is not None else "default").strip().lower() or "default"
    if raw not in ("default", "block_all"):
        raise ValueError("review.symbol_scope_mode must be 'default' or 'block_all'")
    return raw


def _parse_feishu_settings(block: dict[str, Any]) -> FeishuSettings:
    if not isinstance(block, dict):
        raise ValueError("feishu must be a mapping or omitted")
    domain_raw = str(block.get("domain", "feishu")).strip().lower() or "feishu"
    if domain_raw not in ("feishu", "lark"):
        raise ValueError("feishu.domain must be 'feishu' or 'lark'")
    return FeishuSettings(
        enabled=bool(block.get("enabled", False)),
        app_id=str(block.get("app_id", "")).strip(),
        app_secret=str(block.get("app_secret", "")).strip(),
        encrypt_key=str(block.get("encrypt_key", "")).strip(),
        verification_token=str(block.get("verification_token", "")).strip(),
        domain=domain_raw,
    )


def _parse_assistant_approval_allowlist(block: Any) -> AssistantApprovalAllowlist:
    if block is None:
        return AssistantApprovalAllowlist()
    if not isinstance(block, dict):
        raise ValueError("assistant.approval_allowlist must be a mapping or omitted")
    raw_keys = block.get("rule_keys", [])
    raw_prefixes = block.get("command_prefixes", [])
    if raw_keys is None:
        raw_keys = []
    if raw_prefixes is None:
        raw_prefixes = []
    if not isinstance(raw_keys, list):
        raise ValueError("assistant.approval_allowlist.rule_keys must be a list")
    if not isinstance(raw_prefixes, list):
        raise ValueError("assistant.approval_allowlist.command_prefixes must be a list")
    rule_keys = tuple(
        str(item).strip() for item in raw_keys if str(item).strip()
    )
    command_prefixes = tuple(
        str(item).strip() for item in raw_prefixes if str(item).strip()
    )
    return AssistantApprovalAllowlist(
        rule_keys=rule_keys,
        command_prefixes=command_prefixes,
    )


def _parse_assistant_settings(block: dict[str, Any]) -> AssistantSettings:
    if not isinstance(block, dict):
        raise ValueError("assistant must be a mapping or omitted")
    return AssistantSettings(
        tool_result_max_chars=int(block.get("tool_result_max_chars", 50000)),
        approval_allowlist=_parse_assistant_approval_allowlist(
            block.get("approval_allowlist")
        ),
    )


def _env_override(name: str, fallback: Any) -> Any:
    """Return the env var ``name`` (stripped) when set & non-empty, else ``fallback``.

    Operator escape hatch for retention knobs: a non-empty env value wins over
    the YAML/config.yaml value. The raw string is handed to the field parser,
    which validates it like any other value.
    """
    raw = os.environ.get(name)
    if raw is not None and raw.strip():
        return raw.strip()
    return fallback


def _parse_retention_settings(block: Any) -> RetentionSettings:
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise ValueError("retention must be a mapping or omitted")
    return RetentionSettings(
        enabled=_parse_bool(
            block.get("enabled", True), field_name="retention.enabled"
        ),
        observability_ttl_days=_parse_positive_int(
            _env_override(
                "DOYOUTRADE_OBSERVABILITY_TTL_DAYS",
                block.get("observability_ttl_days", 7),
            ),
            field_name="retention.observability_ttl_days",
        ),
        prune_interval_hours=_parse_positive_int(
            _env_override(
                "DOYOUTRADE_RETENTION_PRUNE_INTERVAL_HOURS",
                block.get("prune_interval_hours", 24),
            ),
            field_name="retention.prune_interval_hours",
        ),
        prune_on_startup=_parse_bool(
            block.get("prune_on_startup", True),
            field_name="retention.prune_on_startup",
        ),
    )


def _parse_auto_update_settings(block: Any) -> AutoUpdateSettings:
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise ValueError("auto_update must be a mapping or omitted")
    defaults = AutoUpdateSettings()
    repo = str(block.get("repo", defaults.repo)).strip() or defaults.repo
    owner, sep, name = repo.partition("/")
    if not sep or not owner.strip() or not name.strip() or "/" in name or any(c.isspace() for c in repo):
        raise ValueError(
            f"auto_update.repo must look like 'owner/name' (a GitHub repo), got {repo!r}"
        )
    return AutoUpdateSettings(
        enabled=_parse_bool(
            block.get("enabled", defaults.enabled), field_name="auto_update.enabled"
        ),
        check_interval_hours=_parse_positive_float(
            block.get("check_interval_hours", defaults.check_interval_hours),
            field_name="auto_update.check_interval_hours",
        ),
        repo=repo,
    )


def _parse_api_settings(block: dict[str, Any]) -> ApiSettings:
    if not isinstance(block, dict):
        raise ValueError("api must be a mapping or omitted")
    base_url = _maybe_str(block.get("base_url"))
    if base_url is not None and not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError(
            f"api.base_url must start with http:// or https://, got {base_url!r}"
        )
    return ApiSettings(base_url=base_url)


def _parse_qmt_proxy_settings(block: dict[str, Any]) -> QmtProxySettings:
    if not isinstance(block, dict):
        raise ValueError("qmt_proxy must be a mapping or omitted")
    defaults = QmtProxySettings()
    host = _maybe_str(block.get("host")) or defaults.host
    port = int(block.get("port", defaults.port))
    mode = str(block.get("mode", defaults.mode)).strip().lower() or defaults.mode
    if mode not in ("mock", "dev", "prod"):
        raise ValueError(
            f"qmt_proxy.mode must be one of mock|dev|prod, got {mode!r}"
        )
    grpc_enabled = _parse_bool(
        block.get("grpc_enabled", defaults.grpc_enabled),
        field_name="qmt_proxy.grpc_enabled",
    )
    local_token = _maybe_str(block.get("local_token")) or defaults.local_token
    return QmtProxySettings(
        host=host,
        port=port,
        mode=mode,
        grpc_enabled=grpc_enabled,
        local_token=local_token,
    )


def _parse_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in {0, 1}:
            return bool(value)
        raise ValueError(f"{field_name} must be a boolean")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
        raise ValueError(f"{field_name} must be a boolean")
    raise ValueError(f"{field_name} must be a boolean")


def _maybe_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _parse_anthropic_thinking(value: Any) -> dict[str, Any] | None:
    """Parse ``model.anthropic.thinking`` for the Messages API ``thinking`` field."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("model.anthropic.thinking must be a mapping or null")
    if not value:
        raise ValueError("model.anthropic.thinking must not be empty; omit the key to disable thinking")
    t = value.get("type")
    if not isinstance(t, str) or not t.strip():
        raise ValueError("model.anthropic.thinking.type is required and must be a non-empty string")
    normalized = t.strip().lower()
    if normalized not in ("enabled", "disabled", "adaptive"):
        raise ValueError(
            "model.anthropic.thinking.type must be 'enabled', 'disabled', or 'adaptive'"
        )
    display = value.get("display")
    if display is not None and display not in ("summarized", "omitted"):
        raise ValueError("model.anthropic.thinking.display must be 'summarized' or 'omitted'")
    if normalized == "enabled":
        if "budget_tokens" not in value:
            raise ValueError("model.anthropic.thinking with type 'enabled' requires budget_tokens")
        try:
            budget = int(value["budget_tokens"])
        except (TypeError, ValueError) as exc:
            raise ValueError("model.anthropic.thinking.budget_tokens must be an integer") from exc
        if budget < 1024:
            raise ValueError(
                "model.anthropic.thinking.budget_tokens must be at least 1024 (Anthropic API)"
            )
    out = dict(value)
    out["type"] = normalized
    return out


def _parse_anthropic_cache_control(value: Any) -> dict[str, Any] | None:
    """Parse ``model.anthropic.cache_control`` for the Messages API ``cache_control`` field."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("model.anthropic.cache_control must be a mapping or null")
    if not value:
        raise ValueError("model.anthropic.cache_control must not be empty; omit the key to disable")
    t = value.get("type")
    if not isinstance(t, str) or not t.strip():
        raise ValueError("model.anthropic.cache_control.type is required and must be a non-empty string")
    normalized = t.strip().lower()
    if normalized not in ("ephemeral",):
        raise ValueError("model.anthropic.cache_control.type must be 'ephemeral'")
    return {"type": normalized}


def _resolve_secret(value: Any) -> Optional[str]:
    text = _maybe_str(value)
    if text is None:
        return None
    if text.startswith("${") and text.endswith("}") and len(text) > 3:
        env_name = text[2:-1].strip()
        return _maybe_str(os.environ.get(env_name))
    return text


def default_base_dir() -> Path:
    """Return the DoYouTrade home directory (``~/.doyoutrade`` by default).

    Honours ``DOYOUTRADE_HOME`` (same convention as the strategy-storage root in
    :mod:`doyoutrade.bootstrap` and the knowledge-base root in
    :mod:`doyoutrade.tools._sandbox`) so tests / alternate-home deployments point
    at an isolated directory. The path is expanded but not created here.
    """
    return Path(
        os.getenv("DOYOUTRADE_HOME", str(Path.home() / ".doyoutrade"))
    ).expanduser()


def resolve_writable_config_path() -> Path:
    """Return the canonical writable config location: ``<home>/config.yaml``.

    This is where the Web UI / :mod:`doyoutrade.config_store` persist edits. It
    is a pure path helper with no filesystem side effects — call
    :func:`seed_writable_config_if_missing` to materialise it.
    """
    return default_base_dir() / "config.yaml"


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("DOYOUTRADE_CONFIG")
    if env:
        paths.append(Path(env).expanduser().resolve())
    # ``<home>/config.yaml`` (the UI-writable file) wins over cwd/repo/bundled so
    # that a save through the Settings page becomes the effective config on the
    # next reload; only an explicit ``$DOYOUTRADE_CONFIG`` override outranks it.
    paths.append(resolve_writable_config_path())
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
        "No config file found. Set DOYOUTRADE_CONFIG or add config.yaml (see doyoutrade/default_config.yaml)."
    )


def seed_writable_config_if_missing() -> Path:
    """Materialise ``<home>/config.yaml`` on first boot; return its path.

    Idempotent: if the file already exists it is returned untouched.

    The seed source is the current *effective* config file
    (:func:`resolve_config_path`) rather than only the bundled default. On a
    fresh install ``resolve_config_path()`` already resolves to the bundled
    ``default_config.yaml`` (so this matches "seed from default_config.yaml"),
    while on a box that already had a repo/cwd ``config.yaml`` this preserves
    those values as the migration to ``~/.doyoutrade`` happens — seeding raw
    bundled defaults there would silently shadow (and effectively drop) the
    operator's existing config. A plain byte copy preserves all comments.
    """
    target = resolve_writable_config_path()
    if target.is_file():
        return target
    try:
        source = resolve_config_path()
    except FileNotFoundError:
        source = bundled_default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    logger.info(
        "seeded writable config target=%s source=%s", target, source
    )
    return target


def bundled_default_config_path() -> Path:
    return Path(__file__).resolve().parent / "default_config.yaml"


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
