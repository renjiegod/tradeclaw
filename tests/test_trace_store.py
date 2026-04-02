import unittest

from tradeclaw.persistence.trace_store import InMemoryTraceStore


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


if __name__ == "__main__":
    unittest.main()
