from __future__ import annotations

import argparse
import asyncio
import importlib.resources
import os
import sys
from datetime import date, datetime
from pathlib import Path

from doyoutrade.api.app import create_app
from doyoutrade.data.account_resolution import resolved_account_from_record
from doyoutrade.infra.qmt import create_qmt_proxy_rest_client
from doyoutrade.runtime.trigger_scheduler import TriggerScheduler
from doyoutrade.assistant.cron_executors import (
    AgentChatReplyExecutor,
    DailyReviewExecutor,
    DeviationMonitorExecutor,
    JobExecutorRegistry,
    JobTaskRegistry,
    LoadedStrategy,
    NoopExecutor,
    StockReportExecutor,
)
from doyoutrade.assistant.cron_manager import AgentCronManager
from doyoutrade.bootstrap import build_platform_runtime
from doyoutrade.config import get_config
from doyoutrade.observability import get_logger, initialize_observability
from doyoutrade.persistence.repositories import (
    SqlAlchemyCronJobRepository,
    SqlAlchemyCronJobRunRepository,
)

logger = get_logger(__name__)

# Uvicorn waits this long for open connections / ASGI tasks before cancelling them.
_DEFAULT_GRACEFUL_SHUTDOWN_S = 30


def _frontend_dist_dir() -> Path | None:
    """Locate the built frontend bundle, or None when it isn't shipped.

    Source checkout: ``frontend/dist`` at the repo root (built by
    ``npm --prefix frontend run build``). Installed wheel: the copy bundled at
    ``doyoutrade/_frontend`` by the custom hatch build hook. Returns None when
    neither exists — the server then runs API-only (no bundled UI), which is a
    valid degraded mode, not an error.
    """

    repo_root = Path(__file__).resolve().parents[2]
    source = repo_root / "frontend" / "dist"
    if (source / "index.html").is_file():
        return source
    packaged = Path(str(importlib.resources.files("doyoutrade"))) / "_frontend"
    if (packaged / "index.html").is_file():
        return packaged
    return None


# First path segments owned by the SPA router (frontend/src/App.tsx <Routes>).
# Several collide with same-named JSON API routes (GET /tasks, /accounts,
# /watchlist, /approvals, and parametrized ones like /tasks/{task_id}) — FastAPI
# matches API routes before the SPA mount, so a browser hard-navigation to
# /tasks used to render raw JSON instead of the app. Keep in sync with the
# frontend router when adding top-level pages.
SPA_ROUTE_PREFIXES = frozenset(
    {
        "agents",
        "cron_jobs",
        "channels",
        "assistant",
        "swarm",
        "tasks",
        "accounts",
        "stocks",
        "watchlist",
        "stock_monitor",
        "market_review",
        "strategies",
        "knowledge",
        "approvals",
        "model_invocations",
        "settings",
        "data_console",
    }
)


def _mount_frontend(app) -> None:
    """Serve the SPA bundle same-origin, as the LAST route so it never shadows
    an API route. Missing paths fall back to index.html for client-side routing.

    Browser hard-navigations (Accept lists text/html) to SPA-owned paths are
    answered with index.html via middleware even when a same-named API route
    exists; programmatic clients (fetch/httpx/curl send ``*/*`` or JSON,
    EventSource sends ``text/event-stream``) still reach the API unchanged.
    """

    dist_dir = _frontend_dist_dir()
    if dist_dir is None:
        logger.info(
            "frontend bundle not found; serving API only. Build it with "
            "`npm --prefix frontend run build` (dev) or run the Vite dev server."
        )
        return

    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.responses import FileResponse
    from starlette.staticfiles import StaticFiles

    class _SPAStaticFiles(StaticFiles):
        async def get_response(self, path, scope):
            try:
                return await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                # SPA client-side routes (/agents, /settings/...) have no file
                # on disk — serve index.html so the router renders them. Genuine
                # asset 404s also fall back, which is standard SPA behavior.
                if exc.status_code == 404:
                    return await super().get_response("index.html", scope)
                raise

    spa_index = dist_dir / "index.html"

    @app.middleware("http")
    async def spa_navigation_fallback(request, call_next):
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            first_segment = request.url.path.lstrip("/").split("/", 1)[0]
            if first_segment in SPA_ROUTE_PREFIXES:
                return FileResponse(spa_index)
        return await call_next(request)

    app.mount("/", _SPAStaticFiles(directory=str(dist_dir), html=True), name="frontend")
    logger.info("serving frontend bundle from %s", dist_dir)


