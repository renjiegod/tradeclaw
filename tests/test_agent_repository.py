import unittest
import tempfile
from pathlib import Path

from doyoutrade.assistant.repository import InMemoryAgentRepository, SqlAlchemyAgentRepository
from doyoutrade.assistant.main_agent import (
    MAIN_AGENT_ID,
    MAIN_AGENT_NAME,
    MAIN_AGENT_PROMPT_TEMPLATE_ID,
    builtin_skill_names,
    builtin_tool_names,
)
from doyoutrade.assistant.signal_composer_agent import (
    SIGNAL_COMPOSER_AGENT_ID,
    SIGNAL_COMPOSER_AGENT_NAME,
    SIGNAL_COMPOSER_PROMPT_TEMPLATE_ID,
)
from doyoutrade.persistence.db import Base, create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.errors import (
    AgentInUseError,
    BuiltinAgentImmutableError,
    RecordNotFoundError,
)
from doyoutrade.persistence.models import (
    AssistantEventRecord,
    AssistantLoadedSkillRecord,
    AssistantMessageRecord,
    AssistantSessionRecord,
    ChannelRecord,
)
from sqlalchemy import func, select


class InMemoryAgentRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repo = InMemoryAgentRepository()

    async def test_create_and_get_agent(self):
        agent = await self.repo.create_agent({
            "name": "Test Agent",
            "system_prompt": "You are a helpful agent.",
            "max_turns": 5,
        })
        self.assertEqual(agent["name"], "Test Agent")
        self.assertEqual(agent["max_turns"], 5)
        self.assertFalse(agent["is_default"])

        fetched = await self.repo.get_agent(agent["id"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["name"], "Test Agent")

    async def test_create_agent_defaults_context_compaction(self):
        agent = await self.repo.create_agent({
            "name": "Compaction Agent",
            "system_prompt": "hi",
        })
        cfg = agent["context_compaction"]
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["mode"], "auto")
        self.assertTrue(cfg["micro_compaction_enabled"])
        self.assertTrue(cfg["full_compaction_enabled"])
        self.assertTrue(cfg["allow_slash_compact"])

    async def test_create_agent_persists_prompt_template_id(self):
        agent = await self.repo.create_agent({
            "name": "Template Agent",
            "system_prompt": "",
            "system_prompt_template_id": "swing-trader",
        })

        self.assertEqual(agent["system_prompt_template_id"], "swing-trader")
        self.assertTrue(agent["resolved_system_prompt"])

        fetched = await self.repo.get_agent(agent["id"])
        self.assertEqual(fetched["system_prompt_template_id"], "swing-trader")

    async def test_list_agents_excludes_inactive_by_default(self):
        await self.repo.create_agent({"name": "Active", "system_prompt": "hi", "status": "active"})
        await self.repo.create_agent({"name": "Inactive", "system_prompt": "hi", "status": "inactive"})
        agents = await self.repo.list_agents()
        names = [a["name"] for a in agents]
        self.assertIn("Active", names)
        self.assertNotIn("Inactive", names)

        agents_all = await self.repo.list_agents(include_inactive=True)
        names_all = [a["name"] for a in agents_all]
        self.assertIn("Active", names_all)
        self.assertIn("Inactive", names_all)

    async def test_update_agent_merges_partial_context_compaction(self):
        agent = await self.repo.create_agent({
            "name": "Compaction Agent",
            "system_prompt": "hi",
        })
        updated = await self.repo.update_agent(agent["id"], {
            "context_compaction": {
                "mode": "manual",
                "auto_threshold_tokens": 12345,
            }
        })
        cfg = updated["context_compaction"]
        self.assertEqual(cfg["mode"], "manual")
        self.assertEqual(cfg["auto_threshold_tokens"], 12345)
        self.assertTrue(cfg["micro_compaction_enabled"])

    async def test_update_agent_copies_tool_and_skill_name_lists(self):
        agent = await self.repo.create_agent({
            "name": "Mutable Lists Agent",
            "system_prompt": "hi",
        })
        tool_names = ["tool_a"]
        skill_names = ["skill_a"]

        updated = await self.repo.update_agent(agent["id"], {
            "tool_names": tool_names,
            "skill_names": skill_names,
        })
        tool_names.append("tool_b")
        skill_names.append("skill_b")

        self.assertEqual(updated["tool_names"], ["tool_a"])
        self.assertEqual(updated["skill_names"], ["skill_a"])

        fetched = await self.repo.get_agent(agent["id"])
        self.assertEqual(fetched["tool_names"], ["tool_a"])
        self.assertEqual(fetched["skill_names"], ["skill_a"])

    async def test_create_agent_normalizes_tool_configs_and_derives_tool_names(self):
        agent = await self.repo.create_agent({
            "name": "Tool Config Agent",
            "system_prompt": "hi",
            "tool_configs": [
                {"name": "tool_base", "load_mode": "base"},
                {"name": "tool_deferred", "load_mode": "deferred"},
            ],
        })
        self.assertEqual(
            agent["tool_configs"],
            [
                {"name": "tool_base", "load_mode": "base"},
                {"name": "tool_deferred", "load_mode": "deferred"},
            ],
        )
        self.assertEqual(agent["tool_names"], ["tool_base", "tool_deferred"])

    async def test_create_agent_backfills_tool_configs_from_legacy_tool_names(self):
        agent = await self.repo.create_agent({
            "name": "Legacy Tool Agent",
            "system_prompt": "hi",
            "tool_names": ["tool_a", "tool_b"],
        })
        self.assertEqual(
            agent["tool_configs"],
            [
                {"name": "tool_a", "load_mode": "base"},
                {"name": "tool_b", "load_mode": "base"},
            ],
        )
        self.assertEqual(agent["tool_names"], ["tool_a", "tool_b"])

    async def test_returned_agent_context_compaction_is_not_aliased(self):
        agent = await self.repo.create_agent({
            "name": "Safe Copy Agent",
            "system_prompt": "hi",
        })
        agent["context_compaction"]["mode"] = "manual"

        fetched = await self.repo.get_agent(agent["id"])
        self.assertEqual(fetched["context_compaction"]["mode"], "auto")

        listed = await self.repo.list_agents()
        listed[0]["context_compaction"]["mode"] = "manual"
        fetched_again = await self.repo.get_agent(agent["id"])
        self.assertEqual(fetched_again["context_compaction"]["mode"], "auto")

        updated = await self.repo.update_agent(agent["id"], {
            "context_compaction": {"warning_threshold_tokens": 123},
        })
        updated["context_compaction"]["mode"] = "manual"
        fetched_after_update = await self.repo.get_agent(agent["id"])
        self.assertEqual(fetched_after_update["context_compaction"]["mode"], "auto")

        clone = await self.repo.clone_agent(agent["id"], "Safe Copy Clone")
        clone["context_compaction"]["mode"] = "manual"
        fetched_clone = await self.repo.get_agent(clone["id"])
        self.assertEqual(fetched_clone["context_compaction"]["mode"], "auto")

    async def test_delete_builtin_agent_raises(self):
        await self.repo.ensure_main_agent()
        with self.assertRaises(BuiltinAgentImmutableError):
            await self.repo.delete_agent(MAIN_AGENT_ID)

    async def test_ensure_main_agent_creates_fixed_identity(self):
        agent = await self.repo.ensure_main_agent()
        self.assertEqual(agent["id"], MAIN_AGENT_ID)
        self.assertEqual(agent["name"], MAIN_AGENT_NAME)
        self.assertTrue(agent["is_builtin"])
        self.assertTrue(agent["is_default"])
        self.assertEqual(agent["status"], "active")
        self.assertEqual(agent["system_prompt_template_id"], MAIN_AGENT_PROMPT_TEMPLATE_ID)
        self.assertEqual(agent["editable_fields"], ["model_route_name", "context_compaction", "max_turns"])

    async def test_ensure_main_agent_is_idempotent_and_preserves_editable(self):
        await self.repo.ensure_main_agent()
        await self.repo.update_agent(MAIN_AGENT_ID, {
            "model_route_name": "fast-route",
            "max_turns": 12,
            "context_compaction": {"mode": "manual"},
        })
        # Re-pin on a subsequent boot must not clobber the operator's knobs.
        again = await self.repo.ensure_main_agent()
        self.assertEqual(again["model_route_name"], "fast-route")
        self.assertEqual(again["max_turns"], 12)
        self.assertEqual(again["context_compaction"]["mode"], "manual")
        # Identity stays pinned.
        self.assertEqual(again["name"], MAIN_AGENT_NAME)
        self.assertTrue(again["is_builtin"])

    async def test_update_builtin_rejects_locked_fields(self):
        await self.repo.ensure_main_agent()
        for locked in ({"name": "Renamed"}, {"skill_names": ["x"]},
                       {"system_prompt": "hacked"}, {"tool_names": ["read_file"]}):
            with self.assertRaises(BuiltinAgentImmutableError):
                await self.repo.update_agent(MAIN_AGENT_ID, dict(locked))

    async def test_update_builtin_allows_editable_fields(self):
        await self.repo.ensure_main_agent()
        updated = await self.repo.update_agent(MAIN_AGENT_ID, {
            "model_route_name": "route-x",
            "max_turns": 9,
            "context_compaction": {"auto_threshold_tokens": 30000},
        })
        self.assertEqual(updated["model_route_name"], "route-x")
        self.assertEqual(updated["max_turns"], 9)
        self.assertEqual(updated["context_compaction"]["auto_threshold_tokens"], 30000)

    async def test_create_rejects_fixed_main_agent_id(self):
        with self.assertRaises(BuiltinAgentImmutableError):
            await self.repo.create_agent({"id": MAIN_AGENT_ID, "name": "X", "system_prompt": "y"})
        with self.assertRaises(BuiltinAgentImmutableError):
            await self.repo.create_agent({"name": "X", "system_prompt": "y", "is_builtin": True})

    async def test_clone_builtin_inherits_code_defaults(self):
        await self.repo.ensure_main_agent()
        clone = await self.repo.clone_agent(MAIN_AGENT_ID, "My Copy")
        self.assertFalse(clone["is_builtin"])
        self.assertFalse(clone["is_default"])
        self.assertEqual(clone["name"], "My Copy")
        self.assertEqual(clone["tool_names"], list(builtin_tool_names()))
        self.assertEqual(clone["skill_names"], builtin_skill_names())
        self.assertEqual(clone["system_prompt_template_id"], MAIN_AGENT_PROMPT_TEMPLATE_ID)

    async def test_ensure_signal_composer_agent_creates_fixed_identity(self):
        agent = await self.repo.ensure_signal_composer_agent()
        self.assertEqual(agent["id"], SIGNAL_COMPOSER_AGENT_ID)
        self.assertEqual(agent["name"], SIGNAL_COMPOSER_AGENT_NAME)
        self.assertTrue(agent["is_builtin"])
        # The composer is NOT a routing default — it only serves explicit compose turns.
        self.assertFalse(agent["is_default"])
        self.assertEqual(agent["status"], "active")
        self.assertEqual(agent["system_prompt_template_id"], SIGNAL_COMPOSER_PROMPT_TEMPLATE_ID)
        self.assertEqual(agent["editable_fields"], ["model_route_name", "context_compaction", "max_turns"])
        # Compose-only: zero tools, zero skills (the whole point — noise reduction).
        self.assertEqual(agent["tool_names"], [])
        self.assertEqual(agent["skill_names"], [])

    async def test_ensure_signal_composer_agent_is_idempotent_and_preserves_editable(self):
        await self.repo.ensure_signal_composer_agent()
        await self.repo.update_agent(SIGNAL_COMPOSER_AGENT_ID, {
            "model_route_name": "composer-route",
            "max_turns": 3,
            "context_compaction": {"mode": "manual"},
        })
        again = await self.repo.ensure_signal_composer_agent()
        # Operator knobs survive a re-pin.
        self.assertEqual(again["model_route_name"], "composer-route")
        self.assertEqual(again["max_turns"], 3)
        self.assertEqual(again["context_compaction"]["mode"], "manual")
        # Identity stays pinned.
        self.assertEqual(again["name"], SIGNAL_COMPOSER_AGENT_NAME)
        self.assertTrue(again["is_builtin"])
        self.assertEqual(again["tool_names"], [])
        self.assertEqual(again["skill_names"], [])

    async def test_signal_composer_rejects_locked_fields_and_delete(self):
        await self.repo.ensure_signal_composer_agent()
        for locked in ({"name": "Renamed"}, {"skill_names": ["x"]},
                       {"system_prompt": "hacked"}, {"tool_names": ["read_file"]}):
            with self.assertRaises(BuiltinAgentImmutableError):
                await self.repo.update_agent(SIGNAL_COMPOSER_AGENT_ID, dict(locked))
        with self.assertRaises(BuiltinAgentImmutableError):
            await self.repo.delete_agent(SIGNAL_COMPOSER_AGENT_ID)

    async def test_create_rejects_signal_composer_id(self):
        with self.assertRaises(BuiltinAgentImmutableError):
            await self.repo.create_agent(
                {"id": SIGNAL_COMPOSER_AGENT_ID, "name": "X", "system_prompt": "y"}
            )

    async def test_clone_agent(self):
        source = await self.repo.create_agent({
            "name": "Source",
            "system_prompt": "Source prompt",
            "system_prompt_template_id": "swing-trader",
            "model_route_name": "gpt-4",
            "tool_names": ["tool_a"],
            "max_turns": 8,
        })
        cloned = await self.repo.clone_agent(source["id"], "Cloned Agent")
        self.assertEqual(cloned["name"], "Cloned Agent")
        self.assertEqual(cloned["system_prompt"], "Source prompt")
        self.assertEqual(cloned["system_prompt_template_id"], "swing-trader")
        self.assertEqual(cloned["model_route_name"], "gpt-4")
        self.assertEqual(cloned["tool_names"], ["tool_a"])
        self.assertEqual(cloned["max_turns"], 8)
        self.assertFalse(cloned["is_default"])
        self.assertNotEqual(cloned["id"], source["id"])


class SqlAlchemyAgentRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "agent-repository.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.repo = SqlAlchemyAgentRepository(self.session_factory)

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_create_agent_defaults_context_compaction(self):
        agent = await self.repo.create_agent({
            "name": "SQL Compaction Agent",
            "system_prompt": "hi",
        })
        cfg = agent["context_compaction"]
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["mode"], "auto")
        self.assertTrue(cfg["micro_compaction_enabled"])
        self.assertTrue(cfg["full_compaction_enabled"])
        self.assertTrue(cfg["allow_slash_compact"])

    async def test_create_agent_persists_prompt_template_id(self):
        agent = await self.repo.create_agent({
            "name": "SQL Template Agent",
            "system_prompt": "",
            "system_prompt_template_id": "swing-trader",
        })

        self.assertEqual(agent["system_prompt_template_id"], "swing-trader")
        self.assertTrue(agent["resolved_system_prompt"])

        fetched = await self.repo.get_agent(agent["id"])
        self.assertEqual(fetched["system_prompt_template_id"], "swing-trader")

    async def test_update_agent_merges_partial_context_compaction(self):
        agent = await self.repo.create_agent({
            "name": "SQL Partial Update Agent",
            "system_prompt": "hi",
        })
        updated = await self.repo.update_agent(agent["id"], {
            "context_compaction": {
                "mode": "manual",
                "auto_threshold_tokens": 12345,
            }
        })
        cfg = updated["context_compaction"]
        self.assertEqual(cfg["mode"], "manual")
        self.assertEqual(cfg["auto_threshold_tokens"], 12345)
        self.assertTrue(cfg["micro_compaction_enabled"])

    async def test_ensure_main_agent_creates_fixed_identity(self):
        agent = await self.repo.ensure_main_agent()
        self.assertEqual(agent["id"], MAIN_AGENT_ID)
        self.assertEqual(agent["name"], MAIN_AGENT_NAME)
        self.assertTrue(agent["is_builtin"])
        self.assertTrue(agent["is_default"])
        self.assertEqual(agent["status"], "active")
        self.assertEqual(agent["system_prompt_template_id"], MAIN_AGENT_PROMPT_TEMPLATE_ID)
        # Template-linked, so a non-empty resolved prompt is rendered from main_agent.j2.
        self.assertTrue(agent["resolved_system_prompt"])
        self.assertEqual(
            agent["editable_fields"],
            ["model_route_name", "context_compaction", "max_turns"],
        )

        fetched = await self.repo.get_agent(MAIN_AGENT_ID)
        self.assertIsNotNone(fetched)
        self.assertTrue(fetched["is_builtin"])

    async def test_ensure_main_agent_idempotent_preserves_editable(self):
        await self.repo.ensure_main_agent()
        await self.repo.update_agent(MAIN_AGENT_ID, {
            "model_route_name": "fast-route",
            "max_turns": 11,
            "context_compaction": {"mode": "manual"},
        })
        again = await self.repo.ensure_main_agent()
        # Editable knobs survive the boot re-pin.
        self.assertEqual(again["model_route_name"], "fast-route")
        self.assertEqual(again["max_turns"], 11)
        self.assertEqual(again["context_compaction"]["mode"], "manual")
        # Identity is re-pinned from code.
        self.assertEqual(again["name"], MAIN_AGENT_NAME)
        self.assertTrue(again["is_builtin"])
        self.assertTrue(again["is_default"])

    async def test_delete_builtin_agent_raises(self):
        await self.repo.ensure_main_agent()
        with self.assertRaises(BuiltinAgentImmutableError):
            await self.repo.delete_agent(MAIN_AGENT_ID)
        self.assertIsNotNone(await self.repo.get_agent(MAIN_AGENT_ID))

    async def test_update_builtin_rejects_locked_allows_editable(self):
        await self.repo.ensure_main_agent()
        with self.assertRaises(BuiltinAgentImmutableError):
            await self.repo.update_agent(MAIN_AGENT_ID, {"name": "Renamed"})
        with self.assertRaises(BuiltinAgentImmutableError):
            await self.repo.update_agent(MAIN_AGENT_ID, {"skill_names": ["x"]})
        updated = await self.repo.update_agent(MAIN_AGENT_ID, {
            "model_route_name": "route-x",
            "max_turns": 9,
        })
        self.assertEqual(updated["model_route_name"], "route-x")
        self.assertEqual(updated["max_turns"], 9)

    async def test_create_rejects_fixed_main_agent_id(self):
        with self.assertRaises(BuiltinAgentImmutableError):
            await self.repo.create_agent({"id": MAIN_AGENT_ID, "name": "X", "system_prompt": "y"})

    async def test_ensure_signal_composer_agent_creates_fixed_identity(self):
        agent = await self.repo.ensure_signal_composer_agent()
        self.assertEqual(agent["id"], SIGNAL_COMPOSER_AGENT_ID)
        self.assertEqual(agent["name"], SIGNAL_COMPOSER_AGENT_NAME)
        self.assertTrue(agent["is_builtin"])
        self.assertFalse(agent["is_default"])
        self.assertEqual(agent["system_prompt_template_id"], SIGNAL_COMPOSER_PROMPT_TEMPLATE_ID)
        # Compose-only: zero tools, zero skills.
        self.assertEqual(agent["tool_names"], [])
        self.assertEqual(agent["skill_names"], [])
        # Template-linked, so a non-empty resolved prompt renders from signal_card_composer.j2.
        self.assertTrue(agent["resolved_system_prompt"])

        fetched = await self.repo.get_agent(SIGNAL_COMPOSER_AGENT_ID)
        self.assertIsNotNone(fetched)
        self.assertTrue(fetched["is_builtin"])
        # Re-pin preserves identity and stays idempotent on the ORM path.
        again = await self.repo.ensure_signal_composer_agent()
        self.assertEqual(again["id"], SIGNAL_COMPOSER_AGENT_ID)
        self.assertEqual(again["tool_names"], [])

    async def test_signal_composer_orm_rejects_delete_and_locked_fields(self):
        await self.repo.ensure_signal_composer_agent()
        with self.assertRaises(BuiltinAgentImmutableError):
            await self.repo.delete_agent(SIGNAL_COMPOSER_AGENT_ID)
        with self.assertRaises(BuiltinAgentImmutableError):
            await self.repo.update_agent(SIGNAL_COMPOSER_AGENT_ID, {"name": "Mutated"})
        with self.assertRaises(BuiltinAgentImmutableError):
            await self.repo.create_agent(
                {"id": SIGNAL_COMPOSER_AGENT_ID, "name": "X", "system_prompt": "y"}
            )

    async def test_signal_composer_inherits_main_agent_model_route_when_blank(self):
        # Main agent carries a configured route; the composer is seeded after it.
        await self.repo.ensure_main_agent()
        await self.repo.update_agent(MAIN_AGENT_ID, {"model_route_name": "main-route"})
        composer = await self.repo.ensure_signal_composer_agent()
        # Blank composer route inherits the main agent's route so a fresh seed
        # is callable (an empty route would resolve to the keyless baseline 500).
        self.assertEqual(composer["model_route_name"], "main-route")

        # An explicit composer route is preserved across a re-pin (operator override).
        await self.repo.update_agent(SIGNAL_COMPOSER_AGENT_ID, {"model_route_name": "composer-route"})
        again = await self.repo.ensure_signal_composer_agent()
        self.assertEqual(again["model_route_name"], "composer-route")

    async def test_clone_builtin_inherits_code_defaults(self):
        await self.repo.ensure_main_agent()
        clone = await self.repo.clone_agent(MAIN_AGENT_ID, "Copy Of Main")
        self.assertFalse(clone["is_builtin"])
        self.assertFalse(clone["is_default"])
        self.assertEqual(clone["tool_names"], list(builtin_tool_names()))
        self.assertEqual(clone["skill_names"], builtin_skill_names())
        # The clone is a normal editable agent (mutating it must not raise).
        await self.repo.update_agent(clone["id"], {"name": "Renamed Clone"})

    async def test_create_agent_persists_tool_configs_and_derived_tool_names(self):
        agent = await self.repo.create_agent({
            "name": "SQL Tool Config Agent",
            "system_prompt": "hi",
            "tool_configs": [
                {"name": "tool_base", "load_mode": "base"},
                {"name": "tool_deferred", "load_mode": "deferred"},
            ],
        })
        self.assertEqual(agent["tool_names"], ["tool_base", "tool_deferred"])
        self.assertEqual(
            agent["tool_configs"],
            [
                {"name": "tool_base", "load_mode": "base"},
                {"name": "tool_deferred", "load_mode": "deferred"},
            ],
        )

    async def test_delete_agent_with_sessions_raises_agent_in_use(self):
        agent = await self.repo.create_agent({
            "name": "SQL Referenced Agent",
            "system_prompt": "hi",
        })
        async with self.session_factory() as session:
            session.add(
                AssistantSessionRecord(
                    session_id="sess-referencing",
                    agent_id=agent["id"],
                    title="referencing session",
                )
            )
            await session.commit()

        with self.assertRaises(AgentInUseError) as ctx:
            await self.repo.delete_agent(agent["id"])
        self.assertIn("assistant session", str(ctx.exception))
        # The agent must still exist after the rejected delete.
        self.assertIsNotNone(await self.repo.get_agent(agent["id"]))

    async def test_delete_agent_without_sessions_succeeds(self):
        agent = await self.repo.create_agent({
            "name": "SQL Deletable Agent",
            "system_prompt": "hi",
        })
        await self.repo.delete_agent(agent["id"])
        self.assertIsNone(await self.repo.get_agent(agent["id"]))

    async def test_force_delete_agent_cascades_sessions_and_children(self):
        agent = await self.repo.create_agent({
            "name": "SQL Force Delete Agent",
            "system_prompt": "hi",
        })
        agent_id = agent["id"]
        async with self.session_factory() as session:
            session.add_all([
                AssistantSessionRecord(session_id="sess-fd-1", agent_id=agent_id, title="s1"),
                AssistantSessionRecord(session_id="sess-fd-2", agent_id=agent_id, title="s2"),
                AssistantMessageRecord(message_id="msg-fd-1", session_id="sess-fd-1", role="user", content="hi"),
                AssistantEventRecord(event_id="evt-fd-1", session_id="sess-fd-1", event_type="message", payload={}),
                AssistantLoadedSkillRecord(
                    session_id="sess-fd-1",
                    skill_name="some-skill",
                    skill_path="/tmp/x",
                    body="body",
                    body_hash="hash",
                    byte_size=4,
                ),
            ])
            await session.commit()

        await self.repo.delete_agent(agent_id, force=True)

        self.assertIsNone(await self.repo.get_agent(agent_id))
        async with self.session_factory() as session:
            for model, attr in (
                (AssistantSessionRecord, AssistantSessionRecord.agent_id == agent_id),
                (AssistantMessageRecord, AssistantMessageRecord.session_id.in_(["sess-fd-1", "sess-fd-2"])),
                (AssistantEventRecord, AssistantEventRecord.session_id.in_(["sess-fd-1", "sess-fd-2"])),
                (AssistantLoadedSkillRecord, AssistantLoadedSkillRecord.session_id.in_(["sess-fd-1", "sess-fd-2"])),
            ):
                remaining = await session.scalar(select(func.count()).select_from(model).where(attr))
                self.assertEqual(remaining, 0, f"{model.__name__} rows should be gone")

    async def test_delete_agent_with_channel_blocks_even_with_force(self):
        agent = await self.repo.create_agent({
            "name": "SQL Channel Agent",
            "system_prompt": "hi",
        })
        agent_id = agent["id"]
        async with self.session_factory() as session:
            session.add(
                ChannelRecord(id="chan-1", name="c1", type="feishu", agent_id=agent_id)
            )
            await session.commit()

        with self.assertRaises(AgentInUseError) as ctx:
            await self.repo.delete_agent(agent_id, force=True)
        self.assertIn("channel", str(ctx.exception))
        self.assertIsNotNone(await self.repo.get_agent(agent_id))
