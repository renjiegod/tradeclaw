"""Merge persisted instance configuration with per-backtest ``config_overrides``."""

from __future__ import annotations

from typing import Any

from doyoutrade.config import _deep_merge
from doyoutrade.persistence.repositories import TaskSnapshot
from doyoutrade.runtime.cycle_task import (
    CycleTaskConfig,
    cycle_task_config_from_params,
    merge_task_settings,
    validate_optional_task_settings,
)

_BACKTEST_OVERRIDE_TOP_LEVEL = frozenset({"settings", "watch_symbols", "universe"})


def _as_str_list(label: str, value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"config_overrides.{label} must be an array of strings")
    out: list[str] = []
    for i, x in enumerate(value):
        if not isinstance(x, str):
            raise ValueError(f"config_overrides.{label}[{i}] must be a string")
        s = x.strip()
        if s:
            out.append(s)
    return out


def normalize_backtest_config_overrides(raw: object) -> dict[str, Any] | None:
    """Validate API payload; return a dict to persist or ``None`` if absent/empty."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("config_overrides must be a JSON object")
    extra = set(raw.keys()) - _BACKTEST_OVERRIDE_TOP_LEVEL
    if extra:
        raise ValueError(f"config_overrides unknown keys: {sorted(extra)}")
    if not any(k in raw for k in _BACKTEST_OVERRIDE_TOP_LEVEL):
        return None
    out: dict[str, Any] = {}
    if "settings" in raw:
        st = raw["settings"]
        if st is None:
            pass
        elif not isinstance(st, dict):
            raise ValueError("config_overrides.settings must be a JSON object")
        elif st:
            out["settings"] = dict(st)
    if "universe" in raw:
        uni = raw["universe"]
        if uni is None:
            pass
        else:
            out["universe"] = _as_str_list("universe", uni)
    # Deprecated compatibility: accept and silently drop watch_symbols overrides.
    if "watch_symbols" in raw:
        ws = raw["watch_symbols"]
        if ws is not None:
            _as_str_list("watch_symbols", ws)
    return out or None


def build_cycle_task_config_with_backtest_overrides(
    record: TaskSnapshot,
    config_overrides: dict[str, Any] | None,
) -> CycleTaskConfig:
    """``config_overrides`` is normalized (only settings / universe)."""
    merged_settings = merge_task_settings(record.settings)
    # model_route_name is now sourced exclusively from merged_settings (settings)
    if config_overrides:
        patch = config_overrides.get("settings")
        if patch:
            merged_settings = _deep_merge(dict(merged_settings), patch)
        if "universe" in config_overrides:
            merged_settings["universe"] = list(config_overrides["universe"])
        merged_settings = merge_task_settings(merged_settings)
    validate_optional_task_settings(merged_settings)
    return cycle_task_config_from_params(
        name=record.name,
        mode=record.mode,
        description=record.description,
        data_provider=record.data_provider,
        universe=list(record.universe or ()),
        settings=merged_settings,
    )
