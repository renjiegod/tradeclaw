from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelRequest:
    system_prompt: str
    user_prompt: str


@dataclass(frozen=True)
class ModelResponse:
    text: str
    raw: Any = None


class ModelAdapter(ABC):
    @abstractmethod
    def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate a model response from the provided prompts."""
