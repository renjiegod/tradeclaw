import unittest
from unittest.mock import patch

from tradeclaw.config import AnthropicModelSettings, ModelSettings, OpenAICompatibleModelSettings
from tradeclaw.models.factory import build_model_adapter


class ModelFactoryTests(unittest.TestCase):
    def _build_config(self, provider: str, anthropic_key=None, openai_key=None, openai_base_url=None):
        return ModelSettings(
            provider=provider,
            model="test-model",
            temperature=0.1,
            max_tokens=256,
            timeout_seconds=12.0,
            anthropic=AnthropicModelSettings(api_key=anthropic_key, base_url=None),
            openai_compatible=OpenAICompatibleModelSettings(
                api_key=openai_key,
                base_url=openai_base_url,
            ),
        )

    def test_unknown_provider_raises(self):
        cfg = self._build_config(provider="unknown")

        with self.assertRaises(ValueError):
            build_model_adapter(cfg)

    def test_anthropic_requires_api_key(self):
        cfg = self._build_config(provider="anthropic", anthropic_key=None)

        with self.assertRaisesRegex(ValueError, "api_key"):
            build_model_adapter(cfg)

    def test_openai_compatible_requires_api_key_and_base_url(self):
        cfg_missing_key = self._build_config(
            provider="openai_compatible",
            openai_key=None,
            openai_base_url="https://example.com/v1",
        )
        cfg_missing_url = self._build_config(
            provider="openai_compatible",
            openai_key="key",
            openai_base_url=None,
        )

        with self.assertRaisesRegex(ValueError, "api_key"):
            build_model_adapter(cfg_missing_key)
        with self.assertRaisesRegex(ValueError, "base_url"):
            build_model_adapter(cfg_missing_url)

    @patch("tradeclaw.models.factory.AnthropicAdapter")
    def test_builds_anthropic_adapter(self, adapter_cls):
        cfg = self._build_config(provider="anthropic", anthropic_key="ak")

        build_model_adapter(cfg)

        adapter_cls.assert_called_once()

    @patch("tradeclaw.models.factory.OpenAICompatibleAdapter")
    def test_builds_openai_compatible_adapter(self, adapter_cls):
        cfg = self._build_config(
            provider="openai_compatible",
            openai_key="ok",
            openai_base_url="https://example.com/v1",
        )

        build_model_adapter(cfg)

        adapter_cls.assert_called_once()


if __name__ == "__main__":
    unittest.main()
