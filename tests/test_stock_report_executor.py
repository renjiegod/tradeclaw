"""``StockReportExecutor`` — the 模板化个股研报 cron task executor.

Covers validate_params, the happy text path (gather → render → deliver →
KB write-back), the as_image path (image via bound channel), the md2img
fallback to text, single-symbol failure continuation, and the all-symbols
failure. Delivery is patched; bars come from injected fakes; the journal
write goes to an isolated ``DOYOUTRADE_HOME``.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from doyoutrade.assistant.cron_executors.base import JobRunContext
from doyoutrade.assistant.cron_executors.stock_report import (
    StockReportExecutor,
    build_report_item,
)
from doyoutrade.assistant.rendering.md2img import Md2ImgResult

_FIRED_AT = datetime(2026, 6, 17, 7, 30, tzinfo=timezone.utc)  # 15:30 Asia/Shanghai
_CTX = JobRunContext(cron_job_run_id="crun-1", job_id="task-1", fired_at=_FIRED_AT)


class _Bar:
    def __init__(self, close):
        self.close = close


def _bars(n=60, start=10.0, step=0.1):
    return [_Bar(start + i * step) for i in range(n)]


def _make_bars_provider(per_symbol):
    """per_symbol: symbol -> list[_Bar] | Exception."""

    async def _provider(symbol, start_iso, end_iso):
        value = per_symbol[symbol]
        if isinstance(value, Exception):
            raise value
        return value

    return _provider


class _Channel:
    channel_type = "feishu"

    def __init__(self, exc=None):
        self._exc = exc
        self.sent = []

    async def send(self, session_id, content, meta):
        if self._exc is not None:
            raise self._exc
        self.sent.append((session_id, content, meta))


class _ChannelManager:
    def __init__(self, channel):
        self._channel = channel

    def get(self, channel_id):
        return self._channel


class _Svc:
    def __init__(self, channel=None, session_config=None):
        self.channel_manager = _ChannelManager(channel) if channel else None
        self._session_config = session_config if session_config is not None else {
            "channel": {"channel_id": "ch-1", "channel_type": "feishu", "meta": {"chat_id": "c1"}}
        }

    async def get_session(self, session_id):
        return {"session_id": session_id, "config": self._session_config}


class _CronRepo:
    async def get_job(self, jid):
        return {"id": jid, "name": "每日研报"}


def _patch_deliver(status="delivered", info=None):
    async def _fake(svc, *, target_session_id, content, cron_job_id, cron_job_run_id, cron_task_kind):
        _fake.content = content
        _fake.target = target_session_id
        _fake.called = True
        return (status, info or {})

    _fake.called = False
    return mock.patch(
        "doyoutrade.assistant.cron_executors.stock_report.deliver_assistant_message_to_session",
        _fake,
    ), _fake


def _patch_md2img(result):
    async def _fake(markdown_text, **kwargs):
        _fake.markdown = markdown_text
        return result

    return mock.patch(
        "doyoutrade.assistant.cron_executors.stock_report.render_markdown_to_image",
        _fake,
    ), _fake


class StockReportValidateTests(unittest.TestCase):
    def setUp(self):
        self.ex = StockReportExecutor(
            assistant_service=_Svc(),
            cron_job_repository=_CronRepo(),
            bars_provider=_make_bars_provider({}),
        )

    def test_non_dict_params(self):
        self.assertEqual(self.ex.validate_params([])["error_code"], "invalid_task_params")

    def test_missing_symbols(self):
        self.assertEqual(self.ex.validate_params({})["error_code"], "invalid_symbols")

    def test_empty_symbols(self):
        err = self.ex.validate_params({"symbols": []})
        self.assertEqual(err["error_code"], "invalid_symbols")

    def test_non_string_symbol(self):
        err = self.ex.validate_params({"symbols": ["600519.SH", 42]})
        self.assertEqual(err["error_code"], "invalid_symbols")

    def test_invalid_language(self):
        err = self.ex.validate_params({"symbols": ["600519.SH"], "language": "fr"})
        self.assertEqual(err["error_code"], "invalid_language")

    def test_invalid_as_image(self):
        err = self.ex.validate_params({"symbols": ["600519.SH"], "as_image": "yes"})
        self.assertEqual(err["error_code"], "invalid_as_image")

    def test_invalid_title(self):
        err = self.ex.validate_params({"symbols": ["600519.SH"], "title": 42})
        self.assertEqual(err["error_code"], "invalid_title")

    def test_invalid_target(self):
        err = self.ex.validate_params({"symbols": ["600519.SH"], "target_session_id": 5})
        self.assertEqual(err["error_code"], "invalid_target_session_id")

    def test_ok(self):
        self.assertIsNone(
            self.ex.validate_params(
                {"symbols": ["600519.SH"], "language": "en", "as_image": True}
            )
        )


class BuildReportItemTests(unittest.TestCase):
    def test_uptrend_scores_buy(self):
        item = build_report_item("600519.SH", _bars(60, 10.0, 0.1), language="zh")
        self.assertEqual(item.action, "buy")
        self.assertGreaterEqual(item.score, 65)
        self.assertIn("MA20", item.key_indicators)
        self.assertIn("RSI14", item.key_indicators)

    def test_downtrend_scores_sell(self):
        item = build_report_item("600519.SH", _bars(60, 20.0, -0.1), language="zh")
        self.assertEqual(item.action, "sell")
        self.assertLessEqual(item.score, 35)

    def test_insufficient_bars_raises(self):
        with self.assertRaises(ValueError):
            build_report_item("600519.SH", _bars(5), language="zh")


class StockReportRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._prev_home = os.environ.get("DOYOUTRADE_HOME")
        self._tmp = tempfile.mkdtemp()
        os.environ["DOYOUTRADE_HOME"] = self._tmp
        self.kb = Path(self._tmp) / "knowledge"

    def tearDown(self):
        if self._prev_home is None:
            os.environ.pop("DOYOUTRADE_HOME", None)
        else:
            os.environ["DOYOUTRADE_HOME"] = self._prev_home

    def _executor(self, per_symbol, svc=None):
        return StockReportExecutor(
            assistant_service=svc or _Svc(),
            cron_job_repository=_CronRepo(),
            bars_provider=_make_bars_provider(per_symbol),
        )

    async def test_text_happy_path(self):
        patch, fake = _patch_deliver("delivered")
        ex = self._executor({"600519.SH": _bars(), "000001.SZ": _bars(60, 5.0, 0.05)})
        with patch:
            r = await ex.run(
                {"symbols": ["600519.SH", "000001.SZ"], "target_session_id": "s1"},
                _CTX,
            )
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.delivery_status, "delivered")
        self.assertEqual(r.data["delivery_mode"], "text")
        self.assertEqual(r.data["failed_symbols"], [])
        self.assertFalse(r.data["image_ok"])
        # Delivered markdown contains both symbols.
        self.assertIn("600519.SH", fake.content)
        self.assertIn("000001.SZ", fake.content)
        # KB write-back to reports/<YYYY>/<date>-<slug>.md.
        self.assertTrue(str(r.data["report_path"]).startswith("reports/2026/2026-06-17-"))
        body = (self.kb / r.data["report_path"]).read_text(encoding="utf-8")
        self.assertIn("600519.SH", body)

    async def test_as_image_success_delivers_via_channel(self):
        channel = _Channel()
        svc = _Svc(channel=channel)
        patch_d, fake_d = _patch_deliver("delivered")
        patch_i, _fake_i = _patch_md2img(Md2ImgResult(image=b"PNGBYTES"))
        ex = self._executor({"600519.SH": _bars()}, svc=svc)
        with patch_d, patch_i:
            r = await ex.run(
                {"symbols": ["600519.SH"], "target_session_id": "s1", "as_image": True},
                _CTX,
            )
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.delivery_status, "delivered")
        self.assertEqual(r.data["delivery_mode"], "image")
        self.assertTrue(r.data["image_ok"])
        # Image went through the channel; text deliver not called.
        self.assertEqual(len(channel.sent), 1)
        session_id, content, meta = channel.sent[0]
        self.assertEqual(session_id, "s1")
        self.assertEqual(content.data, b"PNGBYTES")
        self.assertEqual(meta, {"chat_id": "c1"})
        self.assertFalse(fake_d.called)

    async def test_md2img_failure_falls_back_to_text(self):
        patch_d, fake_d = _patch_deliver("delivered")
        patch_i, _fake_i = _patch_md2img(
            Md2ImgResult(reason="playwright_missing", hint="install", detail="x")
        )
        ex = self._executor({"600519.SH": _bars()})
        with patch_d, patch_i:
            r = await ex.run(
                {"symbols": ["600519.SH"], "target_session_id": "s1", "as_image": True},
                _CTX,
            )
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.delivery_status, "delivered")
        self.assertEqual(r.data["delivery_mode"], "text")
        self.assertFalse(r.data["image_ok"])
        self.assertTrue(fake_d.called)
        self.assertIn("600519.SH", fake_d.content)

    async def test_image_channel_send_failure_falls_back_to_text(self):
        channel = _Channel(exc=RuntimeError("upload rejected"))
        svc = _Svc(channel=channel)
        patch_d, fake_d = _patch_deliver("delivered")
        patch_i, _fake_i = _patch_md2img(Md2ImgResult(image=b"PNG"))
        ex = self._executor({"600519.SH": _bars()}, svc=svc)
        with patch_d, patch_i:
            r = await ex.run(
                {"symbols": ["600519.SH"], "target_session_id": "s1", "as_image": True},
                _CTX,
            )
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.data["delivery_mode"], "text")
        self.assertFalse(r.data["image_ok"])
        self.assertTrue(fake_d.called)

    async def test_image_no_channel_binding_falls_back_to_text(self):
        svc = _Svc(channel=None, session_config={})  # no channel bound
        patch_d, fake_d = _patch_deliver("delivered")
        patch_i, _fake_i = _patch_md2img(Md2ImgResult(image=b"PNG"))
        ex = self._executor({"600519.SH": _bars()}, svc=svc)
        with patch_d, patch_i:
            r = await ex.run(
                {"symbols": ["600519.SH"], "target_session_id": "s1", "as_image": True},
                _CTX,
            )
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.data["delivery_mode"], "text")
        self.assertTrue(fake_d.called)

    async def test_single_symbol_failure_continues(self):
        patch, fake = _patch_deliver("delivered")
        ex = self._executor(
            {"600519.SH": _bars(), "BAD.SZ": RuntimeError("fetch timeout")}
        )
        with patch:
            r = await ex.run(
                {"symbols": ["600519.SH", "BAD.SZ"], "target_session_id": "s1"}, _CTX
            )
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.data["failed_symbols"], ["BAD.SZ"])
        self.assertIn("600519.SH", fake.content)
        self.assertNotIn("BAD.SZ", fake.content)

    async def test_short_bars_counts_as_symbol_failure(self):
        patch, fake = _patch_deliver("delivered")
        ex = self._executor({"600519.SH": _bars(), "SHORT.SZ": _bars(3)})
        with patch:
            r = await ex.run(
                {"symbols": ["600519.SH", "SHORT.SZ"], "target_session_id": "s1"}, _CTX
            )
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.data["failed_symbols"], ["SHORT.SZ"])

    async def test_all_symbols_fail_returns_failed(self):
        ex = self._executor(
            {"A.SZ": RuntimeError("boom"), "B.SZ": RuntimeError("boom")}
        )
        r = await ex.run({"symbols": ["A.SZ", "B.SZ"], "target_session_id": "s1"}, _CTX)
        self.assertEqual(r.status, "failed")
        self.assertIn("stock_report_gather_failed", r.error)
        self.assertEqual(sorted(r.data["failed_symbols"]), ["A.SZ", "B.SZ"])

    async def test_no_target_session_skips_delivery(self):
        # No target_session_id → the shared primitive reports "skipped"; the
        # report is still rendered and persisted to the KB.
        patch, fake = _patch_deliver("skipped", None)
        ex = self._executor({"600519.SH": _bars()})
        with patch:
            r = await ex.run({"symbols": ["600519.SH"]}, _CTX)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.delivery_status, "skipped")
        self.assertIsNotNone(r.data["report_path"])

    async def test_repeat_fire_does_not_overwrite_journal(self):
        patch, _fake = _patch_deliver("delivered")
        ex = self._executor({"600519.SH": _bars()})
        with patch:
            r1 = await ex.run(
                {"symbols": ["600519.SH"], "title": "研报", "target_session_id": "s1"},
                _CTX,
            )
            r2 = await ex.run(
                {"symbols": ["600519.SH"], "title": "研报", "target_session_id": "s1"},
                _CTX,
            )
        self.assertNotEqual(r1.data["report_path"], r2.data["report_path"])
        self.assertTrue((self.kb / r1.data["report_path"]).exists())
        self.assertTrue((self.kb / r2.data["report_path"]).exists())

    async def test_english_report(self):
        patch, fake = _patch_deliver("delivered")
        ex = self._executor({"600519.SH": _bars()})
        with patch:
            r = await ex.run(
                {
                    "symbols": ["600519.SH"],
                    "target_session_id": "s1",
                    "language": "en",
                },
                _CTX,
            )
        self.assertEqual(r.status, "ok")
        self.assertIn("Stock Report", fake.content)
        self.assertIn("Core conclusion", fake.content)


if __name__ == "__main__":
    unittest.main()
