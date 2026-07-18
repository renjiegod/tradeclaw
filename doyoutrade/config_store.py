"""Read / write the writable DoYouTrade config (``~/.doyoutrade/config.yaml``).

Backs the ``GET /config`` and ``PUT /config`` API surface. Two operations:

* :func:`read_config_masked` — return the effective config with secret fields
  masked (contract A: ``{path, values, restart_required_fields}``).
* :func:`write_config` — deep-merge a partial patch into the writable YAML
  (ruamel round-trip so comments survive), re-validate the *whole* merged
  document through :func:`doyoutrade.config._parse` (no silent coercion — a bad
  value raises :class:`ConfigValidationError` carrying the offending field), then
  persist and drop the in-process config cache.

Discipline (CLAUDE.md §错误可见性): validation is structured — bad input is
rejected with an ``error_code`` / ``field``, never coerced or swallowed; each
successful write logs which fields changed + whether a restart is required.

Restart classification: the "hot reload" candidates were each checked against
their consumer. Most snapshot their value at process startup, so a change does
NOT take effect until restart; those are listed in
:data:`RESTART_REQUIRED_FIELDS`. See the module-level comment on
:data:`RESTART_REQUIRED_FIELDS` for the per-field verdict.
"""

from __future__ import annotations

import io
import logging
from typing import Any, Iterable, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from doyoutrade import config

logger = logging.getLogger(__name__)

#: Masked stand-in returned for secret values (GET) and interpreted as
#: "unchanged, keep the stored value" on write (PUT).
MASK = "********"

#: Dotted paths whose value is a secret. On GET the value is replaced by
#: :data:`MASK` and a companion ``<leaf>_set: bool`` is emitted; on PUT a value
#: equal to :data:`MASK` (or ``None``) leaves the stored value untouched.
SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "data.tushare.token",
        "qmt_proxy.local_token",
        "feishu.app_secret",
        "feishu.encrypt_key",
        "feishu.verification_token",
    }
)

# Fields whose change only takes effect after a process restart. The base set
# comes from contract A; the trailing group was DOWNGRADED from "hot reload"
# after verifying each consumer snapshots the value at startup rather than
# re-reading get_config() on every use:
#   * data.default_provider / data.tushare.*  -> snapshotted into
#     PlatformService (self.default_data_provider / self.app_cfg.data) at
#     construction and used by the per-cycle worker build; on-demand data ops
#     do re-read get_config() but running instances need a restart.
#   * assistant.tool_result_max_chars -> read once in AssistantService.__init__
#     to build the tool registry (startup singleton).
#   * retention.* -> snapshotted at bootstrap into the one-shot startup prune and
#     the recurring ObservabilityPruneService (fixed ttl_days / interval_hours).
# review.symbol_scope_mode is the only remaining hot field (no runtime consumer
# snapshots it — it resolves through get_config() on demand).
RESTART_REQUIRED_FIELDS: tuple[str, ...] = (
    "server.host",
    "server.port",
    "server.tick_seconds",
    "database.url",
    "database.echo",
    "database.pool_pre_ping",
    "market_data.database_url",
    "market_data.enabled_intervals",
    "market_data.lookback_years",
    "market_data.default_provider",
    "market_data.sync_on_startup",
    "market_data.sync_concurrency",
    "market_data.provider_rate_limit_per_second",
    "market_data.sync_full_market",
    "observability.service_name",
    "observability.log_level",
    "observability.console_enabled",
    "observability.tracing_enabled",
    "qmt_proxy.host",
    "qmt_proxy.port",
    "qmt_proxy.mode",
    "qmt_proxy.grpc_enabled",
    "qmt_proxy.local_token",
    "feishu.enabled",
    "feishu.app_id",
    "feishu.app_secret",
    "feishu.encrypt_key",
    "feishu.verification_token",
    "feishu.domain",
    # --- downgraded from hot-reload after consumer verification ---
    "data.default_provider",
    "data.tushare.token",
    "data.tushare.timeout_seconds",
    "assistant.tool_result_max_chars",
    "retention.enabled",
    "retention.observability_ttl_days",
    "retention.prune_interval_hours",
    "retention.prune_on_startup",
)

_RESTART_SET: frozenset[str] = frozenset(RESTART_REQUIRED_FIELDS)

#: Fields that genuinely take effect on the next get_config() with no restart.
#: auto_update.* qualifies because UpdateService re-reads get_config() on every
#: loop tick (doyoutrade/infra/updater.py) instead of snapshotting at startup.
HOT_RELOAD_FIELDS: tuple[str, ...] = (
    "review.symbol_scope_mode",
    "auto_update.enabled",
    "auto_update.check_interval_hours",
    "auto_update.repo",
    # Approval allowlist is re-read via get_config() on every tool gate.
    "assistant.approval_allowlist.rule_keys",
    "assistant.approval_allowlist.command_prefixes",
)

