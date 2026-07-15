"""Render Markdown to a PNG image, pure-Python (no system binaries).

Pipeline: Markdown → HTML (``markdown`` lib) → PNG (Playwright headless
Chromium). Both dependencies are optional and imported lazily; when either is
missing — or rendering fails, or the content is too long — this returns a
:class:`Md2ImgResult` whose ``image`` is ``None`` and whose ``reason`` names the
failure mode, so the caller can fall back to text delivery **and** emit a
structured ``md2img_unavailable`` debug event (see ``MD2IMG_UNAVAILABLE_EVENT``).

We never raise on a rendering failure: producing an image is best-effort and
must never collapse a notification. But per the error-visibility rules we never
swallow silently either — every failure logs a ``warning`` with the exception
type + message and returns a distinguishable ``reason``.

Playwright manages its own Chromium download (``playwright install chromium``),
so no OS-level ``wkhtmltoimage`` binary is required; that keeps CI/Docker setup
to plain ``pip`` + one install command.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Debug-event type the caller should emit when ``image`` comes back ``None``.
# Kept as a stable token so downstream (debug UI / trace) can filter on it.
MD2IMG_UNAVAILABLE_EVENT = "md2img_unavailable"

# Stable ``reason`` tokens set on :class:`Md2ImgResult` when rendering is skipped
# or fails. Callers put these in the debug-event payload / logs.
REASON_CONTENT_TOO_LONG = "content_too_long"
REASON_MARKDOWN_LIB_MISSING = "markdown_lib_missing"
REASON_PLAYWRIGHT_MISSING = "playwright_missing"
REASON_RENDER_FAILED = "render_failed"

# Repair hints paired with each reason so the caller's event payload can point at
# the fix without the caller having to know the internals.
_REASON_HINTS = {
    REASON_CONTENT_TOO_LONG: "shorten the markdown or raise max_chars; sending as text",
    REASON_MARKDOWN_LIB_MISSING: "install the 'report' extra: pip install 'doyoutrade[report]'",
    REASON_PLAYWRIGHT_MISSING: (
        "install the 'report' extra and run 'playwright install chromium'"
    ),
    REASON_RENDER_FAILED: "check logs for the Playwright/Chromium error; sending as text",
}


@dataclass
class Md2ImgResult:
    """Outcome of a Markdown→image render.

    ``image`` is the PNG bytes on success, else ``None``. On failure ``reason``
    is one of the ``REASON_*`` tokens and ``hint`` points at the fix; ``detail``
    carries the exception ``type: message`` when an exception was involved.
    """

    image: bytes | None = None
    reason: str | None = None
    hint: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.image is not None

    def failure_payload(self) -> dict[str, str]:
        """Payload for a ``md2img_unavailable`` debug event / structured log."""
        payload: dict[str, str] = {}
        if self.reason:
            payload["reason"] = self.reason
        if self.hint:
            payload["hint"] = self.hint
        if self.detail:
            payload["detail"] = self.detail
        return payload


_CSS = """
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px 28px;
  background: #ffffff;
  color: #1f2328;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  font-size: 15px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
