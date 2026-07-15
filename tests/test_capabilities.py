import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from doyoutrade.capabilities import CapabilityRegistry, load_builtin_capabilities


class CapabilityRegistryTests(unittest.TestCase):
    def test_builtin_capabilities_include_runtime_visible_core_surfaces(self):
        registry = load_builtin_capabilities()

        self.assertIn("data.mock", registry.ids(kind="data_provider"))
        self.assertIn("data.qmt", registry.ids(kind="data_provider"))
        self.assertIn("model.anthropic", registry.ids(kind="model_provider"))
        self.assertIn("model.openai_compatible", registry.ids(kind="model_provider"))
        self.assertIn("channel.feishu", registry.ids(kind="channel"))

    def test_registry_rejects_duplicate_capability_ids(self):
        first = {
            "id": "data.mock",
            "kind": "data_provider",
            "label": "Mock",
            "description": "Mock provider",
            "config_schema": {"type": "object", "additionalProperties": False, "properties": {}},
            "runtime": {"factory": "doyoutrade.data.factory:_build_mock_stack"},
        }
        second = {**first, "label": "Duplicate"}

        with self.assertRaisesRegex(ValueError, "duplicate capability id"):
            CapabilityRegistry.from_dicts([first, second])

    def test_registry_returns_public_summaries_without_runtime_factory_details(self):
        registry = load_builtin_capabilities()
        summary = registry.summary(kind="model_provider")
        anthropic = next(item for item in summary if item["id"] == "model.anthropic")

        self.assertEqual(anthropic["kind"], "model_provider")
        self.assertEqual(anthropic["provider_kind"], "anthropic")
        self.assertIn("config_schema", anthropic)
        self.assertNotIn("runtime", anthropic)

    def test_builtin_loader_accepts_extra_manifest_directories(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "plugin_channel.json"
            path.write_text(
                """
{
  "id": "channel.plugin_test",
  "kind": "channel",
  "label": "Plugin Test",
  "description": "External plugin channel manifest.",
  "config_schema": {"type": "object", "additionalProperties": false, "properties": {}},
  "metadata": {"channel_type": "plugin_test"},
  "runtime": {"channel": "tests.fake:PluginChannel"}
}
""".strip(),
                encoding="utf-8",
            )

            registry = load_builtin_capabilities(extra_dirs=[Path(tmp)])

        self.assertIn("channel.plugin_test", registry.ids(kind="channel"))
        self.assertIn("plugin_test", registry.channel_types())


if __name__ == "__main__":
    unittest.main()
