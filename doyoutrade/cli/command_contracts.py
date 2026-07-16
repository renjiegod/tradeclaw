"""Machine-readable CLI command contracts for agent self-discovery.

``doyoutrade-cli schema <path>`` describes the shell command surface:
flag names, mappings to OpenAPI request fields, semantic identifier kinds,
and examples.
Keep this declarative so skills and prompt snippets can be validated against
the same source instead of relying on hand-written CLI guesses.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


_CONTRACTS: dict[str, dict[str, Any]] = {
    "strategy.authoring.open": {
        "command_path": "strategy authoring open",
        "invocation": "doyoutrade-cli strategy authoring open",
        "flags": [
            {
                "name": "name",
                "maps_to": "name",
                "type": "string",
                "required": False,
                "description": "Display name for a new strategy definition.",
            },
            {
                "name": "definition-id",
                "maps_to": "definition_id",
                "type": "string",
                "required": False,
                "semantic": "strategy_definition_id",
                "accepts_prefix": "sd-",
                "description": "Existing strategy definition id to copy into a draft.",
            },
        ],
        "required_one_of": [["name"], ["definition-id"]],
        "examples": [
            "doyoutrade-cli strategy authoring open --name <display-name>",
            "doyoutrade-cli strategy authoring open --definition-id <sd-...>",
        ],
    },
    "strategy.authoring.compile": {
        "command_path": "strategy authoring compile",
        "invocation": "doyoutrade-cli strategy authoring compile",
        "flags": [
            {
                "name": "session-id",
                "maps_to": "session_id",
                "type": "string",
                "required": True,
                "semantic": "strategy_authoring_session_id",
                "accepts_prefix": "sess-",
                "description": "Authoring session id returned by authoring open.",
            }
        ],
        "examples": ["doyoutrade-cli strategy authoring compile --session-id <sess-...>"],
    },
    "strategy.authoring.cancel": {
        "command_path": "strategy authoring cancel",
        "invocation": "doyoutrade-cli strategy authoring cancel",
        "flags": [
            {
                "name": "session-id",
                "maps_to": "session_id",
                "type": "string",
                "required": True,
                "semantic": "strategy_authoring_session_id",
                "accepts_prefix": "sess-",
                "description": "Authoring session id returned by authoring open.",
            }
        ],
        "examples": ["doyoutrade-cli strategy authoring cancel --session-id <sess-...>"],
    },
    "strategy.authoring.finalize": {
        "command_path": "strategy authoring finalize",
        "invocation": "doyoutrade-cli strategy authoring finalize",
        "flags": [
            {
                "name": "session-id",
                "maps_to": "session_id",
                "type": "string",
                "required": True,
                "semantic": "strategy_authoring_session_id",
                "accepts_prefix": "sess-",
                "description": "Authoring session id returned by authoring open.",
            }
        ],
        "examples": ["doyoutrade-cli strategy authoring finalize --session-id <sess-...>"],
    },
    "backtest.run": {
        "command_path": "backtest run",
        "invocation": "doyoutrade-cli backtest run",
        "flags": [
            {
                "name": "task",
                "maps_to": "task_id",
                "type": "string",
                "required": False,
                "semantic": "task_id",
                "description": "Existing backtest task id.",
            },
            {
                "name": "definition",
                "maps_to": "definition_id",
                "type": "string",
                "required": False,
                "semantic": "strategy_definition_id",
                "accepts_prefix": "sd-",
                "description": "Strategy definition id for auto-created backtest task.",
            },
            {
                "name": "params",
                "maps_to": "parameters",
                "type": "json_object",
                "required": False,
                "description": "Strategy parameter overrides when using --definition.",
            },
            {
                "name": "range-start",
                "maps_to": "range_start",
                "type": "date",
                "required": True,
                "description": "Inclusive start date YYYY-MM-DD.",
            },
            {
                "name": "range-end",
                "maps_to": "range_end",
                "type": "date",
                "required": True,
                "description": "Inclusive end date YYYY-MM-DD.",
            },
            {
                "name": "universe",
                "maps_to": "universe",
                "type": "csv",
                "required": False,
                "description": "Comma-separated symbols; required in definition mode.",
            },
            {"name": "name", "maps_to": "name", "type": "string", "required": False},
            {"name": "data-provider", "maps_to": "data_provider", "type": "string", "required": False},
            {"name": "config-overrides", "maps_to": "config_overrides", "type": "json_object", "required": False},
            {"name": "market-profile", "maps_to": "market_profile", "type": "string", "required": False},
            {"name": "bar-interval", "maps_to": "bar_interval", "type": "string", "required": False},
            {"name": "model-route", "maps_to": "model_route_name", "type": "string", "required": False},
            {"name": "debug", "maps_to": "debug_enabled", "type": "bool", "required": False},
            {"name": "timeout", "maps_to": "timeout_seconds", "type": "number", "required": False},
            {
                "name": "poll-interval",
                "maps_to": "poll_interval_seconds",
                "type": "number",
                "required": False,
            },
            {"name": "progress", "maps_to": "progress", "type": "bool", "required": False},
        ],
        "required_one_of": [["task"], ["definition"]],
        "mutually_exclusive": [
            ["task", "definition"],
        ],
        "conditional_required": [
            {
                "when": {"flag": "definition"},
                "required": ["universe"],
                "reason": "definition mode auto-creates a backtest task and needs a symbol universe.",
            }
        ],
        "examples": [
            "doyoutrade-cli backtest run --task <task-id> --range-start YYYY-MM-DD --range-end YYYY-MM-DD",
            "doyoutrade-cli backtest run --definition <sd-...> --params '{\"window\":14}' --universe <symbol> --range-start YYYY-MM-DD --range-end YYYY-MM-DD",
        ],
    },
    "backtest.summary": {
        "command_path": "backtest summary",
        "invocation": "doyoutrade-cli backtest summary",
        "arguments": [
            {
                "name": "run_id",
                "type": "string",
                "required": True,
                "semantic": "backtest_run_id",
                "accepts_prefix": "btjob-",
            }
        ],
        "flags": [
            {
                "name": "format",
                "maps_to": "format",
                "type": "choice",
                "choices": ["markdown", "json"],
                "required": False,
            }
        ],
        "examples": ["doyoutrade-cli backtest summary <run-id> --format markdown"],
    },
    "backtest.watch": {
        "command_path": "backtest watch",
        "invocation": "doyoutrade-cli backtest watch",
        "arguments": [
            {
                "name": "run_id",
                "type": "string",
                "required": True,
                "semantic": "backtest_run_id",
                "accepts_prefix": "btjob-",
            }
        ],
        "flags": [
            {"name": "interval", "maps_to": "interval", "type": "number", "required": False},
            {"name": "max-events", "maps_to": "max_events", "type": "integer", "required": False},
            {"name": "timeout", "maps_to": "timeout", "type": "number", "required": False},
            {
                "name": "until",
                "maps_to": "until",
                "type": "choice",
                "choices": ["terminal", "none"],
                "required": False,
            },
        ],
        "examples": ["doyoutrade-cli backtest watch <run-id> --until terminal"],
    },
    "data.run": {
        "command_path": "data run",
        "invocation": "doyoutrade-cli data run",
        "arguments": [
            {
                "name": "code",
                "type": "string",
                "required": False,
                "semantic": "canonical_symbol",
                "description": (
                    "Single canonical CODE.EXCHANGE symbol. Mutually "
                    "exclusive with --symbols / --universe-file; pass "
                    "exactly one of the three input modes."
                ),
            }
        ],
        "flags": [
            {"name": "symbols", "maps_to": "symbols", "type": "csv_or_json_array", "required": False},
            {"name": "universe-file", "maps_to": "universe_file", "type": "path", "required": False},
            {"name": "period", "maps_to": "period", "type": "string", "required": False},
            {"name": "start", "maps_to": "start_date", "type": "date", "required": False},
            {"name": "range-start", "maps_to": "start_date", "type": "date", "required": False},
            {"name": "end", "maps_to": "end_date", "type": "date", "required": False},
            {"name": "range-end", "maps_to": "end_date", "type": "date", "required": False},
            {"name": "interval", "maps_to": "interval", "type": "string", "required": False},
            {
                "name": "data-source",
                "maps_to": "data_source",
                "type": "choice",
                "choices": ["auto", "qmt", "akshare", "tushare", "baostock", "mootdx"],
                "required": False,
            },
            {"name": "indicators", "maps_to": "indicators", "type": "csv_or_json_array", "required": False},
            {
                "name": "indicator-params",
                "maps_to": "indicator_params",
                "type": "json_object",
                "required": False,
            },
            {"name": "script", "maps_to": "script", "type": "string", "required": False},
            {"name": "script-file", "maps_to": "script_file", "type": "path", "required": False},
            {"name": "script-params", "maps_to": "script_params", "type": "json_object", "required": False},
            {"name": "script-timeout", "maps_to": "script_timeout", "type": "number", "required": False},
            {"name": "warmup-bars", "maps_to": "warmup_bars", "type": "integer", "required": False},
            {"name": "tail", "maps_to": "tail", "type": "integer", "required": False},
        ],
        "mutually_exclusive": [
            ["code", "symbols", "universe-file"],
            ["period", "start", "range-start", "end", "range-end"],
            ["script", "script-file"],
        ],
        "examples": [
            "doyoutrade-cli data run 600519.SH --start YYYY-MM-DD --end YYYY-MM-DD --indicators rsi,macd --warmup-bars 120",
            "doyoutrade-cli data run --symbols 600519.SH,000001.SZ --period 6m --indicators rsi",
            "doyoutrade-cli data run --universe-file /tmp/u.txt --period 6m --script-file ./factor.py --script-params '{\"window\":20}'",
        ],
    },
    "data.sync": {
        "command_path": "data sync",
        "invocation": "doyoutrade-cli data sync",
        "arguments": [
            {
                "name": "symbol",
                "type": "string",
                "required": True,
                "semantic": "canonical_symbol",
                "description": (
                    "Single canonical CODE.EXCHANGE symbol to warm into the local "
                    "market_bars warehouse so a later 'stock screen' reads it locally."
                ),
            }
        ],
        "flags": [
            {"name": "start", "maps_to": "start", "type": "date", "required": True},
            {"name": "end", "maps_to": "end", "type": "date", "required": True},
            {
                "name": "interval",
                "maps_to": "interval",
                "type": "choice",
                "choices": ["1d", "5m"],
                "required": False,
            },
            {
                "name": "mode",
                "maps_to": "mode",
                "type": "choice",
                "choices": ["fill_gap", "force_refresh"],
                "required": False,
            },
            {"name": "provider", "maps_to": "provider", "type": "string", "required": False},
            {"name": "adjust", "maps_to": "adjust", "type": "string", "required": False},
        ],
        "examples": [
            "doyoutrade-cli data sync 600519.SH --start YYYY-MM-DD --end YYYY-MM-DD",
            "doyoutrade-cli data sync 300750.SZ --start YYYY-MM-DD --end YYYY-MM-DD --mode force_refresh",
        ],
    },
    "assistant.agent.create": {
        "command_path": "assistant agent create",
        "invocation": "doyoutrade-cli assistant agent create",
        "flags": [
            {"name": "name", "maps_to": "name", "type": "string", "required": True},
            {
                "name": "status",
                "maps_to": "status",
                "type": "choice",
                "choices": ["active", "inactive"],
                "required": False,
            },
            {
                "name": "system-prompt",
                "maps_to": "system_prompt",
                "type": "string",
                "required": False,
                "description": "Inline raw system prompt text.",
            },
            {
                "name": "system-prompt-file",
                "maps_to": "system_prompt",
                "type": "path",
                "required": False,
                "description": "UTF-8 file containing the raw system prompt.",
            },
            {
                "name": "prompt-template-id",
                "maps_to": "prompt_template_id",
                "type": "string",
                "required": False,
                "description": "Built-in prompt template id, e.g. main-agent.",
            },
            {"name": "model-route", "maps_to": "model_route_name", "type": "string", "required": False},
            {
                "name": "tool",
                "maps_to": "tool_names",
                "type": "repeatable_string",
                "required": False,
                "description": "Repeat to set an explicit tool allowlist.",
            },
            {
                "name": "tool-config",
                "maps_to": "tool_configs",
                "type": "repeatable_string",
                "required": False,
                "description": "Repeat NAME or NAME=LOAD_MODE (base|deferred) to set tool configs explicitly.",
            },
            {
                "name": "tool-configs-json",
                "maps_to": "tool_configs",
                "type": "json_array",
                "required": False,
                "description": "Explicit tool_configs array; mutually exclusive with --tool / --tool-config.",
            },
            {
                "name": "skill",
                "maps_to": "skill_names",
                "type": "repeatable_string",
                "required": False,
                "description": "Repeat to set the initial skill allowlist.",
            },
            {"name": "max-turns", "maps_to": "max_turns", "type": "integer", "required": False},
            {
                "name": "context-compaction-json",
                "maps_to": "context_compaction",
                "type": "json_object",
                "required": False,
            },
            {"name": "compaction-enabled", "maps_to": "context_compaction.enabled", "type": "boolean", "required": False},
            {"name": "compaction-mode", "maps_to": "context_compaction.mode", "type": "string", "required": False},
            {"name": "compaction-auto-threshold-tokens", "maps_to": "context_compaction.auto_threshold_tokens", "type": "integer", "required": False},
            {"name": "compaction-warning-threshold-tokens", "maps_to": "context_compaction.warning_threshold_tokens", "type": "integer", "required": False},
            {"name": "compaction-preserve-recent-messages", "maps_to": "context_compaction.preserve_recent_messages", "type": "integer", "required": False},
            {"name": "compaction-preserve-recent-tool-pairs", "maps_to": "context_compaction.preserve_recent_tool_pairs", "type": "integer", "required": False},
            {"name": "compaction-micro-enabled", "maps_to": "context_compaction.micro_compaction_enabled", "type": "boolean", "required": False},
            {"name": "compaction-tool-result-max-chars", "maps_to": "context_compaction.tool_result_max_chars", "type": "integer", "required": False},
            {"name": "compaction-full-enabled", "maps_to": "context_compaction.full_compaction_enabled", "type": "boolean", "required": False},
            {"name": "compaction-summary-model-route", "maps_to": "context_compaction.summary_model_route_name", "type": "string", "required": False},
            {"name": "compaction-allow-slash-compact", "maps_to": "context_compaction.allow_slash_compact", "type": "boolean", "required": False},
        ],
        "required_one_of": [["system-prompt"], ["system-prompt-file"], ["prompt-template-id"]],
        "mutually_exclusive": [["system-prompt", "system-prompt-file"], ["tool", "tool-config", "tool-configs-json"]],
        "examples": [
            "doyoutrade-cli assistant agent create --name \"Validation Agent\" --prompt-template-id main-agent --skill strategy-authoring --skill doyoutrade-data",
            "doyoutrade-cli assistant agent create --name \"Custom Agent\" --system-prompt-file ./prompt.txt --tool read_file --tool execute_bash",
            "doyoutrade-cli assistant agent create --name \"Context Agent\" --system-prompt \"...\" --tool-config execute_bash=deferred --compaction-mode manual --compaction-summary-model-route summary-route",
        ],
    },
    "assistant.agent.update": {
        "command_path": "assistant agent update",
        "invocation": "doyoutrade-cli assistant agent update",
        "arguments": [
            {
                "name": "agent_id",
                "type": "string",
                "required": True,
                "description": "Target assistant agent id.",
            }
        ],
        "flags": [
            {"name": "name", "maps_to": "name", "type": "string", "required": False},
            {
                "name": "status",
                "maps_to": "status",
                "type": "choice",
                "choices": ["active", "inactive"],
                "required": False,
            },
            {"name": "system-prompt", "maps_to": "system_prompt", "type": "string", "required": False},
            {"name": "system-prompt-file", "maps_to": "system_prompt", "type": "path", "required": False},
            {"name": "prompt-template-id", "maps_to": "prompt_template_id", "type": "string", "required": False},
            {
                "name": "clear-prompt-template",
                "maps_to": "prompt_template_id",
                "type": "boolean",
                "required": False,
            },
            {"name": "model-route", "maps_to": "model_route_name", "type": "string", "required": False},
            {"name": "clear-model-route", "maps_to": "model_route_name", "type": "boolean", "required": False},
            {
                "name": "tool",
                "maps_to": "tool_names",
                "type": "repeatable_string",
                "required": False,
                "description": "Replace the tool allowlist with the provided values.",
            },
            {"name": "clear-tools", "maps_to": "tool_names", "type": "boolean", "required": False},
            {"name": "add-tool", "maps_to": "tool_names", "type": "repeatable_string", "required": False},
            {"name": "remove-tool", "maps_to": "tool_names", "type": "repeatable_string", "required": False},
            {"name": "tool-config", "maps_to": "tool_configs", "type": "repeatable_string", "required": False},
            {"name": "tool-configs-json", "maps_to": "tool_configs", "type": "json_array", "required": False},
            {
                "name": "skill",
                "maps_to": "skill_names",
                "type": "repeatable_string",
                "required": False,
                "description": "Replace the skill allowlist with the provided values.",
            },
            {"name": "clear-skills", "maps_to": "skill_names", "type": "boolean", "required": False},
            {"name": "add-skill", "maps_to": "skill_names", "type": "repeatable_string", "required": False},
            {"name": "remove-skill", "maps_to": "skill_names", "type": "repeatable_string", "required": False},
            {"name": "max-turns", "maps_to": "max_turns", "type": "integer", "required": False},
            {"name": "context-compaction-json", "maps_to": "context_compaction", "type": "json_object", "required": False},
            {"name": "compaction-enabled", "maps_to": "context_compaction.enabled", "type": "boolean", "required": False},
            {"name": "compaction-mode", "maps_to": "context_compaction.mode", "type": "string", "required": False},
            {"name": "compaction-auto-threshold-tokens", "maps_to": "context_compaction.auto_threshold_tokens", "type": "integer", "required": False},
            {"name": "compaction-warning-threshold-tokens", "maps_to": "context_compaction.warning_threshold_tokens", "type": "integer", "required": False},
            {"name": "compaction-preserve-recent-messages", "maps_to": "context_compaction.preserve_recent_messages", "type": "integer", "required": False},
            {"name": "compaction-preserve-recent-tool-pairs", "maps_to": "context_compaction.preserve_recent_tool_pairs", "type": "integer", "required": False},
            {"name": "compaction-micro-enabled", "maps_to": "context_compaction.micro_compaction_enabled", "type": "boolean", "required": False},
            {"name": "compaction-tool-result-max-chars", "maps_to": "context_compaction.tool_result_max_chars", "type": "integer", "required": False},
            {"name": "compaction-full-enabled", "maps_to": "context_compaction.full_compaction_enabled", "type": "boolean", "required": False},
            {"name": "compaction-summary-model-route", "maps_to": "context_compaction.summary_model_route_name", "type": "string", "required": False},
            {"name": "clear-compaction-summary-model-route", "maps_to": "context_compaction.summary_model_route_name", "type": "boolean", "required": False},
            {"name": "compaction-allow-slash-compact", "maps_to": "context_compaction.allow_slash_compact", "type": "boolean", "required": False},
        ],
        "mutually_exclusive": [
            ["system-prompt", "system-prompt-file"],
            ["prompt-template-id", "clear-prompt-template"],
            ["model-route", "clear-model-route"],
            ["tool", "add-tool", "remove-tool", "clear-tools", "tool-config", "tool-configs-json"],
            ["skill", "add-skill", "remove-skill", "clear-skills"],
            ["compaction-summary-model-route", "clear-compaction-summary-model-route"],
        ],
        "examples": [
            "doyoutrade-cli assistant agent update <agent-id> --add-skill strategy-iteration --remove-skill doyoutrade-data",
            "doyoutrade-cli assistant agent update <agent-id> --clear-prompt-template --system-prompt-file ./prompt.txt",
            "doyoutrade-cli assistant agent update <agent-id> --tool-config execute_bash=deferred --compaction-mode manual",
        ],
    },
    # --- tasks (CLI PUT/POST + OperationHandler schema; contract closes flag gap) -
    "task.get": {
        "command_path": "task get",
        "invocation": "doyoutrade-cli task get",
        "arguments": [
            {
                "name": "identifier",
                "type": "string",
                "required": True,
                "semantic": "task_id",
                "description": "Task id (UUID) or exact task name.",
            },
        ],
        "flags": [],
        "examples": ["doyoutrade-cli task get <task-id>"],
    },
    "task.list": {
        "command_path": "task list",
        "invocation": "doyoutrade-cli task list",
        "flags": [
            {
                "name": "q",
                "maps_to": "q",
                "type": "string",
                "required": False,
                "description": "Substring filter on task name. This is --q, not --query.",
            },
            {"name": "status", "maps_to": "status", "type": "string", "required": False},
            {"name": "mode", "maps_to": "mode", "type": "string", "required": False},
            {
                "name": "definition",
                "maps_to": "definition_id",
                "type": "string",
                "required": False,
                "semantic": "strategy_definition_id",
                "accepts_prefix": "sd-",
            },
            {"name": "limit", "maps_to": "limit", "type": "integer", "required": False},
            {"name": "offset", "maps_to": "offset", "type": "integer", "required": False},
        ],
        "examples": [
            "doyoutrade-cli task list --q 风华高科",
            "doyoutrade-cli task list --mode live --status running",
        ],
    },
    "task.create": {
        "command_path": "task create",
        "invocation": "doyoutrade-cli task create",
        "flags": [
            {"name": "name", "maps_to": "name", "type": "string", "required": True},
            {
                "name": "definition",
                "maps_to": "strategy.definition_id",
                "type": "string",
                "required": False,
                "semantic": "strategy_definition_id",
                "accepts_prefix": "sd-",
                "description": "Strategy definition id to bind (required in practice).",
            },
            {
                "name": "mode",
                "maps_to": "mode",
                "type": "string",
                "required": False,
                "description": "backtest / paper / live / signal_only. Default paper.",
            },
            {"name": "description", "maps_to": "description", "type": "string", "required": False},
            {
                "name": "data-provider",
                "maps_to": "data_provider",
                "type": "string",
                "required": False,
            },
            {
                "name": "account",
                "maps_to": "settings.account_id",
                "type": "string",
                "required": False,
                "semantic": "account_id",
                "accepts_prefix": "acct-",
                "description": (
                    "Account this task runs against. Omit to use the default account."
                ),
            },
            {
                "name": "universe",
                "maps_to": "settings.universe",
                "type": "csv",
                "required": False,
                "description": "Comma-separated symbols or @watchlist:<tag> tokens.",
            },
            {
                "name": "params",
                "maps_to": "settings",
                "type": "json_object",
                "required": False,
                "description": "Nested agent / strategy blocks; flat keys become parameter_overrides with --definition.",
            },
        ],
        "examples": [
            "doyoutrade-cli task create --name 'MR Demo' --definition sd-3f1c2a9b8e7d --universe 600519.SH",
            "doyoutrade-cli task create --name 'mock paper' --definition sd-... --mode paper --account <acct-mock-id>",
        ],
    },
    "task.update": {
        "command_path": "task update",
        "invocation": "doyoutrade-cli task update",
        "arguments": [
            {
                "name": "identifier",
                "type": "string",
                "required": True,
                "semantic": "task_id",
                "description": "Task id (UUID) or exact task name.",
            },
        ],
        "flags": [
            {"name": "name", "maps_to": "name", "type": "string", "required": False},
            {
                "name": "mode",
                "maps_to": "mode",
                "type": "string",
                "required": False,
                "description": "backtest / paper / live / signal_only.",
            },
            {"name": "description", "maps_to": "description", "type": "string", "required": False},
            {
                "name": "data-provider",
                "maps_to": "data_provider",
                "type": "string",
                "required": False,
            },
            {
                "name": "account",
                "maps_to": "settings.account_id",
                "type": "string",
                "required": False,
                "semantic": "account_id",
                "accepts_prefix": "acct-",
                "description": (
                    "Rebind to account id (acct-...). Pass empty string to clear "
                    "binding (use default account). Omit to leave unchanged."
                ),
            },
            {
                "name": "definition",
                "maps_to": "strategy.definition_id",
                "type": "string",
                "required": False,
                "semantic": "strategy_definition_id",
                "accepts_prefix": "sd-",
            },
            {
                "name": "universe",
                "maps_to": "settings.universe",
                "type": "csv",
                "required": False,
            },
            {
                "name": "params",
                "maps_to": "settings",
                "type": "json_object",
                "required": False,
            },
        ],
        "examples": [
            "doyoutrade-cli task update <task-id> --universe 600519.SH",
            "doyoutrade-cli task update <task-id> --account <acct-mock-id>",
            "doyoutrade-cli task update <task-id> --account ''",
        ],
    },
    "task.start": {
        "command_path": "task start",
        "invocation": "doyoutrade-cli task start",
        "arguments": [
            {
                "name": "identifier",
                "type": "string",
                "required": True,
                "semantic": "task_id",
                "description": "Task id (UUID). Backtest tasks do not support start.",
            },
        ],
        "flags": [],
        "examples": ["doyoutrade-cli task start <task-id>"],
    },
    "task.pause": {
        "command_path": "task pause",
        "invocation": "doyoutrade-cli task pause",
        "arguments": [
            {
                "name": "identifier",
                "type": "string",
                "required": True,
                "semantic": "task_id",
                "description": "Task id (UUID). Backtest tasks do not support pause.",
            },
        ],
        "flags": [],
        "examples": ["doyoutrade-cli task pause <task-id>"],
    },
    "task.stop": {
        "command_path": "task stop",
        "invocation": "doyoutrade-cli task stop",
        "arguments": [
            {
                "name": "identifier",
                "type": "string",
                "required": True,
                "semantic": "task_id",
                "description": (
                    "Task id (UUID). Stops a paper/live/signal_only task and keeps "
                    "the task record; backtest tasks do not support stop."
                ),
            },
        ],
        "flags": [],
        "examples": ["doyoutrade-cli task stop <task-id>"],
    },
    "task.delete": {
        "command_path": "task delete",
        "invocation": "doyoutrade-cli task delete",
        "arguments": [
            {
                "name": "identifier",
                "type": "string",
                "required": True,
                "semantic": "task_id",
                "description": "Task id (UUID) or exact task name.",
            },
        ],
        "flags": [],
        "examples": ["doyoutrade-cli task delete <task-id>"],
    },
    # --- accounts (CRUD has no OperationHandler class; contract-only) --------
    "account.create": {
        "command_path": "account create",
        "invocation": "doyoutrade-cli account create",
        "flags": [
            {"name": "name", "maps_to": "name", "type": "string", "required": True,
             "description": "Human-readable account label."},
            {"name": "mode", "maps_to": "mode", "type": "enum", "required": True,
             "enum": ["live", "mock"],
             "description": "live = real QMT trading terminal; mock = simulated portfolio."},
            {"name": "base-url", "maps_to": "base_url", "type": "string", "required": False,
             "description": "QMT proxy base URL. Required for live and for any account meant to be the market-data default."},
            {"name": "token", "maps_to": "token", "type": "string", "required": False,
             "description": "QMT proxy API key (proxy-level, plaintext)."},
            {"name": "qmt-account-id", "maps_to": "qmt_account_id", "type": "string", "required": False,
             "description": "Broker trading account number (live trading.connect)."},
            {"name": "session-id", "maps_to": "session_id", "type": "string", "required": False,
             "description": "Trading session id (normally auto-managed; rarely set by hand)."},
            {"name": "timeout-seconds", "maps_to": "timeout_seconds", "type": "number", "required": False},
            {"name": "mock-cash", "maps_to": "mock_cash", "type": "number", "required": False},
            {"name": "mock-equity", "maps_to": "mock_equity", "type": "number", "required": False},
            {"name": "mock-positions", "maps_to": "mock_positions", "type": "json_array", "required": False,
             "description": 'JSON list of {symbol,quantity,cost_price}.'},
            {"name": "default", "maps_to": "is_default", "type": "bool_flag", "required": False,
             "description": "--default / --no-default: make this the sole default account."},
            {"name": "enabled", "maps_to": "enabled", "type": "bool_flag", "required": False,
             "description": "--enabled / --disabled."},
        ],
        "examples": [
            "doyoutrade-cli account create --name 'QMT live X' --mode live --base-url http://your-qmt-host:8000 --token <k> --qmt-account-id <broker-no> --default",
            "doyoutrade-cli account create --name mock-sandbox --mode mock",
        ],
    },
    "account.update": {
        "command_path": "account update",
        "invocation": "doyoutrade-cli account update",
        "arguments": [
            {"name": "account_id", "type": "string", "required": True,
             "semantic": "account_id", "accepts_prefix": "acct-",
             "description": "Account id to update."},
        ],
        "flags": [
            {"name": "name", "maps_to": "name", "type": "string", "required": False},
            {"name": "mode", "maps_to": "mode", "type": "enum", "enum": ["live", "mock"], "required": False},
            {"name": "base-url", "maps_to": "base_url", "type": "string", "required": False},
            {"name": "token", "maps_to": "token", "type": "string", "required": False},
            {"name": "qmt-account-id", "maps_to": "qmt_account_id", "type": "string", "required": False},
            {"name": "session-id", "maps_to": "session_id", "type": "string", "required": False},
            {"name": "timeout-seconds", "maps_to": "timeout_seconds", "type": "number", "required": False},
            {"name": "mock-cash", "maps_to": "mock_cash", "type": "number", "required": False},
            {"name": "mock-equity", "maps_to": "mock_equity", "type": "number", "required": False},
            {"name": "mock-positions", "maps_to": "mock_positions", "type": "json_array", "required": False},
            {"name": "default", "maps_to": "is_default", "type": "bool_flag", "required": False},
            {"name": "enabled", "maps_to": "enabled", "type": "bool_flag", "required": False},
        ],
        "examples": [
            "doyoutrade-cli account update <acct-...> --base-url http://newhost:8000 --token <k>",
            "doyoutrade-cli account update <acct-...> --disabled",
        ],
    },
    "account.set-default": {
        "command_path": "account set-default",
        "invocation": "doyoutrade-cli account set-default",
        "arguments": [
            {"name": "account_id", "type": "string", "required": True,
             "semantic": "account_id", "accepts_prefix": "acct-",
             "description": "Account to make the sole default (clears is_default on all others)."},
        ],
        "examples": ["doyoutrade-cli account set-default <acct-...>"],
    },
    "account.delete": {
        "command_path": "account delete",
        "invocation": "doyoutrade-cli account delete",
        "arguments": [
            {"name": "account_id", "type": "string", "required": True,
             "semantic": "account_id", "accepts_prefix": "acct-",
             "description": "Account to delete. Refused with account_in_use (409) if a task binds it."},
        ],
        "examples": ["doyoutrade-cli account delete <acct-...>"],
    },
    "account.get": {
        "command_path": "account get",
        "invocation": "doyoutrade-cli account get",
        "arguments": [
            {"name": "account_id", "type": "string", "required": True,
             "semantic": "account_id", "accepts_prefix": "acct-"},
        ],
        "examples": ["doyoutrade-cli account get <acct-...>"],
    },
    "account.statement": {
        "command_path": "account statement",
        "invocation": "doyoutrade-cli account statement",
        "flags": [
            {"name": "account", "maps_to": "account_id", "type": "string", "required": False,
             "semantic": "account_id", "accepts_prefix": "acct-",
             "description": "Account id to query. Omit to use the enabled default account."},
            {"name": "asof", "maps_to": "asof", "type": "date", "required": False,
             "description": "Trading day YYYY-MM-DD. Omit to use today."},
        ],
        "examples": [
            "doyoutrade-cli account statement --account <acct-...> --asof YYYY-MM-DD",
            "doyoutrade-cli account statement --asof YYYY-MM-DD",
        ],
    },
    "account.list": {
        "command_path": "account list",
        "invocation": "doyoutrade-cli account list",
        "flags": [],
        "examples": ["doyoutrade-cli account list"],
    },
    # --- cron writes (OperationHandler classes exist but aren't in _SCHEMA_TARGETS;
    #     declarative contract closes the schema gap). See doyoutrade-cron skill
    #     for the tagged-union schedule nuance a flat schema can't express. ----
    "cron.create": {
        "command_path": "cron create",
        "invocation": "doyoutrade-cli cron create",
        "flags": [
            {"name": "agent-id", "maps_to": "agent_id", "type": "string", "required": False,
             "description": "Owning agent; falls back to DOYOUTRADE_AGENT_ID."},
            {"name": "name", "maps_to": "name", "type": "string", "required": True},
            {"name": "in", "maps_to": "in_duration", "type": "duration", "required": False,
             "description": "Fire-in-N: 60s/5m/2h/1d. PREFER for 'in N minutes' intents (second-precise, no tz math)."},
            {"name": "at", "maps_to": "at_iso", "type": "iso8601_offset", "required": False,
             "description": "One-shot at an ISO-8601 instant with offset."},
            {"name": "cron-expression", "maps_to": "cron_expression", "type": "string", "required": False,
             "description": "5-field recurring cron. Use only for genuine recurrence."},
            {"name": "timezone", "maps_to": "timezone", "type": "string", "required": False},
            {"name": "task-kind", "maps_to": "task_kind", "type": "string", "required": False},
            {"name": "task-params", "maps_to": "task_params_json", "type": "json_object", "required": False},
            {"name": "input-template", "maps_to": "input_template", "type": "string", "required": False},
            {"name": "delete-after-run", "maps_to": "delete_after_run", "type": "bool_flag", "required": False},
            {"name": "enabled", "maps_to": "enabled", "type": "bool_flag", "required": False},
        ],
        "required_one_of": [["in"], ["at"], ["cron-expression"]],
        "examples": [
            "doyoutrade-cli cron create --name '1-min reminder' --in 60s --input-template '时间到啦'",
            "doyoutrade-cli cron create --name 'morning brief' --cron-expression '0 9 * * *' --timezone Asia/Shanghai --input-template '...'",
        ],
    },
    "cron.update": {
        "command_path": "cron update",
        "invocation": "doyoutrade-cli cron update",
        "arguments": [
            {"name": "job_id", "type": "string", "required": True,
             "semantic": "cron_job_id", "accepts_prefix": "cron-"},
        ],
        "flags": [
            {"name": "name", "maps_to": "name", "type": "string", "required": False},
            {"name": "cron-expression", "maps_to": "cron_expression", "type": "string", "required": False},
            {"name": "timezone", "maps_to": "timezone", "type": "string", "required": False},
            {"name": "enabled", "maps_to": "enabled", "type": "bool_flag", "required": False,
             "description": "--enabled / --disabled (tri-state: omit = unchanged)."},
            {"name": "task-kind", "maps_to": "task_kind", "type": "string", "required": False},
            {"name": "task-params", "maps_to": "task_params_json", "type": "json_object", "required": False},
            {"name": "pre-action", "maps_to": "pre_action", "type": "json_object", "required": False},
            {"name": "clear-pre-action", "maps_to": "pre_action", "type": "bool_flag", "required": False,
             "description": "Send pre_action: null. Mutually exclusive with --pre-action."},
        ],
        "examples": [
            "doyoutrade-cli cron update <cron-...> --disabled",
            "doyoutrade-cli cron update <cron-...> --cron-expression '0 9 * * 1-5' --name 'weekday 9am'",
        ],
    },
    "cron.delete": {
        "command_path": "cron delete",
        "invocation": "doyoutrade-cli cron delete",
        "arguments": [
            {"name": "job_id", "type": "string", "required": True,
             "semantic": "cron_job_id", "accepts_prefix": "cron-"},
        ],
        "examples": ["doyoutrade-cli cron delete <cron-...>"],
    },
    "cron.pause": {
        "command_path": "cron pause",
        "invocation": "doyoutrade-cli cron pause",
        "arguments": [
            {"name": "job_id", "type": "string", "required": True,
             "semantic": "cron_job_id", "accepts_prefix": "cron-"},
        ],
        "examples": ["doyoutrade-cli cron pause <cron-...>"],
    },
    "cron.resume": {
        "command_path": "cron resume",
        "invocation": "doyoutrade-cli cron resume",
        "arguments": [
            {"name": "job_id", "type": "string", "required": True,
             "semantic": "cron_job_id", "accepts_prefix": "cron-"},
        ],
        "examples": ["doyoutrade-cli cron resume <cron-...>"],
    },
    "cron.trigger": {
        "command_path": "cron trigger",
        "invocation": "doyoutrade-cli cron trigger",
        "arguments": [
            {"name": "job_id", "type": "string", "required": True,
             "semantic": "cron_job_id", "accepts_prefix": "cron-"},
        ],
        "examples": ["doyoutrade-cli cron trigger <cron-...>"],
    },
    # --- watchlist (自选股) — CRUD + tags + quotes; routes to /watchlist and
    #     /market/quotes. No backing OperationHandler class (contract-only). ---
    "watchlist.list": {
        "command_path": "watchlist list",
        "invocation": "doyoutrade-cli watchlist list",
        "flags": [
            {"name": "tag", "maps_to": "tag", "type": "string", "required": False,
             "description": "Only entries carrying this tag (query param)."},
        ],
        "examples": [
            "doyoutrade-cli watchlist list",
            "doyoutrade-cli watchlist list --tag 半导体",
        ],
    },
    "watchlist.get": {
        "command_path": "watchlist get",
        "invocation": "doyoutrade-cli watchlist get",
        "arguments": [
            {"name": "entry_id", "type": "string", "required": True,
             "semantic": "watchlist_entry_id", "accepts_prefix": "wl-",
             "description": "Watchlist entry id. 404 → watchlist_not_found."},
        ],
        "examples": ["doyoutrade-cli watchlist get <wl-...>"],
    },
    "watchlist.add": {
        "command_path": "watchlist add",
        "invocation": "doyoutrade-cli watchlist add",
        "arguments": [
            {"name": "symbol", "type": "string", "required": False,
             "semantic": "canonical_symbol",
             "description": (
                 "Single canonical CODE.EXCHANGE symbol. Mutually exclusive "
                 "with --universe-file; pass exactly one."
             )},
        ],
        "flags": [
            {"name": "universe-file", "maps_to": "universe_file", "type": "path", "required": False,
             "description": "One CODE.EXCHANGE per line (# comments). Adds each (one POST per symbol)."},
            {"name": "tags", "maps_to": "tags", "type": "csv_or_json_array", "required": False,
             "description": "Comma-separated list or JSON array; applied to every symbol added."},
            {"name": "note", "maps_to": "note", "type": "string", "required": False},
            {"name": "display-name", "maps_to": "display_name", "type": "string", "required": False},
            {"name": "sort-order", "maps_to": "sort_order", "type": "integer", "required": False},
        ],
        "required_one_of": [["symbol"], ["universe-file"]],
        "mutually_exclusive": [["symbol", "universe-file"]],
        "examples": [
            "doyoutrade-cli watchlist add 600519.SH --tags 白酒,核心 --note '龙头'",
            "doyoutrade-cli watchlist add --universe-file /tmp/u.txt --tags 半导体",
        ],
    },
    "watchlist.update": {
        "command_path": "watchlist update",
        "invocation": "doyoutrade-cli watchlist update",
        "arguments": [
            {"name": "entry_id", "type": "string", "required": True,
             "semantic": "watchlist_entry_id", "accepts_prefix": "wl-",
             "description": "Watchlist entry id. 404 → watchlist_not_found."},
        ],
        "flags": [
            {"name": "tags", "maps_to": "tags", "type": "csv_or_json_array", "required": False,
             "description": "Comma-separated list or JSON array; replaces existing tags."},
            {"name": "note", "maps_to": "note", "type": "string", "required": False},
            {"name": "display-name", "maps_to": "display_name", "type": "string", "required": False},
            {"name": "sort-order", "maps_to": "sort_order", "type": "integer", "required": False},
        ],
        "examples": [
            "doyoutrade-cli watchlist update <wl-...> --tags 半导体,核心",
            "doyoutrade-cli watchlist update <wl-...> --note '减仓观察'",
        ],
    },
    "watchlist.remove": {
        "command_path": "watchlist remove",
        "invocation": "doyoutrade-cli watchlist remove",
        "arguments": [
            {"name": "entry_id", "type": "string", "required": True,
             "semantic": "watchlist_entry_id", "accepts_prefix": "wl-",
             "description": "Watchlist entry id to delete. 404 → watchlist_not_found."},
        ],
        "examples": ["doyoutrade-cli watchlist remove <wl-...>"],
    },
    "watchlist.tags": {
        "command_path": "watchlist tags",
        "invocation": "doyoutrade-cli watchlist tags",
        "flags": [],
        "examples": ["doyoutrade-cli watchlist tags"],
    },
    "watchlist.quotes": {
        "command_path": "watchlist quotes",
        "invocation": "doyoutrade-cli watchlist quotes",
        "flags": [
            {"name": "tag", "maps_to": "tag", "type": "string", "required": False,
             "description": "Resolve watchlist symbols carrying this tag, then quote them."},
            {"name": "symbols", "maps_to": "symbols", "type": "csv_or_json_array", "required": False,
             "description": "Explicit symbols (comma-separated or JSON array)."},
            {"name": "universe-file", "maps_to": "universe_file", "type": "path", "required": False,
             "description": "One CODE.EXCHANGE per line (# comments)."},
        ],
        "required_one_of": [["tag"], ["symbols"], ["universe-file"]],
        "mutually_exclusive": [["tag", "symbols", "universe-file"]],
        "examples": [
            "doyoutrade-cli watchlist quotes --tag 半导体",
            "doyoutrade-cli watchlist quotes --symbols 600519.SH,000001.SZ",
        ],
    },
    "monitor.list": {
        "command_path": "monitor list",
        "invocation": "doyoutrade-cli monitor list",
        "flags": [],
        "examples": ["doyoutrade-cli monitor list"],
    },
    "monitor.get": {
        "command_path": "monitor get",
        "invocation": "doyoutrade-cli monitor get",
        "arguments": [
            {"name": "monitor_id", "type": "string", "required": True,
             "semantic": "monitor_id", "accepts_prefix": "mon-",
             "description": "Monitor rule id. 404 → monitor_not_found."},
        ],
        "examples": ["doyoutrade-cli monitor get <mon-...>"],
    },
    "monitor.create": {
        "command_path": "monitor create",
        "invocation": "doyoutrade-cli monitor create",
        "flags": [
            {"name": "name", "maps_to": "name", "type": "string", "required": True,
             "description": "Human-readable rule name."},
            {"name": "scope-kind", "maps_to": "scope_kind", "type": "string", "required": True,
             "description": "watchlist_tag | symbols — which stocks to watch."},
            {"name": "tag", "maps_to": "tag", "type": "string", "required": False,
             "description": "Watchlist tag (scope_kind=watchlist_tag; omit = all watched)."},
            {"name": "symbols", "maps_to": "symbols", "type": "csv_or_json_array", "required": False,
             "description": "Explicit symbols (scope_kind=symbols)."},
            {"name": "universe-file", "maps_to": "universe_file", "type": "path", "required": False,
             "description": "One CODE.EXCHANGE per line (scope_kind=symbols)."},
            {"name": "channel-id", "maps_to": "channel_id", "type": "string", "required": False,
             "semantic": "channel_id",
             "description": "Delivery channel id (chan-…); pair with --chat-id."},
            {"name": "chat-id", "maps_to": "chat_id", "type": "string", "required": False,
             "description": "Feishu group chat id (oc_…)."},
            {"name": "preset", "maps_to": "preset", "type": "string", "required": False,
             "description": "Single-preset condition: limit_up|limit_down|limit_up_seal_shrink|"
                            "limit_down_seal_shrink|limit_up_open|limit_down_open."},
            {"name": "condition", "maps_to": "condition_json", "type": "json_object", "required": False,
             "description": "AND/OR condition tree as a JSON string. invalid → invalid_condition_json."},
            {"name": "condition-file", "maps_to": "condition_json", "type": "path", "required": False,
             "description": "Path to a JSON file with the condition tree."},
            {"name": "cooldown", "maps_to": "cooldown_seconds", "type": "integer", "required": False,
             "description": "Min seconds between alerts (default 300)."},
            {"name": "disabled", "maps_to": "enabled", "type": "flag", "required": False,
             "description": "Create paused (enabled=false)."},
        ],
        "required_one_of": [["preset"], ["condition"], ["condition-file"]],
        "mutually_exclusive": [["preset", "condition", "condition-file"], ["symbols", "universe-file"]],
        "examples": [
            "doyoutrade-cli monitor create --name 半导体涨停 --scope-kind watchlist_tag --tag 半导体 "
            "--channel-id <chan-...> --chat-id <oc_...> --preset limit_up",
            "doyoutrade-cli monitor create --name 平安打板 --scope-kind symbols --symbols 000001.SZ "
            "--channel-id <chan-...> --chat-id <oc_...> "
            "--condition '{\"op\":\"or\",\"children\":[{\"preset\":\"limit_up_open\"},{\"preset\":\"limit_up_seal_shrink\"}]}'",
        ],
    },
    "monitor.update": {
        "command_path": "monitor update",
        "invocation": "doyoutrade-cli monitor update",
        "arguments": [
            {"name": "monitor_id", "type": "string", "required": True,
             "semantic": "monitor_id", "accepts_prefix": "mon-",
             "description": "Monitor rule id. 404 → monitor_not_found."},
        ],
        "flags": [
            {"name": "name", "maps_to": "name", "type": "string", "required": False},
            {"name": "scope-kind", "maps_to": "scope_kind", "type": "string", "required": False},
            {"name": "tag", "maps_to": "tag", "type": "string", "required": False},
            {"name": "symbols", "maps_to": "symbols", "type": "csv_or_json_array", "required": False},
            {"name": "universe-file", "maps_to": "universe_file", "type": "path", "required": False},
            {"name": "channel-id", "maps_to": "channel_id", "type": "string", "required": False, "semantic": "channel_id"},
            {"name": "chat-id", "maps_to": "chat_id", "type": "string", "required": False},
            {"name": "preset", "maps_to": "preset", "type": "string", "required": False},
            {"name": "condition", "maps_to": "condition_json", "type": "json_object", "required": False},
            {"name": "condition-file", "maps_to": "condition_json", "type": "path", "required": False},
            {"name": "cooldown", "maps_to": "cooldown_seconds", "type": "integer", "required": False},
        ],
        "examples": ["doyoutrade-cli monitor update <mon-...> --cooldown 600 --tag 核心"],
    },
    "monitor.enable": {
        "command_path": "monitor enable",
        "invocation": "doyoutrade-cli monitor enable",
        "arguments": [
            {"name": "monitor_id", "type": "string", "required": True,
             "semantic": "monitor_id", "accepts_prefix": "mon-",
             "description": "Monitor rule id. 404 → monitor_not_found."},
        ],
        "examples": ["doyoutrade-cli monitor enable <mon-...>"],
    },
    "monitor.disable": {
        "command_path": "monitor disable",
        "invocation": "doyoutrade-cli monitor disable",
        "arguments": [
            {"name": "monitor_id", "type": "string", "required": True,
             "semantic": "monitor_id", "accepts_prefix": "mon-",
             "description": "Monitor rule id. 404 → monitor_not_found."},
        ],
        "examples": ["doyoutrade-cli monitor disable <mon-...>"],
    },
    "monitor.delete": {
        "command_path": "monitor delete",
        "invocation": "doyoutrade-cli monitor delete",
        "arguments": [
            {"name": "monitor_id", "type": "string", "required": True,
             "semantic": "monitor_id", "accepts_prefix": "mon-",
             "description": "Monitor rule id to delete. 404 → monitor_not_found."},
        ],
        "examples": ["doyoutrade-cli monitor delete <mon-...>"],
    },
    "monitor.alerts": {
        "command_path": "monitor alerts",
        "invocation": "doyoutrade-cli monitor alerts",
        "arguments": [
            {"name": "monitor_id", "type": "string", "required": True,
             "semantic": "monitor_id", "accepts_prefix": "mon-",
             "description": "Monitor rule id. 404 → monitor_not_found."},
        ],
        "flags": [
            {"name": "symbol", "maps_to": "symbol", "type": "string", "required": False},
            {"name": "limit", "maps_to": "limit", "type": "integer", "required": False},
        ],
        "examples": ["doyoutrade-cli monitor alerts <mon-...> --symbol 000001.SZ"],
    },
    "monitor.run-once": {
        "command_path": "monitor run-once",
        "invocation": "doyoutrade-cli monitor run-once",
        "arguments": [
            {"name": "monitor_id", "type": "string", "required": True,
             "semantic": "monitor_id", "accepts_prefix": "mon-",
             "description": "Monitor rule id to dry-run. 404 → monitor_not_found."},
        ],
        "examples": ["doyoutrade-cli monitor run-once <mon-...>"],
    },
    "decision-signal.list": {
        "command_path": "decision-signal list",
        "invocation": "doyoutrade-cli decision-signal list",
        "flags": [
            {"name": "task-id", "maps_to": "task_id", "type": "string", "required": False,
             "semantic": "task_id", "accepts_prefix": "task-",
             "description": "Filter by owning task."},
            {"name": "run-id", "maps_to": "run_id", "type": "string", "required": False,
             "description": "Filter by producing run id (backtest job / cycle run)."},
            {"name": "symbol", "maps_to": "symbol", "type": "string", "required": False,
             "description": "Filter by canonical symbol (e.g. 600519.SH)."},
            {"name": "status", "maps_to": "status", "type": "string", "required": False,
             "description": "active|expired|invalidated|evaluated."},
            {"name": "limit", "maps_to": "limit", "type": "integer", "required": False,
             "description": "Max rows (<=500, default 50)."},
            {"name": "offset", "maps_to": "offset", "type": "integer", "required": False,
             "description": "Pagination offset."},
        ],
        "examples": [
            "doyoutrade-cli decision-signal list --symbol 600519.SH --status evaluated",
            "doyoutrade-cli decision-signal list --run-id <btjob-...>",
        ],
    },
    "decision-signal.get": {
        "command_path": "decision-signal get",
        "invocation": "doyoutrade-cli decision-signal get",
        "arguments": [
            {"name": "signal_id", "type": "string", "required": True,
             "semantic": "decision_signal_id", "accepts_prefix": "dsig-",
             "description": "Decision signal id. 404 → decision_signal_not_found."},
        ],
        "examples": ["doyoutrade-cli decision-signal get <dsig-...>"],
    },
    "decision-signal.evaluate": {
        "command_path": "decision-signal evaluate",
        "invocation": "doyoutrade-cli decision-signal evaluate",
        "arguments": [
            {"name": "signal_id", "type": "string", "required": True,
             "semantic": "decision_signal_id", "accepts_prefix": "dsig-",
             "description": "Decision signal id to evaluate. 404 → decision_signal_not_found."},
        ],
        "flags": [
            {"name": "horizon", "maps_to": "horizon", "type": "string", "required": False,
             "description": "Evaluation window like '5d' (default: the signal's own horizon)."},
            {"name": "provider", "maps_to": "provider", "type": "string", "required": False,
             "description": "Cached-bars provider to read from."},
        ],
        "examples": [
            "doyoutrade-cli decision-signal evaluate <dsig-...> --horizon 5d",
        ],
    },
    "portfolio.import-csv": {
        "command_path": "portfolio import-csv",
        "invocation": "doyoutrade-cli portfolio import-csv",
        "flags": [
            {"name": "file", "maps_to": "file_path", "type": "string", "required": True,
             "description": "Broker statement (交割单) CSV path; parsed locally, "
             "no API server needed."},
            {"name": "broker", "maps_to": "broker", "type": "string", "required": True,
             "description": "Broker slug used as the knowledge partition "
             "(trades/<broker>/<YYYY-MM>.csv), e.g. huatai."},
            {"name": "dry-run", "maps_to": "dry_run", "type": "boolean", "required": False,
             "description": "Rehearse only: report would-append / duplicate "
             "counts without writing files or refreshing the index."},
        ],
        "examples": [
            "doyoutrade-cli portfolio import-csv --file ./交割单.csv --broker huatai",
            "doyoutrade-cli portfolio import-csv --file ./交割单.csv --broker huatai --dry-run",
        ],
    },
    "portfolio.import-image": {
        "command_path": "portfolio import-image",
        "invocation": "doyoutrade-cli portfolio import-image",
        "flags": [
            {"name": "file", "maps_to": "file_path", "type": "string", "required": True,
             "description": "Position screenshot path."},
            {"name": "mime", "maps_to": "mime_type", "type": "string", "required": False,
             "description": "image/png|image/jpeg|image/webp|image/gif."},
        ],
        "examples": [
            "doyoutrade-cli portfolio import-image --file ./positions.png",
        ],
        "notes": "Always returns error_code=not_available_via_cli: vision extraction "
        "needs the in-process assistant tool import_positions_from_image "
        "(model adapter lives in the API server runtime).",
    },
}


def get_cli_contract(command_path: str) -> dict[str, Any] | None:
    """Return a copy of the CLI contract for ``command_path`` if declared."""

    contract = _CONTRACTS.get(command_path)
    if contract is None:
        return None
    return deepcopy(contract)


def cli_contract_paths() -> list[str]:
    """Return sorted command paths with declared CLI contracts."""

    return sorted(_CONTRACTS)


__all__ = ["cli_contract_paths", "get_cli_contract"]
