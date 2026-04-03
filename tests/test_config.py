import tempfile
import unittest
from pathlib import Path

from tradeclaw.config import get_config, load_config, reset_config, resolve_config_path


class ConfigTests(unittest.TestCase):
    def tearDown(self):
        reset_config()

    def test_load_partial_yaml_merges_defaults(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("server:\n  port: 9001\n")
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.server.port, 9001)
            self.assertEqual(cfg.server.host, "0.0.0.0")
            self.assertEqual(cfg.server.tick_seconds, 5.0)
            self.assertEqual(cfg.risk.max_single_order_amount, 20000.0)
            self.assertIsNone(cfg.data.qmt.session_id)
            self.assertEqual(cfg.data.default_provider, "auto")
        finally:
            path.unlink(missing_ok=True)

    def test_resolve_config_finds_repo_or_package_default(self):
        path = resolve_config_path()
        self.assertTrue(path.is_file())
        self.assertEqual(path.suffix, ".yaml")

    def test_get_config_singleton(self):
        reset_config()
        a = get_config()
        b = get_config()
        self.assertIs(a, b)

    def test_model_defaults_available(self):
        cfg = load_config(resolve_config_path())
        self.assertIn(cfg.model.provider, {"demo", "anthropic", "openai_compatible"})
