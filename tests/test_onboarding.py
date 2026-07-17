import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from doyoutrade import onboarding
from doyoutrade.onboarding import (
    PRESETS,
    _Answers,
    _apply,
    _maybe_prompt_qmt_proxy,
    _prompt,
    _unique_route_name,
    agent_route_usable,
    create_route_and_bind_agent,
    first_usable_route,
    serialize_presets,
)
from doyoutrade.persistence.repositories import ModelRouteRecord


def _preset_index(slug: str) -> int:
    for i, preset in enumerate(PRESETS):
        if preset.slug == slug:
            return i
    raise AssertionError(f"preset slug not found: {slug}")

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


class _FakeAccountRepo:
    def __init__(self, accounts=None):
        self.accounts = list(accounts or [])
        self.upserts: list[dict] = []

    async def list_accounts(self):
        return list(self.accounts)

    async def upsert_account(self, payload):
        self.upserts.append(dict(payload))
        return dict(payload)


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

    async def test_create_route_and_bind_agent_shared_by_terminal_and_web(self):
        # The same helper _apply delegates to is what POST /setup/complete
        # (doyoutrade/api/app.py) calls directly — exercise it standalone so a
        # regression here is caught without going through the terminal prompt.
        route_repo = _FakeRouteRepo()
        agent_repo = _FakeAgentRepo()

        resolved = await create_route_and_bind_agent(
            route_repo, agent_repo,
            route_name="default", provider_kind="openai_compatible",
            api_key="sk-web", base_url="https://api.deepseek.com",
            target_model="deepseek-chat",
        )

        self.assertEqual(resolved, "default")
        routes = await route_repo.list_routes()
        self.assertEqual(routes[0].api_key, "sk-web")
        self.assertEqual(agent_repo.agent["model_route_name"], "default")

    async def test_route_usable_true_after_apply(self):
        route_repo = _FakeRouteRepo()
        agent_repo = _FakeAgentRepo()
        await _apply(
            _Answers("openai_compatible", "deepseek", "sk-test",
                     "https://api.deepseek.com", "deepseek-chat", "default"),
            route_repo, agent_repo,
        )

        self.assertTrue(await agent_route_usable(route_repo, agent_repo))

    async def test_route_not_usable_when_agent_unbound(self):
        route_repo = _FakeRouteRepo()
        agent_repo = _FakeAgentRepo(model_route_name="")

        self.assertFalse(await agent_route_usable(route_repo, agent_repo))

    async def test_route_not_usable_when_route_missing_api_key(self):
        route_repo = _FakeRouteRepo()
        await route_repo.create(
            route_name="default", provider_kind="openai_compatible",
            api_key="", base_url="https://api.deepseek.com", target_model="deepseek-chat",
        )
        agent_repo = _FakeAgentRepo(model_route_name="default")

        self.assertFalse(await agent_route_usable(route_repo, agent_repo))

    async def test_first_usable_route_binds_existing_when_agent_unbound(self):
        route_repo = _FakeRouteRepo()
        await route_repo.create(
            route_name="default", provider_kind="openai_compatible",
            api_key="sk-test", base_url="https://api.deepseek.com", target_model="deepseek-chat",
        )

        found = await first_usable_route(route_repo)
        self.assertEqual(found, "default")

    async def test_first_usable_route_none_when_no_route_builds(self):
        route_repo = _FakeRouteRepo()
        await route_repo.create(
            route_name="default", provider_kind="openai_compatible",
            api_key="", base_url=None, target_model="",
        )

        self.assertIsNone(await first_usable_route(route_repo))

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


