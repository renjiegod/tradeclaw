import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from doyoutrade import onboarding
from doyoutrade.onboarding import (
    _Answers,
    _agent_route_usable,
    _apply,
    _first_usable_route,
    _prompt,
    _unique_route_name,
)
from doyoutrade.persistence.repositories import ModelRouteRecord

_DT = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _FakeRouteRepo:
    def __init__(self):
        self._by_name: dict[str, ModelRouteRecord] = {}

    async def create(self, *, route_name, provider_kind, api_key, base_url=None,
                     target_model=None, settings=None, id=None):
        rid = id or f"route-{len(self._by_name) + 1}"
        rec = ModelRouteRecord(
            id=rid, route_name=route_name, provider_kind=provider_kind,
            base_url=base_url, api_key=api_key, target_model=target_model,
            settings=settings, created_at=_DT, updated_at=_DT,
        )
        self._by_name[route_name] = rec
        return rec

    async def get_by_route_name(self, route_name):
        return self._by_name[route_name]

    async def list_routes(self):
        return list(self._by_name.values())


class _FakeAgentRepo:
    def __init__(self, model_route_name=""):
        self.agent = {"id": "agent_default", "model_route_name": model_route_name}
        self.updates: list[dict] = []

    async def get_agent(self, agent_id):
        return dict(self.agent) if agent_id == "agent_default" else None

    async def update_agent(self, agent_id, updates):
        self.updates.append(updates)
        self.agent.update(updates)
        return dict(self.agent)


def _runtime(route_repo):
    return {
        "model_route_repository": route_repo,
    }


class OnboardingWizardTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_creates_route_and_binds_default_agent(self):
        route_repo = _FakeRouteRepo()
        agent_repo = _FakeAgentRepo()
        answers = _Answers(
            provider_kind="openai_compatible", slug="deepseek",
            api_key="sk-test", base_url="https://api.deepseek.com",
            model="deepseek-chat", route_name="default",
        )

        await _apply(answers, route_repo, agent_repo)

        routes = await route_repo.list_routes()
        self.assertEqual([r.route_name for r in routes], ["default"])
        self.assertEqual(routes[0].provider_kind, "openai_compatible")
        self.assertEqual(routes[0].api_key, "sk-test")
        self.assertEqual(routes[0].target_model, "deepseek-chat")
        self.assertEqual(agent_repo.agent["model_route_name"], "default")

    async def test_route_usable_true_after_apply(self):
        route_repo = _FakeRouteRepo()
        agent_repo = _FakeAgentRepo()
        await _apply(
            _Answers("openai_compatible", "deepseek", "sk-test",
                     "https://api.deepseek.com", "deepseek-chat", "default"),
            route_repo, agent_repo,
        )
        runtime = _runtime(route_repo)

        self.assertTrue(await _agent_route_usable(runtime, agent_repo))

    async def test_route_not_usable_when_agent_unbound(self):
        route_repo = _FakeRouteRepo()
        agent_repo = _FakeAgentRepo(model_route_name="")
        runtime = _runtime(route_repo)

        self.assertFalse(await _agent_route_usable(runtime, agent_repo))

    async def test_route_not_usable_when_route_missing_api_key(self):
        route_repo = _FakeRouteRepo()
        await route_repo.create(
            route_name="default", provider_kind="openai_compatible",
            api_key="", base_url="https://api.deepseek.com", target_model="deepseek-chat",
        )
        agent_repo = _FakeAgentRepo(model_route_name="default")
        runtime = _runtime(route_repo)

        self.assertFalse(await _agent_route_usable(runtime, agent_repo))

    async def test_first_usable_route_binds_existing_when_agent_unbound(self):
        route_repo = _FakeRouteRepo()
        await route_repo.create(
            route_name="default", provider_kind="openai_compatible",
            api_key="sk-test", base_url="https://api.deepseek.com", target_model="deepseek-chat",
        )
        runtime = _runtime(route_repo)

        found = await _first_usable_route(runtime, route_repo)
        self.assertEqual(found, "default")

    async def test_first_usable_route_none_when_no_route_builds(self):
        route_repo = _FakeRouteRepo()
        await route_repo.create(
            route_name="default", provider_kind="openai_compatible",
            api_key="", base_url=None, target_model="",
        )
        runtime = _runtime(route_repo)

        self.assertIsNone(await _first_usable_route(runtime, route_repo))

    async def test_unique_route_name_suffixes_on_conflict(self):
        route_repo = _FakeRouteRepo()
        await route_repo.create(
            route_name="default", provider_kind="openai_compatible",
            api_key="sk", base_url="https://api.deepseek.com", target_model="deepseek-chat",
        )
        name = await _unique_route_name(route_repo, "default")
        self.assertNotEqual(name, "default")
        self.assertTrue(name.startswith("default-"))

    async def test_unique_route_name_free_when_no_conflict(self):
        route_repo = _FakeRouteRepo()
        name = await _unique_route_name(route_repo, "default")
        self.assertEqual(name, "default")


class OnboardingPromptTests(unittest.TestCase):
    """The interactive prompt parses answers; it never touches the DB."""

    def test_prompt_deepseek_flow_accepts_defaults(self):
        # DeepSeek is preset #1; empty inputs accept the suggested base_url/model/route.
        inputs = iter(["1", "", "", "default"])  # choice, base_url, model, route_name
        with patch("builtins.input", lambda *_: next(inputs)), \
             patch.object(onboarding.getpass, "getpass", return_value="sk-live"):
            answers = _prompt()
        self.assertIsNotNone(answers)
        self.assertEqual(answers.provider_kind, "openai_compatible")
        self.assertEqual(answers.slug, "deepseek")
        self.assertEqual(answers.api_key, "sk-live")
        self.assertEqual(answers.base_url, "https://api.deepseek.com")
        self.assertEqual(answers.model, "deepseek-chat")
        self.assertEqual(answers.route_name, "default")

    def test_prompt_choice_zero_skips(self):
        with patch("builtins.input", lambda *_: "0"):
            self.assertIsNone(_prompt())

    def test_prompt_missing_key_aborts(self):
        inputs = iter(["1", "", "", "default"])
        with patch("builtins.input", lambda *_: next(inputs)), \
             patch.object(onboarding.getpass, "getpass", return_value="  "):
            self.assertIsNone(_prompt())

    def test_prompt_custom_openai_requires_base_url(self):
        # Preset #6 (自定义 OpenAI 兼容) with an empty base_url must abort.
        inputs = iter(["6", ""])  # choice, base_url (blank)
        with patch("builtins.input", lambda *_: next(inputs)):
            self.assertIsNone(_prompt())


if __name__ == "__main__":
    unittest.main()
