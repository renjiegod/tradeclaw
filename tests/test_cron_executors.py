import unittest
from datetime import datetime, timezone

from doyoutrade.assistant.cron_executors.base import (
    JobExecutorRegistry,
    JobRunContext,
    PreActionResult,
)
from doyoutrade.assistant.cron_executors.noop import NoopExecutor


class JobExecutorRegistryTests(unittest.TestCase):
    def test_register_and_get(self):
        reg = JobExecutorRegistry()
        reg.register(NoopExecutor())
        self.assertIsNotNone(reg.get("noop"))

    def test_unknown_kind_returns_none(self):
        reg = JobExecutorRegistry()
        self.assertIsNone(reg.get("does_not_exist"))

    def test_known_kinds_returns_sorted_list(self):
        class _A:
            kind = "alpha"
            async def execute(self, params, ctx): ...
        class _Z:
            kind = "zeta"
            async def execute(self, params, ctx): ...
        reg = JobExecutorRegistry()
        reg.register(_Z())
        reg.register(_A())
        self.assertEqual(reg.known_kinds(), ["alpha", "zeta"])

    def test_register_replaces_existing(self):
        class _A:
            kind = "noop"
            async def execute(self, params, ctx): ...
        reg = JobExecutorRegistry()
        reg.register(NoopExecutor())
        first = reg.get("noop")
        reg.register(_A())
        second = reg.get("noop")
        self.assertIsNot(first, second)


class PreActionResultTests(unittest.TestCase):
    def test_defaults_empty_data_and_no_error(self):
        result = PreActionResult(status="ok")
        self.assertEqual(result.data, {})
        self.assertIsNone(result.error)
        self.assertIsNone(result.run_id)
        self.assertIsNone(result.debug_session_id)

    def test_data_default_is_independent_per_instance(self):
        a = PreActionResult(status="ok")
        b = PreActionResult(status="ok")
        a.data["x"] = 1
        self.assertEqual(b.data, {})


class NoopExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_ok_with_empty_data(self):
        ctx = JobRunContext(
            cron_job_run_id="r1",
            job_id="c1",
            fired_at=datetime.now(timezone.utc),
        )
        result = await NoopExecutor().execute({}, ctx)
        self.assertIsInstance(result, PreActionResult)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data, {})
        self.assertIsNone(result.error)

    async def test_ignores_params(self):
        # Should not raise even if params has bogus keys.
        ctx = JobRunContext(
            cron_job_run_id="r1",
            job_id="c1",
            fired_at=datetime.now(timezone.utc),
        )
        result = await NoopExecutor().execute({"bogus": "field"}, ctx)
        self.assertEqual(result.status, "ok")


if __name__ == "__main__":
    unittest.main()
