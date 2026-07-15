"""Version-controlled LLM prompts as Jinja2 templates."""

from doyoutrade.prompts.render import render_prompt

SIGNAL_SYSTEM = "signal/system.j2"
SIGNAL_USER = "signal/user.j2"
REVIEW_SYSTEM = "review/system.j2"
REVIEW_USER = "review/user.j2"

__all__ = [
    "REVIEW_SYSTEM",
    "REVIEW_USER",
    "SIGNAL_SYSTEM",
    "SIGNAL_USER",
    "render_prompt",
]
