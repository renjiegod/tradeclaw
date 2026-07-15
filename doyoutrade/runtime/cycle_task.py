from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from doyoutrade.data.cache_policy import DataCachePolicy

# Defaults when instance ``settings`` omit risk / approval (formerly app-level config).
DEFAULT_MAX_POSITION_RATIO: float = 0.30
# Review-phase T: ``min(max_single_order_amount, equity * review_equity_fraction)`` when cap is set;
# ``max_single_order_amount`` omitted or null means **no per-order cap** (T = equity × f only).
DEFAULT_REVIEW_EQUITY_FRACTION: float = 1.0
# Exchange board lot in shares for the explicit-target rebalance paths.
# ``1`` = whole-share trading (byte-identical to pre-lot behavior); A股 grids
# set ``100``. See ``PositionConstraints.lot_size``.
DEFAULT_LOT_SIZE: int = 1
# Rebalance dead band in lots for the explicit-target paths. ``0`` = disabled.
DEFAULT_REBALANCE_HYSTERESIS_LOTS: int = 0
DEFAULT_MIN_NOTIONAL_FOR_APPROVAL: float = 1000.0
DEFAULT_APPROVAL_TIMEOUT_SECONDS: int = 300
DEFAULT_REACT_MAX_TURNS: int = 500
DEFAULT_SIGNAL_TOOL_NAMES: tuple[str, ...] = ("data_bars_relative", "invoke_skill")


