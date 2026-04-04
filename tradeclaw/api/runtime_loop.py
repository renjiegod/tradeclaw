from __future__ import annotations

import asyncio
import contextlib

from tradeclaw.observability import get_logger, get_tracer


logger = get_logger(__name__)
tracer = get_tracer(__name__)


class RuntimeTickLoop:
    """Background loop that advances running instances at a fixed cadence."""

    def __init__(self, service, approval_gate, interval_seconds: float = 5.0):
        self.service = service
        self.approval_gate = approval_gate
        self.interval_seconds = max(0.1, float(interval_seconds))
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="tradeclaw-runtime-loop")

    async def stop(self):
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self):
        while True:
            try:
                with tracer.start_as_current_span("runtime.tick"):
                    try:
                        logger.info("runtime tick started interval_seconds=%.2f", self.interval_seconds)
                        executed = await self.service.tick_once()
                        expired = []
                        if hasattr(self.approval_gate, "expire_pending"):
                            expired = await self.approval_gate.expire_pending()
                        logger.info(
                            "runtime tick completed executed=%s expired_count=%s",
                            executed,
                            len(expired),
                        )
                    except Exception:  # pragma: no cover - defensive loop logging
                        logger.exception("runtime tick failed")
                        raise
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive loop logging
                await asyncio.sleep(self.interval_seconds)