h1, h2, h3, h4 { font-weight: 600; line-height: 1.3; margin: 1.1em 0 0.5em; }
h1 { font-size: 1.7em; border-bottom: 2px solid #eaecef; padding-bottom: 0.3em; }
h2 { font-size: 1.35em; border-bottom: 1px solid #eaecef; padding-bottom: 0.25em; }
h3 { font-size: 1.15em; }
p { margin: 0.5em 0; }
ul, ol { margin: 0.4em 0; padding-left: 1.5em; }
li { margin: 0.2em 0; }
code {
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
  background: #f3f4f6; padding: 0.15em 0.35em; border-radius: 4px; font-size: 0.9em;
}
pre { background: #f6f8fa; padding: 14px 16px; border-radius: 8px; overflow-x: auto; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; margin: 0.7em 0; width: 100%; }
th, td { border: 1px solid #d0d7de; padding: 7px 12px; text-align: left; }
th { background: #f6f8fa; font-weight: 600; }
blockquote {
  margin: 0.6em 0; padding: 0.2em 1em; color: #57606a;
  border-left: 4px solid #d0d7de;
}
hr { border: none; border-top: 1px solid #eaecef; margin: 1.2em 0; }
strong { font-weight: 600; }
"""


def _markdown_to_html(markdown_text: str) -> str:
    """Wrap rendered Markdown in a self-contained HTML document (inline CSS)."""
    import markdown as _markdown  # lazy; may raise ImportError

    body = _markdown.markdown(
        markdown_text,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{_CSS}</style></head><body>{body}</body></html>"
    )


async def _html_to_png(html: str, *, width: int) -> bytes:
    """Screenshot an HTML string with headless Chromium. May raise ImportError."""
    from playwright.async_api import async_playwright  # lazy

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        try:
            page = await browser.new_page(
                viewport={"width": width, "height": 600},
                device_scale_factor=2,
            )
            # Content is fully self-contained (inline CSS, no remote assets), so
            # "load" is enough — "networkidle" could hang without a network.
            await page.set_content(html, wait_until="load")
            png = await page.screenshot(full_page=True, type="png")
        finally:
            await browser.close()
    return png


async def render_markdown_to_image(
    markdown_text: str,
    *,
    max_chars: int = 15000,
    width: int = 800,
) -> Md2ImgResult:
    """Render Markdown to PNG bytes; best-effort, never raises.

    Returns an :class:`Md2ImgResult`. On any skip/failure the ``image`` is
    ``None`` and ``reason``/``hint``/``detail`` explain why (already logged at
    ``warning``); the caller should fall back to text and emit a
    ``MD2IMG_UNAVAILABLE_EVENT`` debug event with ``result.failure_payload()``.

    ``max_chars`` guards against pathologically tall images; ``width`` is the
    render viewport width in CSS px.
    """
    if len(markdown_text) > max_chars:
        logger.warning(
            "md2img skipped: content %d chars exceeds max_chars=%d (falling back to text)",
            len(markdown_text),
            max_chars,
        )
        return Md2ImgResult(
            reason=REASON_CONTENT_TOO_LONG,
            hint=_REASON_HINTS[REASON_CONTENT_TOO_LONG],
            detail=f"len={len(markdown_text)} max_chars={max_chars}",
        )

    try:
        html = _markdown_to_html(markdown_text)
    except ImportError as exc:
        logger.warning(
            "md2img unavailable: markdown lib missing (%s: %s); falling back to text",
            type(exc).__name__,
            exc,
        )
        return Md2ImgResult(
            reason=REASON_MARKDOWN_LIB_MISSING,
            hint=_REASON_HINTS[REASON_MARKDOWN_LIB_MISSING],
            detail=f"{type(exc).__name__}: {exc}",
        )

    try:
        png = await _html_to_png(html, width=width)
    except ImportError as exc:
        logger.warning(
            "md2img unavailable: playwright missing (%s: %s); "
            "install the 'report' extra + 'playwright install chromium'; falling back to text",
            type(exc).__name__,
            exc,
        )
        return Md2ImgResult(
            reason=REASON_PLAYWRIGHT_MISSING,
            hint=_REASON_HINTS[REASON_PLAYWRIGHT_MISSING],
            detail=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - best-effort render, but never silent
        logger.warning(
            "md2img render failed (%s: %s); falling back to text",
            type(exc).__name__,
            exc,
        )
        return Md2ImgResult(
            reason=REASON_RENDER_FAILED,
            hint=_REASON_HINTS[REASON_RENDER_FAILED],
            detail=f"{type(exc).__name__}: {exc}",
        )

    if not png:
        logger.warning("md2img render returned empty bytes; falling back to text")
        return Md2ImgResult(
            reason=REASON_RENDER_FAILED,
            hint=_REASON_HINTS[REASON_RENDER_FAILED],
            detail="empty screenshot bytes",
        )

    return Md2ImgResult(image=png)
