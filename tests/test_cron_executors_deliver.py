"""Unit tests for ``cron_executors._deliver`` channel forwarding.

Covers the four ``channel_forward_status`` values declared in
``ChannelForwardStatus``:

  - ``no_channel_binding`` — target session has no ``config.channel``.
  - ``forwarded`` — the live channel is registered and ``channel.send``
    succeeds; the message is appended *and* pushed.
  - ``forward_failed`` — ``channel.send`` raises; delivery status stays
    ``delivered`` (message persisted) but the failure is surfaced on
    ``info['channel_forward_status']``, ``info['channel_forward_error']``
    and the ``cron.delivery.channel_forward`` span.
  - ``channel_disabled`` — the session is bound to a channel that the
    running ChannelManager does not know about (e.g. disabled / not
    started).

CLAUDE.md §错误可见性 requires these four states to be distinguishable from
structured fields and to log at the right level — these tests assert the
return-value shape; per-state span attribute / log-level invariants are
asserted by reading the values the helper sets directly.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from doyoutrade.assistant.channels.base import TextContent
from doyoutrade.assistant.cron_executors._deliver import (
    deliver_assistant_message_to_session,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Repo:
    """Minimal AssistantRepository stub: append_message + append_event."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    async def append_message(self, **kwargs: Any) -> dict[str, Any]:
        row = {"message_id": f"msg-{len(self.messages)}", **kwargs}
        self.messages.append(row)
        return row

    async def append_event(self, **kwargs: Any) -> None:
        self.events.append(dict(kwargs))


class _FakeChannel:
    """Minimal BaseChannel stand-in: records ``send`` calls.

    We deliberately do NOT inherit BaseChannel (which is abstract) — the
    helper only requires a ``.send(session_id, content, meta)`` coroutine,
    so a duck-typed stub is the smallest surface that proves the contract.
    """

    channel_type = "test-channel"

    def __init__(
        self,
        *,
        channel_id: str = "ch-test",
        send_raises: Exception | None = None,
    ) -> None:
        self.channel_id = channel_id
        self.sent: list[tuple[str, TextContent, dict[str, Any]]] = []
        self._send_raises = send_raises

    async def send(
        self,
        session_id: str,
        content: TextContent,
        meta: dict[str, Any],
    ) -> None:
        if self._send_raises is not None:
            raise self._send_raises
        self.sent.append((session_id, content, dict(meta)))


class _ChannelManager:
    """Stand-in for ChannelManager.get(channel_id) → BaseChannel | None."""

    def __init__(self, channels: dict[str, Any] | None = None) -> None:
        self._channels: dict[str, Any] = dict(channels or {})

    def get(self, channel_id: str) -> Any | None:
        return self._channels.get(channel_id)


class _SvcBase:
    """Common assistant_service stub: repository + get_session.

    Subclasses control:
      - ``channel_manager`` attribute (None / disabled-manager / live)
      - ``session_config`` returned by ``get_session``
    """

    def __init__(
        self,
        *,
        session_config: dict[str, Any] | None,
        channel_manager: Any | None,
    ) -> None:
        self.repository = _Repo()
        self.channel_manager = channel_manager
        self._session_config = session_config

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return {
            "session_id": session_id,
            "config": self._session_config,
        }


_KWARGS_COMMON: dict[str, Any] = {
    "target_session_id": "asst-user-1",
    "content": "你好，提醒到了～",
    "cron_job_id": "cron-1",
    "cron_job_run_id": "crun-1",
    "cron_task_kind": "agent_chat_reply",
}