def _normalize_signal_tool_names_value(raw: Any) -> list[str]:
    """Coerce ``signal_tool_names`` from JSON (list or comma-separated string) to strings."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _validate_agent_signal_fields(block: dict[str, Any], prefix: str = "settings.agent") -> None:
    """Validate react_max_turns and signal_tool_names in an agent settings block.

    两个字段都已退化为"显式给了才校验格式"——缺省时分别回退到 ``DEFAULT_REACT_MAX_TURNS``
    与 ``DEFAULT_SIGNAL_TOOL_NAMES``。instance/definition 策略不读这两个值；保留校验只
    是为了在 caller 主动传入时拦住明显错误的形状。
    """
    if "react_max_turns" in block:
        rt = block["react_max_turns"]
        if isinstance(rt, bool):
            raise ValueError(f"{prefix}.react_max_turns must be an integer")
        try:
            n = int(rt)
        except (TypeError, ValueError):
            raise ValueError(f"{prefix}.react_max_turns must be an integer")
        if n < 1:
            raise ValueError(f"{prefix}.react_max_turns must be >= 1")
    if "signal_tool_names" in block:
        st = block["signal_tool_names"]
        if not isinstance(st, list) or any(not isinstance(x, str) for x in st):
            raise ValueError(f"{prefix}.signal_tool_names must be an array of strings")


def _normalize_strategy_binding_block(raw: dict[str, Any]) -> dict[str, Any]:
    block = raw.get("strategy")
    if not isinstance(block, dict) or not block:
        return {}
    out: dict[str, Any] = {}
    definition_id = block.get("definition_id")
    if isinstance(definition_id, str) and definition_id.strip():
        out["strategy_definition_id"] = definition_id.strip()
    parameter_overrides = block.get("parameter_overrides")
    if isinstance(parameter_overrides, dict):
        out["strategy_parameter_overrides"] = dict(parameter_overrides)
    execution_profile = block.get("execution_profile")
    if isinstance(execution_profile, str) and execution_profile.strip():
        out["strategy_execution_profile"] = execution_profile.strip()
    return out


def validate_strategy_binding_block(strategy_binding: dict[str, Any]) -> None:
    """Require a ``definition_id`` on ``settings.strategy``.

    StrategyInstance / ``si-`` bindings were removed; the runtime resolves
    the strategy purely from ``definition_id`` + ``parameter_overrides``.
    """
    definition_id = strategy_binding.get("definition_id")
    has_definition = isinstance(definition_id, str) and definition_id.strip()
    if not has_definition:
        raise ValueError(
            "settings.strategy must bind a definition_id"
        )
    parameter_overrides = strategy_binding.get("parameter_overrides")
    if parameter_overrides is not None and not isinstance(parameter_overrides, dict):
        raise ValueError("settings.strategy.parameter_overrides must be an object")
    execution_profile = strategy_binding.get("execution_profile")
    if execution_profile is not None and not isinstance(execution_profile, str):
        raise ValueError("settings.strategy.execution_profile must be a string")


def _expand_nested_agent_block(raw: dict[str, Any]) -> dict[str, Any]:
    """Expand the ``agent`` nested block into flat keys.

    The frontend now sends settings with nested ``agent`` and ``factor`` blocks.
    This function expands ``settings["agent"]`` into flat ``agent_*`` keys so that
    :func:`cycle_task_config_from_params` can read them directly.
    """
    block = raw.get("agent")
    if not isinstance(block, dict) or not block:
        return {}
    out: dict[str, Any] = {}
    # react_max_turns
    rt = block.get("react_max_turns")
    if rt is not None:
        try:
            out["agent_react_max_turns"] = max(1, int(rt))
        except (TypeError, ValueError):
            pass
    # signal_tool_names
    st = block.get("signal_tool_names")
    if isinstance(st, list):
        out["agent_signal_tool_names"] = [str(x).strip() for x in st if str(x).strip()]
    elif isinstance(st, str):
        out["agent_signal_tool_names"] = [x.strip() for x in st.split(",") if x.strip()]
    # enabled_skills
    es = block.get("enabled_skills")
    if isinstance(es, list):
        out["agent_enabled_skills"] = [str(x).strip() for x in es if str(x).strip()]
    # position_constraints
    pc = block.get("position_constraints")
    if isinstance(pc, dict):
        v = pc.get("max_single_order_amount")
        if v is not None:
            try:
                out["agent_pc_max_single_order_amount"] = float(v)
            except (TypeError, ValueError):
                pass
        v = pc.get("max_position_ratio")
        if v is not None:
            try:
                out["agent_pc_max_position_ratio"] = float(v)
            except (TypeError, ValueError):
                pass
        v = pc.get("review_equity_fraction")
        if v is not None:
            try:
                out["agent_pc_review_equity_fraction"] = float(v)
            except (TypeError, ValueError):
                pass
        v = pc.get("lot_size")
        if v is not None:
            try:
                out["agent_pc_lot_size"] = int(v)
            except (TypeError, ValueError):
                pass
        v = pc.get("rebalance_hysteresis_lots")
        if v is not None:
            try:
                out["agent_pc_rebalance_hysteresis_lots"] = int(v)
            except (TypeError, ValueError):
                pass
        v = pc.get("max_task_position_amount")
        if v is not None:
            try:
                out["agent_pc_max_task_position_amount"] = float(v)
            except (TypeError, ValueError):
                pass
        v = pc.get("max_task_position_ratio")
        if v is not None:
            try:
                out["agent_pc_max_task_position_ratio"] = float(v)
            except (TypeError, ValueError):
                pass
    # approval
    ap = block.get("approval")
    if isinstance(ap, dict):
        mv = ap.get("min_notional_for_approval")
        if mv is not None:
            try:
                out["agent_approval_min_notional"] = float(mv)
            except (TypeError, ValueError):
                pass
        tv = ap.get("timeout_seconds")
        if tv is not None:
            try:
                out["agent_approval_timeout"] = int(tv)
            except (TypeError, ValueError):
                pass
    return out


def merge_task_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of *settings* with signal-agent defaults filled if missing.

    The ``agent`` and ``strategy`` nested blocks are expanded into flat keys so that
    :func:`cycle_task_config_from_params` can read them directly.
    """
    base: dict[str, Any] = {}
    if isinstance(settings, dict):
        for k, v in settings.items():
            if k in ("agent", "strategy"):
                continue  # handled via expansion below
            base[k] = v

    # Expand nested blocks into flat keys.
    base.update(_expand_nested_agent_block(settings or {}))
    base.update(_normalize_strategy_binding_block(settings or {}))

    # Backward compat: if no agent block but legacy root-level keys exist, copy to agent_* keys
    has_agent_block = isinstance(settings, dict) and isinstance(settings.get("agent"), dict)
    if not has_agent_block:
        if "react_max_turns" in base:
            try:
                base["agent_react_max_turns"] = max(1, int(base["react_max_turns"]))
            except (TypeError, ValueError):
                pass
        if "signal_tool_names" in base:
            base["agent_signal_tool_names"] = _normalize_signal_tool_names_value(base.get("signal_tool_names"))
        if "enabled_skills" in base:
            base["agent_enabled_skills"] = base["enabled_skills"]
    else:
        # Backward compat for position_constraints/approval: when agent block is present,
        # also migrate root-level position_constraints/approval to agent_pc_*/agent_approval_* keys
        # so factory fallback can find them.
        root_pc = settings.get("position_constraints")
        if isinstance(root_pc, dict):
            if "agent_pc_max_single_order_amount" not in base and root_pc.get("max_single_order_amount") is not None:
                try:
                    base["agent_pc_max_single_order_amount"] = float(root_pc["max_single_order_amount"])
                except (TypeError, ValueError):
                    pass
            if "agent_pc_max_position_ratio" not in base and root_pc.get("max_position_ratio") is not None:
                try:
                    base["agent_pc_max_position_ratio"] = float(root_pc["max_position_ratio"])
                except (TypeError, ValueError):
                    pass
            if "agent_pc_review_equity_fraction" not in base and root_pc.get("review_equity_fraction") is not None:
                try:
                    base["agent_pc_review_equity_fraction"] = float(root_pc["review_equity_fraction"])
                except (TypeError, ValueError):
                    pass
            if "agent_pc_lot_size" not in base and root_pc.get("lot_size") is not None:
                try:
                    base["agent_pc_lot_size"] = int(root_pc["lot_size"])
                except (TypeError, ValueError):
                    pass
            if (
                "agent_pc_rebalance_hysteresis_lots" not in base
                and root_pc.get("rebalance_hysteresis_lots") is not None
            ):
                try:
                    base["agent_pc_rebalance_hysteresis_lots"] = int(
                        root_pc["rebalance_hysteresis_lots"]
                    )
                except (TypeError, ValueError):
                    pass
            if (
                "agent_pc_max_task_position_amount" not in base
                and root_pc.get("max_task_position_amount") is not None
            ):
                try:
                    base["agent_pc_max_task_position_amount"] = float(
                        root_pc["max_task_position_amount"]
                    )
                except (TypeError, ValueError):
                    pass
            if (
                "agent_pc_max_task_position_ratio" not in base
                and root_pc.get("max_task_position_ratio") is not None
            ):
                try:
                    base["agent_pc_max_task_position_ratio"] = float(
                        root_pc["max_task_position_ratio"]
                    )
                except (TypeError, ValueError):
                    pass
        root_ap = settings.get("approval")
        if isinstance(root_ap, dict):
            if "agent_approval_min_notional" not in base and root_ap.get("min_notional_for_approval") is not None:
                try:
                    base["agent_approval_min_notional"] = float(root_ap["min_notional_for_approval"])
                except (TypeError, ValueError):
                    pass
            if "agent_approval_timeout" not in base and root_ap.get("timeout_seconds") is not None:
                try:
                    base["agent_approval_timeout"] = int(root_ap["timeout_seconds"])
                except (TypeError, ValueError):
                    pass

    # Defaults for agent fields (only when not already set by expansion).
    if "agent_react_max_turns" not in base:
        base["agent_react_max_turns"] = DEFAULT_REACT_MAX_TURNS
    else:
        try:
            base["agent_react_max_turns"] = max(1, int(base["agent_react_max_turns"]))
        except (TypeError, ValueError):
            base["agent_react_max_turns"] = DEFAULT_REACT_MAX_TURNS
    if "agent_signal_tool_names" not in base:
        base["agent_signal_tool_names"] = list(DEFAULT_SIGNAL_TOOL_NAMES)
    else:
        base["agent_signal_tool_names"] = _normalize_signal_tool_names_value(base["agent_signal_tool_names"])
    # Legacy: signal user message no longer injects prefetched quotes/ticks.
    base.pop("omit_prefetched_market", None)
    # Runtime semantics are standardized on universe; ignore deprecated watch_symbols.
    base.pop("watch_symbols", None)
    return base


