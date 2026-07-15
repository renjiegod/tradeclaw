"""Tests for the outbound push notification channels (email/wecom/dingtalk/telegram/slack).

All HTTP is stubbed by patching each channel module's ``http_post``; SMTP is
stubbed by replacing ``EmailChannel._smtp_send``. No network access.
"""
from __future__ import annotations

import unittest
from unittest import mock

from doyoutrade.assistant.channels._push_common import ChannelSendError
from doyoutrade.assistant.channels.base import ImageContent, TextContent
from doyoutrade.assistant.channels.dingtalk import DingtalkChannel
from doyoutrade.assistant.channels.email import EmailChannel
from doyoutrade.assistant.channels.slack import SlackChannel
from doyoutrade.assistant.channels.telegram import TelegramChannel
from doyoutrade.assistant.channels.wecom import WecomChannel

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def _ok_post(capture: list):
    async def _post(url, **kwargs):
        capture.append((url, kwargs))
        return True, 200, '{"errcode":0,"ok":true}'

    return _post


def _failing_post(status=500, body="boom"):
    async def _post(url, **kwargs):
        return False, status, body

    return _post


class EmailChannelTests(unittest.IsolatedAsyncioTestCase):
    def _channel(self, **overrides) -> EmailChannel:
        kwargs = dict(
            smtp_host="smtp.example.com",
            username="bot@example.com",
            password="secret",
            to_addrs=["ops@example.com"],
        )
        kwargs.update(overrides)
        return EmailChannel(None, **kwargs)

    async def test_not_configured(self):
        channel = self._channel(smtp_host="")
        with self.assertRaises(ChannelSendError) as ctx:
            await channel.send("sess", TextContent(text="hi"), {})
        self.assertEqual(ctx.exception.reason, "not_configured")

    async def test_no_recipients(self):
        channel = self._channel(to_addrs=[])
        with self.assertRaises(ChannelSendError) as ctx:
            await channel.send("sess", TextContent(text="hi"), {})
        self.assertEqual(ctx.exception.reason, "no_recipients")

    async def test_text_send_sets_subject_from_first_line(self):
        channel = self._channel()
        sent: list = []
        channel._smtp_send = mock.AsyncMock(side_effect=lambda m: sent.append(m))
        receipt = await channel.send("sess", TextContent(text="# 日报\n正文"), {})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["Subject"], "[Doyoutrade] 日报")
        self.assertEqual(sent[0]["To"], "ops@example.com")
        self.assertIsNotNone(receipt)

    async def test_meta_email_to_overrides_recipients(self):
        channel = self._channel()
        sent: list = []
        channel._smtp_send = mock.AsyncMock(side_effect=lambda m: sent.append(m))
        await channel.send("sess", TextContent(text="hi"), {"email_to": "a@x.com, b@x.com"})
        self.assertEqual(sent[0]["To"], "a@x.com, b@x.com")

    async def test_image_send_attaches_bytes(self):
        channel = self._channel()
        sent: list = []
        channel._smtp_send = mock.AsyncMock(side_effect=lambda m: sent.append(m))
        await channel.send(
            "sess",
            ImageContent(data=PNG_BYTES, mime_type="image/png", filename="r.png", caption="研报"),
            {},
        )
        msg = sent[0]
        attachments = [part for part in msg.iter_attachments()]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), "r.png")

    async def test_smtp_error_is_structured(self):
        channel = self._channel()
        channel._smtp_send = mock.AsyncMock(
            side_effect=ChannelSendError("email", "smtp_error", "ConnectionRefusedError: x")
        )
        with self.assertRaises(ChannelSendError) as ctx:
            await channel.send("sess", TextContent(text="hi"), {})
        self.assertEqual(ctx.exception.reason, "smtp_error")


class WecomChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_not_configured(self):
        channel = WecomChannel(None, webhook_url="")
        with self.assertRaises(ChannelSendError) as ctx:
            await channel.send("sess", TextContent(text="hi"), {})
        self.assertEqual(ctx.exception.reason, "not_configured")

    async def test_markdown_text_body(self):
        calls: list = []
        channel = WecomChannel(None, webhook_url="https://wecom.example/hook")
        with mock.patch(
            "doyoutrade.assistant.channels.wecom.channel.http_post", _ok_post(calls)
        ):
            await channel.send("sess", TextContent(text="**hi**", markdown=True), {})
        url, kwargs = calls[0]
        self.assertEqual(url, "https://wecom.example/hook")
        self.assertEqual(kwargs["json"]["msgtype"], "markdown")
        self.assertEqual(kwargs["json"]["markdown"]["content"], "**hi**")

    async def test_image_body_has_base64_and_md5(self):
        calls: list = []
        channel = WecomChannel(None, webhook_url="https://wecom.example/hook")
        with mock.patch(
            "doyoutrade.assistant.channels.wecom.channel.http_post", _ok_post(calls)
        ):
            await channel.send("sess", ImageContent(data=PNG_BYTES), {})
        body = calls[0][1]["json"]
        self.assertEqual(body["msgtype"], "image")
        self.assertTrue(body["image"]["base64"])
        self.assertEqual(len(body["image"]["md5"]), 32)

    async def test_http_error_raises(self):
        channel = WecomChannel(None, webhook_url="https://wecom.example/hook")
        with mock.patch(
            "doyoutrade.assistant.channels.wecom.channel.http_post", _failing_post()
        ):
            with self.assertRaises(ChannelSendError) as ctx:
                await channel.send("sess", TextContent(text="hi"), {})
        self.assertEqual(ctx.exception.reason, "http_error")

    async def test_nonzero_errcode_raises_api_error(self):
        async def _post(url, **kwargs):
            return True, 200, '{"errcode":93000,"errmsg":"invalid webhook"}'

        channel = WecomChannel(None, webhook_url="https://wecom.example/hook")
        with mock.patch("doyoutrade.assistant.channels.wecom.channel.http_post", _post):
            with self.assertRaises(ChannelSendError) as ctx:
                await channel.send("sess", TextContent(text="hi"), {})
        self.assertEqual(ctx.exception.reason, "api_error")


class DingtalkChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_url_appends_signature(self):
        channel = DingtalkChannel(
            None, webhook_url="https://oapi.dingtalk.com/robot/send?access_token=t", sign_secret="s3"
        )
        url = channel._signed_url()
        self.assertIn("&timestamp=", url)
        self.assertIn("&sign=", url)

    async def test_markdown_body_has_title(self):
        calls: list = []
        channel = DingtalkChannel(None, webhook_url="https://ding.example/hook")
        with mock.patch(
            "doyoutrade.assistant.channels.dingtalk.channel.http_post", _ok_post(calls)
        ):
            await channel.send("sess", TextContent(text="# 早报\n内容", markdown=True), {})
        body = calls[0][1]["json"]
        self.assertEqual(body["msgtype"], "markdown")
        self.assertEqual(body["markdown"]["title"], "早报")

    async def test_image_falls_back_to_caption(self):
        calls: list = []
        channel = DingtalkChannel(None, webhook_url="https://ding.example/hook", msg_type="text")
        with mock.patch(
            "doyoutrade.assistant.channels.dingtalk.channel.http_post", _ok_post(calls)
        ):
            await channel.send("sess", ImageContent(data=PNG_BYTES, caption="标题"), {})
        body = calls[0][1]["json"]
        self.assertEqual(body["msgtype"], "text")
        self.assertEqual(body["text"]["content"], "标题")

    async def test_image_without_caption_is_skipped(self):
        channel = DingtalkChannel(None, webhook_url="https://ding.example/hook")
        with mock.patch(
            "doyoutrade.assistant.channels.dingtalk.channel.http_post",
            mock.AsyncMock(side_effect=AssertionError("should not be called")),
        ):
            receipt = await channel.send("sess", ImageContent(data=PNG_BYTES), {})
        self.assertIsNone(receipt)


