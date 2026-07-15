from __future__ import annotations

import json
from typing import Any

from doyoutrade.money.decimal_helpers import json_default_with_decimals


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=json_default_with_decimals)


def build_strategy_authoring_contract(class_name: str) -> dict[str, Any]:
    """Authoring contract surfaced to agents that edit strategy source.

    Mirrors the public ``Strategy`` API + ``StrategyCompiler`` rule set so
    a model writing or repairing a definition has the same checklist the
    compiler uses to accept / reject the source.
    """
    return {
        "required_class_name": class_name,
        "required_base_class": "Strategy",
        "required_method": "on_bar(self, df, ctx) -> Signal",
        "optional_methods": [
            "informative_data(self, ctx) -> list[DataRequest]",
            "populate_indicators(self, df, ctx) -> pandas.DataFrame",
            "on_strategy_start(self, ctx) / on_cycle_start(self, ctx)",
            "@informative('1w') / @informative('1d', symbol='600519.SH') / @informative_each('1d', symbols=(...))",
        ],
        "class_attributes": {
            "name": "str — display name (defaults to class name).",
            "timeframe": (
                'str — bar frequency for on_bar evaluation. One of: '
                '"1m" / "5m" / "15m" / "30m" / "60m" / "1d" / "1w" / "1mo". '
                'Default "1d". (Hourly is "60m" not "1h"; monthly is "1mo".)'
            ),
            "startup_history": (
                "int — minimum base-timeframe bars provisioned before "
                "populate_indicators / on_bar are called. Must >= the "
                "longest rolling window the strategy uses; the compiler "
                "rejects rolling(N) literals where N > startup_history "
                "with error_code=history_check_literal_disallowed."
            ),
        },
        "tunable_parameters": [
            'IntParameter(low, high, default=...)',
            'DecimalParameter(low, high, default=..., decimals=...)',
            'CategoricalParameter([...], default=...)',
            'BooleanParameter(default=...)',
            "Read at runtime via self.<name>.value.",
        ],
        "allowed_imports": [
            "__future__",
            "decimal",
            "math",
            "numpy",
            "pandas",
            "doyoutrade.strategy_sdk",
            "typing",
        ],
        "signal_rules": [
            "Signal.buy(tag='factor_id') — target-state long. tag mandatory.",
            "Signal.sell(tag='exit_reason') — target-state flat / close long. tag mandatory.",
            "Signal.target_exposure(target=0.5, tag='grid_l2') — rebalance toward explicit long exposure as a fraction of equity.",
            "Signal.target_quantity(quantity=300, tag='grid_l3') — strict inventory target in shares; emits only the share delta needed to reach that post-cycle quantity.",
            "Signal.hold() — no opinion this cycle; current position preserved.",
            "Read ctx.position.is_long / current_profit to inform decisions.",
            "Never manage stops / sizing / orders directly — that's PositionManager's job.",
        ],
        "data_access": [
            "ctx.dp.get_bars(symbol=None, *, window, freq='1d', fields=None) — current symbol's bars by default.",
            "ctx.dp.get_index_bars(code, *, window, freq='1d') — market index / ETF.",
            "ctx.dp.get_industry_members(industry=None, *, top_n=20) — peer list.",
            "ctx.dp.get_peer_bars(*, window, top_n=20, industry=None) — peer DataFrames.",
            "ctx.dp.get_fundamentals(symbol=None, *, fields=(...)) — latest fundamentals.",
            "All cross-symbol access must ALSO be declared in informative_data().",
        ],
        "ast_rules": [
            "disallowed_import — no import outside the whitelist above.",
            "missing_on_bar — on_bar must be implemented (abstract on Strategy).",
            "missing_signal_tag — Signal.buy() / Signal.sell() / Signal.target_exposure() / Signal.target_quantity() must have tag= kwarg.",
            "invalid_target_exposure — Signal.target_exposure(target=...) must be in [0, 1].",
            "invalid_target_quantity — Signal.target_quantity(quantity=...) must be >= 0.",
            "lookahead_access — df.iloc[i] with i >= 0 / df.shift(-n) reads forward.",
            "populate_cross_symbol_access — ctx.dp.get_bars(symbol=other) forbidden inside populate_indicators.",
            "silent_exception_swallow — except Exception: pass / continue forbidden.",
            "silent_type_coercion — `if not isinstance(x, T): x = default` patterns forbidden.",
            "unknown_dp_method — ctx.dp.<name>() must be a registered method.",
            "unknown_data_request_type — DataRequest.<name>() must be a registered factory.",
            "invalid_class_attribute — timeframe / startup_history / name must be valid types.",
            "history_check_literal_disallowed — rolling(N) literal must not exceed startup_history.",
        ],
        "recommended_workflow": [
            "Call list_dp_methods + list_data_requests + list_indicators to discover available APIs.",
            "Open an authoring session with open_strategy_authoring, then write_file / edit_file to put code on disk.",
            "Call compile_strategy_draft(session_id=...) — runs compile + smoke with zero side effects.",
            "Fix every error_code listed before declaring done; do not paper over with silent fallbacks.",
            "Once green, call finalize_strategy_authoring to persist and bind a backtest.",
        ],
        "forbidden_patterns": [
            "Changing the stored class name during an update.",
            "Renaming on_bar.",
            "Reading df.iloc[i] with i >= 0 (lookahead).",
            "Returning Signal.buy() without tag=.",
            "Implementing stops / sizing / orders inside the strategy (PositionManager's job).",
            "Importing modules outside the SDK whitelist.",
        ],
        "diagnostic_events": [
            "Each cycle emits strategy_runner_cycle with signals_buy / signals_sell / signals_hold / signals_target_exposure / signals_target_quantity counts plus per_symbol_tags (every symbol appears; untagged Signal.hold collapses to <untagged_hold> — always tag holds for diagnosability).",
            "Aggregated per-run timeline lives on debug_view.signal_timeline (NOT cycle_runs[i]); compact summary on debug_view.signal_timeline_summary survives tool-result truncation.",
            "Failure paths emit strategy_<phase>_failed with error_code / hint.",
            "Per-method ctx.dp.* call emits strategy_dp_<method> / strategy_dp_<method>_failed.",
        ],
    }
