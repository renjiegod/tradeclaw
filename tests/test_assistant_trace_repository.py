from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from doyoutrade.assistant.repository import SqlAlchemyAssistantRepository
from doyoutrade.persistence.db import create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.models import Base, DebugSessionSpanRecord, ModelInvocationRecord


class AssistantTraceRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "assistant-trace.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}",
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_trace_detail_model_invocations_include_cache_tokens(self) -> None:
        trace_id = "a" * 32
        now = datetime(2026, 5, 2, 1, 2, 3, tzinfo=timezone.utc)
        async with self.session_factory() as session:
            session.add(
                DebugSessionSpanRecord(
                    span_id="b" * 16,
                    trace_id=trace_id,
                    parent_span_id=None,
                    session_id="session-1",
                    name="assistant.loop",
                    span_type="assistant",
                    start_time=now,
                    end_time=now,
                    duration_ms=1.0,
                    attributes={},
                    status="ok",
                    span_source="assistant",
                )
            )
            session.add(
                ModelInvocationRecord(
                    model_id="model-id",
                    provider_kind="anthropic",
                    model_route_name=None,
                    provider_key=None,
                    model="claude-test",
                    task_id=None,
                    run_id=None,
                    trace_id=trace_id,
                    span_id="b" * 16,
                    call_kind="assistant",
                    first_token_latency_ms=None,
                    total_latency_ms=5296,
                    input_tokens=285,
                    output_tokens=317,
                    total_tokens=602,
                    cache_read_tokens=12345,
                    cache_write_tokens=678,
                    ok=True,
                    error_message="",
                    request_payload={},
                    response_payload={},
                )
            )
            await session.commit()

        repo = SqlAlchemyAssistantRepository(self.session_factory)
        detail = await repo.get_trace_detail(trace_id)

        self.assertIsNotNone(detail)
        invocation = detail["model_invocations"][0]
        self.assertEqual(invocation["cache_read_tokens"], 12345)
        self.assertEqual(invocation["cache_write_tokens"], 678)
