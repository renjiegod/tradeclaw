"""Load and render Jinja2 prompt templates from ``doyoutrade/prompts/``."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from jinja2 import Environment, PackageLoader


def _tojson(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _tojson_pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


@lru_cache(maxsize=1)
def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("doyoutrade", "prompts"),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["tojson"] = _tojson
    env.filters["tojson_pretty"] = _tojson_pretty
    return env


def render_prompt(template_name: str, **context: Any) -> str:
    """Render a template by path relative to the ``prompts`` package (e.g. ``signal/system.j2``)."""
    return _environment().get_template(template_name).render(**context).strip()