class OnboardingPresetCatalogTests(unittest.TestCase):
    def test_presets_cover_opencode_style_openai_compat_vendors(self):
        slugs = {p.slug for p in PRESETS}
        for expected in (
            "deepseek",
            "kimi",
            "qwen",
            "zhipu",
            "siliconflow",
            "volcengine",
            "minimax",
            "openai",
            "anthropic",
            "openrouter",
            "groq",
            "xai",
            "mistral",
            "together",
            "fireworks",
            "cerebras",
            "deepinfra",
            "ollama",
            "lmstudio",
            "custom",
        ):
            self.assertIn(expected, slugs)

    def test_presets_only_use_supported_provider_kinds(self):
        allowed = {"anthropic", "openai_compatible", "lmstudio"}
        for preset in PRESETS:
            self.assertIn(preset.provider_kind, allowed, preset.slug)

    def test_local_presets_do_not_require_api_key(self):
        for slug in ("ollama", "lmstudio"):
            self.assertFalse(PRESETS[_preset_index(slug)].needs_key)


class OnboardingPromptTests(unittest.TestCase):
    """The interactive prompt parses answers; it never touches the DB."""

    def test_prompt_deepseek_flow_accepts_defaults(self):
        # Menu returns DeepSeek; empty inputs accept suggested base_url/model/name.
        inputs = iter(["", "", "default"])  # base_url, model, route_name
        with patch.object(onboarding, "select_index", return_value=_preset_index("deepseek")), \
             patch("builtins.input", lambda *_: next(inputs)), \
             patch.object(onboarding.getpass, "getpass", return_value="sk-live"):
            answers = _prompt()
        self.assertIsNotNone(answers)
        self.assertEqual(answers.provider_kind, "openai_compatible")
        self.assertEqual(answers.slug, "deepseek")
        self.assertEqual(answers.api_key, "sk-live")
        self.assertEqual(answers.base_url, "https://api.deepseek.com")
        self.assertEqual(answers.model, "deepseek-chat")
        self.assertEqual(answers.route_name, "default")

    def test_prompt_skip_returns_none(self):
        with patch.object(onboarding, "select_index", return_value=None):
            self.assertIsNone(_prompt())

    def test_prompt_missing_key_aborts(self):
        inputs = iter(["", "", "default"])
        with patch.object(onboarding, "select_index", return_value=_preset_index("deepseek")), \
             patch("builtins.input", lambda *_: next(inputs)), \
             patch.object(onboarding.getpass, "getpass", return_value="  "):
            self.assertIsNone(_prompt())

    def test_prompt_custom_openai_requires_base_url(self):
        inputs = iter([""])  # blank base_url
        with patch.object(onboarding, "select_index", return_value=_preset_index("custom")), \
             patch("builtins.input", lambda *_: next(inputs)):
            self.assertIsNone(_prompt())

    def test_prompt_openai_preset_uses_official_base_url(self):
        inputs = iter(["", "gpt-4.1", "default"])
        with patch.object(onboarding, "select_index", return_value=_preset_index("openai")), \
             patch("builtins.input", lambda *_: next(inputs)), \
             patch.object(onboarding.getpass, "getpass", return_value="sk-openai"):
            answers = _prompt()
        self.assertIsNotNone(answers)
        self.assertEqual(answers.slug, "openai")
        self.assertEqual(answers.base_url, "https://api.openai.com/v1")
        self.assertEqual(answers.model, "gpt-4.1")


