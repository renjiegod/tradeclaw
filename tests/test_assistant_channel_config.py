import unittest
from doyoutrade.assistant.channels.config import (
    BaseChannelConfig,
    FeishuChannelConfig,
    HttpChannelConfig,
)


class TestChannelConfigs(unittest.TestCase):
    def test_base_config_defaults(self):
        cfg = BaseChannelConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.bot_prefix, "")
        self.assertEqual(cfg.dm_policy, "open")
        self.assertEqual(cfg.group_policy, "open")
        self.assertEqual(cfg.allow_from, [])
        self.assertEqual(cfg.deny_message, "")
        self.assertFalse(cfg.require_mention)

    def test_base_config_custom_values(self):
        cfg = BaseChannelConfig(
            enabled=True,
            bot_prefix="/",
            dm_policy="allowlist",
            allow_from=["user_a", "user_b"],
        )
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.bot_prefix, "/")
        self.assertEqual(cfg.dm_policy, "allowlist")
        self.assertEqual(cfg.allow_from, ["user_a", "user_b"])

    def test_base_config_invalid_dm_policy(self):
        """Pydantic should reject invalid dm_policy values."""
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            BaseChannelConfig(dm_policy="invalid")

    def test_feishu_config_fields(self):
        cfg = FeishuChannelConfig(
            enabled=True,
            app_id="cli_aaa",
            app_secret="secret_xyz",
            encrypt_key="key_abc",
            verification_token="tok_123",
            domain="lark",
        )
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.app_id, "cli_aaa")
        self.assertEqual(cfg.app_secret, "secret_xyz")
        self.assertEqual(cfg.encrypt_key, "key_abc")
        self.assertEqual(cfg.verification_token, "tok_123")
        self.assertEqual(cfg.domain, "lark")

    def test_feishu_config_inherits_base(self):
        cfg = FeishuChannelConfig(
            enabled=True,
            app_id="cli_aaa",
            app_secret="secret",
            dm_policy="allowlist",
            allow_from=["ou_1", "ou_2"],
        )
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.dm_policy, "allowlist")
        self.assertEqual(cfg.allow_from, ["ou_1", "ou_2"])

    def test_feishu_config_default_domain(self):
        cfg = FeishuChannelConfig(app_id="cli_aaa", app_secret="secret")
        self.assertEqual(cfg.domain, "feishu")

    def test_http_config_inherits_base(self):
        cfg = HttpChannelConfig(enabled=True)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.dm_policy, "open")

    def test_http_config_empty(self):
        cfg = HttpChannelConfig()
        self.assertFalse(cfg.enabled)

    def test_feishu_config_has_card_id_fields(self):
        from doyoutrade.assistant.channels.config import FeishuChannelConfig
        config = FeishuChannelConfig(
            app_id="app_123",
            app_secret="secret",
            thinking_card_id="oc_xxx",
            tool_call_card_id="oc_yyy",
            rich_text_card_id="oc_zzz",
        )
        assert config.thinking_card_id == "oc_xxx"
        assert config.tool_call_card_id == "oc_yyy"
        assert config.rich_text_card_id == "oc_zzz"

    def test_feishu_config_has_any_card_id(self):
        from doyoutrade.assistant.channels.config import FeishuChannelConfig
        config_no_card = FeishuChannelConfig(app_id="app", app_secret="sec")
        config_with_card = FeishuChannelConfig(app_id="app", app_secret="sec", thinking_card_id="oc_xxx")
        assert not config_no_card.has_any_card_id()
        assert config_with_card.has_any_card_id()


if __name__ == "__main__":
    unittest.main()
