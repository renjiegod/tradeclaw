from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from doyoutrade.config import default_model_route_baseline
from doyoutrade.models.route_resolution import resolve_model_settings
from doyoutrade.models.route_settings_validate import validate_route_settings
from doyoutrade.persistence.db import Base, create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.errors import RecordNotFoundError
from doyoutrade.persistence.repositories import SqlAlchemyModelRouteRepository


class TestModelRouteSettingsValidation(unittest.TestCase):
    def test_route_settings_unknown_top_level_key_raises(self):
        with self.assertRaises(ValueError) as ctx:
            validate_route_settings({"foo": 1})
        self.assertIn("model_route.settings", str(ctx.exception))
        self.assertIn("foo", str(ctx.exception))

    def test_empty_and_none_normalize_to_empty_dict(self):
        self.assertEqual(validate_route_settings(None), {})
        self.assertEqual(validate_route_settings({}), {})

    def test_allows_prediction_config_extra(self):
        out = validate_route_settings(
            {"prediction_config_extra": {"promptTemplate": {"type": "jinja"}}}
        )
        self.assertEqual(out["prediction_config_extra"]["promptTemplate"]["type"], "jinja")

    def test_nested_thinking_object_passes(self):
        thinking = {"type": "enabled", "budget_tokens": 2048}
        out = validate_route_settings({"thinking": thinking, "temperature": 0.3})
        self.assertEqual(out["thinking"], thinking)
        self.assertEqual(out["temperature"], 0.3)

    def test_non_mapping_raises_with_path(self):
        with self.assertRaises(ValueError) as ctx:
            validate_route_settings([])
        self.assertIn("model_route.settings", str(ctx.exception))


class TestResolveModelSettings(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "route_resolution.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.route_repo = SqlAlchemyModelRouteRepository(self.session_factory)

    async def asyncTearDown(self):
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_route_temperature_and_model_override(self):
        baseline = default_model_route_baseline()
        await self.route_repo.create(
            route_name="resolve-test-route",
            provider_kind="anthropic",
            api_key="sk-test",
            target_model="route-chosen-model",
            settings={"temperature": 0.03125, "thinking": {"type": "disabled"}},
        )
        resolved = await resolve_model_settings(
            route_name="resolve-test-route",
            route_repository=self.route_repo,
        )
        self.assertEqual(resolved.model, "route-chosen-model")
        self.assertEqual(resolved.temperature, 0.03125)
        self.assertEqual(resolved.max_tokens, baseline.max_tokens)
        self.assertEqual(resolved.provider, "resolve-test-route")
        self.assertEqual(resolved.provider_kind, "anthropic")
        self.assertEqual(resolved.anthropic.api_key, "sk-test")

    async def test_openai_compatible_missing_db_base_url_raises(self):
        await self.route_repo.create(
            route_name="resolve-test-oai-route",
            provider_kind="openai_compatible",
            api_key="k",
            base_url=None,
            target_model="gpt-4o-mini",
            settings={"base_url": "https://example.invalid/v1"},
        )
        with self.assertRaises(ValueError) as ctx:
            await resolve_model_settings(
                route_name="resolve-test-oai-route",
                route_repository=self.route_repo,
            )
        self.assertIn("base_url", str(ctx.exception).lower())

    async def test_openai_compatible_uses_db_base_url_not_json_when_both_set(self):
        await self.route_repo.create(
            route_name="resolve-oai-db-url",
            provider_kind="openai_compatible",
            api_key="secret",
            base_url="https://db.example/v1",
            target_model="m1",
        )
        resolved = await resolve_model_settings(
            route_name="resolve-oai-db-url",
            route_repository=self.route_repo,
        )
        self.assertEqual(resolved.openai_compatible.base_url, "https://db.example/v1")

    async def test_lmstudio_resolves_without_db_base_url(self):
        await self.route_repo.create(
            route_name="resolve-lmstudio-no-db-url",
            provider_kind="lmstudio",
            api_key="lm-key",
            base_url=None,
            target_model="route-model",
        )
        resolved = await resolve_model_settings(
            route_name="resolve-lmstudio-no-db-url",
            route_repository=self.route_repo,
        )
        self.assertIsNone(resolved.lmstudio.base_url)
        self.assertEqual(resolved.provider_kind, "lmstudio")
        self.assertEqual(resolved.model, "route-model")
        self.assertEqual(resolved.lmstudio.api_key, "lm-key")

    async def test_lmstudio_prediction_config_extra_from_settings(self):
        await self.route_repo.create(
            route_name="resolve-lmstudio-pred-route",
            provider_kind="lmstudio",
            api_key="k",
            base_url=None,
            target_model="m0",
            settings={"prediction_config_extra": {"temperature": 0.55}},
        )
        resolved = await resolve_model_settings(
            route_name="resolve-lmstudio-pred-route",
            route_repository=self.route_repo,
        )
        self.assertEqual(resolved.lmstudio.prediction_config_extra, {"temperature": 0.55})

    async def test_empty_target_model_raises(self):
        await self.route_repo.create(
            route_name="resolve-no-model",
            provider_kind="anthropic",
            api_key="sk-test",
            target_model=None,
        )
        with self.assertRaises(ValueError) as ctx:
            await resolve_model_settings(
                route_name="resolve-no-model",
                route_repository=self.route_repo,
            )
        self.assertIn("model", str(ctx.exception).lower())

    async def test_missing_route_propagates_record_not_found(self):
        with self.assertRaises(RecordNotFoundError):
            await resolve_model_settings(
                route_name="no-such-route-ever",
                route_repository=self.route_repo,
            )


if __name__ == "__main__":
    unittest.main()
