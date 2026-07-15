import unittest
from unittest.mock import patch

from doyoutrade.config import (
    AnthropicModelSettings,
    LmStudioModelSettings,
    ModelSettings,
    OpenAICompatibleModelSettings,
    _inactive_lmstudio_settings,
)
from doyoutrade.models.factory import build_model_adapter


class ModelFactoryTests(unittest.TestCase):
    def _build_config(
        self,
        provider_kind: str,
        anthropic_key=None,
        openai_key=None,
        openai_base_url=None,
        *,
        profile: str = "test-profile",
        openai_tool_choice=None,
        lmstudio: LmStudioModelSettings | None = None,
    ):
        return ModelSettings(
            provider=profile,
            provider_kind=provider_kind,
            model="test-model",
            temperature=0.1,
            max_tokens=256,
            timeout_seconds=12.0,
            signal_strategy="agent",
            anthropic=AnthropicModelSettings(api_key=anthropic_key, base_url=None),
            openai_compatible=OpenAICompatibleModelSettings(
                api_key=openai_key,
                base_url=openai_base_url,
                tool_choice=openai_tool_choice,
            ),
            lmstudio=lmstudio if lmstudio is not None else _inactive_lmstudio_settings(),
        )

    def test_unknown_provider_raises(self):
        cfg = self._build_config(provider_kind="unknown")

        with self.assertRaises(ValueError):
            build_model_adapter(cfg)

    def test_anthropic_requires_api_key(self):
        cfg = self._build_config(provider_kind="anthropic", anthropic_key=None)

        with self.assertRaisesRegex(ValueError, "api_key"):
            build_model_adapter(cfg)

    def test_openai_compatible_requires_api_key_and_base_url(self):
        cfg_missing_key = self._build_config(
            provider_kind="openai_compatible",
            openai_key=None,
            openai_base_url="https://example.com/v1",
        )
        cfg_missing_url = self._build_config(
            provider_kind="openai_compatible",
            openai_key="key",
            openai_base_url=None,
        )

        with self.assertRaisesRegex(ValueError, "api_key"):
            build_model_adapter(cfg_missing_key)
        with self.assertRaisesRegex(ValueError, "base_url"):
            build_model_adapter(cfg_missing_url)

    @patch("doyoutrade.models.factory.AnthropicAdapter")
    def test_builds_anthropic_adapter(self, adapter_cls):
        cfg = self._build_config(provider_kind="anthropic", anthropic_key="ak")

        build_model_adapter(cfg)

        adapter_cls.assert_called_once()
        _, kwargs = adapter_cls.call_args
        self.assertIsNone(kwargs.get("thinking"))
        self.assertEqual(kwargs["max_tokens"], 256)

    @patch("doyoutrade.models.factory.AnthropicAdapter")
    def test_anthropic_uses_default_max_tokens_when_model_unset(self, adapter_cls):
        cfg = ModelSettings(
            provider="p",
            provider_kind="anthropic",
            model="m",
            temperature=0.0,
            max_tokens=None,
            timeout_seconds=30.0,
            signal_strategy="agent",
            anthropic=AnthropicModelSettings(api_key="ak", base_url=None),
            openai_compatible=OpenAICompatibleModelSettings(api_key=None, base_url=None),
            lmstudio=_inactive_lmstudio_settings(),
        )

        build_model_adapter(cfg)

        adapter_cls.assert_called_once()
        _, kwargs = adapter_cls.call_args
        self.assertEqual(kwargs["max_tokens"], 100000)

    @patch("doyoutrade.models.factory.AnthropicAdapter")
    def test_builds_anthropic_adapter_with_thinking(self, adapter_cls):
        thinking = {"type": "enabled", "budget_tokens": 2048}
        cfg = ModelSettings(
            provider="p",
            provider_kind="anthropic",
            model="test-model",
            temperature=0.1,
            max_tokens=256,
            timeout_seconds=12.0,
            signal_strategy="agent",
            anthropic=AnthropicModelSettings(api_key="ak", base_url=None, thinking=thinking),
            openai_compatible=OpenAICompatibleModelSettings(api_key=None, base_url=None),
            lmstudio=_inactive_lmstudio_settings(),
        )

        build_model_adapter(cfg)

        adapter_cls.assert_called_once()
        _, kwargs = adapter_cls.call_args
        self.assertEqual(kwargs["thinking"], thinking)

    @patch("doyoutrade.models.factory.OpenAICompatibleAdapter")
    def test_builds_openai_compatible_adapter(self, adapter_cls):
        cfg = self._build_config(
            provider_kind="openai_compatible",
            openai_key="ok",
            openai_base_url="https://example.com/v1",
        )

        build_model_adapter(cfg)

        adapter_cls.assert_called_once()

    @patch("doyoutrade.models.factory.OpenAICompatibleAdapter")
    def test_builds_openai_compatible_adapter_with_tool_choice(self, adapter_cls):
        cfg = self._build_config(
            provider_kind="openai_compatible",
            openai_key="ok",
            openai_base_url="https://example.com/v1",
            openai_tool_choice="required",
        )

        build_model_adapter(cfg)

        adapter_cls.assert_called_once()
        _, kwargs = adapter_cls.call_args
        self.assertEqual(kwargs["tool_choice"], "required")

    @patch("doyoutrade.models.factory.OpenAICompatibleAdapter")
    def test_openai_compatible_respects_provider_max_tokens_override(self, adapter_cls):
        cfg = ModelSettings(
            provider="p",
            provider_kind="openai_compatible",
            model="m",
            temperature=0.0,
            max_tokens=100_000,
            timeout_seconds=30.0,
            signal_strategy="agent",
            anthropic=AnthropicModelSettings(api_key=None, base_url=None),
            openai_compatible=OpenAICompatibleModelSettings(
                api_key="k",
                base_url="https://api.example.com/v1",
                max_tokens=8192,
            ),
            lmstudio=_inactive_lmstudio_settings(),
        )

        build_model_adapter(cfg)

        adapter_cls.assert_called_once()
        _, kwargs = adapter_cls.call_args
        self.assertEqual(kwargs["max_tokens"], 8192)

    @patch("doyoutrade.models.factory.OpenAICompatibleAdapter")
    def test_openai_compatible_omits_max_tokens_when_unset(self, adapter_cls):
        cfg = ModelSettings(
            provider="p",
            provider_kind="openai_compatible",
            model="m",
            temperature=0.0,
            max_tokens=None,
            timeout_seconds=30.0,
            signal_strategy="agent",
            anthropic=AnthropicModelSettings(api_key=None, base_url=None),
            openai_compatible=OpenAICompatibleModelSettings(
                api_key="k",
                base_url="https://api.example.com/v1",
                max_tokens=None,
            ),
            lmstudio=_inactive_lmstudio_settings(),
        )

        build_model_adapter(cfg)

        adapter_cls.assert_called_once()
        _, kwargs = adapter_cls.call_args
        self.assertIsNone(kwargs["max_tokens"])

    @patch("doyoutrade.models.factory.LmStudioAdapter")
    def test_builds_lmstudio_adapter(self, adapter_cls):
        cfg = self._build_config(
            provider_kind="lmstudio",
            lmstudio=LmStudioModelSettings(api_key=None, base_url=None),
        )

        build_model_adapter(cfg)

        adapter_cls.assert_called_once()
        _, kwargs = adapter_cls.call_args
        self.assertIsNone(kwargs["api_key"])
        self.assertIsNone(kwargs["base_url"])
        self.assertEqual(kwargs["temperature"], 0.1)
        self.assertEqual(kwargs["max_tokens"], 256)
        self.assertEqual(kwargs["timeout_seconds"], 12.0)
        self.assertIsNone(kwargs["tool_choice"])

    @patch("doyoutrade.models.factory.LmStudioAdapter")
    def test_builds_lmstudio_adapter_forwards_tool_choice(self, adapter_cls):
        cfg = self._build_config(
            provider_kind="lmstudio",
            lmstudio=LmStudioModelSettings(
                api_key=None, base_url=None, tool_choice="required"
            ),
        )

        build_model_adapter(cfg)

        adapter_cls.assert_called_once()
        _, kwargs = adapter_cls.call_args
        self.assertEqual(kwargs["tool_choice"], "required")

    @patch("doyoutrade.models.factory.LmStudioAdapter")
    def test_builds_lmstudio_adapter_forwards_prediction_config_extra(self, adapter_cls):
        cfg = self._build_config(
            provider_kind="lmstudio",
            lmstudio=LmStudioModelSettings(
                api_key=None,
                base_url=None,
                prediction_config_extra={"promptTemplate": {"type": "jinja"}},
            ),
        )
        build_model_adapter(cfg)
        adapter_cls.assert_called_once()
        _, kwargs = adapter_cls.call_args
        self.assertEqual(
            kwargs["prediction_config_extra"], {"promptTemplate": {"type": "jinja"}}
        )

    @patch("doyoutrade.models.factory.LmStudioAdapter")
    def test_lmstudio_respects_provider_max_tokens_override(self, adapter_cls):
        cfg = ModelSettings(
            provider="p",
            provider_kind="lmstudio",
            model="m",
            temperature=0.0,
            max_tokens=100_000,
            timeout_seconds=30.0,
            signal_strategy="agent",
            anthropic=AnthropicModelSettings(api_key=None, base_url=None),
            openai_compatible=OpenAICompatibleModelSettings(api_key=None, base_url=None),
            lmstudio=LmStudioModelSettings(
                api_key=None, base_url=None, max_tokens=4096
            ),
        )

        build_model_adapter(cfg)

        adapter_cls.assert_called_once()
        _, kwargs = adapter_cls.call_args
        self.assertEqual(kwargs["max_tokens"], 4096)


if __name__ == "__main__":
    unittest.main()