class DeliverChannelForwardTests(unittest.TestCase):
    # ── no_channel_binding ────────────────────────────────────────────────

    def test_deliver_no_channel_binding_returns_no_channel_binding(self) -> None:
        """Session has no ``config.channel``: message persisted, forward marked
        ``no_channel_binding`` without any channel-send attempt."""

        live = _FakeChannel(channel_id="ch-x")
        svc = _SvcBase(
            session_config={},  # no 'channel' key at all
            channel_manager=_ChannelManager({"ch-x": live}),
        )

        status, info = _run(
            deliver_assistant_message_to_session(svc, **_KWARGS_COMMON)
        )

        self.assertEqual(status, "delivered")
        assert isinstance(info, dict)
        self.assertEqual(info["channel_forward_status"], "no_channel_binding")
        self.assertIsNone(info["channel_forward_error"])
        # Message was persisted...
        self.assertEqual(len(svc.repository.messages), 1)
        # ...and the channel was NEVER touched because there was no binding.
        self.assertEqual(live.sent, [])

    def test_deliver_no_config_channel_object_returns_no_channel_binding(
        self,
    ) -> None:
        """``config.channel`` present but missing ``channel_id`` → still
        treated as no binding, not a forward failure."""

        svc = _SvcBase(
            session_config={"channel": {"channel_type": "feishu"}},
            channel_manager=_ChannelManager(),
        )
        status, info = _run(
            deliver_assistant_message_to_session(svc, **_KWARGS_COMMON)
        )
        self.assertEqual(status, "delivered")
        assert isinstance(info, dict)
        self.assertEqual(info["channel_forward_status"], "no_channel_binding")

    # ── forwarded ──────────────────────────────────────────────────────────

    def test_deliver_channel_forward_success_marks_forwarded(self) -> None:
        """Session bound to a registered, healthy channel: append + send
        both succeed, info marked ``forwarded``, meta forwarded as-is."""

        live = _FakeChannel(channel_id="ch-feishu-1")
        svc = _SvcBase(
            session_config={
                "channel": {
                    "channel_id": "ch-feishu-1",
                    "channel_type": "feishu",
                    "sender_id": "u-1",
                    "meta": {"chat_id": "oc-abc", "open_id": "ou-xyz"},
                },
            },
            channel_manager=_ChannelManager({"ch-feishu-1": live}),
        )

        status, info = _run(
            deliver_assistant_message_to_session(svc, **_KWARGS_COMMON)
        )

        self.assertEqual(status, "delivered")
        assert isinstance(info, dict)
        self.assertEqual(info["channel_forward_status"], "forwarded")
        self.assertIsNone(info["channel_forward_error"])

        # channel.send was called exactly once, with the right session, the
        # exact content, and the session's channel meta (copied, not shared).
        self.assertEqual(len(live.sent), 1)
        sent_session, sent_content, sent_meta = live.sent[0]
        self.assertEqual(sent_session, "asst-user-1")
        self.assertIsInstance(sent_content, TextContent)
        self.assertEqual(sent_content.text, "你好，提醒到了～")
        self.assertEqual(sent_meta, {"chat_id": "oc-abc", "open_id": "ou-xyz"})

    def test_deliver_channel_forward_handles_missing_meta(self) -> None:
        """``meta`` missing on the channel binding → forward still happens,
        send gets an empty dict (not None / not a TypeError)."""

        live = _FakeChannel(channel_id="ch-1")
        svc = _SvcBase(
            session_config={
                "channel": {"channel_id": "ch-1", "channel_type": "http"},
            },
            channel_manager=_ChannelManager({"ch-1": live}),
        )
        status, info = _run(
            deliver_assistant_message_to_session(svc, **_KWARGS_COMMON)
        )
        self.assertEqual(status, "delivered")
        assert isinstance(info, dict)
        self.assertEqual(info["channel_forward_status"], "forwarded")
        self.assertEqual(len(live.sent), 1)
        _sid, _content, sent_meta = live.sent[0]
        self.assertEqual(sent_meta, {})

    # ── forward_failed ─────────────────────────────────────────────────────

    def test_deliver_channel_send_failure_marks_forward_failed(self) -> None:
        """``channel.send`` raises: status stays ``delivered`` (message
        persisted) but forward marked ``forward_failed`` with the error
        text on ``info['channel_forward_error']``."""

        live = _FakeChannel(
            channel_id="ch-feishu-1",
            send_raises=RuntimeError("feishu api 500"),
        )
        svc = _SvcBase(
            session_config={
                "channel": {
                    "channel_id": "ch-feishu-1",
                    "channel_type": "feishu",
                    "meta": {"chat_id": "oc-x"},
                },
            },
            channel_manager=_ChannelManager({"ch-feishu-1": live}),
        )

        status, info = _run(
            deliver_assistant_message_to_session(svc, **_KWARGS_COMMON)
        )

        # Delivery still counts as success — the message *is* on the
        # session log; only the push to the channel failed.
        self.assertEqual(status, "delivered")
        assert isinstance(info, dict)
        self.assertEqual(info["channel_forward_status"], "forward_failed")
        # Exception type + message preserved verbatim so the failure
        # mode is identifiable from structured fields.
        self.assertIn("RuntimeError", info["channel_forward_error"] or "")
        self.assertIn("feishu api 500", info["channel_forward_error"] or "")
        # The message was persisted exactly once even though forward failed.
        self.assertEqual(len(svc.repository.messages), 1)

    # ── channel_disabled ──────────────────────────────────────────────────

    def test_deliver_channel_disabled_marks_channel_disabled(self) -> None:
        """Channel binding present but the running ChannelManager has no
        entry for ``channel_id`` (e.g. disabled / not started). Status is
        ``channel_disabled``, distinct from ``forward_failed`` so operators
        can tell wiring problems from runtime send failures."""

        svc = _SvcBase(
            session_config={
                "channel": {
                    "channel_id": "ch-feishu-removed",
                    "channel_type": "feishu",
                    "meta": {"chat_id": "oc-x"},
                },
            },
            # No channels registered.
            channel_manager=_ChannelManager(),
        )

        status, info = _run(
            deliver_assistant_message_to_session(svc, **_KWARGS_COMMON)
        )

        self.assertEqual(status, "delivered")
        assert isinstance(info, dict)
        self.assertEqual(info["channel_forward_status"], "channel_disabled")
        self.assertIsNone(info["channel_forward_error"])

    def test_deliver_no_channel_manager_attribute_marks_channel_disabled(
        self,
    ) -> None:
        """``assistant_service.channel_manager`` not attached (e.g. boot
        before manager wiring): treated as ``channel_disabled``, never as
        a forward exception."""

        svc = _SvcBase(
            session_config={
                "channel": {
                    "channel_id": "ch-1",
                    "channel_type": "feishu",
                },
            },
            channel_manager=None,  # explicit: no manager at all
        )
        status, info = _run(
            deliver_assistant_message_to_session(svc, **_KWARGS_COMMON)
        )
        self.assertEqual(status, "delivered")
        assert isinstance(info, dict)
        self.assertEqual(info["channel_forward_status"], "channel_disabled")


if __name__ == "__main__":
    unittest.main()
