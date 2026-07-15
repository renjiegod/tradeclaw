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
- Never crash startup on a wizard error. Any failure is logged (with type +
  message) and the server proceeds; a missing model only degrades the assistant
  (it already returns a structured "not configured" fallback), it must not take
  the whole platform down.
- Idempotent: if a usable route already exists it is bound and the wizard is
  skipped, so re-runs never re-prompt.
"""

from __future__ import annotations

import getpass
import sys
import uuid
from dataclasses import dataclass

from doyoutrade.observability import get_logger
from doyoutrade.terminal_menu import select_index

logger = get_logger(__name__)

DEFAULT_ROUTE_NAME = "default"


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
_PRESETS: tuple[_Preset, ...] = (
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


@dataclass(frozen=True)
class _Answers:
    provider_kind: str
    slug: str
    api_key: str
    base_url: str | None
    model: str
    route_name: str


async def maybe_run_setup_wizard(runtime: dict, *, launch_mode: str = "doyoutrade") -> None:
    """Configure a model if the default agent has none. Never raises.

    ``launch_mode`` is the resolved ``doyoutrade --mode``. On a doyoutrade-only
    launch (macOS/Linux default) the first-run wizard also offers to register a
    remote qmt-proxy address; in ``both`` mode the embedded proxy is auto-wired
    instead so this prompt is skipped."""

    try:
        await _run(runtime, launch_mode=launch_mode)
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


async def _run(runtime: dict, *, launch_mode: str = "doyoutrade") -> None:
    from doyoutrade.assistant.repository import SqlAlchemyAgentRepository

    session_factory = runtime["session_factory"]
    route_repo = runtime["model_route_repository"]
    agent_repo = SqlAlchemyAgentRepository(session_factory)

    # Already usable → nothing to do.
    if await _agent_route_usable(runtime, agent_repo):
        return

    # A usable route exists but the default agent isn't bound → bind it silently.
    existing = await _first_usable_route(runtime, route_repo)
    if existing is not None:
        await agent_repo.update_agent("agent_default", {"model_route_name": existing})
        logger.info("setup: bound default agent to existing model route %r", existing)
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
    """Interactively register a remote qmt-proxy address as the default account.

    Skips silently when an account with a ``base_url`` already exists (so it
    never nags on later runs) or when no account repository is wired. Leaving
    the answer blank keeps the free data sources — not an error."""

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
        "\n实时行情 / 实盘走 QMT，需要一台已登录 miniQMT 的 Windows 机器运行 qmt-proxy。\n"
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


async def _agent_route_usable(runtime: dict, agent_repo) -> bool:
    agent = await agent_repo.get_agent("agent_default")
    route_name = str((agent or {}).get("model_route_name") or "").strip()
    if not route_name:
        return False
    return await _route_builds(runtime, route_name)


async def _first_usable_route(runtime: dict, route_repo) -> str | None:
    try:
        routes = await route_repo.list_routes()
    except Exception:
        return None
    for route in routes:
        if await _route_builds(runtime, route.route_name):
            return route.route_name
    return None


async def _route_builds(runtime: dict, route_name: str) -> bool:
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
            route_repository=runtime["model_route_repository"],
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


def _prompt() -> _Answers | None:
    print("\n" + "=" * 60, flush=True)
    print("DoYouTrade 安装向导 — 未检测到可用的模型配置", flush=True)
    print("=" * 60, flush=True)
    print("为默认智能体配置一个大模型供应商（可随时在网页 /settings/models 修改）。", flush=True)

    choice = select_index(
        "请选择供应商（↑↓ 选择，Enter 确认；无方向键时输入编号）",
        [p.label for p in _PRESETS],
        allow_skip=True,
        skip_label="跳过（稍后在网页配置）",
        default=0,
    )
    if choice is None:
        return None
    preset = _PRESETS[choice]

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
    route_name = await _unique_route_name(route_repo, answers.route_name)
    await route_repo.create(
        route_name=route_name,
        provider_kind=answers.provider_kind,
        api_key=answers.api_key,
        base_url=answers.base_url,
        target_model=answers.model,
    )
    await agent_repo.update_agent("agent_default", {"model_route_name": route_name})

    logger.info(
        "setup: created model route=%r kind=%r model=%r and bound default agent",
        route_name,
        answers.provider_kind,
        answers.model,
    )
    print("\n✓ 已写入模型配置，并绑定默认智能体。", flush=True)
    print(f"  配置名称 = {route_name}", flush=True)
    print(f"  模型 ID  = {answers.model}", flush=True)
    print("正在启动服务……\n", flush=True)


async def _unique_route_name(route_repo, route_name: str) -> str:
    try:
        existing = {r.route_name for r in await route_repo.list_routes()}
    except Exception:
        existing = set()
    if route_name not in existing:
        return route_name
    return f"{route_name}-{uuid.uuid4().hex[:6]}"