async def build_api_with_runtime(tick_interval_seconds: float | None = None):
    cfg = get_config()
    runtime = await build_platform_runtime(app_cfg=cfg)
    service = runtime["service"]
    approval_gate = runtime["approval_gate"]

    assistant_service = runtime.get("assistant_service")
    session_factory = runtime.get("session_factory")
    account_repository = runtime.get("account_repository")
    strategy_definition_repository = runtime.get("strategy_definition_repository")
    quote_stream_service = runtime.get("quote_stream_service")
    cron_job_repo = SqlAlchemyCronJobRepository(session_factory)
    cron_run_repo = SqlAlchemyCronJobRunRepository(session_factory)

    executor_registry = JobExecutorRegistry()
    executor_registry.register(NoopExecutor())

    # New task-dispatch registry — each ``task.kind`` owns its own
    # fire-time pipeline. AgentChatReplyExecutor covers chat reminders.
    # Strategy execution is now scheduled via Task Triggers
    # (``doyoutrade-cli task trigger add ...``), not cron task_kinds.
    task_registry = JobTaskRegistry()
    if assistant_service is not None:
        task_registry.register(
            AgentChatReplyExecutor(
                assistant_service=assistant_service,
                cron_job_repository=cron_job_repo,
            )
        )
        # ``stock_report`` (模板化个股研报): deterministic rule-scored per-symbol
        # report rendered from templates (no LLM). Bars come from the lazy
        # default provider stack inside the executor, so no extra wiring here.
        task_registry.register(
            StockReportExecutor(
                assistant_service=assistant_service,
                cron_job_repository=cron_job_repo,
            )
        )

    # ``daily_review`` (每日复盘): pre-gathers a live account statement +
    # private-KB digest and composes a 复盘 each trading day. The executor is
    # compose-only, so the statement / trading-day data is fetched here at fire
    # time through a QmtAccountReader built from the chosen (or default) account.
    if assistant_service is not None and account_repository is not None:

        async def _daily_review_statement_provider(
            account_id: str | None, asof: date, captured_at: datetime
        ) -> dict:
            return await service.get_account_statement(
                account_id, asof=asof, captured_at=captured_at
            )

        async def _daily_review_trading_day_checker(asof: date) -> bool:
            record = await account_repository.get_default_account()
            if not record:
                # No account to query the calendar with → don't suppress; treat
                # as a trading day and let the review run (data errors surface).
                return True
            # Market-only client: the calendar is a data API, no trading session.
            resolved = resolved_account_from_record(record).market_only()
            client = create_qmt_proxy_rest_client(resolved)
            try:
                calendar = await client.get_trading_calendar(asof.year)
                wanted = asof.strftime("%Y%m%d")
                normalized = {
                    str(d).replace("-", "")[:8] for d in (calendar.trading_dates or [])
                }
                # Empty calendar (unavailable) → safe default: treat as trading day.
                return wanted in normalized if normalized else True
            finally:
                await client.aclose()

        task_registry.register(
            DailyReviewExecutor(
                assistant_service=assistant_service,
                cron_job_repository=cron_job_repo,
                statement_provider=_daily_review_statement_provider,
                trading_day_checker=_daily_review_trading_day_checker,
                # 步骤 5b（知识图谱 LLM 抽取）的可选依赖：缺席时执行器发
                # daily_review_kg_extract_skipped 显式跳过，复盘主链路不受影响。
                knowledge_graph_repository=runtime.get("knowledge_graph_repository"),
                model_adapter_factory=getattr(
                    assistant_service, "model_adapter_factory", None
                ),
                instrument_catalog_repository=runtime.get(
                    "instrument_catalog_repository"
                ),
            )
        )

        # ``deviation_monitor`` (交易纪律提醒): at fire time compile a
        # user-authored deviation strategy (sd-), read the live ~14:50 quote +
        # real position cost basis, evaluate the rule per held symbol, and
        # remind the user — recalling their original thesis — only when the plan
        # is violated. Needs the strategy definition repository (to compile the
        # rule) and the quote stream (live price) in addition to the account
        # statement; registered only when all are wired.
        if (
            strategy_definition_repository is not None
            and quote_stream_service is not None
        ):

            async def _deviation_strategy_loader(sd_id: str) -> LoadedStrategy:
                from doyoutrade.strategy_runtime.compiler import StrategyCompiler

                snap = await strategy_definition_repository.get_definition(sd_id)
                _version, code_root = (
                    await strategy_definition_repository.read_current_code(sd_id)
                )
                class_name = getattr(snap, "class_name", None) or "Strategy"
                compiler = StrategyCompiler()
                compile_result = compiler.validate_directory(
                    code_root, strategy_class_name=class_name
                )
                if not compile_result.success or compile_result.artifact is None:
                    detail = "; ".join(compile_result.errors or []) or "compile failed"
                    raise RuntimeError(
                        f"{compile_result.error_code or 'compile_failed'}: {detail}"
                    )
                smoke = compiler.smoke_test(compile_result.artifact)
                if not smoke.success:
                    raise RuntimeError(
                        f"{smoke.error_code or 'smoke_failed'}: "
                        f"{smoke.error_message or 'strategy crashed during smoke test'}"
                    )
                return LoadedStrategy(
                    strategy_class=compile_result.artifact.strategy_class,
                    class_name=class_name,
                )

            async def _deviation_quote_fetcher(symbols: list[str]):
                return await quote_stream_service.fetch_once(list(symbols))

            async def _deviation_history_fetcher_factory(
                symbols: list[str], data_source: str
            ):
                from doyoutrade.data.account_resolution import (
                    resolve_default_market_account,
                )
                from doyoutrade.data.factory import build_trading_data_stack
                from doyoutrade.strategy_sdk.history_fetcher import BarsHistoryFetcher

                account = await resolve_default_market_account()
                provider, _universe, _account = build_trading_data_stack(
                    data_source or "auto", cfg.data, list(symbols), account=account
                )
                del _universe, _account
                return BarsHistoryFetcher(data_provider=provider)

            task_registry.register(
                DeviationMonitorExecutor(
                    assistant_service=assistant_service,
                    cron_job_repository=cron_job_repo,
                    strategy_loader=_deviation_strategy_loader,
                    statement_provider=_daily_review_statement_provider,
                    quote_fetcher=_deviation_quote_fetcher,
                    history_fetcher_factory=_deviation_history_fetcher_factory,
                )
            )

    agent_cron_manager = AgentCronManager(
        assistant_service=assistant_service,
        cron_repo=cron_job_repo,
        cron_run_repo=cron_run_repo,
        executor_registry=executor_registry,
        task_registry=task_registry,
        timezone="UTC",
    )

    # Release-based self-update (设置页「自动更新」): checks GitHub releases in
    # the background and stages a user-triggered reinstall + restart.
    from doyoutrade.infra.updater import UpdateService

    update_service = UpdateService()

    app = create_app(
        service,
        approval_gate,
        runtime.get("model_invocation_repository"),
        runtime.get("strategy_registry_service"),
        runtime.get("strategy_definition_repository"),
        runtime.get("assistant_service"),
        channel_manager=runtime.get("channel_manager"),
        channel_repository=runtime.get("channel_repository"),
        cron_manager=agent_cron_manager,
        cron_run_repo=cron_run_repo,
        strategy_storage=runtime.get("strategy_storage"),
        compiler=runtime.get("compiler"),
        quote_stream_service=runtime.get("quote_stream_service"),
        update_service=update_service,
        knowledge_graph_repository=runtime.get("knowledge_graph_repository"),
    )
    initialize_observability(
        service_name=cfg.observability.service_name,
        log_level=cfg.observability.log_level,
        tracing_enabled=cfg.observability.tracing_enabled,
        console_enabled=cfg.observability.console_enabled,
        app=app,
    )
    interval = tick_interval_seconds if tick_interval_seconds is not None else cfg.server.tick_seconds

    app.state.runtime = runtime

    # Phase 3: the TriggerScheduler is the SOLE scheduler. RuntimeTickLoop is gone;
    # a Task fires cycles only via its Task Triggers. The scheduler also expires
    # stale pending approvals each scan (the duty the tick loop used to own), so it
    # takes the same ``approval_gate`` the tick loop did.
    trigger_repo = runtime.get("task_trigger_repository")
    trigger_scheduler = (
        TriggerScheduler(
            service=service,
            trigger_repository=trigger_repo,
            interval_seconds=interval,
            assistant_service=assistant_service,
            cycle_run_repository=runtime.get("cycle_run_repository"),
            approval_gate=approval_gate,
        )
        if trigger_repo is not None
        else None
    )
    app.state.trigger_scheduler = trigger_scheduler

    # Realtime stock monitoring daemon (盯盘): a standalone, event-driven
    # consumer of the quote stream — independent of any running trading task.
    # Built here (not in bootstrap) like the TriggerScheduler, where the
    # assistant_service (for delivery) + quote_stream + repos are all available.
    monitor_rule_repo = runtime.get("monitor_rule_repository")
    quote_stream_service = runtime.get("quote_stream_service")
    monitor_daemon = None
    if monitor_rule_repo is not None and quote_stream_service is not None:
        from doyoutrade.monitoring.daemon import MonitorDaemon

        monitor_daemon = MonitorDaemon(
            quote_stream_service=quote_stream_service,
            monitor_rule_repository=monitor_rule_repo,
            monitor_alert_repository=runtime.get("monitor_alert_repository"),
            watchlist_repository=runtime.get("watchlist_repository"),
            debug_session_repository=runtime.get("debug_session_repository"),
            debug_session_span_repository=runtime.get("debug_session_span_repository"),
            assistant_service=assistant_service,
        )
    app.state.monitor_daemon = monitor_daemon

    job_watch_service = runtime.get("job_watch_service")
    observability_prune_service = runtime.get("observability_prune_service")
    update_service = app.state.update_service

    @app.on_event("startup")
    async def _on_startup():
        if trigger_scheduler is not None:
            trigger_scheduler.start()
        await agent_cron_manager.start()
        if job_watch_service is not None:
            job_watch_service.start()
        if observability_prune_service is not None:
            observability_prune_service.start()
        if monitor_daemon is not None:
            await monitor_daemon.start()
        if update_service is not None:
            update_service.start()

    @app.on_event("shutdown")
    async def _on_shutdown():
        logger.info("api shutdown: stopping trigger scheduler and releasing resources")
        if trigger_scheduler is not None:
            await trigger_scheduler.stop()
        await agent_cron_manager.stop()
        if job_watch_service is not None:
            await job_watch_service.stop()
        if observability_prune_service is not None:
            await observability_prune_service.stop()
        if monitor_daemon is not None:
            await monitor_daemon.stop()
        if update_service is not None:
            await update_service.stop()
        close_runtime = runtime.get("aclose")
        if close_runtime is not None:
            await close_runtime()
        else:
            close = getattr(service, "aclose", None)
            if close is not None:
                await close()
        assistant_close = getattr(assistant_service, "aclose", None)
        if assistant_close is not None:
            await assistant_close()

    # Mount the SPA bundle LAST so its catch-all "/" never shadows an API route
    # (Starlette matches routes in registration order; all API routes were added
    # inside create_app above).
    _mount_frontend(app)

    return app


