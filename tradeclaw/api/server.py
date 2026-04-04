from __future__ import annotations

import asyncio

from tradeclaw.api.app import create_app
from tradeclaw.api.runtime_loop import RuntimeTickLoop
from tradeclaw.bootstrap import build_platform_runtime
from tradeclaw.config import get_config
from tradeclaw.observability import initialize_observability


async def build_api_with_runtime(tick_interval_seconds: float | None = None):
    cfg = get_config()
    runtime = await build_platform_runtime(app_cfg=cfg)
    service = runtime["service"]
    approval_gate = runtime["approval_gate"]
    app = create_app(service, approval_gate)
    initialize_observability(
        service_name=cfg.observability.service_name,
        log_level=cfg.observability.log_level,
        tracing_enabled=cfg.observability.tracing_enabled,
        console_enabled=cfg.observability.console_enabled,
        app=app,
    )
    interval = tick_interval_seconds if tick_interval_seconds is not None else cfg.server.tick_seconds

    loop = RuntimeTickLoop(service=service, approval_gate=approval_gate, interval_seconds=interval)
    app.state.runtime = runtime
    app.state.runtime_loop = loop

    @app.on_event("startup")
    async def _on_startup():
        loop.start()

    @app.on_event("shutdown")
    async def _on_shutdown():
        await loop.stop()
        close_runtime = runtime.get("aclose")
        if close_runtime is not None:
            await close_runtime()
            return
        close = getattr(service, "aclose", None)
        if close is not None:
            await close()

    return app


def main():
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("uvicorn is not installed. Install fastapi and uvicorn first.") from exc

    cfg = get_config()

    app = asyncio.run(build_api_with_runtime())
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port)


if __name__ == "__main__":  # pragma: no cover
    main()
