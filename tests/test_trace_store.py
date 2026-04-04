import tempfile
import unittest
from pathlib import Path

from tradeclaw.persistence.db import create_engine_and_session_factory, dispose_engine
from tradeclaw.persistence.models import Base
from tradeclaw.persistence.repositories import SqlAlchemyTraceEventRepository
from tradeclaw.persistence.trace_store import AsyncTraceStore, InMemoryTraceStore, TraceEvent


class TraceStoreTests(unittest.TestCase):
    def test_append_only_and_query_by_run(self):
        store = InMemoryTraceStore()

        store.append(run_id="run-1", phase="load_context", payload={"ok": True})
        store.append(run_id="run-1", phase="dispatch_orders", payload={"count": 1})
        store.append(run_id="run-2", phase="load_context", payload={"ok": True})

        run1_events = store.get_run_events("run-1")

        self.assertEqual(len(run1_events), 2)
        self.assertEqual(run1_events[0].sequence, 1)
        self.assertEqual(run1_events[1].sequence, 2)


class AsyncTraceStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_async_trace_store_persists_and_loads_run_events(self):
        store = AsyncTraceStore(SqlAlchemyTraceEventRepository(self.session_factory))

        first = await store.append("run-1", "load_context", {"ok": True})
        second = await store.append("run-1", "dispatch_orders", {"count": 1})
        other = await store.append("run-2", "load_context", {"ok": True})
        run1_events = await store.get_run_events("run-1")

        self.assertIsInstance(first, TraceEvent)
        self.assertIsInstance(run1_events[0], TraceEvent)
        self.assertEqual(first.sequence, 1)
        self.assertEqual(second.sequence, 2)
        self.assertEqual(other.sequence, 1)
        self.assertIsInstance(first.timestamp, str)
        self.assertEqual([event.phase for event in run1_events], ["load_context", "dispatch_orders"])


if __name__ == "__main__":
    unittest.main()