class DataSourcePromptTests(unittest.IsolatedAsyncioTestCase):
    """数据源向导分支：DoYouTrade Cloud / 自建 qmt-proxy / 跳过。"""

    async def test_cloud_branch_registers_mock_default_account_with_defaults(self):
        repo = _FakeAccountRepo()
        inputs = iter(["", "dytc_abc123"])  # 云网关地址（回车用默认）、API key
        with patch.object(onboarding, "select_index", return_value=0), \
             patch("builtins.input", lambda *_: next(inputs)), \
             redirect_stdout(io.StringIO()) as out:
            await _maybe_prompt_qmt_proxy({"account_repository": repo})
        self.assertEqual(len(repo.upserts), 1)
        account = repo.upserts[0]
        self.assertEqual(account["name"], "DoYouTrade Cloud")
        self.assertEqual(account["mode"], "mock")
        self.assertEqual(account["base_url"], onboarding.CLOUD_GATEWAY_DEFAULT_URL)
        self.assertEqual(account["token"], "dytc_abc123")
        self.assertTrue(account["is_default"])
        self.assertTrue(account["enabled"])
        self.assertIn("云端行情 + 本地模拟交易", out.getvalue())
        self.assertIn("不可用于实盘交易", out.getvalue())

    async def test_cloud_branch_warns_on_wrong_prefix_but_continues(self):
        repo = _FakeAccountRepo()
        inputs = iter(["", "sk-not-a-cloud-key"])
        with patch.object(onboarding, "select_index", return_value=0), \
             patch("builtins.input", lambda *_: next(inputs)), \
             redirect_stdout(io.StringIO()) as out:
            await _maybe_prompt_qmt_proxy({"account_repository": repo})
        self.assertIn("不是 dytc_ 前缀", out.getvalue())
        self.assertEqual(len(repo.upserts), 1)
        self.assertEqual(repo.upserts[0]["token"], "sk-not-a-cloud-key")
        self.assertEqual(repo.upserts[0]["mode"], "mock")

    async def test_cloud_branch_custom_gateway_gets_https_scheme(self):
        repo = _FakeAccountRepo()
        inputs = iter(["cloud.example.com", "dytc_xyz"])
        with patch.object(onboarding, "select_index", return_value=0), \
             patch("builtins.input", lambda *_: next(inputs)), \
             redirect_stdout(io.StringIO()):
            await _maybe_prompt_qmt_proxy({"account_repository": repo})
        self.assertEqual(repo.upserts[0]["base_url"], "https://cloud.example.com")

    async def test_remote_proxy_branch_keeps_existing_flow(self):
        repo = _FakeAccountRepo()
        inputs = iter(["192.168.1.10:8001", "proxy-token"])  # 地址、token
        with patch.object(onboarding, "select_index", return_value=1), \
             patch("builtins.input", lambda *_: next(inputs)), \
             redirect_stdout(io.StringIO()):
            await _maybe_prompt_qmt_proxy({"account_repository": repo})
        self.assertEqual(len(repo.upserts), 1)
        account = repo.upserts[0]
        self.assertEqual(account["name"], "QMT 远程")
        self.assertEqual(account["mode"], "mock")
        self.assertEqual(account["base_url"], "http://192.168.1.10:8001")
        self.assertEqual(account["token"], "proxy-token")

    async def test_skip_choice_registers_nothing(self):
        repo = _FakeAccountRepo()
        with patch.object(onboarding, "select_index", return_value=None), \
             redirect_stdout(io.StringIO()) as out:
            await _maybe_prompt_qmt_proxy({"account_repository": repo})
        self.assertEqual(repo.upserts, [])
        self.assertIn("已跳过数据源配置", out.getvalue())

    async def test_existing_base_url_account_skips_prompt_entirely(self):
        repo = _FakeAccountRepo(accounts=[{"base_url": "http://10.0.0.2:8001"}])
        select = MagicMock()
        with patch.object(onboarding, "select_index", select):
            await _maybe_prompt_qmt_proxy({"account_repository": repo})
        select.assert_not_called()
        self.assertEqual(repo.upserts, [])

    async def test_missing_account_repository_is_noop(self):
        # runtime 没接 account_repository 时不得抛错，也不打印任何提示。
        with redirect_stdout(io.StringIO()) as out:
            await _maybe_prompt_qmt_proxy({})
        self.assertEqual(out.getvalue(), "")


class SetupProvidersSerializationTests(unittest.TestCase):
    """GET /setup/providers (doyoutrade/api/app.py) serializes exactly this."""

    def test_serialize_presets_matches_preset_tuple(self):
        items = serialize_presets()
        self.assertEqual(len(items), len(PRESETS))
        for item, preset in zip(items, PRESETS):
            self.assertEqual(item["label"], preset.label)
            self.assertEqual(item["provider_kind"], preset.provider_kind)
            self.assertEqual(item["base_url"], preset.base_url)
            self.assertEqual(item["model_hint"], preset.model_hint)
            self.assertEqual(item["needs_key"], preset.needs_key)

    def test_serialize_presets_is_json_serializable(self):
        import json

        json.dumps(serialize_presets())


