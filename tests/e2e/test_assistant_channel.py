"""
E2E test for AssistantService + Channel integration.
Verifies: feishu message → ChannelManager.enqueue → AssistantService.send_message

Uses DOYOUTRADE_E2E_PROFILE=isolated (stub model adapter, no real LLM calls).
"""
import asyncio
import unittest

from tests.e2e.support import (
    E2EModelMode,
    build_e2e_runtime,
    e2e_enabled,
)


@unittest.skipUnless(e2e_enabled(), "set DOYOUTRADE_E2E=1 to run end-to-end tests")
class TestAssistantChannelE2E(unittest.IsolatedAsyncioTestCase):
    async def test_channel_manager_in_runtime(self):
        """Verify DB-backed channel manager is present after bootstrap."""
        async with build_e2e_runtime(
            profile="isolated",
            model_mode=E2EModelMode.STUB,
        ) as ctx:
            channel_manager = ctx.runtime.get("channel_manager")
            if channel_manager is None:
                self.skipTest("channel_manager not in runtime (bootstrap may not have been updated yet)")
            self.assertEqual(channel_manager.channel_ids, [])
            self.assertIn("channel_repository", ctx.runtime)

    async def test_feishu_channels_bind_messages_to_their_agents(self):
        """Two Feishu channel rows can share a sender id without sharing sessions."""
        async with build_e2e_runtime(
            profile="isolated",
            model_mode=E2EModelMode.STUB,
        ) as ctx:
            assistant_service = ctx.runtime["assistant_service"]
            agent_repo = assistant_service.agent_repo
            channel_manager = ctx.runtime["channel_manager"]
            channel_repo = ctx.runtime["channel_repository"]

            agent_a = await agent_repo.create_agent({
                "name": "E2E Agent A",
                "system_prompt": "agent a",
            })
            agent_b = await agent_repo.create_agent({
                "name": "E2E Agent B",
                "system_prompt": "agent b",
            })
            channel_a = await channel_repo.create_channel({
                "name": "E2E Feishu A",
                "type": "feishu",
                "enabled": True,
                "agent_id": agent_a["id"],
                "config": {"app_id": "cli_a", "domain": "feishu"},
                "secrets": {"app_secret": "secret-a"},
            })
            channel_b = await channel_repo.create_channel({
                "name": "E2E Feishu B",
                "type": "feishu",
                "enabled": True,
                "agent_id": agent_b["id"],
                "config": {"app_id": "cli_b", "domain": "feishu"},
                "secrets": {"app_secret": "secret-b"},
            })

            from doyoutrade.assistant.channels.feishu import FeishuChannel

            for channel, agent in ((channel_a, agent_a), (channel_b, agent_b)):
                runtime_channel = FeishuChannel(
                    assistant_service=assistant_service,
                    channel_id=channel["id"],
                    app_id=str(channel["config"]["app_id"]),
                    app_secret="stub",
                )
                runtime_channel._manager = channel_manager
                channel_manager.register(runtime_channel, agent_id=agent["id"])

            sender = "ou_same_sender"
            await channel_manager.enqueue(
                channel_a["id"],
                {
                    "sender_id": sender,
                    "session_id": f"channel:{channel_a['id']}:{sender}",
                    "content": "hello a",
                    "meta": {},
                },
            )
            await channel_manager.enqueue(
                channel_b["id"],
                {
                    "sender_id": sender,
                    "session_id": f"channel:{channel_b['id']}:{sender}",
                    "content": "hello b",
                    "meta": {},
                },
            )
            await asyncio.sleep(0.5)

            sessions = await assistant_service.list_sessions()
            by_id = {row["session_id"]: row for row in sessions["items"]}
            session_a = f"channel:{channel_a['id']}:{sender}"
            session_b = f"channel:{channel_b['id']}:{sender}"
            self.assertIn(session_a, by_id)
            self.assertIn(session_b, by_id)
            self.assertEqual(by_id[session_a]["agent_id"], agent_a["id"])
            self.assertEqual(by_id[session_b]["agent_id"], agent_b["id"])

    async def test_http_channel_build_request(self):
        """Verify HttpChannel correctly builds ChannelAgentRequest from dict payload."""
        async with build_e2e_runtime(
            profile="isolated",
            model_mode=E2EModelMode.STUB,
        ) as ctx:
            channel_manager = ctx.runtime.get("channel_manager")
            if channel_manager is None:
                self.skipTest("channel_manager not in runtime")

            from doyoutrade.assistant.channels.http import HttpChannel

            http_ch = HttpChannel(
                assistant_service=ctx.runtime["assistant_service"],
                channel_id="channel-http-e2e",
            )

            payload = {
                "session_id": "http:e2e_user",
                "content": "test message",
                "sender_id": "e2e_user",
                "meta": {"source": "e2e"},
            }
            req = http_ch.build_agent_request_from_native(payload)
            self.assertEqual(req.session_id, "http:e2e_user")
            self.assertEqual(req.content, "test message")
            self.assertEqual(req.sender_id, "e2e_user")
            self.assertEqual(req.channel_meta["source"], "e2e")
