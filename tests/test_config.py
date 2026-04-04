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

    def test_observability_defaults_available(self):
        cfg = load_config(resolve_config_path())
        self.assertEqual(cfg.observability.service_name, "tradeclaw")
        self.assertEqual(cfg.observability.log_level, "INFO")
        self.assertTrue(cfg.observability.console_enabled)
        self.assertTrue(cfg.observability.tracing_enabled)

    def test_database_defaults_available(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("")
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.database.url, "sqlite+aiosqlite:///./tradeclaw.db")
            self.assertFalse(cfg.database.echo)
            self.assertTrue(cfg.database.pool_pre_ping)
        finally:
            path.unlink(missing_ok=True)

    def test_database_override_is_loaded(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
database:
  url: postgresql+asyncpg://user:pass@localhost:5432/tradeclaw
  echo: true
  pool_pre_ping: false
""".strip()
            )
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(
                cfg.database.url,
                "postgresql+asyncpg://user:pass@localhost:5432/tradeclaw",
            )
            self.assertTrue(cfg.database.echo)
            self.assertFalse(cfg.database.pool_pre_ping)
        finally:
            path.unlink(missing_ok=True)

    def test_database_url_explicit_null_raises(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("database:\n  url: null\n")
            path = Path(handle.name)
        try:
            with self.assertRaises(ValueError):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_database_url_blank_raises(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write('database:\n  url: "   "\n')
            path = Path(handle.name)
        try:
            with self.assertRaises(ValueError):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_database_boolean_string_values_are_parsed(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
database:
  echo: "false"
  pool_pre_ping: "on"
""".strip()
            )
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertFalse(cfg.database.echo)
            self.assertTrue(cfg.database.pool_pre_ping)
        finally:
            path.unlink(missing_ok=True)

    def test_database_invalid_boolean_raises(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
database:
  echo: "not-a-bool"
""".strip()
            )
            path = Path(handle.name)
        try:
            with self.assertRaises(ValueError):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_database_null_raises_database_mapping_error(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("database: null\n")
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "database must be a mapping"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_database_list_raises_database_mapping_error(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("database: []\n")
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "database must be a mapping"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_database_url_non_string_values_raise(self):
        cases = (
            "database:\n  url: 123\n",
            "database:\n  url: true\n",
            "database:\n  url: {}\n",
        )
        for content in cases:
            with self.subTest(content=content):
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".yaml", delete=False, encoding="utf-8"
                ) as handle:
                    handle.write(content)
                    path = Path(handle.name)
                try:
                    with self.assertRaisesRegex(
                        ValueError, "database.url must be a non-empty string"
                    ):
                        load_config(path)
                finally:
                    path.unlink(missing_ok=True)
