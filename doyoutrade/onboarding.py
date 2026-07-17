"""First-run setup wizard: configure a model route when none exists.

Runs once, right before the API server starts serving (see
``doyoutrade/api/server.py`` ``main``). When the default agent (``agent_default``)
has no usable model route, an interactive TTY prompts for a backend kind + api_key +
model and writes a single self-contained model route straight to the DB via the
existing repository, so the assistant works out of the box after a single
``doyoutrade`` / ``uvx`` command.

Design rules (mirroring AGENTS.md error-visibility discipline):

- Never block a non-interactive startup. If stdin/stdout is not a TTY (systemd,
  Docker, CI, ``&`` background), the wizard logs clear guidance and returns — the
  server still starts; the operator can configure via the /settings/models page
  or ``POST /model-routes`` afterwards.
- Double-click launches (``DOYOUTRADE_WEB_SETUP=1``, see :func:`maybe_run_setup_wizard`)
  short-circuit the same way *before* the TTY check: the web console's
  ``SetupWizard`` overlay (``GET /setup/status`` + ``POST /setup/complete``,
  wired in ``doyoutrade/api/app.py``) owns first-run configuration instead of a
  blocking terminal prompt, so a user who never opens a terminal is never asked
  to type into one.
- Never crash startup on a wizard error. Any failure is logged (with type +
  message) and the server proceeds; a missing model only degrades the assistant
  (it already returns a structured "not configured" fallback), it must not take
  the whole platform down.
- Idempotent: if a usable route already exists it is bound and the wizard is
  skipped, so re-runs never re-prompt.

``agent_route_usable`` / ``first_usable_route`` / ``route_builds`` and
``create_route_and_bind_agent`` / ``serialize_presets`` are public (no leading
underscore) precisely because ``doyoutrade/api/app.py`` imports them for the web
setup endpoints — this module is the single source of truth for "what counts as
configured" and "how a route gets created + bound", shared by the terminal
wizard and the web API so they can never drift apart.
"""

from __future__ import annotations

import getpass
import os
import sys
import uuid
from dataclasses import dataclass

from doyoutrade.observability import get_logger
from doyoutrade.terminal_menu import select_index

logger = get_logger(__name__)

DEFAULT_ROUTE_NAME = "default"

# DoYouTrade Cloud —— 官方云行情网关（占位地址，正式上线前替换）。
# 云网关只代理行情：/trading/* 一律 403，因此 Cloud 账户永远以 mode=mock 落库
# （云端行情 + 本地模拟交易）；实盘必须本地部署 qmt-proxy 并使用单独账户。
CLOUD_GATEWAY_DEFAULT_URL = "https://cloud.doyoutrade.com"
CLOUD_CONSOLE_URL = "https://cloud.doyoutrade.com/console"
CLOUD_TOKEN_PREFIX = "dytc_"


@dataclass(frozen=True)
class _Preset:
    label: str
    provider_kind: str
    slug: str
    base_url: str | None
    model_hint: str
    needs_key: bool = True


