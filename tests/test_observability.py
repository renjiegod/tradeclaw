import io
import unittest

from tradeclaw.observability import (
    get_logger,
    get_tracer,
    initialize_observability,
    reset_observability,
)


class ObservabilityTests(unittest.TestCase):
    def tearDown(self):
        reset_observability()

    def test_log_records_include_trace_and_span_ids_inside_span(self):
        stream = io.StringIO()
        initialize_observability(service_name="tradeclaw-test", stream=stream)
        logger = get_logger("tests.observability")
        tracer = get_tracer("tests.observability")

        with tracer.start_as_current_span("sample"):
            logger.info("hello from span")

        output = stream.getvalue()
        self.assertIn("trace_id=", output)
        self.assertIn("span_id=", output)
        self.assertNotIn("trace_id=-", output)
        self.assertNotIn("span_id=-", output)
        self.assertIn("hello from span", output)

    def test_initialize_observability_is_idempotent(self):
        stream = io.StringIO()
        initialize_observability(service_name="tradeclaw-test", stream=stream)
        initialize_observability(service_name="tradeclaw-test", stream=stream)

        logger = get_logger("tests.observability")
        logger.info("only once")

        self.assertEqual(stream.getvalue().count("only once"), 1)

    def test_logs_outside_span_render_placeholders(self):
        stream = io.StringIO()
        initialize_observability(service_name="tradeclaw-test", stream=stream)

        logger = get_logger("tests.observability")
        logger.info("outside span")

        output = stream.getvalue()
        self.assertIn("trace_id=-", output)
        self.assertIn("span_id=-", output)


if __name__ == "__main__":
    unittest.main()