# --- Launch-mode selection ----------------------------------------------------
# One executable, three shapes: run only DoYouTrade, only the bundled qmt-proxy,
# or both in a single process. The default is OS-derived (Windows bundles
# qmt-proxy so real QMT quotes work out of the box; macOS/Linux run DoYouTrade
# alone and point at a remote qmt-proxy). ``--mode`` and DOYOUTRADE_LAUNCH_MODE
# override.
LAUNCH_MODES = ("doyoutrade", "qmt-proxy", "both")


def _default_launch_mode() -> str:
    return "both" if sys.platform == "win32" else "doyoutrade"


def _resolve_launch_mode(argv: list[str] | None = None) -> tuple[str, int | None]:
    """Resolve (launch_mode, qmt_port_override) from argv → env → OS default.

    Precedence: ``--mode`` flag > ``DOYOUTRADE_LAUNCH_MODE`` env > OS default.
    An unrecognized value is a hard error (SystemExit via argparse for the flag,
    explicit ValueError for the env var) — never a silent fallback."""

    parser = argparse.ArgumentParser(
        prog="doyoutrade",
        description="DoYouTrade API server (optionally with the bundled qmt-proxy).",
    )
    parser.add_argument(
        "--mode",
        choices=LAUNCH_MODES,
        default=None,
        help="which service(s) to launch (default: 'both' on Windows, else 'doyoutrade')",
    )
    parser.add_argument(
        "--qmt-port",
        type=int,
        default=None,
        help="override the embedded qmt-proxy port (default from config qmt_proxy.port)",
    )
    args, _unknown = parser.parse_known_args(argv)

    mode = args.mode
    if mode is None:
        env_mode = (os.environ.get("DOYOUTRADE_LAUNCH_MODE") or "").strip().lower()
        if env_mode:
            if env_mode not in LAUNCH_MODES:
                raise ValueError(
                    f"DOYOUTRADE_LAUNCH_MODE must be one of {LAUNCH_MODES}, got {env_mode!r}"
                )
            mode = env_mode
        else:
            mode = _default_launch_mode()
    return mode, args.qmt_port


