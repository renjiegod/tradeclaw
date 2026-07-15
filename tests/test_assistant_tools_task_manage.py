from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from doyoutrade.api.operations.task_tools import (
    CreateTaskTool,
    DeleteTaskTool,
    GetTaskTool,
    ListTasksTool,
    UpdateTaskTool,
)
from doyoutrade.persistence.errors import RecordNotFoundError

from tests._tool_result_helpers import payload as _payload


def _extract_candidates(text):
    """Parse the bullet-listed candidate lines that ``tool_result_from_error_dict``
    appends after ``[error:ambiguous_task_name]``.

    Each candidate line looks like ``- <task_id> [<status>] <name> (<mode>)`` —
    return them as a list of ``{"task_id": ..., "status": ..., ...}`` dicts so
    tests can assert on ids easily.
    """
    import re as _re
    out = []
    for line in text.splitlines():
        m = _re.match(r"^- (\S+) \[([^\]]*)\] (\S*) \(([^)]*)\)$", line.strip())
        if m:
            out.append({"task_id": m.group(1), "status": m.group(2), "name": m.group(3), "mode": m.group(4)})
    return out



    try:
        return _json.loads(m.group(1))
    except _json.JSONDecodeError:
        return {}

class _FakePlatformService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        # Tasks keyed by task_id; allows look-up-by-id to succeed and any
        # other identifier (i.e. names) to fall through to RecordNotFoundError.
        self.tasks_by_id: dict[str, dict] = {
            "task-1": {"task_id": "task-1", "name": "alpha", "status": "running", "mode": "paper"},
        }
        # list_tasks_summary returns these items verbatim (already filtered by ``q``
        # in production; tests substitute their own values).
        self.list_items: list[dict] = [
            {"task_id": "task-1", "name": "alpha", "status": "running", "mode": "paper"},
        ]

    async def create_task(self, **payload):
        self.calls.append(("create", (), payload))
        return SimpleNamespace(
            task_id="task-1",
            config=SimpleNamespace(
                name=payload["name"],
                mode=payload.get("mode") or "paper",
            ),
        )

    async def get_task_status(self, identifier: str):
        self.calls.append(("get", (identifier,), {}))
        record = self.tasks_by_id.get(identifier)
        if record is None:
            raise RecordNotFoundError(f"task not found: {identifier}")
        return {"task_id": record["task_id"], "status": record.get("status", "running")}

    async def list_tasks_summary(self, **payload):
        self.calls.append(("list", (), payload))
        q = payload.get("q")
        if q:
            items = [it for it in self.list_items if q in (it.get("name") or "")]
        else:
            items = list(self.list_items)
        return {"items": items, "total": len(items), "limit": payload.get("limit", 20), "offset": payload.get("offset", 0)}

    async def update_task(self, identifier: str, **payload):
        self.calls.append(("update", (identifier,), payload))
        record = self.tasks_by_id.get(identifier)
        if record is None:
            raise RecordNotFoundError(f"task not found: {identifier}")
        return {"task_id": identifier, "status": "paused"}

    async def delete_task(self, identifier: str):
        self.calls.append(("delete", (identifier,), {}))
        record = self.tasks_by_id.get(identifier)
        if record is None:
            raise RecordNotFoundError(f"task not found: {identifier}")
        return None


class TaskToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service = _FakePlatformService()
        self.create_tool = CreateTaskTool(self.service)
        self.get_tool = GetTaskTool(self.service)
        self.list_tool = ListTasksTool(self.service)
        self.update_tool = UpdateTaskTool(self.service)
        self.delete_tool = DeleteTaskTool(self.service)

    async def test_tool_metadata(self) -> None:
        self.assertEqual(self.create_tool.name, "create_task")
        self.assertEqual(self.get_tool.name, "get_task")
        self.assertEqual(self.list_tool.name, "list_tasks")
        self.assertEqual(self.update_tool.name, "update_task")
        self.assertEqual(self.delete_tool.name, "delete_task")

        create_props = self.create_tool.parameters["properties"]
        self.assertFalse(self.create_tool.parameters["additionalProperties"])
        self.assertEqual(
            sorted(self.create_tool.parameters["required"]),
            ["name", "strategy"],
        )
        for key in (
            "name", "mode", "description", "universe",
            "strategy_preferences", "data_provider", "account_id",
            "agent", "strategy",
        ):
            self.assertIn(key, create_props)
        # 旧 settings 嵌套已下线
        self.assertNotIn("settings", create_props)
        # model_route_name 不再向模型暴露
        self.assertNotIn("model_route_name", create_props)
        # agent 嵌套对象内部所有字段都已可省（runtime 用默认值兜底）
        self.assertNotIn("required", create_props["agent"])
        self.assertFalse(create_props["agent"]["additionalProperties"])
        self.assertNotIn("required", create_props["strategy"])
        self.assertFalse(create_props["strategy"]["additionalProperties"])

        update_props = self.update_tool.parameters["properties"]
        self.assertFalse(self.update_tool.parameters["additionalProperties"])
        for key in (
            "identifier", "name", "mode", "description", "universe",
            "strategy_preferences", "data_provider", "account_id",
            "agent", "strategy",
        ):
            self.assertIn(key, update_props)
        # update_task 顶层只要求 identifier
        self.assertEqual(self.update_tool.parameters["required"], ["identifier"])
        self.assertNotIn("settings", update_props)
        self.assertNotIn("model_route_name", update_props)

    # ----- create_task: contract / schema --------------------------------

    async def test_create_rejects_unknown_top_level_arg(self) -> None:
        result = await self.create_tool.execute(
            name="t1",
            strategy={"definition_id": "sd-1"},
            agent={"react_max_turns": 1, "signal_tool_names": []},
            legacy_mode="paper",
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_arguments]", result.text)
        self.assertIn("legacy_mode", result.text)
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_arguments]", result.text)
        self.assertIn("legacy_mode", result.text)
        self.assertEqual(self.service.calls, [])

    async def test_create_rejects_legacy_settings_top_level(self) -> None:
        result = await self.create_tool.execute(
            name="t1",
            settings={"strategy": {"definition_id": "sd-1"}},
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_arguments]", result.text)
        self.assertIn("[error:unknown_arguments]", result.text)
        self.assertIn("settings", result.text)
        self.assertEqual(self.service.calls, [])

    # ----- create_task: schema coercion (JSON-string fallback) -----------

    async def test_create_coerces_strategy_json_string(self) -> None:
        result = await self.create_tool.execute(
            name="t1",
            strategy='{"definition_id": "sd-1"}',
            agent={"react_max_turns": 1, "signal_tool_names": []},
        )
        self.assertFalse(result.is_error)
        self.assertFalse(result.is_error)
        self.assertIn("Created task", result.text)
        self.assertEqual(self.service.calls[0][2]["settings"]["strategy"], {"definition_id": "sd-1"})

    async def test_create_coerces_agent_json_string(self) -> None:
        result = await self.create_tool.execute(
            name="t1",
            strategy={"definition_id": "sd-1"},
            agent='{"react_max_turns": 3, "signal_tool_names": []}',
        )
        self.assertFalse(result.is_error)
        self.assertIn("Created task", result.text)
        self.assertEqual(
            self.service.calls[0][2]["settings"]["agent"],
            {"react_max_turns": 3, "signal_tool_names": []},
        )

    async def test_create_coerces_universe_json_string(self) -> None:
        result = await self.create_tool.execute(
            name="t1",
            strategy={"definition_id": "sd-1"},
            agent={"react_max_turns": 1, "signal_tool_names": []},
            universe='["300058.SZ", "000001.SZ"]',
        )
        self.assertFalse(result.is_error)
        self.assertIn("Created task", result.text)
        self.assertEqual(
            self.service.calls[0][2]["settings"]["universe"],
            ["300058.SZ", "000001.SZ"],
        )

    async def test_create_rejects_malformed_strategy_json_string(self) -> None:
        result = await self.create_tool.execute(
            name="t1",
            strategy="{not json",
            agent={"react_max_turns": 1, "signal_tool_names": []},
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_strategy_json]", result.text)
        self.assertIn("[error:invalid_strategy_json]", result.text)
        self.assertEqual(self.service.calls, [])

    async def test_create_rejects_malformed_agent_json_string(self) -> None:
        result = await self.create_tool.execute(
            name="t1",
            strategy={"definition_id": "sd-1"},
            agent="{not json",
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_agent_json]", result.text)
        self.assertEqual(self.service.calls, [])

    async def test_create_rejects_non_string_universe_items(self) -> None:
        result = await self.create_tool.execute(
            name="t1",
            strategy={"definition_id": "sd-1"},
            agent={"react_max_turns": 1, "signal_tool_names": []},
            universe=["300058.SZ", 123],
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_universe_json]", result.text)

    # ----- create_task: required-field validation -----------------------

    async def test_create_rejects_missing_name(self) -> None:
        result = await self.create_tool.execute(
            name="   ",
            strategy={"definition_id": "sd-1"},
            agent={"react_max_turns": 1, "signal_tool_names": []},
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:missing_name]", result.text)
        self.assertIn("[error:missing_name]", result.text)
        self.assertEqual(self.service.calls, [])

    async def test_create_rejects_missing_strategy_block(self) -> None:
        result = await self.create_tool.execute(
            name="t1",
            agent={"react_max_turns": 1, "signal_tool_names": []},
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:missing_strategy_binding]", result.text)
        self.assertEqual(self.service.calls, [])

    async def test_create_rejects_empty_strategy_definition_id(self) -> None:
        result = await self.create_tool.execute(
            name="t1",
            strategy={"definition_id": "   "},
            agent={"react_max_turns": 1, "signal_tool_names": []},
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:missing_strategy_binding]", result.text)

    async def test_create_accepts_strategy_definition_binding(self) -> None:
        result = await self.create_tool.execute(
            name="t1",
            strategy={"definition_id": "sd-1", "parameter_overrides": {"window": 14}},
            agent={"react_max_turns": 1, "signal_tool_names": []},
        )
        self.assertFalse(result.is_error)
        self.assertEqual(len(self.service.calls), 1)
        settings = self.service.calls[0][2]["settings"]
        self.assertEqual(settings["strategy"]["definition_id"], "sd-1")
        self.assertEqual(settings["strategy"]["parameter_overrides"], {"window": 14})

    async def test_create_reuses_validate_api_task_settings(self) -> None:
        # 显式给的 signal_tool_names 仍走格式校验：非数组 → validation_error。
        # （缺省 signal_tool_names / react_max_turns 已都不会报错，runtime 用默认值兜底。）
        result = await self.create_tool.execute(
            name="t1",
            strategy={"definition_id": "sd-1"},
            agent={"signal_tool_names": "trade"},  # 非数组
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:validation_error]", result.text)
        self.assertIn("signal_tool_names", result.text)
        self.assertIn("[error:validation_error]", result.text)
        self.assertIn("signal_tool_names", result.text)
        self.assertEqual(self.service.calls, [])

    # ----- create_task: happy path ---------------------------------------

    async def test_create_happy_path_flat_args(self) -> None:
        result = await self.create_tool.execute(
            name="My Task",
            mode="backtest",
            description="demo",
            universe=["300058.SZ"],
            strategy_preferences="MACD golden cross",
            data_provider="auto",
            agent={
                "react_max_turns": 2,
                "signal_tool_names": ["data_bars_relative"],
            },
            strategy={"definition_id": "sd-1", "execution_profile": "backtest"},
        )
        self.assertFalse(result.is_error)
        self.assertFalse(result.is_error)
        self.assertIn("Created task", result.text)
        self.assertIn("task-1", result.text)
        self.assertIn("Created task 'My Task'", result.text)
        self.assertIn("task_id=task-1", result.text)
        self.assertIn("mode=backtest", result.text)
        call_kwargs = self.service.calls[0][2]
        self.assertEqual(call_kwargs["name"], "My Task")
        self.assertEqual(call_kwargs["mode"], "backtest")
        self.assertEqual(call_kwargs["description"], "demo")
        self.assertEqual(call_kwargs["data_provider"], "auto")
        # 工具把 flat kwargs 重打包成 settings dict
        self.assertEqual(
            call_kwargs["settings"],
            {
                "universe": ["300058.SZ"],
                "strategy_preferences": "MACD golden cross",
                "agent": {
                    "react_max_turns": 2,
                    "signal_tool_names": ["data_bars_relative"],
                },
                "strategy": {"definition_id": "sd-1", "execution_profile": "backtest"},
            },
        )

    async def test_create_omits_unset_settings_keys(self) -> None:
        # 只传必填，settings dict 里只应有 strategy。
        await self.create_tool.execute(
            name="t1",
            strategy={"definition_id": "sd-1"},
            agent={"react_max_turns": 1, "signal_tool_names": []},
        )
        settings = self.service.calls[0][2]["settings"]
        self.assertEqual(set(settings.keys()), {"strategy", "agent"})
        # data_provider 缺省时不进 settings，而是用工具 schema 默认 "auto"
        self.assertEqual(self.service.calls[0][2]["data_provider"], "auto")

    async def test_get_action(self) -> None:
        result = await self.get_tool.execute(identifier="task-1")
        self.assertFalse(result.is_error)
        self.assertEqual(_payload(result)["status"], "ok")
        self.assertEqual(_payload(result)["task"]["task_id"], "task-1")
        self.assertIn("task-1", result.text)

    async def test_list_action(self) -> None:
        result = await self.list_tool.execute(q="alpha")
        self.assertFalse(result.is_error)
        self.assertFalse(result.is_error)
        self.assertIn("task-1", result.text)
        # Prose text now front-loads identifiers + statuses for the model.
        self.assertIn("task-1", result.text)
        self.assertIn("[running]", result.text)
        self.assertIn("alpha", result.text)

    async def test_list_empty(self) -> None:
        result = await self.list_tool.execute(q="nomatch")
        self.assertFalse(result.is_error)
        self.assertIn("No tasks found", result.text)
        self.assertIn("No tasks found", result.text)

    async def test_list_emits_pagination_hint_when_more_pages_exist(self) -> None:
        # Stand in for "350 total, showing first 2": the fake service trims
        # its own list but reports total via the summary contract. We patch
        # the summary to inflate ``total`` so the hint path fires.
        self.service.list_items = [
            {"task_id": f"task-{i}", "name": f"alpha-{i}", "status": "running", "mode": "paper"}
            for i in range(2)
        ]

        async def _summary(**payload):
            self.service.calls.append(("list", (), payload))
            items = self.service.list_items[: payload.get("limit", 20)]
            return {"items": items, "total": 350, "limit": payload.get("limit", 20), "offset": payload.get("offset", 0)}

        self.service.list_tasks_summary = _summary  # type: ignore[assignment]

        result = await self.list_tool.execute(q="alpha", limit=2)
        self.assertFalse(result.is_error)
        self.assertIn("348 more", result.text)
        self.assertIn("list_tasks(", result.text)
        self.assertIn("offset=2", result.text)
        self.assertIn("limit=2", result.text)
        # carry-over filter
        self.assertIn("q='alpha'", result.text)

    async def test_list_omits_pagination_hint_on_last_page(self) -> None:
        result = await self.list_tool.execute(q="alpha")
        self.assertFalse(result.is_error)
        self.assertNotIn("more. Call list_tasks", result.text)

    # ----- update_task ---------------------------------------------------

    async def test_update_patches_universe_only(self) -> None:
        result = await self.update_tool.execute(
            identifier="task-1",
            universe=["300058.SZ"],
        )
        self.assertFalse(result.is_error)
        self.assertEqual(_payload(result)["status"], "updated")
        update_call = next(c for c in self.service.calls if c[0] == "update")
        self.assertEqual(update_call[2]["settings"], {"universe": ["300058.SZ"]})

    async def test_update_coerces_universe_json_string(self) -> None:
        result = await self.update_tool.execute(
            identifier="task-1",
            universe='["300058.SZ"]',
        )
        self.assertEqual(_payload(result)["status"], "updated")
        update_call = next(c for c in self.service.calls if c[0] == "update")
        self.assertEqual(update_call[2]["settings"], {"universe": ["300058.SZ"]})

    async def test_update_patches_strategy_binding(self) -> None:
        await self.update_tool.execute(
            identifier="task-1",
            strategy={"definition_id": "sd-new"},
        )
        update_call = next(c for c in self.service.calls if c[0] == "update")
        self.assertEqual(update_call[2]["settings"], {"strategy": {"definition_id": "sd-new"}})

    async def test_update_rejects_legacy_settings_top_level(self) -> None:
        result = await self.update_tool.execute(
            identifier="task-1",
            settings={"strategy": {"definition_id": "sd-new"}},
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_arguments]", result.text)
        self.assertIn("[error:unknown_arguments]", result.text)
        self.assertIn("settings", result.text)

    async def test_update_rejects_unknown_top_level_kwargs(self) -> None:
        result = await self.update_tool.execute(
            identifier="task-1",
            bogus_field=1,
        )
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_arguments]", result.text)
        self.assertIn("bogus_field", result.text)

    async def test_update_passes_data_provider_through(self) -> None:
        await self.update_tool.execute(
            identifier="task-1",
            data_provider="qmt",
        )
        update_call = next(c for c in self.service.calls if c[0] == "update")
        self.assertEqual(update_call[2]["data_provider"], "qmt")

    async def test_update_patches_account_id(self) -> None:
        await self.update_tool.execute(
            identifier="task-1",
            account_id="acct-mock-001",
        )
        update_call = next(c for c in self.service.calls if c[0] == "update")
        self.assertEqual(update_call[2]["settings"], {"account_id": "acct-mock-001"})

    async def test_update_clears_account_id_with_empty_string(self) -> None:
        await self.update_tool.execute(
            identifier="task-1",
            account_id="",
        )
        update_call = next(c for c in self.service.calls if c[0] == "update")
        self.assertEqual(update_call[2]["settings"], {"account_id": ""})

    async def test_delete_action(self) -> None:
        result = await self.delete_tool.execute(identifier="task-1")
        self.assertFalse(result.is_error)
        self.assertIn("Deleted task", result.text)
        self.assertIn("task-1", result.text)
        self.assertIn("Deleted task task-1", result.text)

    # --- name fallback resolution -------------------------------------------------

    async def test_get_resolves_unique_task_name(self) -> None:
        result = await self.get_tool.execute(identifier="alpha")
        self.assertFalse(result.is_error)
        self.assertEqual(_payload(result)["status"], "ok")
        self.assertEqual(_payload(result)["task"]["task_id"], "task-1")
        self.assertIn("alpha", result.text)
        # First call probes by id (fails), helper consults list, second call uses task_id.
        get_calls = [c for c in self.service.calls if c[0] == "get"]
        self.assertEqual([c[1][0] for c in get_calls], ["alpha", "task-1"])
        self.assertTrue(any(c[0] == "list" for c in self.service.calls))

    async def test_get_returns_ambiguous_candidates_for_duplicate_name(self) -> None:
        self.service.tasks_by_id["task-2"] = {
            "task_id": "task-2",
            "name": "alpha",
            "status": "configured",
            "mode": "backtest",
        }
        self.service.list_items.append(
            {"task_id": "task-2", "name": "alpha", "status": "configured", "mode": "backtest"}
        )
        result = await self.get_tool.execute(identifier="alpha")
        self.assertTrue(result.is_error)
        self.assertIn("[error:ambiguous_task_name]", result.text)
        self.assertIn("[error:ambiguous_task_name]", result.text)
        self.assertIn("Hint:", result.text)
        ids = sorted(c["task_id"] for c in _extract_candidates(result.text))
        self.assertEqual(ids, ["task-1", "task-2"])

    async def test_get_returns_helpful_error_when_name_does_not_exist(self) -> None:
        result = await self.get_tool.execute(identifier="ghost")
        self.assertTrue(result.is_error)
        self.assertIn("[error:task_not_found]", result.text)
        self.assertIn("list_tasks(q=...)", result.text)
        self.assertIn("[error:task_not_found]", result.text)

    async def test_update_resolves_unique_task_name(self) -> None:
        result = await self.update_tool.execute(
            identifier="alpha", universe=["000001.SZ"]
        )
        self.assertEqual(_payload(result)["status"], "updated")
        self.assertEqual(_payload(result)["task"]["task_id"], "task-1")
        self.assertIn("alpha", result.text)

    async def test_delete_resolves_unique_task_name(self) -> None:
        result = await self.delete_tool.execute(identifier="alpha")
        self.assertIn("Deleted task", result.text)
        self.assertIn("task-1", result.text)
        self.assertIn("alpha", result.text)

    async def test_delete_returns_ambiguous_candidates(self) -> None:
        self.service.tasks_by_id["task-2"] = {
            "task_id": "task-2",
            "name": "alpha",
            "status": "configured",
            "mode": "backtest",
        }
        self.service.list_items.append(
            {"task_id": "task-2", "name": "alpha", "status": "configured", "mode": "backtest"}
        )
        result = await self.delete_tool.execute(identifier="alpha")
        self.assertTrue(result.is_error)
        self.assertIn("[error:ambiguous_task_name]", result.text)
        # Delete must not happen on either candidate.
        delete_calls = [c for c in self.service.calls if c[0] == "delete"]
        self.assertEqual([c[1][0] for c in delete_calls], ["alpha"])

    async def test_update_returns_helpful_error_when_name_does_not_exist(self) -> None:
        result = await self.update_tool.execute(identifier="ghost", description="x")
        self.assertTrue(result.is_error)
        self.assertIn("[error:task_not_found]", result.text)

    async def test_get_descriptions_advertise_id_and_name(self) -> None:
        desc = self.get_tool.parameters["properties"]["identifier"]["description"]
        self.assertIn("task_id", desc)
        self.assertIn("task name", desc.lower())
        self.assertIn("ambiguous_task_name", desc)
