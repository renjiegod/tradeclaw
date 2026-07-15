"""Deterministic stock-report rendering (Markdown, template-driven).

Unlike ``daily_review`` (which asks an LLM to synthesize prose), this builds a
report **deterministically** from structured per-symbol analysis via Jinja2
templates under ``doyoutrade/prompts/report/``. The rendered Markdown can then be
turned into an image (``rendering.md2img``) and pushed through any channel.
"""

from doyoutrade.assistant.reporting.builder import (
    ReportItem,
    ReportRequest,
    render_report,
)

__all__ = ["ReportItem", "ReportRequest", "render_report"]