def _embedded_base_url(host: str, port: int) -> str:
    """Client-reachable base_url for an embedded qmt-proxy bound to ``host``.

    A wildcard bind (0.0.0.0 / ::) isn't a valid client target, so point the
    loopback address at it instead."""

    reachable = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return f"http://{reachable}:{port}"


async def _auto_wire_qmt_base_url(runtime: dict, qmt_settings) -> None:
    """Point the default account at the embedded qmt-proxy (``both`` mode).

    Zero-config goal: on Windows the box should serve real QMT quotes without
    the user ever running ``doyoutrade-cli account``. If no default account
    exists we create a mock-mode one (real quotes, simulated trading — the safe
    default) wired to the local proxy; if one exists but lacks a ``base_url`` we
    patch just that field. An account that already has a ``base_url`` is left
    untouched. Failures are non-fatal (this is a convenience) but logged with
    type + message so a silent miss is still visible."""

    repo = runtime.get("account_repository")
    if repo is None:
        logger.warning(
            "both mode: no account_repository in runtime; skipping base_url auto-wire"
        )
        return
    base_url = _embedded_base_url(qmt_settings.host, qmt_settings.port)
    try:
        default = await repo.get_default_account()
        if default is None:
            await repo.upsert_account(
                {
                    "name": "本机 QMT（内置 qmt-proxy）",
                    "mode": "mock",
                    "base_url": base_url,
                    "token": qmt_settings.local_token,
                    "is_default": True,
                    "enabled": True,
                }
            )
            logger.info(
                "both mode: created default mock account wired to embedded qmt-proxy %s",
                base_url,
            )
        elif not str(default.get("base_url") or "").strip():
            await repo.upsert_account(
                {
                    "id": default["id"],
                    "base_url": base_url,
                    "token": default.get("token") or qmt_settings.local_token,
                }
            )
            logger.info(
                "both mode: patched default account %s base_url -> %s",
                default.get("id"),
                base_url,
            )
        else:
            logger.info(
                "both mode: default account already has base_url=%r; leaving as-is",
                default.get("base_url"),
            )
    except Exception as exc:
        logger.warning(
            "both mode: qmt-proxy base_url auto-wire failed (%s: %s); "
            "configure manually via `doyoutrade-cli account`",
            type(exc).__name__,
            exc,
        )


