"""Tests for agent-facing CLI command contracts exposed by ``schema``."""

from __future__ import annotations

import json
import unittest

import click
from click.testing import CliRunner

from doyoutrade.cli.command_contracts import get_cli_contract
from doyoutrade.cli.commands.assistant import assistant
from doyoutrade.cli.commands.account import account
from doyoutrade.cli.commands import backtest_runs  # noqa: F401  # registers run/summary commands
from doyoutrade.cli.commands.backtest import backtest
from doyoutrade.cli.commands.data import data
from doyoutrade.cli.commands.schema import schema as schema_command
from doyoutrade.cli.commands.strategy import strategy
from doyoutrade.cli.commands.task import task


def _command_at(root: click.Group, *parts: str) -> click.Command:
    current: click.Command = root
    for part in parts:
        if not isinstance(current, click.Group):
            raise AssertionError(f"{part!r} requested below non-group command {current.name!r}")
        next_command = current.commands.get(part)
        if next_command is None:
            raise AssertionError(f"missing click command path component {part!r}")
        current = next_command
    return current


def _click_option_names(command: click.Command) -> set[str]:
    names: set[str] = set()
    for param in command.params:
        if not isinstance(param, click.Option):
            continue
        for opt in param.opts:
            if opt.startswith("--"):
                names.add(opt.removeprefix("--"))
    return names


def _contract_flag_names(command_path: str) -> set[str]:
    contract = get_cli_contract(command_path)
    if contract is None:
        raise AssertionError(f"missing CLI contract for {command_path}")
    return {flag["name"] for flag in contract.get("flags", [])}


