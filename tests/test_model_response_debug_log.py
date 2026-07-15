"""Tests for DEBUG logging of model responses."""

from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace

from doyoutrade.models.base import ModelResponse, log_model_response_debug


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class ModelResponseDebugLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log = logging.getLogger("doyoutrade.models")
        self._prev_level = self._log.level
        self._handler = _ListHandler()
        self._log.addHandler(self._handler)
        self._log.setLevel(logging.DEBUG)
        self._log.propagate = False

    def tearDown(self) -> None:
        self._log.removeHandler(self._handler)
        self._log.setLevel(self._prev_level)
        self._log.propagate = True

    def test_log_model_response_debug_skips_when_debug_disabled(self) -> None:
        self._log.setLevel(logging.INFO)
        log_model_response_debug(ModelResponse(text="secret"), adapter="anthropic")
        self.assertFalse(any(r.levelno == logging.DEBUG for r in self._handler.records))

    def test_log_model_response_debug_emits_raw_content(self) -> None:
        self._log.setLevel(logging.DEBUG)
        payload = '{"proposals":[]}'
        log_model_response_debug(
            ModelResponse(text=payload, raw=SimpleNamespace(content=payload)),
            adapter="openai_compatible",
        )
        self.assertTrue(any(r.levelno == logging.DEBUG for r in self._handler.records))
        joined = " ".join(r.getMessage() for r in self._handler.records)
        self.assertIn('{"proposals":[]}', joined)
        self.assertIn("openai_compatible", joined)
        self.assertIn("raw_chars=", joined)

    def test_log_model_response_debug_serializes_content_blocks(self) -> None:
        self._log.setLevel(logging.DEBUG)
        blocks = [{"type": "text", "text": "hello"}]
        log_model_response_debug(
            ModelResponse(text="hello", raw=SimpleNamespace(content=blocks)),
            adapter="anthropic",
        )
        msg = self._handler.records[0].getMessage()
        self.assertIn("hello", msg)
        self.assertIn("text", msg)

    def test_log_model_response_debug_truncates_long_raw(self) -> None:
        self._log.setLevel(logging.DEBUG)
        body = "x" * 9000
        log_model_response_debug(
            ModelResponse(text="short", raw=SimpleNamespace(content=body)),
            adapter="anthropic",
        )
        msg = self._handler.records[0].getMessage()
        self.assertIn("[truncated, total 9000 chars]", msg)
        self.assertFalse(msg.endswith("x" * 100))


if __name__ == "__main__":
    unittest.main()