async def _serve_doyoutrade(launch_mode: str) -> None:
    """Build the DoYouTrade runtime + app and serve it (used by ``doyoutrade`` and
    ``both`` modes). In ``both`` mode the embedded qmt-proxy has already been
    started in a daemon thread by ``main`` before this coroutine runs."""

    from uvicorn import Config, Server

    cfg = get_config()
    app = await build_api_with_runtime()

    # First-run setup wizard: if the default agent has no usable model route, an
    # interactive TTY collects a provider + api_key + model and writes it before
    # we start serving. On doyoutrade-only launches it also offers to register a
    # remote qmt-proxy address. Non-interactive startups are never blocked.
    #
    # DOYOUTRADE_WEB_SETUP=1 (set by the double-click launcher, not by this
    # process) marks a GUI launch: the terminal prompt is skipped in favor of
    # the web console's own SetupWizard overlay (GET /setup/status +
    # POST /setup/complete, wired below in create_app / app.py).
    from doyoutrade.onboarding import maybe_run_setup_wizard

    web_setup = os.environ.get("DOYOUTRADE_WEB_SETUP") == "1"
    await maybe_run_setup_wizard(app.state.runtime, launch_mode=launch_mode, web_setup=web_setup)

    if launch_mode == "both":
        await _auto_wire_qmt_base_url(app.state.runtime, cfg.qmt_proxy)

    log_level = (cfg.observability.log_level or "info").lower()
    config = Config(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=log_level,
        log_config=None,
        timeout_graceful_shutdown=_DEFAULT_GRACEFUL_SHUTDOWN_S,
    )
    logger.info(
        "launch mode=%s: DoYouTrade serving on http://%s:%s",
        launch_mode,
        cfg.server.host,
        cfg.server.port,
    )
    server = Server(config)

    # Optional Windows system tray icon (double-click launch UX): no-op unless
    # sys.platform == win32 AND DOYOUTRADE_TRAY=1 (set by the launcher, not
    # here). "退出 DoYouTrade" sets server.should_exit, the same
    # graceful-shutdown trigger the update-restart hook below uses. Any
    # failure degrades to no tray icon; it must never take startup down.
    if launch_mode in ("doyoutrade", "both"):
        from doyoutrade.infra.tray_icon import maybe_start_tray_icon

        maybe_start_tray_icon(server, cfg.server.host, cfg.server.port)

    # Wire the self-updater's graceful-restart hook: an accepted
    # POST /update/apply drains the server, then (below) the process execs
    # into the reinstall-and-relaunch shell. Without this binding, apply is
    # refused with error_code=restart_unsupported.
    update_service = getattr(app.state, "update_service", None)
    if update_service is not None:
        def _request_restart() -> None:
            logger.info("update apply: requesting graceful server shutdown")
            server.should_exit = True

        update_service.bind_restart_requester(_request_restart)

    await server.serve()

    staged = update_service.staged_update if update_service is not None else None
    if staged is not None:
        from doyoutrade.infra.updater import exec_staged_update

        exec_staged_update(staged)  # never returns on success


