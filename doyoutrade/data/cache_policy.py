"""Task-level data-cache / backfill policy (``settings.data_cache``).

Historically the multi-layer bar cache behaviour (local-first read, upstream
gap backfill, cache scope) was hard-coded at assembly time in
``doyoutrade.bootstrap`` (``if market_bars_repository`` / ``if mode == "live"``)
and was not configurable per task. :class:`DataCachePolicy` lifts that into an
explicit, per-task config object that flows
``settings.data_cache`` → :class:`doyoutrade.runtime.cycle_task.CycleTaskConfig`
→ ``bootstrap`` assembly → the provider stack.

Parsing follows the ``fee_config`` contract in ``cycle_task`` (see
``CLAUDE.md`` §Assistant 工具入参规范 / §错误可见性): a malformed value
``raise``\\ s a ``ValueError`` with the offending type/value rather than being
silently coerced to a default. A typo in the continuity strictness must surface
at config time, never get swallowed into "ran but did nothing".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from doyoutrade.data.protocols import (
    PROVIDER_NAME_AKSHARE,
    PROVIDER_NAME_BAOSTOCK,
    PROVIDER_NAME_MOCK,
    PROVIDER_NAME_MOOTDX,
    PROVIDER_NAME_QMT,
    PROVIDER_NAME_TUSHARE,
)

# Default backfill source priority — mirrors ``factory._AUTO_PRIORITY`` so a
# task that does not set ``data_cache.source_priority`` reproduces the legacy
# auto-chain order exactly (no behaviour change for existing tasks/backtests).
DEFAULT_SOURCE_PRIORITY: tuple[str, ...] = (
    PROVIDER_NAME_QMT,
    PROVIDER_NAME_BAOSTOCK,
    PROVIDER_NAME_MOOTDX,
    PROVIDER_NAME_AKSHARE,
    PROVIDER_NAME_TUSHARE,
)

KNOWN_PROVIDER_NAMES: frozenset[str] = frozenset(
    {
        PROVIDER_NAME_QMT,
        PROVIDER_NAME_BAOSTOCK,
        PROVIDER_NAME_MOOTDX,
        PROVIDER_NAME_AKSHARE,
        PROVIDER_NAME_TUSHARE,
        PROVIDER_NAME_MOCK,
    }
)

#: Write-time continuity is always judged against the served provider's
#: authoritative trading calendar (calendar mode is no longer task-configurable).
#: It is only meaningful for providers that publish an authoritative calendar
#: (qmt/baostock); a served source without one auto-degrades to the internal-gap
#: check below and emits ``continuity_degraded`` — that degradation is
#: driven by the source's ``capabilities.authoritative_calendar``, not by config.

#: ``continuity.on_unverifiable_gap`` — what to do when the served provider has
#: an authoritative calendar but no usable suspension source (e.g. qmt skips
#: suspension days and has no historical 停牌 API), so a missing calendar day
#: cannot be attributed to a halt vs a data defect.
#: * ``fail``    — reject the write (conservative; default — never persist data
#:   we cannot prove continuous).
#: * ``degrade`` — accept the write but emit ``continuity_degraded``.
UNVERIFIABLE_GAP_POLICIES: frozenset[str] = frozenset({"fail", "degrade"})

#: Largest acceptable calendar-day gap *inside* the returned bars before the
#: degraded internal-gap check rejects the payload (used when the served provider
#: has no authoritative calendar). Matches ``market_sync.MAX_COVERAGE_GAP_DAYS``
#: so the two write paths agree on what a "long suspension" boundary looks like.
MAX_INTERNAL_GAP_DAYS: int = 90


@dataclass(frozen=True)
class DataCachePolicy:
    """Resolved per-task data-cache / backfill / continuity policy.

    Immutable so it can be shared across the provider stack without a caller
    accidentally mutating another instance's view. Build via
    :func:`parse_data_cache_policy` (validates + raises) — do not construct from
    untrusted input directly.
    """

    #: Ordered provider ids to try when backfilling a local miss. Empty tuple is
    #: not allowed (parse rejects it). Default = :data:`DEFAULT_SOURCE_PRIORITY`.
    source_priority: tuple[str, ...] = DEFAULT_SOURCE_PRIORITY
    #: Read the local DB before hitting upstream (the local-first onion layer).
    local_first: bool = True
    #: Backfill from upstream + persist on a local miss. ``False`` makes the
    #: local layer read-only (a miss returns empty instead of fetching).
    auto_backfill: bool = True
    #: Behaviour for unverifiable calendar gaps — see
    #: :data:`UNVERIFIABLE_GAP_POLICIES`.
    on_unverifiable_gap: str = "fail"

    def as_payload(self) -> dict[str, Any]:
        """Flat dict for debug events / span attributes (``data_cache_policy_applied``)."""
        return {
            "source_priority": list(self.source_priority),
            "local_first": self.local_first,
            "auto_backfill": self.auto_backfill,
            "on_unverifiable_gap": self.on_unverifiable_gap,
        }

    def to_settings_block(self) -> dict[str, Any]:
        """Nested ``settings.data_cache`` shape for API serialization.

        Round-trips through :func:`parse_data_cache_policy` (the ``continuity``
        sub-object matches the input schema), so a frontend that loads a task,
        edits it, and re-submits does not lose the policy.
        """
        return {
            "source_priority": list(self.source_priority),
            "local_first": self.local_first,
            "auto_backfill": self.auto_backfill,
            "continuity": {
                "on_unverifiable_gap": self.on_unverifiable_gap,
            },
        }


def _require_bool(raw: Any, *, field: str) -> bool:
    if isinstance(raw, bool):
        return raw
    raise ValueError(
        f"settings.data_cache.{field} must be a boolean, "
        f"got {type(raw).__name__}: {raw!r}"
    )


def _parse_source_priority(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ValueError(
            "settings.data_cache.source_priority must be an array of provider "
            f"ids, got {type(raw).__name__}: {raw!r}"
        )
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                "settings.data_cache.source_priority entries must be non-empty "
                f"strings, got {type(item).__name__}: {item!r}"
            )
        name = item.strip()
        if name not in KNOWN_PROVIDER_NAMES:
            raise ValueError(
                f"settings.data_cache.source_priority has unknown provider {name!r}; "
                f"allowed: {sorted(KNOWN_PROVIDER_NAMES)}"
            )
        if name not in out:  # de-dupe while preserving order
            out.append(name)
    if not out:
        raise ValueError(
            "settings.data_cache.source_priority must list at least one provider"
        )
    return tuple(out)


def _require_choice(raw: Any, *, field: str, choices: frozenset[str]) -> str:
    if not isinstance(raw, str):
        raise ValueError(
            f"settings.data_cache.{field} must be a string, "
            f"got {type(raw).__name__}: {raw!r}"
        )
    value = raw.strip()
    if value not in choices:
        raise ValueError(
            f"settings.data_cache.{field} must be one of {sorted(choices)}, got {value!r}"
        )
    return value


def parse_data_cache_policy(raw: Any) -> DataCachePolicy:
    """Validate ``settings.data_cache`` and build a :class:`DataCachePolicy`.

    Mirrors the ``fee_config`` contract: a non-dict value, an unknown field, or
    an out-of-range value ``raise``\\ s ``ValueError`` with the offending
    type/value. Callers (``cycle_task._parse_data_cache``) surface this as a
    config validation error rather than silently defaulting.
    """
    if not isinstance(raw, dict):
        raise ValueError(
            f"settings.data_cache must be an object, got {type(raw).__name__}: {raw!r}"
        )

    allowed = {
        "source_priority",
        "local_first",
        "auto_backfill",
        "continuity",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(
            f"settings.data_cache has unknown field(s) {unknown}; allowed: {sorted(allowed)}"
        )

    kwargs: dict[str, Any] = {}
    if "source_priority" in raw:
        kwargs["source_priority"] = _parse_source_priority(raw["source_priority"])
    if "local_first" in raw:
        kwargs["local_first"] = _require_bool(raw["local_first"], field="local_first")
    if "auto_backfill" in raw:
        kwargs["auto_backfill"] = _require_bool(raw["auto_backfill"], field="auto_backfill")

    continuity = raw.get("continuity")
    if continuity is not None:
        if not isinstance(continuity, dict):
            raise ValueError(
                "settings.data_cache.continuity must be an object, "
                f"got {type(continuity).__name__}: {continuity!r}"
            )
        cont_unknown = sorted(set(continuity) - {"on_unverifiable_gap"})
        if cont_unknown:
            raise ValueError(
                f"settings.data_cache.continuity has unknown field(s) {cont_unknown}; "
                "allowed: ['on_unverifiable_gap']"
            )
        if "on_unverifiable_gap" in continuity:
            kwargs["on_unverifiable_gap"] = _require_choice(
                continuity["on_unverifiable_gap"],
                field="continuity.on_unverifiable_gap",
                choices=UNVERIFIABLE_GAP_POLICIES,
            )

    return DataCachePolicy(**kwargs)


__all__ = [
    "DataCachePolicy",
    "parse_data_cache_policy",
    "DEFAULT_SOURCE_PRIORITY",
    "KNOWN_PROVIDER_NAMES",
    "UNVERIFIABLE_GAP_POLICIES",
    "MAX_INTERNAL_GAP_DAYS",
]