def _validate_review_equity_fraction_value(raw: Any) -> None:
    """``position_constraints.review_equity_fraction`` must be in (0, 1] when present."""
    try:
        f = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("position_constraints.review_equity_fraction must be a number") from exc
    if f <= 0.0 or f > 1.0:
        raise ValueError("position_constraints.review_equity_fraction must be in (0, 1]")


def _validate_positive_position_constraint_number(field_name: str, raw: Any) -> None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"position_constraints.{field_name} must be a number") from exc
    if value <= 0.0:
        raise ValueError(f"position_constraints.{field_name} must be > 0")


def _validate_ratio_position_constraint_number(field_name: str, raw: Any) -> None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"position_constraints.{field_name} must be a number") from exc
    if value <= 0.0 or value > 1.0:
        raise ValueError(f"position_constraints.{field_name} must be in (0, 1]")


def _validate_optional_data_cache(settings: dict[str, Any]) -> None:
    """Reject a malformed ``settings.data_cache`` at API time (fail fast).

    Runs the same validator the runtime config builder uses, so a bad source
    id / continuity option is rejected on create/update rather than surfacing
    only when the worker is assembled (CLAUDE.md: 越早暴露越好).
    ``None`` / absent → no-op.
    """
    if settings.get("data_cache") is None:
        return
    from doyoutrade.data.cache_policy import parse_data_cache_policy

    parse_data_cache_policy(settings["data_cache"])


