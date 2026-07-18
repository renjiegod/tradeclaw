"""QuestionBroker unit tests — the future-broker skeleton behind fizz-style
blocking ask_user_question waits (mirrors the ApprovalBroker contract)."""

import asyncio
import unittest

from doyoutrade.assistant.questions import (
    QuestionBroker,
    QuestionResolution,
)


def _create(broker: QuestionBroker, question_id: str = "uq-1", **overrides):
    params = dict(
        question_id=question_id,
        session_id="asst-1",
        attempt_id="attempt-1",
        run_id="run-1",
        question="回测区间用哪个？",
        header="回测区间",
        options=[{"label": "近一年"}, {"label": "近三年"}],
        multi_select=False,
    )
    params.update(overrides)
    return broker.create(**params)


class QuestionBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_list_and_payload(self):
        broker = QuestionBroker()
        request = _create(broker)
        self.assertEqual(request.question_id, "uq-1")
        pending = broker.list_pending("asst-1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["question_id"], "uq-1")
        self.assertEqual(pending[0]["options"], [{"label": "近一年"}, {"label": "近三年"}])
        # Session scoping.
        self.assertEqual(broker.list_pending("other"), [])

    async def test_wait_returns_resolution(self):
        broker = QuestionBroker()
        request = _create(broker)
        waiter = asyncio.create_task(request.wait())
        await asyncio.sleep(0)  # let the waiter suspend
        accepted = broker.resolve(
            "uq-1", selected=["近一年"], custom="", source="option_click"
        )
        self.assertTrue(accepted)
        resolution = await waiter
        self.assertEqual(resolution.selected, ("近一年",))
        self.assertEqual(resolution.source, "option_click")
        self.assertFalse(resolution.timed_out)
        # Once resolved, list_pending drops it.
        self.assertEqual(broker.list_pending("asst-1"), [])

    async def test_double_resolve_is_rejected(self):
        broker = QuestionBroker()
        request = _create(broker)
        waiter = asyncio.create_task(request.wait())
        await asyncio.sleep(0)
        self.assertTrue(broker.resolve("uq-1", selected=["近一年"], source="option_click"))
        self.assertFalse(broker.resolve("uq-1", selected=["近三年"], source="option_click"))
        resolution = await waiter
        self.assertEqual(resolution.selected, ("近一年",))

    async def test_resolve_unknown_id_returns_false(self):
        broker = QuestionBroker()
        self.assertFalse(broker.resolve("uq-missing", selected=["x"], source="web"))

    async def test_wait_times_out(self):
        broker = QuestionBroker()
        request = _create(broker, timeout_seconds=0.05)
        resolution = await request.wait()
        self.assertTrue(resolution.timed_out)
        self.assertEqual(resolution.source, "timeout")
        # A resolve arriving after timeout is rejected (future already done).
        self.assertFalse(broker.resolve("uq-1", selected=["近一年"], source="web"))

    async def test_custom_only_resolution(self):
        broker = QuestionBroker()
        request = _create(broker)
        waiter = asyncio.create_task(request.wait())
        await asyncio.sleep(0)
        broker.resolve("uq-1", selected=[], custom="我自己写的答案", source="free_text")
        resolution = await waiter
        self.assertEqual(resolution.selected, ())
        self.assertEqual(resolution.custom, "我自己写的答案")
        self.assertFalse(resolution.is_empty())

    async def test_discard_removes_pending(self):
        broker = QuestionBroker()
        _create(broker)
        broker.discard("uq-1")
        self.assertEqual(broker.list_pending("asst-1"), [])
        self.assertIsNone(broker.get("uq-1"))

    def test_resolution_is_empty(self):
        self.assertTrue(QuestionResolution().is_empty())
        self.assertTrue(QuestionResolution(custom="   ").is_empty())
        self.assertFalse(QuestionResolution(selected=("A",)).is_empty())


if __name__ == "__main__":
    unittest.main()