def main():
    try:
        import uvicorn  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "uvicorn is not installed. Install fastapi and uvicorn first."
        ) from exc

    launch_mode, qmt_port_override = _resolve_launch_mode()
    cfg = get_config()
    log_level = (cfg.observability.log_level or "info").lower()

    def _qmt_settings():
        # Read the qmt_proxy config only for modes that need it, so a
        # doyoutrade-only launch never depends on that config section.
        settings = cfg.qmt_proxy
        if qmt_port_override is not None:
            from dataclasses import replace

            settings = replace(settings, port=qmt_port_override)
        return settings

    if launch_mode == "qmt-proxy":
        # Standalone bundled qmt-proxy: no DoYouTrade runtime, no wizard.
        from doyoutrade.infra.qmt_proxy_server import run_qmt_proxy_blocking

        run_qmt_proxy_blocking(_qmt_settings(), log_level=log_level)
        return

    if launch_mode == "both":
        # Start the embedded qmt-proxy in a daemon thread BEFORE building the
        # DoYouTrade runtime, so the auto-wired base_url is reachable by the time
        # the first cycle / data fetch runs. Import/dep failures raise here and
        # abort startup (visible), rather than silently degrading to doyoutrade-only.
        from doyoutrade.infra.qmt_proxy_server import start_embedded_qmt_proxy_thread

        start_embedded_qmt_proxy_thread(_qmt_settings(), log_level=log_level)

    asyncio.run(_serve_doyoutrade(launch_mode))


if __name__ == "__main__":  # pragma: no cover
    main()
