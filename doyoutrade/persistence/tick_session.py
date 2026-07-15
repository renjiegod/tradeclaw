from __future__ import annotations

import uuid
from datetime import datetime


class TickSessionRepository:
    """Factory for creating scheduled and manual tick sessions.

    Uses DebugSessionRepository for persistence but provides
    session-type-aware factory methods.

    Span export for tick runs is handled by ``TradingPlatformService.tick_once``
    via ``debug_span_export_for_session`` (OpenTelemetry exporter → DB).
    """

    def __init__(self, debug_session_repo, debug_session_span_repo, task_repository):
        self._session_repo = debug_session_repo
        self._span_repo = debug_session_span_repo
        self._task_repository = task_repository

    async def get_or_create_scheduled_session(self, task_id: str):
        """Get existing running scheduled session for task, or create new one."""
        existing = await self._session_repo.get_active_session(task_id)
        if existing is not None and existing.session_type == "scheduled":
            return existing

        session_id = f"scheduled-{task_id}"
        return await self._session_repo.create_session(
            session_id=session_id,
            task_id=task_id,
            config_overrides=None,
            input_overrides=None,
            session_type="scheduled",
        )

    async def create_manual_session(self, task_id: str):
        """Create a new manual session for a single tick."""
        session_id = f"manual-{uuid.uuid4()}"
        return await self._session_repo.create_session(
            session_id=session_id,
            task_id=task_id,
            config_overrides=None,
            input_overrides=None,
            session_type="manual",
        )

    async def create_cron_session(self, task_id: str):
        """Create a new cron-fired session for a single tick."""
        session_id = f"cron-{uuid.uuid4()}"
        return await self._session_repo.create_session(
            session_id=session_id,
            task_id=task_id,
            config_overrides=None,
            input_overrides=None,
            session_type="cron",
        )

    async def create_trigger_session(self, task_id: str):
        """Create a new Trigger-fired session for a single tick (run_kind='trigger')."""
        session_id = f"trigger-{uuid.uuid4()}"
        return await self._session_repo.create_session(
            session_id=session_id,
            task_id=task_id,
            config_overrides=None,
            input_overrides=None,
            session_type="trigger",
        )

    async def get_latest_for_task(
        self,
        task_id: str,
        *,
        created_after: datetime | None = None,
    ):
        """Return the most recently created session for ``task_id``.

        Used by cron executors to surface the run_id of the just-completed tick_once cycle.
        Pass ``created_after`` to ignore sessions older than the moment cron fired —
        important when ``tick_once`` may have no-op'd (kill switch on, instance not running).
        """
        return await self._session_repo.get_latest_session(task_id, created_after=created_after)