class CliCommandSchemaTests(unittest.TestCase):
    def _schema_data(self, command_path: str) -> dict:
        result = CliRunner().invoke(
            schema_command,
            [command_path],
            catch_exceptions=False,
            obj={"fmt": "json"},
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        envelope = json.loads(result.output)
        self.assertTrue(envelope["ok"], msg=envelope)
        return envelope["data"]

    def test_backtest_run_contract_marks_definition_and_universe_mode(self) -> None:
        # StrategyInstance / ``si-`` entry modes were removed; backtest run
        # now takes either a task_id or a definition_id (``sd-...``).
        data = self._schema_data("backtest.run")

        contract = data["cli_contract"]
        self.assertEqual(contract["command_path"], "backtest run")
        flags = {flag["name"]: flag for flag in contract["flags"]}
        self.assertEqual(flags["definition"]["semantic"], "strategy_definition_id")
        self.assertEqual(flags["definition"]["accepts_prefix"], "sd-")
        self.assertEqual(flags["task"]["semantic"], "task_id")
        self.assertEqual(flags["universe"]["type"], "csv")
        self.assertEqual(flags["range-start"]["maps_to"], "range_start")
        self.assertEqual(flags["range-end"]["maps_to"], "range_end")
        self.assertIn(["task"], contract["required_one_of"])
        self.assertIn(["definition"], contract["required_one_of"])
        self.assertIn(["task", "definition"], contract["mutually_exclusive"])

    def test_strategy_authoring_open_schema_is_discoverable_without_runtime_dependencies(self) -> None:
        data = self._schema_data("strategy.authoring.open")

        self.assertEqual(data["tool_name"], "open_strategy_authoring")
        params = data["parameters"]
        self.assertIn("definition_id", params["properties"])
        self.assertIn("name", params["properties"])
        contract = data["cli_contract"]
        self.assertEqual(contract["command_path"], "strategy authoring open")
        flags = {flag["name"]: flag for flag in contract["flags"]}
        self.assertEqual(flags["definition-id"]["maps_to"], "definition_id")
        self.assertEqual(flags["definition-id"]["semantic"], "strategy_definition_id")

    def test_strategy_authoring_contracts_match_click_options(self) -> None:
        for command_name in ("open", "cancel", "compile", "finalize"):
            with self.subTest(command=command_name):
                command = _command_at(strategy, "authoring", command_name)
                self.assertEqual(
                    _click_option_names(command),
                    _contract_flag_names(f"strategy.authoring.{command_name}"),
                )

    def test_backtest_run_contract_matches_click_options(self) -> None:
        command = _command_at(backtest, "run")

        self.assertEqual(_click_option_names(command), _contract_flag_names("backtest.run"))

    def test_data_run_contract_matches_click_options_and_schema_is_discoverable(self) -> None:
        command = _command_at(data, "run")

        self.assertEqual(_click_option_names(command), _contract_flag_names("data.run"))
        schema_data = self._schema_data("data.run")
        self.assertEqual(schema_data["tool_name"], "data_run")
        self.assertEqual(schema_data["cli_contract"]["command_path"], "data run")
        self.assertIn("script_file", schema_data["parameters"]["properties"])

    def test_analysis_indicators_schema_is_discoverable(self) -> None:
        schema_data = self._schema_data("analysis.indicators")

        self.assertEqual(schema_data["tool_name"], "compute_indicators")
        self.assertIn("indicators", schema_data["parameters"]["properties"])

    def test_assistant_agent_create_contract_matches_click_options(self) -> None:
        command = _command_at(assistant, "agent", "create")

        self.assertEqual(_click_option_names(command), _contract_flag_names("assistant.agent.create"))
        contract = get_cli_contract("assistant.agent.create")
        assert contract is not None
        self.assertIn(["system-prompt"], contract["required_one_of"])
        self.assertIn(["prompt-template-id"], contract["required_one_of"])

    def test_assistant_agent_update_contract_matches_click_options(self) -> None:
        command = _command_at(assistant, "agent", "update")

        self.assertEqual(_click_option_names(command), _contract_flag_names("assistant.agent.update"))
        contract = get_cli_contract("assistant.agent.update")
        assert contract is not None
        self.assertIn(
            "doyoutrade-cli assistant agent update <agent-id> --add-skill strategy-iteration --remove-skill doyoutrade-data",
            contract["examples"],
        )
        self.assertIn(
            "doyoutrade-cli assistant agent update <agent-id> --tool-config execute_bash=deferred --compaction-mode manual",
            contract["examples"],
        )


    def test_task_update_schema_exposes_account_id_and_cli_contract(self) -> None:
        data = self._schema_data("task.update")

        self.assertEqual(data["tool_name"], "update_task")
        props = data["parameters"]["properties"]
        self.assertIn("account_id", props)
        contract = data["cli_contract"]
        flags = {flag["name"]: flag for flag in contract["flags"]}
        self.assertEqual(flags["account"]["maps_to"], "settings.account_id")
        self.assertEqual(flags["account"]["semantic"], "account_id")
        self.assertEqual(flags["account"]["accepts_prefix"], "acct-")

    def test_task_list_schema_exposes_q_cli_contract(self) -> None:
        data = self._schema_data("task.list")

        self.assertEqual(data["tool_name"], "list_tasks")
        contract = data["cli_contract"]
        flags = {flag["name"]: flag for flag in contract["flags"]}
        self.assertIn("q", flags)
        self.assertNotIn("query", flags)
        self.assertEqual(flags["q"]["maps_to"], "q")

    def test_task_create_and_update_contracts_match_click_options(self) -> None:
        for command_name, contract_path in (
            ("list", "task.list"),
            ("create", "task.create"),
            ("update", "task.update"),
        ):
            with self.subTest(command=command_name):
                command = _command_at(task, command_name)
                click_opts = _click_option_names(command)
                contract_flags = _contract_flag_names(contract_path)
                self.assertEqual(click_opts, contract_flags)

    def test_task_lifecycle_contracts_are_discoverable(self) -> None:
        for command_name in ("start", "pause", "stop", "delete"):
            with self.subTest(command=command_name):
                data = self._schema_data(f"task.{command_name}")
                contract = data["cli_contract"]
                self.assertEqual(contract["command_path"], f"task {command_name}")
                self.assertEqual(contract["invocation"], f"doyoutrade-cli task {command_name}")
                self.assertEqual(contract["flags"], [])
                args = {arg["name"]: arg for arg in contract["arguments"]}
                self.assertEqual(args["identifier"]["semantic"], "task_id")

    def test_task_lifecycle_contracts_match_click_options(self) -> None:
        for command_name in ("start", "pause", "stop", "delete"):
            with self.subTest(command=command_name):
                command = _command_at(task, command_name)
                self.assertEqual(_click_option_names(command), _contract_flag_names(f"task.{command_name}"))

    def test_account_create_schema_is_contract_only(self) -> None:
        # account writes have no OperationHandler class; ``schema`` serves the
        # declarative contract only (no tool JSON schema).
        data = self._schema_data("account.create")
        self.assertNotIn("tool_name", data)
        contract = data["cli_contract"]
        self.assertEqual(contract["command_path"], "account create")
        flags = {flag["name"]: flag for flag in contract["flags"]}
        self.assertTrue(flags["name"]["required"])
        self.assertTrue(flags["mode"]["required"])
        self.assertEqual(flags["mode"]["enum"], ["live", "mock"])
        self.assertEqual(flags["qmt-account-id"]["maps_to"], "qmt_account_id")

    def test_account_update_contract_exposes_account_id_argument(self) -> None:
        data = self._schema_data("account.update")
        args = {a["name"]: a for a in data["cli_contract"]["arguments"]}
        self.assertEqual(args["account_id"]["semantic"], "account_id")
        self.assertEqual(args["account_id"]["accepts_prefix"], "acct-")

    def test_account_statement_schema_is_contract_only(self) -> None:
        data = self._schema_data("account.statement")
        self.assertNotIn("tool_name", data)
        contract = data["cli_contract"]
        self.assertEqual(contract["command_path"], "account statement")
        flags = {flag["name"]: flag for flag in contract["flags"]}
        self.assertEqual(flags["account"]["maps_to"], "account_id")
        self.assertEqual(flags["account"]["accepts_prefix"], "acct-")
        self.assertEqual(flags["asof"]["type"], "date")

    def test_cron_create_schema_is_contract_only_with_schedule_union(self) -> None:
        data = self._schema_data("cron.create")
        self.assertNotIn("tool_name", data)
        contract = data["cli_contract"]
        self.assertEqual(contract["required_one_of"], [["in"], ["at"], ["cron-expression"]])

    def test_account_create_contract_matches_click_options(self) -> None:
        command = _command_at(account, "create")
        click_opts = _click_option_names(command)
        contract_flags = _contract_flag_names("account.create")
        # toggle flags expose both polarities in click (--default/--no-default,
        # --enabled/--disabled); the contract lists the positive name only.
        click_opts -= {"no-default", "disabled"}
        self.assertEqual(click_opts, contract_flags)

    def test_account_statement_contract_matches_click_options(self) -> None:
        command = _command_at(account, "statement")
        self.assertEqual(
            _click_option_names(command),
            _contract_flag_names("account.statement"),
        )

    def test_unknown_command_path_still_rejected(self) -> None:
        result = CliRunner().invoke(
            schema_command, ["account.frobnicate"], catch_exceptions=False, obj={"fmt": "json"}
        )
        self.assertNotEqual(result.exit_code, 0)
        envelope = json.loads(result.output)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["error_code"], "unknown_command")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
