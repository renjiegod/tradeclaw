"""Tests for the Markdown→image renderer (best-effort, never raises)."""
import asyncio
import unittest
from unittest import mock

from doyoutrade.assistant.rendering import md2img


def _run(coro):
    return asyncio.run(coro)


class Md2ImgTests(unittest.TestCase):
    def test_content_too_long_skips_and_reports_reason(self):
        result = _run(
            md2img.render_markdown_to_image("x" * 500, max_chars=100)
        )
        self.assertFalse(result.ok)
        self.assertIsNone(result.image)
        self.assertEqual(result.reason, md2img.REASON_CONTENT_TOO_LONG)
        payload = result.failure_payload()
        self.assertEqual(payload["reason"], md2img.REASON_CONTENT_TOO_LONG)
        self.assertIn("hint", payload)

    def test_markdown_lib_missing(self):
        with mock.patch.object(
            md2img, "_markdown_to_html", side_effect=ImportError("no markdown")
        ):
            result = _run(md2img.render_markdown_to_image("# hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, md2img.REASON_MARKDOWN_LIB_MISSING)
        self.assertIn("ImportError", result.detail)

    def test_playwright_missing(self):
        async def _boom(html, *, width):
            raise ImportError("no playwright")

        with mock.patch.object(md2img, "_markdown_to_html", return_value="<html></html>"), \
             mock.patch.object(md2img, "_html_to_png", side_effect=_boom):
            result = _run(md2img.render_markdown_to_image("# hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, md2img.REASON_PLAYWRIGHT_MISSING)

    def test_render_failed_is_not_swallowed_but_returns_reason(self):
        async def _boom(html, *, width):
            raise RuntimeError("chromium crashed")

        with mock.patch.object(md2img, "_markdown_to_html", return_value="<html></html>"), \
             mock.patch.object(md2img, "_html_to_png", side_effect=_boom):
            result = _run(md2img.render_markdown_to_image("# hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, md2img.REASON_RENDER_FAILED)
        self.assertIn("RuntimeError", result.detail)

    def test_empty_png_treated_as_render_failure(self):
        async def _empty(html, *, width):
            return b""

        with mock.patch.object(md2img, "_markdown_to_html", return_value="<html></html>"), \
             mock.patch.object(md2img, "_html_to_png", side_effect=_empty):
            result = _run(md2img.render_markdown_to_image("# hi"))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, md2img.REASON_RENDER_FAILED)

    def test_success_returns_png_bytes(self):
        async def _png(html, *, width):
            return b"\x89PNG\r\n\x1a\n fake"

        with mock.patch.object(md2img, "_markdown_to_html", return_value="<html></html>"), \
             mock.patch.object(md2img, "_html_to_png", side_effect=_png):
            result = _run(md2img.render_markdown_to_image("# hi"))
        self.assertTrue(result.ok)
        self.assertTrue(result.image.startswith(b"\x89PNG"))
        self.assertIsNone(result.reason)
        self.assertEqual(result.failure_payload(), {})


if __name__ == "__main__":
    unittest.main()
