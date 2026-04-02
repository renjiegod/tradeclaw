from __future__ import annotations

from tradeclaw.api.app import create_app
from tradeclaw.api.runtime_loop import RuntimeTickLoop
from tradeclaw.bootstrap import build_platform_runtime
from tradeclaw.config import get_config


def build_api_with_runtime(tick_interval_seconds: float | None = None):
    runtime = build_platform_runtime()
    service = runtime["service"]
    approval_gate = runtime["approval_gate"]
    app = create_app(service, approval_gate)

    cfg = get_config()
    interval = tick_interval_seconds if tick_interval_seconds is not None else cfg.server.tick_seconds

    loop = RuntimeTickLoop(service=service, approval_gate=approval_gate, interval_seconds=interval)
    app.state.runtime_loop = loop

    @app.on_event("startup")
    async def _on_startup():
        loop.start()

    @app.on_event("shutdown")
    async def _on_shutdown():
        await loop.stop()
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

    app = build_api_with_runtime()
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port)


if __name__ == "__main__":  # pragma: no cover
    main()