class TelegramChannelTests(unittest.IsolatedAsyncioTestCase):
    def _channel(self, **overrides) -> TelegramChannel:
        kwargs = dict(bot_token="123:abc", chat_id="-100200")
        kwargs.update(overrides)
        return TelegramChannel(None, **kwargs)

    async def test_not_configured(self):
        channel = self._channel(bot_token="")
        with self.assertRaises(ChannelSendError) as ctx:
            await channel.send("sess", TextContent(text="hi"), {})
        self.assertEqual(ctx.exception.reason, "not_configured")

    async def test_missing_chat_id(self):
        channel = self._channel(chat_id="")
        with self.assertRaises(ChannelSendError) as ctx:
            await channel.send("sess", TextContent(text="hi"), {})
        self.assertEqual(ctx.exception.reason, "no_chat_id")

    async def test_text_uses_send_message(self):
        calls: list = []
        channel = self._channel(message_thread_id="7")
        with mock.patch(
            "doyoutrade.assistant.channels.telegram.channel.http_post", _ok_post(calls)
        ):
            await channel.send("sess", TextContent(text="hi"), {})
        url, kwargs = calls[0]
        self.assertTrue(url.endswith("/bot123:abc/sendMessage"))
        self.assertEqual(kwargs["json"]["chat_id"], "-100200")
        self.assertEqual(kwargs["json"]["message_thread_id"], "7")
        self.assertEqual(kwargs["json"]["text"], "hi")

    async def test_image_uses_send_photo_multipart(self):
        calls: list = []
        channel = self._channel()
        with mock.patch(
            "doyoutrade.assistant.channels.telegram.channel.http_post", _ok_post(calls)
        ):
            await channel.send(
                "sess", ImageContent(data=PNG_BYTES, filename="r.png", caption="研报"), {}
            )
        url, kwargs = calls[0]
        self.assertTrue(url.endswith("/sendPhoto"))
        self.assertEqual(kwargs["data"]["caption"], "研报")
        self.assertEqual(kwargs["files"]["photo"][0], "r.png")

    async def test_api_ok_false_raises(self):
        async def _post(url, **kwargs):
            return True, 200, '{"ok":false,"description":"chat not found"}'

        channel = self._channel()
        with mock.patch("doyoutrade.assistant.channels.telegram.channel.http_post", _post):
            with self.assertRaises(ChannelSendError) as ctx:
                await channel.send("sess", TextContent(text="hi"), {})
        self.assertEqual(ctx.exception.reason, "api_error")

    async def test_image_without_data_falls_back_to_caption(self):
        calls: list = []
        channel = self._channel()
        with mock.patch(
            "doyoutrade.assistant.channels.telegram.channel.http_post", _ok_post(calls)
        ):
            await channel.send("sess", ImageContent(caption="仅文字"), {})
        url, kwargs = calls[0]
        self.assertTrue(url.endswith("/sendMessage"))
        self.assertEqual(kwargs["json"]["text"], "仅文字")


class SlackChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_not_configured(self):
        channel = SlackChannel(None)
        with self.assertRaises(ChannelSendError) as ctx:
            await channel.send("sess", TextContent(text="hi"), {})
        self.assertEqual(ctx.exception.reason, "not_configured")

    async def test_webhook_text(self):
        calls: list = []
        channel = SlackChannel(None, webhook_url="https://hooks.slack.com/x")
        with mock.patch(
            "doyoutrade.assistant.channels.slack.channel.http_post", _ok_post(calls)
        ):
            await channel.send("sess", TextContent(text="hi"), {})
        url, kwargs = calls[0]
        self.assertEqual(url, "https://hooks.slack.com/x")
        self.assertEqual(kwargs["json"], {"text": "hi"})

    async def test_bot_token_uses_post_message_with_auth(self):
        calls: list = []
        channel = SlackChannel(None, bot_token="xoxb-1", slack_channel_id="C123")
        with mock.patch(
            "doyoutrade.assistant.channels.slack.channel.http_post", _ok_post(calls)
        ):
            await channel.send("sess", TextContent(text="hi"), {})
        url, kwargs = calls[0]
        self.assertTrue(url.endswith("/chat.postMessage"))
        self.assertEqual(kwargs["json"]["channel"], "C123")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer xoxb-1")

    async def test_bot_token_without_channel_id_raises(self):
        channel = SlackChannel(None, bot_token="xoxb-1")
        with self.assertRaises(ChannelSendError) as ctx:
            await channel.send("sess", TextContent(text="hi"), {})
        self.assertEqual(ctx.exception.reason, "no_chat_id")

    async def test_image_falls_back_to_caption(self):
        calls: list = []
        channel = SlackChannel(None, webhook_url="https://hooks.slack.com/x")
        with mock.patch(
            "doyoutrade.assistant.channels.slack.channel.http_post", _ok_post(calls)
        ):
            await channel.send("sess", ImageContent(data=PNG_BYTES, caption="标题"), {})
        self.assertEqual(calls[0][1]["json"], {"text": "标题"})

    async def test_api_ok_false_raises(self):
        async def _post(url, **kwargs):
            return True, 200, '{"ok":false,"error":"channel_not_found"}'

        channel = SlackChannel(None, bot_token="xoxb-1", slack_channel_id="C123")
        with mock.patch("doyoutrade.assistant.channels.slack.channel.http_post", _post):
            with self.assertRaises(ChannelSendError) as ctx:
                await channel.send("sess", TextContent(text="hi"), {})
        self.assertEqual(ctx.exception.reason, "api_error")


if __name__ == "__main__":
    unittest.main()
