import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


def _extract_json_payload(text: str) -> dict:
    """Pull the ```json``` block out of a single-channel ToolResult.text."""

    m = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    if m is None:
        raise AssertionError(f"no JSON block in tool result text: {text!r}")
    return json.loads(m.group(1))

from doyoutrade.assistant.strategy_tools import (
    BindStrategyDefinitionToTaskTool,
    GetRunDebugViewTool,
    InspectStrategyResourcesTool,
    PromoteStrategyDefinitionToLiveTool,
)
from doyoutrade.persistence import (
    Base,
    SqlAlchemyStrategyDefinitionRepository,
    create_engine_and_session_factory,
    dispose_engine,
)
from doyoutrade.strategy_registry import StrategyDefinitionCreate, StrategyRegistryService
from doyoutrade.strategy_runtime.compiler import StrategyCompiler


class _PlatformServiceStub:
    def __init__(self) -> None:
        self.update_calls: list[tuple[str, dict]] = []
        self.debug_view_calls: list[str] = []
        self.debug_view_result = {
            "cycle_run": {"run_id": "run-1"},
            "cycle_runs": [{"run_id": "run-1"}],
            "session": {"session_id": "sess-1"},
            "spans": [{"name": "run_strategy"}],
            "model_invocations": [{"run_id": "run-1"}],
            "resolved_from": {"identifier": "run-1", "identifier_type": "cycle_run"},
            "backtest_job": None,
        }

    async def update_task(self, task_id: str, **payload):
        self.update_calls.append((task_id, payload))
        return {"task_id": task_id, **payload}

    async def get_run_debug_view(self, run_id: str):
        self.debug_view_calls.append(run_id)
        return self.debug_view_result


class StrategyOperationHandlersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "strategy-tools.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        self.definition_repo = SqlAlchemyStrategyDefinitionRepository(self.session_factory)
        self.registry_service = StrategyRegistryService(self.definition_repo)
        self.platform_service = _PlatformServiceStub()

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_create_definition_persists_metadata(self) -> None:
        # Regression guard: StrategyDefinitionCreate round-trips through
        # the registry service and the snapshot is returned correctly.
        # The service no longer compiles source on write; parameter_schema
        # must be passed explicitly.
        parameter_schema = {
            "fast": {"type": "integer", "default": 12},
            "slow": {"type": "integer", "default": 26},
        }
        snapshot = await self.registry_service.create_definition(
            payload=StrategyDefinitionCreate(
                definition_id="sd-macd-thaw",
                name="MACD thaw test",
                api_version="v1",
                parameter_schema=parameter_schema,
                capabilities={},
                provenance={"source": "test"},
            )
        )

        # Persisted payload must be plain JSON-serializable dicts.
        self.assertEqual(snapshot.definition_id, "sd-macd-thaw")
        stored_schema = snapshot.parameter_schema_json
        assert isinstance(stored_schema, dict)
        self.assertIn("fast", stored_schema)
        self.assertIsInstance(stored_schema["fast"], dict)
        import json as _json
        _json.dumps(stored_schema)

    async def test_inspect_tool_marks_recommended_reuse_for_duplicate_definitions(self) -> None:
        # code_hash is now passed explicitly since the service no longer
        # compiles source on write.  Shared definitions use the same hash;
        # the unique definition gets a different hash.
        for definition_id, name, code_hash in (
            ("sd-dup-a", "Dup A", "hash-shared"),
            ("sd-dup-b", "Dup B", "hash-shared"),
            ("sd-unique", "Unique", "hash-unique"),
        ):
            await self.registry_service.create_definition(
                payload=StrategyDefinitionCreate(
                    definition_id=definition_id,
                    name=name,
                    api_version="v1",
                    parameter_schema={},
                    default_parameters={},
                    capabilities={},
                    provenance={"source": "test"},
                    code_hash=code_hash,
                )
            )
        inspect_tool = InspectStrategyResourcesTool(self.definition_repo)

        tool_result = await inspect_tool.execute()
        result = _extract_json_payload(tool_result.text)

        self.assertFalse(tool_result.is_error)
        self.assertEqual(result["status"], "ok")
        defs_by_id = {item["definition_id"]: item for item in result["definitions"]}
        # Duplicates share a code_hash; the earliest-created/lowest-id wins
        # the canonical slot (sd-dup-a sorts before sd-dup-b).
        self.assertEqual(defs_by_id["sd-dup-a"]["recommended_reuse_id"], "sd-dup-a")
        self.assertEqual(defs_by_id["sd-dup-b"]["recommended_reuse_id"], "sd-dup-a")
        # Singleton groups should still surface their own id as canonical.
        self.assertEqual(defs_by_id["sd-unique"]["recommended_reuse_id"], "sd-unique")

        groups = result["duplicate_definition_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["recommended_reuse_id"], "sd-dup-a")
        self.assertEqual(sorted(groups[0]["definition_ids"]), ["sd-dup-a", "sd-dup-b"])
        self.assertIn("reuse_hint", result)

    async def test_inspect_tool_supports_fuzzy_query_across_definitions(self) -> None:
        for definition_id, name, prompt in (
            ("sd-macd-trend", "MACD Trend", "MACD crossover trend follow"),
            ("sd-rsi-revert", "RSI Reverter", "RSI mean reversion"),
        ):
            await self.registry_service.create_definition(
                payload=StrategyDefinitionCreate(
                    definition_id=definition_id,
                    name=name,
                    api_version="v1",
                    parameter_schema={},
                    default_parameters={},
                    capabilities={},
                    provenance={"source": "test"},
                    generation_prompt=prompt,
                )
            )

        inspect_tool = InspectStrategyResourcesTool(self.definition_repo)

        # Single-token match: filters to MACD-only and surfaces match_reasons.
        result = _extract_json_payload((await inspect_tool.execute(query="macd")).text)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([d["definition_id"] for d in result["definitions"]], ["sd-macd-trend"])
        self.assertEqual(result["total_definitions"], 2)
        self.assertEqual(result["matched_tokens"], ["macd"])
        self.assertIn("match_reasons", result["definitions"][0])
        self.assertIn("name", result["definitions"][0]["match_reasons"])

        # Multi-token AND: both tokens must appear somewhere.
        result_and = _extract_json_payload((await inspect_tool.execute(query="macd trend")).text)
        self.assertEqual([d["definition_id"] for d in result_and["definitions"]], ["sd-macd-trend"])

        # Token with no match yields empty filtered output but preserves totals.
        result_miss = _extract_json_payload((await inspect_tool.execute(query="kdj")).text)
        self.assertEqual(result_miss["definitions"], [])
        self.assertEqual(result_miss["total_definitions"], 2)
        self.assertNotIn("duplicate_definition_groups", result_miss)

        # Match against the generation_prompt token ("reversion").
        result_prompt = _extract_json_payload((await inspect_tool.execute(query="reversion")).text)
        self.assertEqual([d["definition_id"] for d in result_prompt["definitions"]], ["sd-rsi-revert"])
        self.assertIn("generation_prompt", result_prompt["definitions"][0]["match_reasons"])

        # Empty query degrades to listing everything (backwards compatible).
        result_all = _extract_json_payload((await inspect_tool.execute(query="")).text)
        self.assertEqual(len(result_all["definitions"]), 2)
        self.assertNotIn("match_reasons", result_all["definitions"][0])

    async def test_inspect_tool_rejects_unknown_kwargs(self) -> None:
        inspect_tool = InspectStrategyResourcesTool(self.definition_repo)

        tool_result = await inspect_tool.execute(unknown_field="oops")

        self.assertTrue(tool_result.is_error)
        self.assertIn("[error:unknown_arguments]", tool_result.text)
        self.assertIn("unknown_field", tool_result.text)

    async def test_bind_strategy_definition_tool_updates_task_strategy_binding(self) -> None:
        tool = BindStrategyDefinitionToTaskTool(self.platform_service)

        result = await tool.execute(task_id="task-1", definition_id="sd-primary")

        self.assertFalse(result.is_error)
        self.assertIn("sd-primary", result.text)
        self.assertIn("task-1", result.text)
        self.assertEqual(
            self.platform_service.update_calls,
            [("task-1", {"settings": {"strategy": {"definition_id": "sd-primary"}}})],
        )

    async def test_bind_strategy_definition_tool_rejects_sd_prefix_in_task_id(self) -> None:
        tool = BindStrategyDefinitionToTaskTool(self.platform_service)

        raw = await tool.execute(task_id="sd-mistake", definition_id="sd-1")
        self.assertTrue(raw.is_error)
        self.assertIn("[error:wrong_identifier_type]", raw.text)
        self.assertEqual(self.platform_service.update_calls, [])

    async def test_bind_strategy_definition_tool_rejects_uuid_for_definition_id(self) -> None:
        tool = BindStrategyDefinitionToTaskTool(self.platform_service)

        raw = await tool.execute(task_id="task-1", definition_id="uuid-style")
        self.assertTrue(raw.is_error)
        self.assertIn("[error:wrong_identifier_type]", raw.text)
        self.assertEqual(self.platform_service.update_calls, [])

    async def test_promote_strategy_definition_to_live_tool_updates_task_strategy_binding(self) -> None:
        tool = PromoteStrategyDefinitionToLiveTool(self.platform_service)

        result = await tool.execute(
            task_id="task-1",
            definition_id="sd-live",
            approval_policy={"mode": "manual"},
            risk_overrides={"max_positions": 5},
        )

        self.assertFalse(result.is_error)
        self.assertIn("sd-live", result.text)
        self.assertIn("task-1", result.text)
        self.assertEqual(
            self.platform_service.update_calls,
            [
                (
                    "task-1",
                    {
                        "settings": {
                            "strategy": {
                                "definition_id": "sd-live",
                                "approval_policy": {"mode": "manual"},
                                "risk_overrides": {"max_positions": 5},
                            }
                        }
                    },
                )
            ],
        )

    async def test_promote_omits_unset_fields_instead_of_overwriting_with_empty_dict(self) -> None:
        tool = PromoteStrategyDefinitionToLiveTool(self.platform_service)

        result = await tool.execute(
            task_id="task-1",
            definition_id="sd-live",
            approval_policy={"mode": "manual"},
        )

        self.assertFalse(result.is_error)
        # risk_overrides was None; the patch must not include the key, so the
        # platform's existing risk_overrides is preserved.
        sent = self.platform_service.update_calls[0][1]["settings"]["strategy"]
        self.assertEqual(
            sent,
            {"definition_id": "sd-live", "approval_policy": {"mode": "manual"}},
        )
        self.assertNotIn("risk_overrides", sent)

    async def test_promote_coerces_json_string_approval_policy(self) -> None:
        tool = PromoteStrategyDefinitionToLiveTool(self.platform_service)

        result = await tool.execute(
            task_id="task-1",
            definition_id="sd-live",
            approval_policy='{"mode":"manual"}',
        )

        self.assertFalse(result.is_error)
        sent = self.platform_service.update_calls[0][1]["settings"]["strategy"]
        self.assertEqual(sent["approval_policy"], {"mode": "manual"})

    async def test_promote_rejects_invalid_json_approval_policy(self) -> None:
        tool = PromoteStrategyDefinitionToLiveTool(self.platform_service)

        raw = await tool.execute(
            task_id="task-1",
            definition_id="sd-live",
            approval_policy="{not json",
        )
        self.assertTrue(raw.is_error)
        self.assertIn("[error:invalid_approval_policy_json]", raw.text)
        self.assertEqual(self.platform_service.update_calls, [])

    async def test_definition_tools_support_read_and_update(self) -> None:
        from doyoutrade.assistant.strategy_tools import (
            GetStrategyDefinitionTool,
            UpdateStrategyDefinitionTool,
        )

        await self.registry_service.create_definition(
            payload=StrategyDefinitionCreate(
                definition_id="sd-editable",
                name="Editable",
                api_version="v1",
                parameter_schema={},
                default_parameters={},
                capabilities={},
                provenance={"source": "test"},
            )
        )

        update_tool = UpdateStrategyDefinitionTool(self.registry_service)
        get_definition_tool = GetStrategyDefinitionTool(self.definition_repo)

        # Update metadata only (source code is managed by authoring lifecycle).
        updated_result = await update_tool.execute(
            definition_id="sd-editable",
            name="Editable v2",
            parameter_schema={"lookback": {"type": "integer"}},
            capabilities={"supports_live": False},
        )
        definition_result = await get_definition_tool.execute(definition_id="sd-editable")

        self.assertFalse(updated_result.is_error)
        self.assertIn('"status": "ok"', updated_result.text)
        self.assertIn('"name": "Editable v2"', updated_result.text)
        self.assertIn('"name": "Editable v2"', definition_result.text)
        self.assertIn('"next_steps"', updated_result.text)

    async def test_get_run_debug_view_tool_returns_platform_payload(self) -> None:
        tool = GetRunDebugViewTool(self.platform_service)

        tool_result = await tool.execute(run_id="run-1")

        self.assertFalse(tool_result.is_error)
        self.assertIn("run-1", tool_result.text)
        # Prose header surfaces key counts before the embedded JSON block.
        self.assertIn("cycle_run", tool_result.text)
        self.assertIn("span(s)", tool_result.text)
        self.assertIn("model_invocation(s)", tool_result.text)
        # Embedded JSON block carries the full debug_view payload.
        self.assertIn('"status": "ok"', tool_result.text)
        self.assertIn('"debug_view"', tool_result.text)
        self.assertIn('"identifier_type": "cycle_run"', tool_result.text)
        self.assertEqual(self.platform_service.debug_view_calls, ["run-1"])

    async def test_get_run_debug_view_tool_supports_summary_only_mode(self) -> None:
        self.platform_service.debug_view_result = {
            "cycle_run": {"run_id": "run-1"},
            "cycle_runs": [{"run_id": "run-a"}, {"run_id": "run-b"}, {"run_id": "run-c"}],
            "session": {"session_id": "sess-1"},
            "spans": [{"name": "s1"}, {"name": "s2"}],
            "model_invocations": [{"run_id": "run-1"}, {"run_id": "run-2"}],
            "resolved_from": {"identifier": "run-1", "identifier_type": "cycle_run"},
            "backtest_job": {"run_id": "btjob-1"},
        }
        tool = GetRunDebugViewTool(self.platform_service)

        tool_result = await tool.execute(
            run_id="run-1",
            summary_only=True,
            include_spans=False,
            include_model_invocations=False,
            include_cycle_runs_limit=1,
        )

        self.assertFalse(tool_result.is_error)
        # Embedded JSON carries the trimmed debug_view: cycle_runs truncated
        # to 1, spans/model_invocations dropped, summary attached.
        self.assertIn('"status": "ok"', tool_result.text)
        # cycle_runs truncated to a single entry — only run-a survives.
        self.assertIn('"run_id": "run-a"', tool_result.text)
        self.assertNotIn('"run_id": "run-b"', tool_result.text)
        self.assertNotIn('"run_id": "run-c"', tool_result.text)
        # spans/model_invocations omitted from the debug_view payload.
        self.assertNotIn('"spans"', tool_result.text)
        self.assertNotIn('"model_invocations"', tool_result.text)
        # summary block attached.
        self.assertIn('"summary"', tool_result.text)
        self.assertIn('"cycle_run_count"', tool_result.text)

    async def test_suggest_strategy_iteration_tool_classifies_next_action(self) -> None:
        from doyoutrade.assistant.strategy_tools import SuggestStrategyIterationTool

        tool = SuggestStrategyIterationTool(self.platform_service)

        self.platform_service.debug_view_result = {
            "cycle_run": {"run_id": "run-empty", "details": {"strategy_trace": {"final_target_summary": {"allocation_count": 0}}}},
            "session": {"session_id": "sess-empty"},
            "spans": [{"name": "run_strategy"}],
            "model_invocations": [{"run_id": "run-empty"}],
        }
        empty_result = _extract_json_payload((await tool.execute(run_id="run-empty")).text)
        self.assertEqual(empty_result["suggestion"]["action_type"], "parameter_only")
        self.assertIn("update_task", empty_result["suggestion"]["recommended_tools"])

        self.platform_service.debug_view_result = {
            "cycle_run": {"run_id": "run-missing-trace", "details": {"strategy_trace": {"final_target_summary": {"allocation_count": 1}}}},
            "session": {"session_id": "sess-missing"},
            "spans": [],
            "model_invocations": [],
        }
        missing_trace_result = _extract_json_payload(
            (await tool.execute(run_id="run-missing-trace")).text
        )
        self.assertEqual(missing_trace_result["suggestion"]["action_type"], "binding_change")
        self.assertIn("bind_strategy_definition_to_task", missing_trace_result["suggestion"]["recommended_tools"])

        self.platform_service.debug_view_result = {
            "cycle_run": {"run_id": "run-good", "details": {"strategy_trace": {"final_target_summary": {"allocation_count": 2}}}},
            "session": {"session_id": "sess-good"},
            "spans": [{"name": "run_strategy"}],
            "model_invocations": [{"run_id": "run-good"}],
        }
        good_result = _extract_json_payload((await tool.execute(run_id="run-good")).text)
        self.assertEqual(good_result["suggestion"]["action_type"], "definition_change")
        self.assertIn("update_strategy_definition", good_result["suggestion"]["recommended_tools"])

    async def test_suggest_strategy_iteration_prefers_definition_change_for_definition_risk(self) -> None:
        from doyoutrade.assistant.strategy_tools import SuggestStrategyIterationTool

        self.platform_service.debug_view_result = {
            "cycle_run": {
                "run_id": "run-risk",
                "details": {
                    "strategy_trace": {"final_target_summary": {"allocation_count": 0}},
                    "definition_risks": [
                        {"type": "ctx_position_shape_mismatch"},
                    ],
                },
            },
            "session": {"session_id": "sess-risk"},
            "spans": [{"name": "run_strategy"}],
            "model_invocations": [{"run_id": "run-risk"}],
        }
        tool = SuggestStrategyIterationTool(self.platform_service)

        result = _extract_json_payload((await tool.execute(run_id="run-risk")).text)

        self.assertEqual(result["suggestion"]["action_type"], "definition_change")
        self.assertIn("definition risk", result["suggestion"]["reason"].lower())



    # ------------------------------------------------------------------
    # Auto-smoke gate on the definition-authoring tools
    # ------------------------------------------------------------------

    async def test_update_definition_tool_metadata_only_update_succeeds(self) -> None:
        """Metadata-only updates (name / status / parameter_schema) must succeed."""
        from doyoutrade.assistant.strategy_tools import UpdateStrategyDefinitionTool

        await self.registry_service.create_definition(
            payload=StrategyDefinitionCreate(
                definition_id="sd-metadata-only",
                name="Original Name",
                api_version="v1",
                parameter_schema={},
                default_parameters={},
                capabilities={},
                provenance={"source": "test"},
            )
        )

        update_tool = UpdateStrategyDefinitionTool(self.registry_service)
        raw = await update_tool.execute(
            definition_id="sd-metadata-only",
            name="Renamed",
        )
        self.assertFalse(raw.is_error, raw.text)
        body = _extract_json_payload(raw.text)
        self.assertEqual(body["status"], "ok")
        self.assertNotIn("smoke", body)  # smoke gate removed; no smoke key
        snapshot = await self.definition_repo.get_definition("sd-metadata-only")
        self.assertEqual(snapshot.name, "Renamed")


if __name__ == "__main__":
    unittest.main()
