import io
import asyncio
import json
import unittest
from unittest.mock import MagicMock

from doyoutrade.observability import (
    get_logger,
    get_tracer,
    initialize_observability,
    reset_observability,
)
from opentelemetry.trace import StatusCode

from doyoutrade.observability.debug_span_export import (
    _DatabaseSpanExporter,
    drain_debug_span_persist_queue,
    register_span_persist_sink,
    start_debug_span_persist_worker,
)


class ObservabilityTests(unittest.TestCase):
    def tearDown(self):
        reset_observability()

    def test_log_records_include_trace_id_inside_span(self):
        stream = io.StringIO()
        initialize_observability(service_name="doyoutrade-test", stream=stream)
        logger = get_logger("tests.observability")
        tracer = get_tracer("tests.observability")

        with tracer.start_as_current_span("sample"):
            logger.info("hello from span")

        output = stream.getvalue()
        self.assertIn("trace_id=", output)
        self.assertNotIn("span_id=", output)
        self.assertNotIn("trace_id=-", output)
        self.assertIn("hello from span", output)

    def test_initialize_observability_is_idempotent(self):
        stream = io.StringIO()
        initialize_observability(service_name="doyoutrade-test", stream=stream)
        initialize_observability(service_name="doyoutrade-test", stream=stream)

        logger = get_logger("tests.observability")
        logger.info("only once")

        self.assertEqual(stream.getvalue().count("only once"), 1)

    def test_logs_outside_span_render_placeholders(self):
        stream = io.StringIO()
        initialize_observability(service_name="doyoutrade-test", stream=stream)

        logger = get_logger("tests.observability")
        logger.info("outside span")

        output = stream.getvalue()
        self.assertIn("trace_id=-", output)
        self.assertNotIn("span_id=", output)

    def test_debug_span_exporter_persists_status_message_on_error(self):
        ro = MagicMock()
        ro.attributes = {
            "doyoutrade.session_id": "sess-1",
            "doyoutrade.span_source": "backtest",
            "doyoutrade.span_type": "signal_turn",
            "error": json.dumps({"code": "chat_ainvoke_failed", "message": "boom"}),
        }
        ro.context.trace_id = 0x11111111111111111111111111111111
        ro.context.span_id = 0x2222222222222222
        ro.parent = None
        ro.status.status_code = StatusCode.ERROR
        ro.status.description = "human-readable-desc"
        ro.name = "signal_turn"
        ro.start_time = 1_000_000_000
        ro.end_time = 2_000_000_000
        ro.events = []

        rows: list[dict] = []
        register_span_persist_sink(lambda r: rows.append(dict(r)))
        try:
            _DatabaseSpanExporter().export([ro])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "error")
            self.assertEqual(rows[0]["attributes"].get("span_status_message"), "human-readable-desc")
            err = rows[0]["attributes"].get("error")
            self.assertIsInstance(err, dict)
            self.assertEqual(err.get("code"), "chat_ainvoke_failed")
        finally:
            register_span_persist_sink(None)

    def test_drain_debug_span_queue_ignores_stale_cancelled_worker(self):
        async def _run():
            async def _append(**_row):
                return None

            await start_debug_span_persist_worker(_append)

            import doyoutrade.observability.debug_span_export as debug_export

            assert debug_export._persist_queue is not None
            assert debug_export._persist_worker_task is not None
            debug_export._persist_worker_task.cancel()
            try:
                await debug_export._persist_worker_task
            except asyncio.CancelledError:
                pass

            debug_export._persist_queue.put_nowait({"span_id": "orphaned"})

            await asyncio.wait_for(drain_debug_span_persist_queue(), timeout=0.2)
            self.assertIsNone(debug_export._persist_queue)
            self.assertIsNone(debug_export._persist_worker_task)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
