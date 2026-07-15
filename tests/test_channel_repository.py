import unittest

from doyoutrade.assistant.repository import InMemoryChannelRepository
from doyoutrade.persistence.errors import RecordNotFoundError


class InMemoryChannelRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repo = InMemoryChannelRepository()

    async def test_create_list_and_redact_channel_secrets(self):
        first = await self.repo.create_channel(
            {
                "name": "Feishu Alpha",
                "type": "feishu",
                "enabled": True,
                "agent_id": "agent-alpha",
                "config": {"app_id": "cli_alpha", "domain": "feishu"},
                "secrets": {
                    "app_secret": "secret-alpha",
                    "encrypt_key": "encrypt-alpha",
                },
            }
        )
        second = await self.repo.create_channel(
            {
                "name": "Feishu Beta",
                "type": "feishu",
                "enabled": False,
                "agent_id": "agent-beta",
                "config": {"app_id": "cli_beta", "domain": "lark"},
                "secrets": {"app_secret": "secret-beta"},
            }
        )

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["status"], "stopped")
        self.assertNotIn("secrets", first)
        self.assertEqual(first["secret_keys"], ["app_secret", "encrypt_key"])

        rows = await self.repo.list_channels(type="feishu")
        self.assertEqual([row["id"] for row in rows], [first["id"], second["id"]])
        self.assertEqual({row["agent_id"] for row in rows}, {"agent-alpha", "agent-beta"})
        self.assertTrue(all("secrets" not in row for row in rows))

        raw = await self.repo.get_channel(first["id"], include_secrets=True)
        self.assertEqual(raw["secrets"]["app_secret"], "secret-alpha")

    async def test_update_leaves_missing_and_blank_secrets_unchanged(self):
        created = await self.repo.create_channel(
            {
                "name": "Feishu Alpha",
                "type": "feishu",
                "agent_id": "agent-alpha",
                "config": {"app_id": "cli_alpha"},
                "secrets": {"app_secret": "secret-alpha"},
            }
        )

        updated = await self.repo.update_channel(
            created["id"],
            {
                "name": "Feishu Alpha Renamed",
                "config": {"app_id": "cli_alpha_2"},
                "secrets": {"app_secret": "", "verification_token": "token-1"},
            },
        )

        self.assertEqual(updated["name"], "Feishu Alpha Renamed")
        self.assertEqual(updated["config"], {"app_id": "cli_alpha_2"})
        self.assertEqual(updated["secret_keys"], ["app_secret", "verification_token"])

        raw = await self.repo.get_channel(created["id"], include_secrets=True)
        self.assertEqual(raw["secrets"]["app_secret"], "secret-alpha")
        self.assertEqual(raw["secrets"]["verification_token"], "token-1")

    async def test_copy_secret_and_delete_channel(self):
        created = await self.repo.create_channel(
            {
                "name": "Feishu Alpha",
                "type": "feishu",
                "agent_id": "agent-alpha",
                "secrets": {"app_secret": "secret-alpha"},
            }
        )

        self.assertEqual(
            await self.repo.copy_secret(created["id"], "app_secret"),
            "secret-alpha",
        )
        with self.assertRaises(RecordNotFoundError):
            await self.repo.copy_secret(created["id"], "missing")

        await self.repo.delete_channel(created["id"])
        self.assertIsNone(await self.repo.get_channel(created["id"]))
