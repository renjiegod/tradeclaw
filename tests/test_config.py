import os
import tempfile
import unittest
from pathlib import Path

from doyoutrade.config import (
    default_model_route_baseline,
    get_config,
    load_config,
    reset_config,
    resolve_config_path,
)


VALID_MARKET_DATA_YAML = """
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market
""".strip()


class ConfigTests(unittest.TestCase):
    def tearDown(self):
        reset_config()

    def test_load_partial_yaml_merges_defaults(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(f"server:\n  port: 9001\n{VALID_MARKET_DATA_YAML}\n")
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.server.port, 9001)
            self.assertEqual(cfg.server.host, "0.0.0.0")
            self.assertEqual(cfg.server.tick_seconds, 5.0)
            self.assertEqual(cfg.data.default_provider, "auto")
            self.assertEqual(default_model_route_baseline().signal_strategy, "agent")
            from doyoutrade.config import _parse_signal_strategy

            self.assertEqual(_parse_signal_strategy("factor"), "factor")
            self.assertEqual(_parse_signal_strategy("Agent"), "agent")
            self.assertEqual(cfg.review.symbol_scope_mode, "default")
        finally:
            path.unlink(missing_ok=True)

    def test_review_strategy_agent_raises_migration_message(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("review:\n  strategy: agent\n")
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "review.strategy: agent is no longer supported"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_review_strategy_invalid_raises(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("review:\n  strategy: llm\n")
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "review.strategy .+ is not supported"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_review_strategy_deterministic_still_loads(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                "review:\n"
                "  strategy: deterministic\n"
                "  symbol_scope_mode: block_all\n"
                f"{VALID_MARKET_DATA_YAML}\n"
            )
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.review.symbol_scope_mode, "block_all")
        finally:
            path.unlink(missing_ok=True)

    def test_review_symbol_scope_mode_invalid_raises(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("review:\n  symbol_scope_mode: everything\n")
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "review.symbol_scope_mode"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_yaml_providers_rejected(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                "providers:\n"
                "  - name: x\n"
                "    provider_type: anthropic\n"
                "    api_key: k\n"
            )
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "providers"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_yaml_model_rejected(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("model:\n  provider: x\n")
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "model"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_resolve_config_finds_repo_or_package_default(self):
        path = resolve_config_path()
        self.assertTrue(path.is_file())
        self.assertEqual(path.suffix, ".yaml")

    def test_get_config_singleton(self):
        reset_config()
        old_config = os.environ.get("DOYOUTRADE_CONFIG")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(f"{VALID_MARKET_DATA_YAML}\n")
            path = Path(handle.name)
        try:
            os.environ["DOYOUTRADE_CONFIG"] = str(path)
            reset_config()
            a = get_config()
            b = get_config()
            self.assertIs(a, b)
        finally:
            if old_config is None:
                os.environ.pop("DOYOUTRADE_CONFIG", None)
            else:
                os.environ["DOYOUTRADE_CONFIG"] = old_config
            reset_config()
            path.unlink(missing_ok=True)

    def test_observability_defaults_available(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(f"{VALID_MARKET_DATA_YAML}\n")
            path = Path(handle.name)
        try:
            cfg = load_config(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(cfg.observability.service_name, "doyoutrade")
        self.assertEqual(cfg.observability.log_level, "INFO")
        self.assertTrue(cfg.observability.console_enabled)
        self.assertTrue(cfg.observability.tracing_enabled)

    def test_qmt_proxy_defaults_available(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(f"{VALID_MARKET_DATA_YAML}\n")
            path = Path(handle.name)
        try:
            cfg = load_config(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(cfg.qmt_proxy.host, "127.0.0.1")
        self.assertEqual(cfg.qmt_proxy.port, 8001)
        self.assertEqual(cfg.qmt_proxy.mode, "dev")
        self.assertFalse(cfg.qmt_proxy.grpc_enabled)
        self.assertEqual(cfg.qmt_proxy.local_token, "embedded-local")

    def test_qmt_proxy_override_is_loaded(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                "qmt_proxy:\n"
                "  port: 9099\n"
                "  mode: prod\n"
                "  grpc_enabled: true\n"
                f"{VALID_MARKET_DATA_YAML}\n"
            )
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.qmt_proxy.port, 9099)
            self.assertEqual(cfg.qmt_proxy.mode, "prod")
            self.assertTrue(cfg.qmt_proxy.grpc_enabled)
        finally:
            path.unlink(missing_ok=True)

    def test_qmt_proxy_invalid_mode_raises(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(f"qmt_proxy:\n  mode: bogus\n{VALID_MARKET_DATA_YAML}\n")
            path = Path(handle.name)
        try:
            with self.assertRaises(ValueError):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_database_defaults_available(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(f"{VALID_MARKET_DATA_YAML}\n")
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.database.url, "sqlite+aiosqlite:///./data/doyoutrade.db")
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
  url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade
  echo: true
  pool_pre_ping: false
""".strip()
                + f"\n{VALID_MARKET_DATA_YAML}\n"
            )
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(
                cfg.database.url,
                "postgresql+asyncpg://user:pass@localhost:5432/doyoutrade",
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
                + f"\n{VALID_MARKET_DATA_YAML}\n"
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

    def test_market_data_config_is_loaded(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market
  enabled_intervals: ["1d", "5m"]
  lookback_years: 10
  default_provider: auto
  sync_on_startup: true
  sync_concurrency: 3
  provider_rate_limit_per_second: 1.5
""".strip()
            )
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(
                cfg.market_data.database_url,
                "postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market",
            )
            self.assertEqual(cfg.market_data.enabled_intervals, ("1d", "5m"))
            self.assertEqual(cfg.market_data.lookback_years, 10)
            self.assertEqual(cfg.market_data.default_provider, "auto")
            self.assertTrue(cfg.market_data.sync_on_startup)
            self.assertEqual(cfg.market_data.sync_concurrency, 3)
            self.assertEqual(cfg.market_data.provider_rate_limit_per_second, 1.5)
            # Opt-in full-market sync defaults off (no change to existing deployments).
            self.assertFalse(cfg.market_data.sync_full_market)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_sync_full_market_opt_in(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                "market_data:\n"
                "  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market\n"
                "  sync_full_market: true\n"
            )
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertTrue(cfg.market_data.sync_full_market)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_sync_full_market_invalid_raises(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                "market_data:\n"
                "  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market\n"
                "  sync_full_market: maybe\n"
            )
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "market_data.sync_full_market"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_database_url_missing_raises(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("market_data:\n  database_url: ''\n")
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "market_data.database_url"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_database_url_defaults_to_local_sqlite(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("server:\n  port: 9001\n")
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(
                cfg.market_data.database_url,
                "sqlite+aiosqlite:///./data/market_bars.db",
            )
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_database_url_accepts_sqlite_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("market_data:\n  database_url: sqlite+aiosqlite:///market.db\n")
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(
                cfg.market_data.database_url, "sqlite+aiosqlite:///market.db"
            )
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_database_url_rejects_sqlite_memory(self):
        cases = [
            "market_data:\n  database_url: sqlite+aiosqlite://\n",
            "market_data:\n  database_url: 'sqlite+aiosqlite:///:memory:'\n",
        ]
        for content in cases:
            with self.subTest(content=content):
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".yaml", delete=False, encoding="utf-8"
                ) as handle:
                    handle.write(content)
                    path = Path(handle.name)
                try:
                    with self.assertRaisesRegex(
                        ValueError, "market_data.database_url with sqlite"
                    ):
                        load_config(path)
                finally:
                    path.unlink(missing_ok=True)

    def test_market_data_database_url_rejects_unsupported_driver(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                "market_data:\n  database_url: mysql+aiomysql://user:pass@localhost/market\n"
            )
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(
                ValueError, "market_data.database_url must use sqlite\\+aiosqlite"
            ):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_database_url_resolves_env_secret(self):
        old_value = os.environ.get("MARKET_DATA_DATABASE_URL")
        os.environ[
            "MARKET_DATA_DATABASE_URL"
        ] = "postgresql+asyncpg://env:secret@localhost:5432/doyoutrade_market"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("market_data:\n  database_url: ${MARKET_DATA_DATABASE_URL}\n")
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(
                cfg.market_data.database_url,
                "postgresql+asyncpg://env:secret@localhost:5432/doyoutrade_market",
            )
        finally:
            if old_value is None:
                os.environ.pop("MARKET_DATA_DATABASE_URL", None)
            else:
                os.environ["MARKET_DATA_DATABASE_URL"] = old_value
            path.unlink(missing_ok=True)

    def test_market_data_database_url_malformed_raises_field_error(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                "market_data:\n"
                "  database_url: postgresql+asyncpg://user:pass@localhost:bad/db\n"
            )
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "market_data.database_url"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_database_url_requires_database_name(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                "market_data:\n"
                "  database_url: postgresql+asyncpg://user:pass@localhost:5432\n"
            )
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "market_data.database_url"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_list_raises_mapping_error(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("market_data: []\n")
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "market_data must be a mapping"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_rejects_blank_interval(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market
  enabled_intervals: ["1d", ""]
""".strip()
            )
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "market_data.enabled_intervals"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_rejects_blank_default_provider(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market
  default_provider: ""
""".strip()
            )
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "market_data.default_provider"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_rejects_non_string_default_provider(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market
  default_provider: []
""".strip()
            )
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "market_data.default_provider"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_lowercases_default_provider(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market
  default_provider: CustomProvider
""".strip()
            )
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.market_data.default_provider, "customprovider")
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_rejects_non_integral_lookback_years(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market
  lookback_years: 1.9
""".strip()
            )
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "market_data.lookback_years"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_rejects_bool_sync_concurrency(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market
  sync_concurrency: true
""".strip()
            )
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "market_data.sync_concurrency"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_rejects_bool_rate_limit(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market
  provider_rate_limit_per_second: true
""".strip()
            )
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(
                ValueError, "market_data.provider_rate_limit_per_second"
            ):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_rejects_non_finite_rate_limit(self):
        cases = ('"nan"', '"inf"', ".nan", ".inf")
        for value in cases:
            with self.subTest(value=value):
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".yaml", delete=False, encoding="utf-8"
                ) as handle:
                    handle.write(
                        f"""
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market
  provider_rate_limit_per_second: {value}
""".strip()
                    )
                    path = Path(handle.name)
                try:
                    with self.assertRaisesRegex(
                        ValueError, "market_data.provider_rate_limit_per_second"
                    ):
                        load_config(path)
                finally:
                    path.unlink(missing_ok=True)

    def test_market_data_accepts_decimal_rate_limit_string(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market
  provider_rate_limit_per_second: "1.5"
""".strip()
            )
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.market_data.provider_rate_limit_per_second, 1.5)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_rejects_unsupported_interval(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market
  enabled_intervals: ["1d", "15m"]
""".strip()
            )
            path = Path(handle.name)
        try:
            with self.assertRaisesRegex(ValueError, "market_data.enabled_intervals"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_market_data_accepts_60m_interval(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(
                """
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_market
  enabled_intervals: ["1d", "60m"]
""".strip()
            )
            path = Path(handle.name)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.market_data.enabled_intervals, ("1d", "60m"))
        finally:
            path.unlink(missing_ok=True)

    def _write_cfg(self, body: str) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(f"{VALID_MARKET_DATA_YAML}\n{body}\n")
            return Path(handle.name)

    def test_retention_defaults(self):
        path = self._write_cfg("server:\n  port: 8000")
        try:
            cfg = load_config(path)
            self.assertTrue(cfg.retention.enabled)
            self.assertEqual(cfg.retention.observability_ttl_days, 7)
            self.assertEqual(cfg.retention.prune_interval_hours, 24)
            self.assertTrue(cfg.retention.prune_on_startup)
        finally:
            path.unlink(missing_ok=True)

    def test_retention_yaml_override(self):
        path = self._write_cfg(
            "retention:\n"
            "  enabled: false\n"
            "  observability_ttl_days: 30\n"
            "  prune_interval_hours: 6\n"
            "  prune_on_startup: false"
        )
        try:
            cfg = load_config(path)
            self.assertFalse(cfg.retention.enabled)
            self.assertEqual(cfg.retention.observability_ttl_days, 30)
            self.assertEqual(cfg.retention.prune_interval_hours, 6)
            self.assertFalse(cfg.retention.prune_on_startup)
        finally:
            path.unlink(missing_ok=True)

    def test_retention_env_override_wins_over_yaml(self):
        old_ttl = os.environ.get("DOYOUTRADE_OBSERVABILITY_TTL_DAYS")
        old_iv = os.environ.get("DOYOUTRADE_RETENTION_PRUNE_INTERVAL_HOURS")
        os.environ["DOYOUTRADE_OBSERVABILITY_TTL_DAYS"] = "14"
        os.environ["DOYOUTRADE_RETENTION_PRUNE_INTERVAL_HOURS"] = "12"
        path = self._write_cfg(
            "retention:\n  observability_ttl_days: 30\n  prune_interval_hours: 6"
        )
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.retention.observability_ttl_days, 14)
            self.assertEqual(cfg.retention.prune_interval_hours, 12)
        finally:
            for key, val in (
                ("DOYOUTRADE_OBSERVABILITY_TTL_DAYS", old_ttl),
                ("DOYOUTRADE_RETENTION_PRUNE_INTERVAL_HOURS", old_iv),
            ):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
            path.unlink(missing_ok=True)

    def test_retention_invalid_ttl_raises(self):
        path = self._write_cfg("retention:\n  observability_ttl_days: 0")
        try:
            with self.assertRaisesRegex(ValueError, "retention.observability_ttl_days"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_auto_update_defaults_enabled(self):
        path = self._write_cfg("server:\n  port: 8000")
        try:
            cfg = load_config(path)
            self.assertTrue(cfg.auto_update.enabled)
            self.assertEqual(cfg.auto_update.check_interval_hours, 6.0)
            self.assertEqual(cfg.auto_update.repo, "renjiegod/doyoutrade")
        finally:
            path.unlink(missing_ok=True)

    def test_auto_update_yaml_override(self):
        path = self._write_cfg(
            "auto_update:\n"
            "  enabled: false\n"
            "  check_interval_hours: 1.5\n"
            "  repo: someone/fork"
        )
        try:
            cfg = load_config(path)
            self.assertFalse(cfg.auto_update.enabled)
            self.assertEqual(cfg.auto_update.check_interval_hours, 1.5)
            self.assertEqual(cfg.auto_update.repo, "someone/fork")
        finally:
            path.unlink(missing_ok=True)

    def test_auto_update_invalid_repo_raises(self):
        for bad in ("no-slash", "a/b/c", "owner/ name"):
            path = self._write_cfg(f"auto_update:\n  repo: '{bad}'")
            try:
                with self.assertRaisesRegex(ValueError, "auto_update.repo"):
                    load_config(path)
            finally:
                path.unlink(missing_ok=True)

    def test_auto_update_invalid_interval_raises(self):
        path = self._write_cfg("auto_update:\n  check_interval_hours: 0")
        try:
            with self.assertRaisesRegex(ValueError, "auto_update.check_interval_hours"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def test_auto_update_invalid_enabled_raises(self):
        path = self._write_cfg("auto_update:\n  enabled: sometimes")
        try:
            with self.assertRaisesRegex(ValueError, "auto_update.enabled"):
                load_config(path)
        finally:
            path.unlink(missing_ok=True)


class _IsolatedHomeMixin:
    """Point DOYOUTRADE_HOME at a temp dir and clear DOYOUTRADE_CONFIG per test."""

    def setUp(self):
        super().setUp()
        from doyoutrade import config as config_mod

        self._home = tempfile.mkdtemp()
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("DOYOUTRADE_HOME", "DOYOUTRADE_CONFIG")
        }
        os.environ["DOYOUTRADE_HOME"] = self._home
        os.environ.pop("DOYOUTRADE_CONFIG", None)
        config_mod.reset_config()

    def tearDown(self):
        from doyoutrade import config as config_mod

        for key, val in self._saved_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        config_mod.reset_config()
        import shutil

        shutil.rmtree(self._home, ignore_errors=True)
        super().tearDown()


class WritableConfigPathTests(_IsolatedHomeMixin, unittest.TestCase):
    def test_writable_path_honours_doyoutrade_home(self):
        from doyoutrade.config import default_base_dir, resolve_writable_config_path

        self.assertEqual(default_base_dir(), Path(self._home))
        self.assertEqual(
            resolve_writable_config_path(), Path(self._home) / "config.yaml"
        )

    def test_seed_copies_when_missing_and_is_idempotent(self):
        from doyoutrade.config import (
            resolve_writable_config_path,
            seed_writable_config_if_missing,
        )

        target = resolve_writable_config_path()
        self.assertFalse(target.exists())
        seeded = seed_writable_config_if_missing()
        self.assertEqual(seeded, target)
        self.assertTrue(target.is_file())
        first = target.read_text(encoding="utf-8")
        # Idempotent: a second call must not overwrite an edited file.
        target.write_text(first + "\n# user edit\n", encoding="utf-8")
        seed_writable_config_if_missing()
        self.assertIn("# user edit", target.read_text(encoding="utf-8"))

    def test_candidate_paths_prioritises_writable_over_repo(self):
        from doyoutrade.config import (
            _candidate_paths,
            resolve_writable_config_path,
        )

        writable = resolve_writable_config_path()
        candidates = _candidate_paths()
        # writable must appear, and must come before cwd/repo config.yaml.
        self.assertIn(writable, candidates)
        writable_idx = candidates.index(writable)
        cwd_cfg = Path.cwd() / "config.yaml"
        if cwd_cfg in candidates:
            self.assertLess(writable_idx, candidates.index(cwd_cfg))


class ConfigStoreTests(_IsolatedHomeMixin, unittest.TestCase):
    def _store(self):
        from doyoutrade import config_store

        return config_store

    def test_read_masked_seeds_and_masks_secrets(self):
        store = self._store()
        result = store.read_config_masked()
        self.assertEqual(
            result["path"],
            str(Path(self._home) / "config.yaml"),
        )
        # seeding happened
        self.assertTrue((Path(self._home) / "config.yaml").is_file())
        values = result["values"]
        # secret fields are always masked
        self.assertEqual(values["data"]["tushare"]["token"], store.MASK)
        self.assertEqual(values["feishu"]["app_secret"], store.MASK)
        self.assertEqual(values["qmt_proxy"]["local_token"], store.MASK)
        # _set booleans present
        self.assertIn("token_set", values["data"]["tushare"])
        self.assertIn("app_secret_set", values["feishu"])
        # restart list contains base + downgraded fields
        rr = result["restart_required_fields"]
        self.assertIn("server.port", rr)
        self.assertIn("retention.enabled", rr)
        self.assertIn("data.default_provider", rr)
        self.assertIn("assistant.tool_result_max_chars", rr)
        # hot field is NOT in the restart list
        self.assertNotIn("review.symbol_scope_mode", rr)

    def test_write_hot_field_no_restart(self):
        store = self._store()
        result = store.write_config({"review": {"symbol_scope_mode": "block_all"}})
        self.assertEqual(result["status"], "updated")
        self.assertFalse(result["restart_required"])
        self.assertEqual(result["restart_fields"], [])
        # effective config picks it up after reset_config()
        from doyoutrade.config import get_config

        self.assertEqual(get_config().review.symbol_scope_mode, "block_all")

    def test_write_restart_field_flags_restart(self):
        store = self._store()
        result = store.write_config({"server": {"port": 8123}})
        self.assertTrue(result["restart_required"])
        self.assertEqual(result["restart_fields"], ["server.port"])
        from doyoutrade.config import get_config

        self.assertEqual(get_config().server.port, 8123)

    def test_write_downgraded_field_flags_restart(self):
        store = self._store()
        result = store.write_config({"retention": {"observability_ttl_days": 30}})
        self.assertTrue(result["restart_required"])
        self.assertEqual(result["restart_fields"], ["retention.observability_ttl_days"])

    def test_write_rejects_bad_value_with_field(self):
        store = self._store()
        with self.assertRaises(store.ConfigValidationError) as ctx:
            store.write_config({"qmt_proxy": {"mode": "bogus"}})
        self.assertEqual(ctx.exception.field, "qmt_proxy.mode")
        # nothing persisted / cache untouched
        from doyoutrade.config import get_config

        self.assertEqual(get_config().qmt_proxy.mode, "dev")

    def test_write_rejects_non_dict_patch(self):
        store = self._store()
        with self.assertRaises(store.ConfigValidationError):
            store.write_config(["not", "a", "dict"])  # type: ignore[arg-type]

    def test_secret_preserved_when_mask_submitted(self):
        store = self._store()
        # write a real token
        store.write_config({"data": {"tushare": {"token": "REALTOKEN123"}}})
        target = Path(self._home) / "config.yaml"
        self.assertIn("REALTOKEN123", target.read_text(encoding="utf-8"))
        # submit the mask -> unchanged, no restart flagged
        result = store.write_config({"data": {"tushare": {"token": store.MASK}}})
        self.assertEqual(result["restart_fields"], [])
        self.assertIn("REALTOKEN123", target.read_text(encoding="utf-8"))
        # a real new value IS written and flags restart
        result2 = store.write_config({"data": {"tushare": {"token": "NEWTOKEN456"}}})
        self.assertEqual(result2["restart_fields"], ["data.tushare.token"])
        self.assertIn("NEWTOKEN456", target.read_text(encoding="utf-8"))

    def test_write_preserves_comments(self):
        import shutil as _shutil

        from doyoutrade.config import (
            bundled_default_config_path,
            resolve_writable_config_path,
        )

        store = self._store()
        target = resolve_writable_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copyfile(bundled_default_config_path(), target)
        before = target.read_text(encoding="utf-8")
        comment_lines = sum(1 for ln in before.splitlines() if ln.strip().startswith("#"))
        self.assertGreater(comment_lines, 0)
        store.write_config({"server": {"port": 8222}})
        after = target.read_text(encoding="utf-8")
        self.assertEqual(
            comment_lines,
            sum(1 for ln in after.splitlines() if ln.strip().startswith("#")),
        )
        self.assertIn("8222", after)