# Friendly presets curated from OpenCode's provider directory, limited to what
# our adapters can speak today (anthropic / openai_compatible / lmstudio).
# base_url + model_hint are suggestions the user can override; the model id is
# always confirmed by the user (never silently assumed).
#
# Public (no leading underscore): both the terminal wizard's ``_prompt()`` and
# the web setup API's ``GET /setup/providers`` (``serialize_presets`` below)
# read this exact tuple, so there is only ever one provider list to maintain.
PRESETS: tuple[_Preset, ...] = (
    # —— 国内常用 ——
    _Preset("DeepSeek", "openai_compatible", "deepseek", "https://api.deepseek.com", "deepseek-chat"),
    _Preset("Kimi / Moonshot", "openai_compatible", "kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    _Preset(
        "通义千问 / DashScope",
        "openai_compatible",
        "qwen",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-plus",
    ),
    _Preset("智谱 GLM", "openai_compatible", "zhipu", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    _Preset(
        "硅基流动 SiliconFlow",
        "openai_compatible",
        "siliconflow",
        "https://api.siliconflow.cn/v1",
        "deepseek-ai/DeepSeek-V3",
    ),
    _Preset(
        "火山方舟 Volcengine",
        "openai_compatible",
        "volcengine",
        "https://ark.cn-beijing.volces.com/api/v3",
        "doubao-pro-32k",
    ),
    _Preset("MiniMax", "openai_compatible", "minimax", "https://api.minimax.chat/v1", "MiniMax-Text-01"),
    # —— 国际 / 聚合 ——
    _Preset("OpenAI", "openai_compatible", "openai", "https://api.openai.com/v1", "gpt-4.1"),
    _Preset("Anthropic Claude", "anthropic", "anthropic", None, "claude-sonnet-4-5"),
    _Preset("OpenRouter", "openai_compatible", "openrouter", "https://openrouter.ai/api/v1", "openai/gpt-4.1"),
    _Preset("Groq", "openai_compatible", "groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    _Preset("xAI Grok", "openai_compatible", "xai", "https://api.x.ai/v1", "grok-2-latest"),
    _Preset("Mistral", "openai_compatible", "mistral", "https://api.mistral.ai/v1", "mistral-large-latest"),
    _Preset("Together AI", "openai_compatible", "together", "https://api.together.xyz/v1", "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo"),
    _Preset(
        "Fireworks AI",
        "openai_compatible",
        "fireworks",
        "https://api.fireworks.ai/inference/v1",
        "accounts/fireworks/models/llama-v3p1-70b-instruct",
    ),
    _Preset("Cerebras", "openai_compatible", "cerebras", "https://api.cerebras.ai/v1", "llama3.1-70b"),
    _Preset(
        "DeepInfra",
        "openai_compatible",
        "deepinfra",
        "https://api.deepinfra.com/v1/openai",
        "meta-llama/Meta-Llama-3.1-70B-Instruct",
    ),
    # —— 本地 ——
    _Preset(
        "Ollama（本地）",
        "openai_compatible",
        "ollama",
        "http://localhost:11434/v1",
        "llama3.2",
        needs_key=False,
    ),
    _Preset(
        "LM Studio（本地）",
        "lmstudio",
        "lmstudio",
        "http://localhost:1234/v1",
        "local-model",
        needs_key=False,
    ),
    # —— 兜底 ——
    _Preset("自定义 OpenAI 兼容接口", "openai_compatible", "custom", None, ""),
)


def serialize_presets() -> list[dict]:
    """JSON-serializable view of :data:`PRESETS` for ``GET /setup/providers``.

    Field names match what the web ``SetupWizard`` form needs: ``label`` (menu
    text), ``provider_kind`` / ``base_url`` / ``model_hint`` (form defaults),
    ``needs_key`` (whether to require an API key input). This is the only place
    that shapes the JSON, so the terminal preset list and the web provider list
    can never disagree about what providers exist.
    """

    return [
        {
            "label": p.label,
            "provider_kind": p.provider_kind,
            "base_url": p.base_url,
            "model_hint": p.model_hint,
            "needs_key": p.needs_key,
        }
        for p in PRESETS
    ]


@dataclass(frozen=True)
class _Answers:
    provider_kind: str
    slug: str
    api_key: str
    base_url: str | None
    model: str
    route_name: str


async def maybe_run_setup_wizard(
    runtime: dict, *, launch_mode: str = "doyoutrade", web_setup: bool | None = None
) -> None:
    """Configure a model if the default agent has none. Never raises.

    ``launch_mode`` is the resolved ``doyoutrade --mode``. On a doyoutrade-only
    launch (macOS/Linux default) the first-run wizard also offers to register a
    remote qmt-proxy address; in ``both`` mode the embedded proxy is auto-wired
    instead so this prompt is skipped.

    ``web_setup`` (default: read from ``DOYOUTRADE_WEB_SETUP=1``) marks a
    double-click / GUI launch: first-run configuration is handled by the web
    console's ``SetupWizard`` overlay (``GET /setup/status`` +
    ``POST /setup/complete``) instead of a blocking terminal prompt, so this
    short-circuits *before* the TTY check — even a launch that happens to have
    a real TTY attached (e.g. a bat file that opens a console window) must not
    block on ``input()``/``getpass()`` waiting for someone who is looking at a
    browser, not a terminal.
    """

    if web_setup is None:
        web_setup = os.environ.get("DOYOUTRADE_WEB_SETUP") == "1"
    try:
        await _run(runtime, launch_mode=launch_mode, web_setup=web_setup)
    except (KeyboardInterrupt, EOFError):
        # Ctrl-C / closed stdin mid-wizard: skip, don't kill startup.
        print("\n跳过安装向导，稍后可在网页 /settings/models 配置模型。", flush=True)
    except Exception as exc:  # noqa: BLE001 — startup must survive a wizard bug
        logger.warning(
            "setup wizard failed (%s: %s); starting without model config — "
            "configure at /settings/models",
            type(exc).__name__,
            exc,
        )


async def _run(runtime: dict, *, launch_mode: str = "doyoutrade", web_setup: bool = False) -> None:
    from doyoutrade.assistant.repository import SqlAlchemyAgentRepository

    session_factory = runtime["session_factory"]
    route_repo = runtime["model_route_repository"]
    agent_repo = SqlAlchemyAgentRepository(session_factory)

    # Already usable → nothing to do. This check runs regardless of web_setup:
    # a machine that has already been configured (terminal or web) must never
    # be re-prompted by either surface.
    if await agent_route_usable(route_repo, agent_repo):
        return

    # A usable route exists but the default agent isn't bound → bind it silently.
    existing = await first_usable_route(route_repo)
    if existing is not None:
        await agent_repo.update_agent("agent_default", {"model_route_name": existing})
        logger.info("setup: bound default agent to existing model route %r", existing)
        return

    if web_setup:
        _print_web_setup_guidance()
        return

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _print_headless_guidance()
        return

    answers = _prompt()
    if answers is None:
        print("已跳过。稍后可在网页 /settings/models 配置模型后即可对话。", flush=True)
        return

    await _apply(answers, route_repo, agent_repo)

    # First-run only (we just configured a model interactively): on a
    # doyoutrade-only launch, offer to point at a remote qmt-proxy for real QMT
    # quotes. ``both`` mode auto-wires the embedded proxy, so skip it there.
    if launch_mode != "both":
        await _maybe_prompt_qmt_proxy(runtime)


async def _maybe_prompt_qmt_proxy(runtime: dict) -> None:
    """Interactively register a QMT 级行情数据源 as the default account.

    Offers two sources: DoYouTrade Cloud（官方云行情网关，无需 Windows/QMT）或
    自建 / 远程 qmt-proxy。Skips silently when an account with a ``base_url``
    already exists (so it never nags on later runs) or when no account
    repository is wired. 选择跳过则继续用免费行情源 — not an error."""

    repo = runtime.get("account_repository")
    if repo is None:
        return
    try:
        accounts = await repo.list_accounts()
    except Exception as exc:  # noqa: BLE001 — convenience step, must not kill wizard
        logger.warning(
            "qmt-proxy prompt: could not list accounts (%s: %s); skipping",
            type(exc).__name__,
            exc,
        )
        return
    if any(str(a.get("base_url") or "").strip() for a in accounts):
        return

    print(
        "\nQMT 级实时行情有两种接入方式（现在跳过也没关系，先用免费行情源）：",
        flush=True,
    )
    choice = select_index(
        "请选择行情数据接入方式（↑↓ 选择，Enter 确认；无方向键时输入编号）",
        [
            f"使用 DoYouTrade Cloud（云行情，无需 Windows/QMT，去 {CLOUD_CONSOLE_URL} 获取 API key）",
            "自建 / 远程 qmt-proxy（需一台已登录 miniQMT 的 Windows 机器）",
        ],
        allow_skip=True,
        skip_label="跳过（先用免费行情源）",
        default=0,
    )
    if choice is None:
        print(
            "已跳过数据源配置。稍后可用 `doyoutrade-cli account create --base-url ...` 登记。",
            flush=True,
        )
        return
    if choice == 0:
        await _register_cloud_account(repo)
    else:
        await _register_remote_proxy_account(repo)


async def _register_cloud_account(repo) -> None:
    """Register a DoYouTrade Cloud gateway account as the mock-mode default."""

    print(
        "\nDoYouTrade Cloud 提供 QMT 级行情；交易不经云端（云网关对 /trading/* 一律 403）。\n"
        f"请先在控制台 {CLOUD_CONSOLE_URL} 创建 API key（{CLOUD_TOKEN_PREFIX} 前缀）。",
        flush=True,
    )
    base_url = _ask("云网关地址", default=CLOUD_GATEWAY_DEFAULT_URL).strip() or CLOUD_GATEWAY_DEFAULT_URL
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        base_url = "https://" + base_url
    token = _ask(f"DoYouTrade Cloud API key（{CLOUD_TOKEN_PREFIX} 前缀）", default="").strip() or None
    if token is None or not token.startswith(CLOUD_TOKEN_PREFIX):
        # 前缀不符只警告不阻断：key 体系可能演进，且网页 Accounts 页随时可改。
        print(
            f"警告：输入的 API key 不是 {CLOUD_TOKEN_PREFIX} 前缀"
            f"（{'为空' if token is None else '与控制台签发格式不符'}），云网关可能拒绝鉴权。\n"
            "仍继续登记，稍后可在网页 Accounts 页修改 token。",
            flush=True,
        )
    await repo.upsert_account(
        {
            "name": "DoYouTrade Cloud",
            "mode": "mock",
            "base_url": base_url,
            "token": token,
            "is_default": True,
            "enabled": True,
        }
    )
    logger.info("setup: registered DoYouTrade Cloud default account -> %s", base_url)
    print(
        f"已登记默认账户，行情将连接 {base_url}（mode=mock：云端行情 + 本地模拟交易）。\n"
        "注意：Cloud 账户不可用于实盘交易；要走真实下单，请本地部署 qmt-proxy 并另建 live 账户。",
        flush=True,
    )


async def _register_remote_proxy_account(repo) -> None:
    """Register a self-hosted / remote qmt-proxy address as the default account.

    Leaving the address blank keeps the free data sources — not an error."""

    print(
        "\n自建方式需要一台已登录 miniQMT 的 Windows 机器运行 qmt-proxy。\n"
        "在那台 Windows 上跑 `doyoutrade`（默认已内置 qmt-proxy），记下它的地址即可。\n"
        "现在没有也没关系——直接回车跳过，先用免费行情源。",
        flush=True,
    )
    base_url = _ask(
        "远程 qmt-proxy 地址（如 http://192.168.1.10:8001，回车跳过）", default=""
    ).strip()
    if not base_url:
        print(
            "已跳过 QMT 配置。稍后可用 `doyoutrade-cli account create --base-url ...` 登记。",
            flush=True,
        )
        return
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        base_url = "http://" + base_url
    token = _ask("qmt-proxy API token（回车留空）", default="").strip() or None
    await repo.upsert_account(
        {
            "name": "QMT 远程",
            "mode": "mock",
            "base_url": base_url,
            "token": token,
            "is_default": True,
            "enabled": True,
        }
    )
    logger.info("setup: registered remote qmt-proxy default account -> %s", base_url)
    print(
        f"已登记默认账户，行情将连接 {base_url}（mode=mock：真实行情 + 模拟交易）。\n"
        "要走真实下单，请在网页 Accounts 页把该账户改为 live 模式。",
        flush=True,
    )


async def agent_route_usable(route_repository, agent_repo, *, agent_id: str = "agent_default") -> bool:
    """True iff *agent_id* is bound to a model route that actually builds an adapter.

    Public and decoupled from the ``runtime`` dict (takes ``route_repository``
    directly) so both the terminal wizard's ``_run`` and the web setup API's
    ``GET /setup/status`` (``doyoutrade/api/app.py``) can call it and always see
    the same answer to "is this machine configured?".
    """

    agent = await agent_repo.get_agent(agent_id)
    route_name = str((agent or {}).get("model_route_name") or "").strip()
    if not route_name:
        return False
    return await route_builds(route_repository, route_name)


async def first_usable_route(route_repository) -> str | None:
    """First route in *route_repository* that :func:`route_builds`, or None."""

    try:
        routes = await route_repository.list_routes()
    except Exception:
        return None
    for route in routes:
        if await route_builds(route_repository, route.route_name):
            return route.route_name
    return None


async def route_builds(route_repository, route_name: str) -> bool:
    """True iff the route resolves to settings that build a real adapter.

    ``resolve_model_settings`` checks model/base_url; ``build_model_adapter``
    additionally checks the api_key — together they mean a live conversation
    would actually get an adapter rather than fall back to "not configured".
    Adapter constructors do no network IO, so this is a safe dry validation.
    """

    from doyoutrade.models.factory import build_model_adapter
    from doyoutrade.models.route_resolution import resolve_model_settings

    try:
        settings = await resolve_model_settings(
            route_name=route_name,
            route_repository=route_repository,
        )
        build_model_adapter(settings)
        return True
    except Exception:
        return False


def _print_headless_guidance() -> None:
    logger.warning(
        "no model route configured and startup is non-interactive; "
        "the assistant will reply with a 'model not configured' message until you "
        "add one via the /settings/models page or POST /model-routes"
    )


def _print_web_setup_guidance() -> None:
    logger.warning(
        "no model route configured; DOYOUTRADE_WEB_SETUP is set, so the terminal "
        "wizard is skipped — open the web console to finish setup (it will show "
        "a full-screen setup wizard backed by GET /setup/status + "
        "POST /setup/complete)"
    )
    print(
        "\n未检测到可用的模型配置。请在浏览器里完成设置——网页控制台会自动弹出安装向导。\n",
        flush=True,
    )


def _prompt() -> _Answers | None:
    print("\n" + "=" * 60, flush=True)
    print("DoYouTrade 安装向导 — 未检测到可用的模型配置", flush=True)
    print("=" * 60, flush=True)
    print("为默认智能体配置一个大模型供应商（可随时在网页 /settings/models 修改）。", flush=True)

    choice = select_index(
        "请选择供应商（↑↓ 选择，Enter 确认；无方向键时输入编号）",
        [p.label for p in PRESETS],
        allow_skip=True,
        skip_label="跳过（稍后在网页配置）",
        default=0,
    )
    if choice is None:
        return None
    preset = PRESETS[choice]

    if preset.base_url is not None:
        base_url = _ask("接口地址", default=preset.base_url).strip() or None
    elif preset.provider_kind == "openai_compatible":
        base_url = _ask("接口地址（OpenAI 兼容接口）", default="").strip() or None
        if not base_url:
            print("OpenAI 兼容接口必须填接口地址，已跳过安装向导。", flush=True)
            return None
    else:  # anthropic：官方地址由 SDK 默认，留空即可
        base_url = None

    if preset.needs_key:
        api_key = getpass.getpass("API Key（输入时不显示）: ").strip()
        if not api_key:
            print("未输入 API Key，已跳过安装向导。", flush=True)
            return None
    else:
        api_key = _ask("API Key（本地服务可留空）", default="").strip()

    model = _ask("模型 ID", default=preset.model_hint).strip()
    if not model:
        print("未输入模型 ID，已跳过安装向导。", flush=True)
        return None

    route_name = _ask("配置名称", default=DEFAULT_ROUTE_NAME).strip() or DEFAULT_ROUTE_NAME

    return _Answers(
        provider_kind=preset.provider_kind,
        slug=preset.slug,
        api_key=api_key,
        base_url=base_url,
        model=model,
        route_name=route_name,
    )


def _ask(label: str, *, default: str) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{label}{suffix}: ")
    return raw if raw.strip() else default


async def _apply(answers: _Answers, route_repo, agent_repo) -> None:
    route_name = await create_route_and_bind_agent(
        route_repo,
        agent_repo,
        route_name=answers.route_name,
        provider_kind=answers.provider_kind,
        api_key=answers.api_key,
        base_url=answers.base_url,
        target_model=answers.model,
    )
    print("\n✓ 已写入模型配置，并绑定默认智能体。", flush=True)
    print(f"  配置名称 = {route_name}", flush=True)
    print(f"  模型 ID  = {answers.model}", flush=True)
    print("正在启动服务……\n", flush=True)


async def create_route_and_bind_agent(
    route_repo,
    agent_repo,
    *,
    route_name: str,
    provider_kind: str,
    api_key: str,
    base_url: str | None,
    target_model: str | None,
    agent_id: str = "agent_default",
) -> str:
    """Create a model route and bind it as *agent_id*'s ``model_route_name``.

    This is the single create-then-bind sequence shared by the terminal wizard
    (``_apply``, above) and the web setup API (``POST /setup/complete`` in
    ``doyoutrade/api/app.py``) — the exact two DB writes (route create + agent
    bind) must stay identical across both entry points, so neither may
    reimplement or copy-paste this. Returns the actual route name used (a
    conflicting *route_name* is de-duplicated by :func:`_unique_route_name`).
    """

    resolved_name = await _unique_route_name(route_repo, route_name)
    await route_repo.create(
        route_name=resolved_name,
        provider_kind=provider_kind,
        api_key=api_key,
        base_url=base_url,
        target_model=target_model,
    )
    await agent_repo.update_agent(agent_id, {"model_route_name": resolved_name})
    logger.info(
        "setup: created model route=%r kind=%r model=%r and bound agent=%r",
        resolved_name,
        provider_kind,
        target_model,
        agent_id,
    )
    return resolved_name


async def _unique_route_name(route_repo, route_name: str) -> str:
    try:
        existing = {r.route_name for r in await route_repo.list_routes()}
    except Exception:
        existing = set()
    if route_name not in existing:
        return route_name
    return f"{route_name}-{uuid.uuid4().hex[:6]}"