class _FakeTTY:
    """Stand-in for ``sys.stdin`` / ``sys.stdout`` that always claims to be a TTY."""

    def isatty(self) -> bool:
        return True


class WebSetupShortCircuitTests(unittest.IsolatedAsyncioTestCase):
    """``DOYOUTRADE_WEB_SETUP=1`` (double-click launch): the terminal wizard
    must defer to the web console's SetupWizard *before* the TTY check —
    including when a real TTY happens to be attached (e.g. a launcher script
    that opens a visible console window)."""

    async def test_web_setup_true_skips_prompt_even_with_a_real_tty(self):
        route_repo = _FakeRouteRepo()
        agent_repo = _FakeAgentRepo(model_route_name="")
        runtime = {"session_factory": object(), "model_route_repository": route_repo}
        prompt_mock = MagicMock()

        with patch(
            "doyoutrade.assistant.repository.SqlAlchemyAgentRepository",
            return_value=agent_repo,
        ), patch.object(onboarding, "_prompt", prompt_mock), patch(
            "sys.stdin", new=_FakeTTY()
        ), patch("sys.stdout", new=_FakeTTY()), redirect_stdout(
            io.StringIO()
        ) as out:
            await onboarding._run(runtime, web_setup=True)

        prompt_mock.assert_not_called()
        self.assertEqual(agent_repo.updates, [])
        self.assertIn("请在浏览器里完成设置", out.getvalue())

    async def test_web_setup_false_falls_back_to_headless_guidance_without_tty(self):
        # Baseline: web_setup=False (the non-web / historical path) still
        # behaves exactly as before — non-interactive startup never prompts.
        route_repo = _FakeRouteRepo()
        agent_repo = _FakeAgentRepo(model_route_name="")
        runtime = {"session_factory": object(), "model_route_repository": route_repo}
        prompt_mock = MagicMock()

        with patch(
            "doyoutrade.assistant.repository.SqlAlchemyAgentRepository",
            return_value=agent_repo,
        ), patch.object(onboarding, "_prompt", prompt_mock), patch(
            "sys.stdin.isatty", return_value=False
        ):
            await onboarding._run(runtime, web_setup=False)

        prompt_mock.assert_not_called()
        self.assertEqual(agent_repo.updates, [])

    async def test_already_configured_skips_wizard_regardless_of_web_setup(self):
        # An already-usable route must short-circuit before web_setup is even
        # consulted — configured-once machines are never re-prompted by
        # either surface (terminal or web).
        route_repo = _FakeRouteRepo()
        agent_repo = _FakeAgentRepo()
        await create_route_and_bind_agent(
            route_repo, agent_repo,
            route_name="default", provider_kind="openai_compatible",
            api_key="sk-test", base_url="https://api.deepseek.com",
            target_model="deepseek-chat",
        )
        runtime = {"session_factory": object(), "model_route_repository": route_repo}
        prompt_mock = MagicMock()

        with patch(
            "doyoutrade.assistant.repository.SqlAlchemyAgentRepository",
            return_value=agent_repo,
        ), patch.object(onboarding, "_prompt", prompt_mock):
            await onboarding._run(runtime, web_setup=True)
            await onboarding._run(runtime, web_setup=False)

        prompt_mock.assert_not_called()

    async def test_maybe_run_setup_wizard_reads_env_var_by_default(self):
        import os

        route_repo = _FakeRouteRepo()
        agent_repo = _FakeAgentRepo(model_route_name="")
        runtime = {"session_factory": object(), "model_route_repository": route_repo}

        with patch(
            "doyoutrade.assistant.repository.SqlAlchemyAgentRepository",
            return_value=agent_repo,
        ), patch.dict(os.environ, {"DOYOUTRADE_WEB_SETUP": "1"}), redirect_stdout(
            io.StringIO()
        ) as out:
            await onboarding.maybe_run_setup_wizard(runtime)

        self.assertIn("请在浏览器里完成设置", out.getvalue())
        self.assertEqual(agent_repo.updates, [])


if __name__ == "__main__":
    unittest.main()
