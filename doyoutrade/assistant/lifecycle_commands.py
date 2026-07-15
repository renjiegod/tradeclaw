from __future__ import annotations

import re
from dataclasses import dataclass


_COMMAND_PATTERN = re.compile(r"^/([^\s@]+)\s*$")
_SUPPORTED_COMMANDS = {"new"}


@dataclass(frozen=True)
class AssistantLifecycleCommand:
    name: str
    arguments: str = ""


def parse_lifecycle_command(message: str) -> AssistantLifecycleCommand | None:
    """Return a supported assistant lifecycle command for bare slash commands."""
    if not isinstance(message, str):
        return None
    match = _COMMAND_PATTERN.match(message.strip())
    if not match:
        return None
    name = match.group(1).strip().lower()
    if name not in _SUPPORTED_COMMANDS:
        return None
    return AssistantLifecycleCommand(name=name)