def validate_api_task_settings(settings: dict[str, Any]) -> None:
    """Validate HTTP task settings for strategy binding (definition_id)."""
    strategy_binding = settings.get("strategy")
    if not isinstance(strategy_binding, dict) or not strategy_binding:
        raise ValueError("settings.strategy must bind a definition_id")
    validate_strategy_binding_block(strategy_binding)

    agent_block = settings.get("agent", {})
    if agent_block:
        _validate_agent_signal_fields(agent_block, prefix="settings.agent")
    else:
        _validate_agent_signal_fields(settings, prefix="settings")
    _validate_optional_position_constraints(settings)
    _validate_optional_data_cache(settings)


def validate_optional_task_settings(settings: dict[str, Any]) -> None:
    """Validate optional nested keys when present (e.g. HTTP PUT with full settings)."""
    _validate_optional_position_constraints(settings)
    _validate_optional_data_cache(settings)


def _validate_lot_constraint_value(key: str, raw: Any, *, minimum: int) -> None:
    """``lot_size`` / ``rebalance_hysteresis_lots`` must be integers >= minimum.

    A non-integer float (e.g. ``100.5``) is rejected rather than truncated so
    a typo'd lot does not silently change order sizing (§错误可见性).
    """
    if isinstance(raw, bool):
        raise ValueError(f"position_constraints.{key} must be an integer")
    if isinstance(raw, float) and not raw.is_integer():
        raise ValueError(f"position_constraints.{key} must be an integer")
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"position_constraints.{key} must be an integer") from exc
    if n < minimum:
        raise ValueError(f"position_constraints.{key} must be >= {minimum}")


def _validate_optional_position_constraints(settings: dict[str, Any]) -> None:
    raw = settings.get("position_constraints")
    if not isinstance(raw, dict):
        agent = settings.get("agent")
        if isinstance(agent, dict):
            raw = agent.get("position_constraints")
    if not isinstance(raw, dict):
        return
    if "review_equity_fraction" in raw and raw["review_equity_fraction"] is not None:
        _validate_review_equity_fraction_value(raw["review_equity_fraction"])
    if "lot_size" in raw and raw["lot_size"] is not None:
        _validate_lot_constraint_value("lot_size", raw["lot_size"], minimum=1)
    if (
        "rebalance_hysteresis_lots" in raw
        and raw["rebalance_hysteresis_lots"] is not None
    ):
        _validate_lot_constraint_value(
            "rebalance_hysteresis_lots", raw["rebalance_hysteresis_lots"], minimum=0
        )
    if "max_task_position_amount" in raw and raw["max_task_position_amount"] is not None:
        _validate_positive_position_constraint_number(
            "max_task_position_amount", raw["max_task_position_amount"]
        )
    if "max_task_position_ratio" in raw and raw["max_task_position_ratio"] is not None:
        _validate_ratio_position_constraint_number(
            "max_task_position_ratio", raw["max_task_position_ratio"]
        )


