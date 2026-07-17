"""Cloud-mode detection for accounts pointed at a doyoutrade-cloud gateway.

An ``accounts`` row (``base_url`` / ``token``) may point either at a classic
self-hosted qmt-proxy or at the hosted doyoutrade-cloud gateway. The gateway
exposes ``GET {base_url}/api/cloud/v1/hello`` (Bearer auth) describing the
subscription plan, quota, capabilities, and behavioural *recommendations*; a
classic qmt-proxy answers that path with 404.

:func:`get_cloud_profile` / :func:`get_cloud_profile_sync` probe the endpoint
once (5s timeout) and cache the verdict per ``(base_url, token)`` in-process
for :data:`CLOUD_PROFILE_CACHE_TTL_SECONDS`. **Every** failure mode — 404,
non-200, network error, unparseable payload — resolves to ``None`` ("classic
mode"), so a failed probe can never change existing classic-mode behaviour.
Negative verdicts are cached too, so a classic deployment pays at most one
extra HTTP round-trip per TTL window.

Consumers apply :class:`CloudRecommendations` conservatively (``min`` with
the user's configured value, never an unconditional override):

* :class:`doyoutrade.data.market_sync.MarketDataSyncService` clamps
  ``lookback_years`` and ``rate_limit_per_second``.
* :class:`doyoutrade.infra.qmt_proxy_client.QmtProxyRestClient` skips the
  ``disable_download=False`` history-fetch retry when
  ``recommendations.disable_download`` is true.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

CLOUD_HELLO_PATH = "/api/cloud/v1/hello"
CLOUD_SERVICE_NAME = "doyoutrade-cloud"
CLOUD_PROBE_TIMEOUT_SECONDS = 5.0
CLOUD_PROFILE_CACHE_TTL_SECONDS = 600.0


@dataclass(frozen=True)
class CloudPlan:
    """Subscription plan advertised by the cloud gateway."""

    plan_name: str = ""
    rate_per_minute: int | None = None
    daily_requests: int | None = None
    scopes: tuple[str, ...] = ()
    max_ws_connections: int | None = None


@dataclass(frozen=True)
class CloudQuota:
    """Current-day quota consumption advertised by the cloud gateway."""

    daily_requests: int | None = None
    used_today: int | None = None
    remaining_today: int | None = None


@dataclass(frozen=True)
class CloudRecommendations:
    """Behavioural presets the gateway recommends for this plan.

    Consumers must apply these conservatively: ``min(configured, recommended)``
    for numeric limits, and ``disable_download`` only *removes* the slow
    download-enabled retry — a missing/None field always means "keep the
    user's configured behaviour".
    """

    disable_download: bool = False
    sync_lookback_years: int | None = None
    provider_rate_limit_per_second: float | None = None


@dataclass(frozen=True)
class CloudProfile:
    """Parsed ``/api/cloud/v1/hello`` response (cloud mode detected)."""

    service: str
    protocol_version: int | None = None
    plan: CloudPlan = field(default_factory=CloudPlan)
    quota: CloudQuota = field(default_factory=CloudQuota)
    capabilities: tuple[str, ...] = ()
    recommendations: CloudRecommendations = field(default_factory=CloudRecommendations)

    def has_capability(self, name: str) -> bool:
        return name in self.capabilities


# --- payload parsing ---------------------------------------------------------


def _require_dict(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(
            f"cloud hello {field_name} must be an object, "
            f"got {type(value).__name__}: {value!r}"
        )
    return value


def _opt_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"cloud hello {field_name} must be an integer, got bool: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValueError(
        f"cloud hello {field_name} must be an integer, "
        f"got {type(value).__name__}: {value!r}"
    )


def _opt_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"cloud hello {field_name} must be a number, got bool: {value!r}")
    if isinstance(value, (int, float)):
        out = float(value)
        if not math.isfinite(out):
            raise ValueError(f"cloud hello {field_name} must be finite, got {value!r}")
        return out
    raise ValueError(
        f"cloud hello {field_name} must be a number, "
        f"got {type(value).__name__}: {value!r}"
    )


def _opt_bool(value: Any, field_name: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError(
        f"cloud hello {field_name} must be a boolean, "
        f"got {type(value).__name__}: {value!r}"
    )


def _str_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            f"cloud hello {field_name} must be a list of strings, "
            f"got {type(value).__name__}: {value!r}"
        )
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(
                f"cloud hello {field_name} entries must be strings, "
                f"got {type(item).__name__}: {item!r}"
            )
        out.append(item)
    return tuple(out)


def parse_cloud_profile(payload: Any) -> CloudProfile:
    """Parse a ``/api/cloud/v1/hello`` JSON payload into a :class:`CloudProfile`.

    Raises :class:`ValueError` (with the offending field, type, and value) on
    any schema violation, including a ``service`` field that is not
    ``doyoutrade-cloud`` — callers treat that as "not cloud mode".
    """
    body = _require_dict(payload, "payload")
    if not body:
        raise ValueError("cloud hello payload must be a non-empty object")
    service = str(body.get("service") or "")
    if service != CLOUD_SERVICE_NAME:
        raise ValueError(
            f"cloud hello service mismatch: expected {CLOUD_SERVICE_NAME!r}, "
            f"got {service!r}"
        )
    plan_raw = _require_dict(body.get("plan"), "plan")
    quota_raw = _require_dict(body.get("quota"), "quota")
    rec_raw = _require_dict(body.get("recommendations"), "recommendations")
    return CloudProfile(
        service=service,
        protocol_version=_opt_int(body.get("protocol_version"), "protocol_version"),
        plan=CloudPlan(
            plan_name=str(plan_raw.get("plan_name") or ""),
            rate_per_minute=_opt_int(plan_raw.get("rate_per_minute"), "plan.rate_per_minute"),
            daily_requests=_opt_int(plan_raw.get("daily_requests"), "plan.daily_requests"),
            scopes=_str_tuple(plan_raw.get("scopes"), "plan.scopes"),
            max_ws_connections=_opt_int(
                plan_raw.get("max_ws_connections"), "plan.max_ws_connections"
            ),
        ),
        quota=CloudQuota(
            daily_requests=_opt_int(quota_raw.get("daily_requests"), "quota.daily_requests"),
            used_today=_opt_int(quota_raw.get("used_today"), "quota.used_today"),
            remaining_today=_opt_int(
                quota_raw.get("remaining_today"), "quota.remaining_today"
            ),
        ),
        capabilities=_str_tuple(body.get("capabilities"), "capabilities"),
        recommendations=CloudRecommendations(
            disable_download=_opt_bool(
                rec_raw.get("disable_download"), "recommendations.disable_download"
            ),
            sync_lookback_years=_opt_int(
                rec_raw.get("sync_lookback_years"), "recommendations.sync_lookback_years"
            ),
            provider_rate_limit_per_second=_opt_float(
                rec_raw.get("provider_rate_limit_per_second"),
                "recommendations.provider_rate_limit_per_second",
            ),
        ),
    )


# --- in-process TTL cache ----------------------------------------------------

_CACHE_MISS = object()
# (base_url, token) -> (expires_at_monotonic, CloudProfile | None)
_cache: dict[tuple[str, str], tuple[float, Optional[CloudProfile]]] = {}


def _now() -> float:
    """Monotonic clock — separated so tests can patch TTL expiry."""
    return time.monotonic()


def _cache_key(base_url: str | None, token: str | None) -> tuple[str, str] | None:
    base = (base_url or "").strip().rstrip("/")
    tok = (token or "").strip()
    if not base or not tok:
        # The hello endpoint requires Bearer auth; without a base_url+token
        # pair there is nothing to probe — classic mode, no HTTP.
        return None
    return base, tok


def _cache_get(key: tuple[str, str]) -> Any:
    entry = _cache.get(key)
    if entry is None:
        return _CACHE_MISS
    expires_at, profile = entry
    if _now() >= expires_at:
        _cache.pop(key, None)
        return _CACHE_MISS
    return profile


def _cache_put(
    key: tuple[str, str], profile: Optional[CloudProfile], ttl_seconds: float
) -> None:
    _cache[key] = (_now() + float(ttl_seconds), profile)


def reset_cloud_profile_cache() -> None:
    """Drop all cached probe verdicts (tests / account CRUD refresh)."""
    _cache.clear()


# --- probing -----------------------------------------------------------------


def _profile_from_response(url: str, response: httpx.Response) -> Optional[CloudProfile]:
    if response.status_code == 404:
        # Classic qmt-proxy: the cloud hello path does not exist. Expected,
        # so only debug-level.
        logger.debug("cloud profile probe url=%s -> 404 (classic qmt-proxy)", url)
        return None
    if response.status_code != 200:
        logger.info(
            "cloud profile probe url=%s -> HTTP %s; assuming classic mode",
            url,
            response.status_code,
        )
        return None
    try:
        profile = parse_cloud_profile(response.json())
    except Exception as exc:  # noqa: BLE001 — visible, degrades to classic mode
        logger.warning(
            "cloud profile probe url=%s returned 200 but the payload is invalid "
            "(%s: %s); assuming classic mode",
            url,
            type(exc).__name__,
            exc,
        )
        return None
    logger.info(
        "cloud mode detected url=%s plan=%s remaining_today=%s capabilities=%s "
        "recommendations=disable_download=%s sync_lookback_years=%s "
        "provider_rate_limit_per_second=%s",
        url,
        profile.plan.plan_name,
        profile.quota.remaining_today,
        list(profile.capabilities),
        profile.recommendations.disable_download,
        profile.recommendations.sync_lookback_years,
        profile.recommendations.provider_rate_limit_per_second,
    )
    return profile


def _hello_request_parts(key: tuple[str, str]) -> tuple[str, dict[str, str]]:
    base, token = key
    return f"{base}{CLOUD_HELLO_PATH}", {"Authorization": f"Bearer {token}"}


async def get_cloud_profile(
    base_url: str | None,
    token: str | None,
    *,
    timeout_seconds: float = CLOUD_PROBE_TIMEOUT_SECONDS,
    ttl_seconds: float = CLOUD_PROFILE_CACHE_TTL_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Optional[CloudProfile]:
    """Probe ``base_url`` for doyoutrade-cloud; ``None`` means classic mode.

    One-shot GET with a hard timeout; the verdict (positive *or* negative) is
    cached per ``(base_url, token)`` for ``ttl_seconds``. Never raises.
    """
    key = _cache_key(base_url, token)
    if key is None:
        return None
    cached = _cache_get(key)
    if cached is not _CACHE_MISS:
        return cached
    url, headers = _hello_request_parts(key)
    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds, transport=transport
        ) as client:
            response = await client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001 — probe must never break classic mode
        logger.info(
            "cloud profile probe failed url=%s error_type=%s error=%s; "
            "assuming classic mode",
            url,
            type(exc).__name__,
            exc,
        )
        _cache_put(key, None, ttl_seconds)
        return None
    profile = _profile_from_response(url, response)
    _cache_put(key, profile, ttl_seconds)
    return profile


def get_cloud_profile_sync(
    base_url: str | None,
    token: str | None,
    *,
    timeout_seconds: float = CLOUD_PROBE_TIMEOUT_SECONDS,
    ttl_seconds: float = CLOUD_PROFILE_CACHE_TTL_SECONDS,
    transport: httpx.BaseTransport | None = None,
) -> Optional[CloudProfile]:
    """Synchronous variant of :func:`get_cloud_profile` (shares the cache)."""
    key = _cache_key(base_url, token)
    if key is None:
        return None
    cached = _cache_get(key)
    if cached is not _CACHE_MISS:
        return cached
    url, headers = _hello_request_parts(key)
    try:
        with httpx.Client(timeout=timeout_seconds, transport=transport) as client:
            response = client.get(url, headers=headers)
    except Exception as exc:  # noqa: BLE001 — probe must never break classic mode
        logger.info(
            "cloud profile probe failed url=%s error_type=%s error=%s; "
            "assuming classic mode",
            url,
            type(exc).__name__,
            exc,
        )
        _cache_put(key, None, ttl_seconds)
        return None
    profile = _profile_from_response(url, response)
    _cache_put(key, profile, ttl_seconds)
    return profile