#: Every editable leaf path (restart + hot). Used for best-effort field
#: attribution when validation fails with an unnamed error.
ALL_FIELDS: frozenset[str] = _RESTART_SET | frozenset(HOT_RELOAD_FIELDS)


class ConfigValidationError(ValueError):
    """Raised when a merged config patch fails :func:`doyoutrade.config._parse`.

    Carries the offending dotted ``field`` (best effort — ``None`` when the
    underlying validator did not name one) so the API layer can surface it.
    """

    def __init__(self, message: str, *, field: Optional[str] = None) -> None:
        super().__init__(message)
        self.field = field


def _ruamel() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    # Keep block style + reasonable width so round-tripped files stay readable.
    yaml.width = 4096
    return yaml


def _is_mask(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and value.strip() == MASK


def _token_set(value: Any) -> bool:
    return bool(value is not None and str(value).strip())


def _masked_values(cfg: config.AppConfig) -> dict[str, Any]:
    """Build the contract-A ``values`` block from a parsed AppConfig.

    Secret string fields are always emitted as :data:`MASK`; a companion
    ``<leaf>_set`` boolean reports whether an actual value is configured.
    """
    return {
        "server": {
            "host": cfg.server.host,
            "port": cfg.server.port,
            "tick_seconds": cfg.server.tick_seconds,
        },
        "data": {
            "default_provider": cfg.data.default_provider,
            "tushare": {
                "token": MASK,
                "token_set": _token_set(cfg.data.tushare.token),
                "timeout_seconds": cfg.data.tushare.timeout_seconds,
            },
            # Web-search news API keys are secrets: never echoed. Only a
            # ``*_set`` boolean (how many keys are configured) is surfaced, so
            # the UI can show "configured / not configured" without leaking the
            # keys. Keys are managed via config.yaml / env, not the PUT surface.
            "news": {
                "websearch": {
                    "tavily_api_keys_set": bool(cfg.data.news.websearch.tavily_api_keys),
                    "bocha_api_keys_set": bool(cfg.data.news.websearch.bocha_api_keys),
                    "timeout_seconds": cfg.data.news.websearch.timeout_seconds,
                    "max_results_per_engine": cfg.data.news.websearch.max_results_per_engine,
                },
            },
        },
        "market_data": {
            "database_url": cfg.market_data.database_url,
            "enabled_intervals": list(cfg.market_data.enabled_intervals),
            "lookback_years": cfg.market_data.lookback_years,
            "default_provider": cfg.market_data.default_provider,
            "sync_on_startup": cfg.market_data.sync_on_startup,
            "sync_concurrency": cfg.market_data.sync_concurrency,
            "provider_rate_limit_per_second": cfg.market_data.provider_rate_limit_per_second,
            "sync_full_market": cfg.market_data.sync_full_market,
        },
        "observability": {
            "service_name": cfg.observability.service_name,
            "log_level": cfg.observability.log_level,
            "console_enabled": cfg.observability.console_enabled,
            "tracing_enabled": cfg.observability.tracing_enabled,
        },
        "review": {"symbol_scope_mode": cfg.review.symbol_scope_mode},
        "retention": {
            "enabled": cfg.retention.enabled,
            "observability_ttl_days": cfg.retention.observability_ttl_days,
            "prune_interval_hours": cfg.retention.prune_interval_hours,
            "prune_on_startup": cfg.retention.prune_on_startup,
        },
        "assistant": {
            "tool_result_max_chars": cfg.assistant.tool_result_max_chars,
            "approval_allowlist": {
                "rule_keys": list(cfg.assistant.approval_allowlist.rule_keys),
                "command_prefixes": list(
                    cfg.assistant.approval_allowlist.command_prefixes
                ),
            },
        },
        "auto_update": {
            "enabled": cfg.auto_update.enabled,
            "check_interval_hours": cfg.auto_update.check_interval_hours,
            "repo": cfg.auto_update.repo,
        },
        "database": {
            "url": cfg.database.url,
            "echo": cfg.database.echo,
            "pool_pre_ping": cfg.database.pool_pre_ping,
        },
        "qmt_proxy": {
            "host": cfg.qmt_proxy.host,
            "port": cfg.qmt_proxy.port,
            "mode": cfg.qmt_proxy.mode,
            "grpc_enabled": cfg.qmt_proxy.grpc_enabled,
            "local_token": MASK,
            "local_token_set": _token_set(cfg.qmt_proxy.local_token),
        },
        "feishu": {
            "enabled": cfg.feishu.enabled,
            "app_id": cfg.feishu.app_id,
            "app_secret": MASK,
            "app_secret_set": _token_set(cfg.feishu.app_secret),
            "encrypt_key": MASK,
            "encrypt_key_set": _token_set(cfg.feishu.encrypt_key),
            "verification_token": MASK,
            "verification_token_set": _token_set(cfg.feishu.verification_token),
            "domain": cfg.feishu.domain,
        },
    }


def read_config_masked() -> dict[str, Any]:
    """Return ``{path, values, restart_required_fields}`` (contract A, GET)."""
    config.seed_writable_config_if_missing()
    cfg = config.get_config()
    return {
        "path": str(config.resolve_writable_config_path()),
        "values": _masked_values(cfg),
        "restart_required_fields": list(RESTART_REQUIRED_FIELDS),
    }


def _walk_leaves(patch: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    """Yield ``(dotted_path, value)`` for every non-dict leaf in ``patch``."""
    if isinstance(patch, dict):
        for key, value in patch.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                yield from _walk_leaves(value, path)
            else:
                yield path, value


def _applied_paths(patch: dict[str, Any]) -> list[str]:
    """Dotted leaf paths the patch would actually change.

    Secret leaves whose submitted value is the mask (or None) are excluded —
    they signal "unchanged" and must not count toward the restart set.
    """
    applied: list[str] = []
    for path, value in _walk_leaves(patch):
        if path in SECRET_FIELDS and _is_mask(value):
            continue
        applied.append(path)
    return applied


def _merge_patch_into_doc(doc: CommentedMap, patch: dict[str, Any], prefix: str = "") -> None:
    """Deep-merge ``patch`` into the ruamel ``doc`` in place.

    Only keys present in ``patch`` are touched (partial update). Secret leaves
    equal to the mask (or None) are left untouched so the stored value survives.
    """
    for key, value in patch.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            sub = doc.get(key)
            if not isinstance(sub, dict):
                sub = CommentedMap()
                doc[key] = sub
            _merge_patch_into_doc(sub, value, path)
        else:
            if path in SECRET_FIELDS and _is_mask(value):
                continue
            doc[key] = value


def _to_plain(value: Any) -> Any:
    """Recursively convert ruamel containers/scalars to plain Python types."""
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if value is None:
        return None
    return str(value) if not isinstance(value, str) else value


def _attribute_field(message: str, patch: dict[str, Any]) -> Optional[str]:
    """Best-effort mapping of a validator error message to a dotted field."""
    # Prefer the longest known field path that appears verbatim in the message.
    for path in sorted(ALL_FIELDS, key=len, reverse=True):
        if path in message:
            return path
    applied = _applied_paths(patch)
    if len(applied) == 1:
        return applied[0]
    return applied[0] if applied else None


def write_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial ``patch`` to the writable config and persist it.

    Returns ``{status, restart_required, restart_fields, path}``. Raises
    :class:`ConfigValidationError` (a ``ValueError`` subclass) when the merged
    result fails validation.
    """
    if not isinstance(patch, dict):
        raise ConfigValidationError(
            f"config patch must be a JSON object, got {type(patch).__name__}",
            field=None,
        )

    target = config.seed_writable_config_if_missing()
    yaml = _ruamel()
    with target.open(encoding="utf-8") as handle:
        doc = yaml.load(handle)
    if doc is None:
        doc = CommentedMap()
    if not isinstance(doc, CommentedMap):
        raise ConfigValidationError(
            f"writable config {target} is not a YAML mapping (got "
            f"{type(doc).__name__}); fix or remove the file",
            field=None,
        )

    _merge_patch_into_doc(doc, patch)

    # Validate the WHOLE merged document (defaults + doc) through the canonical
    # parser — no silent coercion; a bad value raises with its field.
    merged_full = config._deep_merge(config._default_dict(), _to_plain(doc))
    try:
        config._parse(merged_full)
    except ValueError as exc:
        field = _attribute_field(str(exc), patch)
        raise ConfigValidationError(str(exc), field=field) from exc

    # Persist round-tripped (comments preserved) via a buffer so a serialization
    # failure never truncates the on-disk file.
    buffer = io.StringIO()
    yaml.dump(doc, buffer)
    target.write_text(buffer.getvalue(), encoding="utf-8")

    config.reset_config()

    changed = _applied_paths(patch)
    restart_fields = [p for p in changed if p in _RESTART_SET]
    restart_required = bool(restart_fields)
    logger.info(
        "config write path=%s changed_fields=%s restart_required=%s restart_fields=%s",
        target,
        changed,
        restart_required,
        restart_fields,
    )
    return {
        "status": "updated",
        "restart_required": restart_required,
        "restart_fields": restart_fields,
        "path": str(target),
    }