def _parse_review_equity_fraction(settings: dict[str, Any] | None) -> float | None:
    """Read optional ``settings["position_constraints"]["review_equity_fraction"]``."""
    if not settings:
        return None
    raw = settings.get("position_constraints")
    if not isinstance(raw, dict):
        return None
    v = raw.get("review_equity_fraction")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_position_constraint_overrides(settings: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """Read optional per-instance risk caps from ``settings["position_constraints"]``."""
    if not settings:
        return None, None
    raw = settings.get("position_constraints")
    if not isinstance(raw, dict):
        return None, None

    def _as_float(key: str) -> float | None:
        v = raw.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return _as_float("max_single_order_amount"), _as_float("max_position_ratio")


def _parse_task_budget_constraint_overrides(
    settings: dict[str, Any] | None,
) -> tuple[float | None, float | None]:
    """Read optional task-level budget caps from ``settings["position_constraints"]``."""
    if not settings:
        return None, None
    raw = settings.get("position_constraints")
    if not isinstance(raw, dict):
        return None, None

    def _as_float(key: str) -> float | None:
        value = raw.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return _as_float("max_task_position_amount"), _as_float("max_task_position_ratio")


def _parse_lot_constraint_overrides(
    settings: dict[str, Any] | None,
) -> tuple[int | None, int | None]:
    """Read optional ``lot_size`` / ``rebalance_hysteresis_lots`` from settings."""
    if not settings:
        return None, None
    raw = settings.get("position_constraints")
    if not isinstance(raw, dict):
        return None, None

    def _as_int(key: str) -> int | None:
        v = raw.get(key)
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return _as_int("lot_size"), _as_int("rebalance_hysteresis_lots")


def _parse_approval_overrides(settings: dict[str, Any] | None) -> tuple[float | None, int | None]:
    """Read optional per-instance approval thresholds from ``settings["approval"]``."""
    if not settings:
        return None, None
    raw = settings.get("approval")
    if not isinstance(raw, dict):
        return None, None

    def _as_float_opt(key: str) -> float | None:
        v = raw.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _as_int_opt(key: str) -> int | None:
        v = raw.get(key)
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return _as_float_opt("min_notional_for_approval"), _as_int_opt("timeout_seconds")


def _parse_data_cache(settings: dict[str, Any] | None) -> "DataCachePolicy | None":
    """Read optional ``settings.data_cache`` into a validated DataCachePolicy.

    Mirrors the ``fee_config`` / ``account_id`` contract: a malformed value
    raises a ``ValueError`` (surfaced as a config validation error) rather than
    being silently coerced to a default. A typo in the continuity strictness or
    an unknown field must fail at config time, not get swallowed into
    "ran but the policy did nothing" (§错误可见性). ``None`` / absent → ``None``
    (assembly applies the legacy hard-coded defaults).
    """
    if not settings:
        return None
    raw = settings.get("data_cache")
    if raw is None:
        return None
    # Local import keeps this sync config builder free of the data-layer import
    # graph at module load (the file deliberately has no heavy imports).
    from doyoutrade.data.cache_policy import parse_data_cache_policy

    return parse_data_cache_policy(raw)


def cycle_task_config_from_params(
    *,
    name: str,
    mode: str,
    description: str = "",
    data_provider: str | None = None,
    universe: list[str] | tuple[str, ...] | None = None,
    settings: dict[str, Any] | None = None,
    model_route_name_override: str | None = None,
) -> CycleTaskConfig:
    """Build config from API / persistence fields (watch list + ``settings`` JSON)."""
    merged = merge_task_settings(settings)
    mrn: str | None = None
    ovr = (model_route_name_override or "").strip()
    if ovr:
        mrn = ovr
    else:
        raw_mrn = merged.get("model_route_name")
        if isinstance(raw_mrn, str) and raw_mrn.strip():
            mrn = raw_mrn.strip()
    # execution_strategy in settings maps to strategy_preferences (supports migration).
    strat = ""
    if merged.get("execution_strategy") is not None:
        strat = str(merged["execution_strategy"])
    elif merged.get("strategy_preferences") is not None:
        strat = str(merged["strategy_preferences"])
    max_amt, max_ratio = _parse_position_constraint_overrides(merged)
    task_budget_amount, task_budget_ratio = _parse_task_budget_constraint_overrides(merged)
    lot_size_override, hysteresis_override = _parse_lot_constraint_overrides(merged)
    min_notional, approval_timeout = _parse_approval_overrides(merged)
    # ``@watchlist:<tag>`` tokens (incl. ``@watchlist:*``) are passed through
    # verbatim here — they are expanded into concrete symbols on the async
    # worker-assembly path via
    # ``doyoutrade.runtime.watchlist_universe.resolve_watchlist_universe`` so the
    # expansion is observable (emits ``watchlist_universe_resolved``). Do NOT
    # resolve them in this sync config builder.
    uni = tuple(str(s) for s in (universe or ()))
    if merged.get("universe") is not None:
        uni = tuple(str(s) for s in merged["universe"])
    # Read agent fields from expanded agent block (agent_* keys).
    raw_rt = merged.get("agent_react_max_turns")
    try:
        react_turns = max(1, int(raw_rt))
    except (TypeError, ValueError):
        react_turns = DEFAULT_REACT_MAX_TURNS
    raw_st = merged.get("agent_signal_tool_names")
    signal_tools = tuple(str(x) for x in _normalize_signal_tool_names_value(raw_st))
    enabled_skills = tuple(str(s) for s in (merged.get("agent_enabled_skills") or []))
    # Position constraints from agent block override parse from root (for API flow).
    agent_pc_max_amt = merged.get("agent_pc_max_single_order_amount")
    if agent_pc_max_amt is not None:
        try:
            max_amt = float(agent_pc_max_amt)
        except (TypeError, ValueError):
            pass
    agent_pc_max_ratio = merged.get("agent_pc_max_position_ratio")
    if agent_pc_max_ratio is not None:
        try:
            max_ratio = float(agent_pc_max_ratio)
        except (TypeError, ValueError):
            pass
    agent_pc_ref_frac = merged.get("agent_pc_review_equity_fraction")
    if agent_pc_ref_frac is not None:
        try:
            ref_frac = float(agent_pc_ref_frac)
            if not (ref_frac <= 0.0 or ref_frac > 1.0):
                pass  # valid
            else:
                ref_frac = None
        except (TypeError, ValueError):
            ref_frac = None
    else:
        ref_frac = _parse_review_equity_fraction(merged)
        if ref_frac is not None and (ref_frac <= 0.0 or ref_frac > 1.0):
            ref_frac = None
    agent_pc_lot_size = merged.get("agent_pc_lot_size")
    if agent_pc_lot_size is not None:
        try:
            lot_size_override = int(agent_pc_lot_size)
        except (TypeError, ValueError):
            pass
    agent_pc_hysteresis = merged.get("agent_pc_rebalance_hysteresis_lots")
    if agent_pc_hysteresis is not None:
        try:
            hysteresis_override = int(agent_pc_hysteresis)
        except (TypeError, ValueError):
            pass
    agent_pc_task_budget_amount = merged.get("agent_pc_max_task_position_amount")
    if agent_pc_task_budget_amount is not None:
        try:
            task_budget_amount = float(agent_pc_task_budget_amount)
        except (TypeError, ValueError):
            pass
    agent_pc_task_budget_ratio = merged.get("agent_pc_max_task_position_ratio")
    if agent_pc_task_budget_ratio is not None:
        try:
            task_budget_ratio = float(agent_pc_task_budget_ratio)
            if task_budget_ratio <= 0.0 or task_budget_ratio > 1.0:
                task_budget_ratio = None
        except (TypeError, ValueError):
            task_budget_ratio = None
    # Approval from agent block.
    agent_min_notional = merged.get("agent_approval_min_notional")
    if agent_min_notional is not None:
        try:
            min_notional = float(agent_min_notional)
        except (TypeError, ValueError):
            pass
    agent_approval_timeout = merged.get("agent_approval_timeout")
    if agent_approval_timeout is not None:
        try:
            approval_timeout = int(agent_approval_timeout)
        except (TypeError, ValueError):
            pass

    raw_strategy_definition_id = merged.get("strategy_definition_id")
    strategy_definition_id = (
        str(raw_strategy_definition_id).strip()
        if isinstance(raw_strategy_definition_id, str)
        else ""
    )
    raw_strategy_parameter_overrides = merged.get("strategy_parameter_overrides")
    strategy_parameter_overrides = (
        dict(raw_strategy_parameter_overrides)
        if isinstance(raw_strategy_parameter_overrides, dict)
        else {}
    )
    raw_strategy_execution_profile = merged.get("strategy_execution_profile")
    strategy_execution_profile = (
        str(raw_strategy_execution_profile).strip()
        if isinstance(raw_strategy_execution_profile, str) and raw_strategy_execution_profile.strip()
        else "default"
    )

    raw_account_id = merged.get("account_id")
    account_id: str = ""
    if raw_account_id is not None:
        if not isinstance(raw_account_id, str):
            raise ValueError(
                f"settings.account_id must be a string, got {type(raw_account_id).__name__}: "
                f"{raw_account_id!r}"
            )
        account_id = raw_account_id.strip()

    raw_fee_config = merged.get("fee_config")
    fee_config: dict[str, Any] | None = None
    if raw_fee_config is not None:
        if not isinstance(raw_fee_config, dict):
            raise ValueError(
                f"settings.fee_config must be an object, got {type(raw_fee_config).__name__}: "
                f"{raw_fee_config!r}"
            )
        fee_config = dict(raw_fee_config)

    raw_protection = merged.get("protection")
    protection_config: dict[str, Any] | None = None
    if raw_protection is not None:
        if not isinstance(raw_protection, dict):
            raise ValueError(
                f"settings.protection must be an object, got {type(raw_protection).__name__}: "
                f"{raw_protection!r}"
            )
        protection_config = dict(raw_protection)

    data_cache = _parse_data_cache(merged)

    return CycleTaskConfig(
        name=name,
        mode=mode,
        description=description,
        data_provider=data_provider,
        universe=uni,
        strategy_preferences=strat,
        max_single_order_amount=max_amt,
        max_position_ratio=(
            DEFAULT_MAX_POSITION_RATIO if max_ratio is None else max_ratio
        ),
        review_equity_fraction=(
            DEFAULT_REVIEW_EQUITY_FRACTION if ref_frac is None else ref_frac
        ),
        lot_size=(
            DEFAULT_LOT_SIZE if lot_size_override is None else lot_size_override
        ),
        rebalance_hysteresis_lots=(
            DEFAULT_REBALANCE_HYSTERESIS_LOTS
            if hysteresis_override is None
            else hysteresis_override
        ),
        max_task_position_amount=task_budget_amount,
        max_task_position_ratio=task_budget_ratio,
        min_notional_for_approval=(
            DEFAULT_MIN_NOTIONAL_FOR_APPROVAL if min_notional is None else min_notional
        ),
        approval_timeout_seconds=(
            DEFAULT_APPROVAL_TIMEOUT_SECONDS
            if approval_timeout is None
            else approval_timeout
        ),
        react_max_turns=max(1, react_turns),
        signal_tool_names=signal_tools,
        enabled_skills=enabled_skills,
        model_route_name=mrn,
        strategy_definition_id=strategy_definition_id,
        strategy_parameter_overrides=strategy_parameter_overrides,
        strategy_execution_profile=strategy_execution_profile,
        account_id=account_id,
        fee_config=fee_config,
        protection_config=protection_config,
        data_cache=data_cache,
    )


@dataclass
class CycleTaskConfig:
    name: str
    mode: str
    description: str = ""
    # Overrides config.data.default_provider when set (auto | mock | qmt or custom registered id).
    data_provider: str | None = None
    # Tradable universe for data stack / risk allowlist; empty means no symbols this cycle (no global fallback).
    universe: tuple[str, ...] = ()
    # Plain-text user strategy notes; injected into signal prompt as non-binding guidance.
    strategy_preferences: str = ""
    # Risk / review cap (from ``settings.position_constraints``); ``None`` = no per-order cap.
    max_single_order_amount: float | None = None
    max_position_ratio: float = DEFAULT_MAX_POSITION_RATIO
    # Live-mode approval queue thresholds (from ``settings.approval`` or defaults).
    min_notional_for_approval: float = DEFAULT_MIN_NOTIONAL_FOR_APPROVAL
    approval_timeout_seconds: int = DEFAULT_APPROVAL_TIMEOUT_SECONDS
    # Signal agent (from ``settings.react_max_turns`` / ``settings.signal_tool_names`` or defaults).
    react_max_turns: int = DEFAULT_REACT_MAX_TURNS
    signal_tool_names: tuple[str, ...] = DEFAULT_SIGNAL_TOOL_NAMES
    # Allowed skill names; empty means all skills accessible (skills_enabled=True with no filter).
    enabled_skills: tuple[str, ...] = ()
    #: Review target notional: ``min(max_single_order_amount, equity * f)`` if cap set, else ``equity * f``.
    review_equity_fraction: float = DEFAULT_REVIEW_EQUITY_FRACTION
    #: Exchange board lot (shares) for the explicit target_quantity / target_exposure
    #: rebalance paths. ``1`` = whole-share trading; A股 grids set ``100``.
    lot_size: int = DEFAULT_LOT_SIZE
    #: Rebalance dead band in lots for the explicit-target paths. ``0`` = disabled.
    rebalance_hysteresis_lots: int = DEFAULT_REBALANCE_HYSTERESIS_LOTS
    #: Optional task-level total marked-to-market position cap in account
    #: currency. ``None`` = disabled.
    max_task_position_amount: float | None = None
    #: Optional task-level total marked-to-market position cap as a fraction of
    #: account equity. ``None`` = disabled.
    max_task_position_ratio: float | None = None
    #: Optional named DB model route. Strategy task execution may omit this.
    model_route_name: str | None = None
    #: Primary strategy runtime binding — the definition_id to execute.
    strategy_definition_id: str = ""
    strategy_parameter_overrides: dict[str, Any] = field(default_factory=dict)
    strategy_execution_profile: str = "default"
    #: Selected account (``acct-...``) for this task — resolved against the
    #: ``accounts`` table at cycle time (see ``_resolve_worker_account``). Empty
    #: means "use the default account". The account record itself carries the
    #: ``live`` | ``mock`` mode and the QMT connection / mock portfolio.
    account_id: str = ""
    #: Optional A-share transaction-fee config (from ``settings.fee_config``).
    #: ``None`` / empty → no transaction cost (default; backtest numbers
    #: unchanged). When set, the backtest ledger deducts 佣金/印花税/过户费 per
    #: fill. See ``doyoutrade.execution.fees.fee_model_from_config``.
    fee_config: dict[str, Any] | None = None
    #: Optional portfolio circuit-breaker config (from ``settings.protection``).
    #: ``None`` / empty → no protection phase (default). When set, the worker
    #: halts new BUY entries on a peak-to-trough drawdown breach. See
    #: ``doyoutrade.execution.protection.protection_engine_from_config``.
    protection_config: dict[str, Any] | None = None
    #: Optional per-task data-cache / backfill / continuity policy (from
    #: ``settings.data_cache``). ``None`` → assembly uses the legacy hard-coded
    #: behaviour (local-first read, auto-backfill, calendar continuity, fail on
    #: unverifiable gap). See ``doyoutrade.data.cache_policy.DataCachePolicy``.
    data_cache: "DataCachePolicy | None" = None


@dataclass
class CycleTask:
    config: CycleTaskConfig
    worker: object
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "configured"
    last_error: str = ""
