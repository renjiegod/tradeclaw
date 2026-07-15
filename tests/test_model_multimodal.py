"""Tests for multimodal ModelRequest support (image parts + recording redaction)."""

from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace

from doyoutrade.models.base import (
    ALLOWED_IMAGE_MIME_TYPES,
    MAX_IMAGE_BYTES,
    ImagePart,
    ModelRequest,
    ModelResponse,
)
from doyoutrade.models.providers import (
    AnthropicAdapter,
    OpenAICompatibleAdapter,
    serialized_model_invocation_request,
)
from doyoutrade.models.providers._common import (
    build_anthropic_messages,
    build_openai_messages,
    redact_image_blocks,
)
from doyoutrade.models.recording import RecordingModelAdapter

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body-0123456789"
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode("ascii")


def _image_part() -> ImagePart:
    return ImagePart(data=_PNG_BYTES, mime_type="image/png")


class ImagePartValidationTests(unittest.TestCase):
    def test_valid_part(self) -> None:
        part = _image_part()
        self.assertEqual(part.mime_type, "image/png")
        self.assertEqual(part.data, _PNG_BYTES)

    def test_empty_data_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ImagePart(data=b"", mime_type="image/png")
        self.assertIn("non-empty", str(ctx.exception))

    def test_oversize_raises_with_actual_size(self) -> None:
        big = b"\x00" * (MAX_IMAGE_BYTES + 1)
        with self.assertRaises(ValueError) as ctx:
            ImagePart(data=big, mime_type="image/png")
        self.assertIn(str(MAX_IMAGE_BYTES + 1), str(ctx.exception))

    def test_bad_mime_raises_with_actual_type(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ImagePart(data=_PNG_BYTES, mime_type="image/tiff")
        self.assertIn("image/tiff", str(ctx.exception))

    def test_allowed_mimes(self) -> None:
        self.assertEqual(
            ALLOWED_IMAGE_MIME_TYPES,
            frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
        )

    def test_model_request_default_has_no_images(self) -> None:
        req = ModelRequest(system_prompt="S", user_prompt="U")
        self.assertIsNone(req.image_parts)


class MessageBuilderTests(unittest.TestCase):
    def test_openai_without_images_unchanged(self) -> None:
        msgs = build_openai_messages("S", "U")
        self.assertEqual(
            msgs,
            [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        )

    def test_anthropic_without_images_unchanged(self) -> None:
        msgs = build_anthropic_messages("S", "U")
        self.assertEqual(
            msgs,
            [{"role": "user", "content": "S"}, {"role": "user", "content": "U"}],
        )

    def test_openai_with_images_block_array(self) -> None:
        msgs = build_openai_messages("S", "U", image_parts=(_image_part(),))
        self.assertEqual(msgs[0], {"role": "system", "content": "S"})
        user = msgs[1]
        self.assertEqual(user["role"], "user")
        blocks = user["content"]
        self.assertIsInstance(blocks, list)
        self.assertEqual(blocks[0], {"type": "text", "text": "U"})
        self.assertEqual(blocks[1]["type"], "image_url")
        self.assertEqual(
            blocks[1]["image_url"]["url"], f"data:image/png;base64,{_PNG_B64}"
        )

    def test_anthropic_with_images_block_array(self) -> None:
        msgs = build_anthropic_messages("S", "U", image_parts=(_image_part(),))
        user = msgs[1]
        blocks = user["content"]
        self.assertIsInstance(blocks, list)
        self.assertEqual(blocks[0]["type"], "image")
        self.assertEqual(
            blocks[0]["source"],
            {"type": "base64", "media_type": "image/png", "data": _PNG_B64},
        )
        self.assertEqual(blocks[-1], {"type": "text", "text": "U"})


class RedactImageBlocksTests(unittest.TestCase):
    def test_redacts_openai_data_url_block(self) -> None:
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hi"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{_PNG_B64}"},
                        },
                    ],
                }
            ]
        }
        out = redact_image_blocks(payload)
        block = out["messages"][0]["content"][1]
        self.assertEqual(block["type"], "image_redacted")
        self.assertIn("image/png", block["note"])
        self.assertNotIn(_PNG_B64, json.dumps(out))
        # Input is not mutated.
        self.assertEqual(payload["messages"][0]["content"][1]["type"], "image_url")

    def test_redacts_anthropic_base64_block(self) -> None:
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": _PNG_B64,
                            },
                        },
                        {"type": "text", "text": "hi"},
                    ],
                }
            ]
        }
        out = redact_image_blocks(payload)
        block = out["messages"][0]["content"][0]
        self.assertEqual(block["type"], "image_redacted")
        self.assertIn("image/jpeg", block["note"])
        self.assertNotIn(_PNG_B64, json.dumps(out))

    def test_passthrough_without_images(self) -> None:
        payload = {"model": "m", "messages": [{"role": "user", "content": "U"}]}
        self.assertEqual(redact_image_blocks(payload), payload)

    def test_non_data_image_url_untouched(self) -> None:
        payload = {"type": "image_url", "image_url": {"url": "https://x/y.png"}}
        self.assertEqual(redact_image_blocks(payload), payload)


class SerializedRequestBodyTests(unittest.TestCase):
    def _openai_adapter(self) -> OpenAICompatibleAdapter:
        return OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.2,
            max_tokens=128,
            timeout_seconds=30,
        )

    def _anthropic_adapter(self) -> AnthropicAdapter:
        return AnthropicAdapter(
            model="claude-test",
            api_key="k",
            temperature=0.4,
            max_tokens=256,
            timeout_seconds=30,
        )

    def test_openai_body_redacts_image(self) -> None:
        body = serialized_model_invocation_request(
            self._openai_adapter(),
            ModelRequest(system_prompt="S", user_prompt="U", image_parts=(_image_part(),)),
        )
        serialized = json.dumps(body)
        self.assertNotIn(_PNG_B64, serialized)
        user_content = body["messages"][1]["content"]
        self.assertEqual(user_content[0], {"type": "text", "text": "U"})
        self.assertEqual(user_content[1]["type"], "image_redacted")
        self.assertIn(f"{len(_PNG_BYTES)} bytes", user_content[1]["note"])
        self.assertIn("image/png", user_content[1]["note"])

    def test_anthropic_body_redacts_image(self) -> None:
        body = serialized_model_invocation_request(
            self._anthropic_adapter(),
            ModelRequest(system_prompt="S", user_prompt="U", image_parts=(_image_part(),)),
        )
        serialized = json.dumps(body)
        self.assertNotIn(_PNG_B64, serialized)
        user_content = body["messages"][0]["content"]
        self.assertEqual(user_content[0]["type"], "image_redacted")
        self.assertEqual(user_content[-1], {"type": "text", "text": "U"})

    def test_bodies_without_images_keep_string_content(self) -> None:
        body = serialized_model_invocation_request(
            self._openai_adapter(),
            ModelRequest(system_prompt="S", user_prompt="U"),
        )
        self.assertEqual(body["messages"][1]["content"], "U")
        body = serialized_model_invocation_request(
            self._anthropic_adapter(),
            ModelRequest(system_prompt="S", user_prompt="U"),
        )
        self.assertEqual(body["messages"][0]["content"], "U")


class OpenAIWireBodyTests(unittest.TestCase):
    """Wire kwargs must carry the real data URL; recorded payload must not."""

    def test_generate_sends_data_url_and_records_redacted(self) -> None:
        class FakeCompletion:
            def __init__(self) -> None:
                self.choices = [
                    SimpleNamespace(
                        message=SimpleNamespace(content="[]", tool_calls=None)
                    )
                ]

            def model_dump(self, *, mode: str = "json"):  # noqa: ARG002
                return {"id": "chatcmpl-fake", "choices": []}

        class FakeCompletions:
            def __init__(self) -> None:
                self.last: dict | None = None

            def create(self, **kwargs):
                self.last = kwargs
                return FakeCompletion()

        completions = FakeCompletions()
        adapter = OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.1,
            max_tokens=64,
            timeout_seconds=30,
        )
        adapter.sync_client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        out = adapter.generate(
            ModelRequest(
                system_prompt="S", user_prompt="U", image_parts=(_image_part(),)
            )
        )

        # Wire body: real base64 data URL reached the SDK call.
        wire_blocks = completions.last["messages"][1]["content"]
        self.assertEqual(
            wire_blocks[1]["image_url"]["url"], f"data:image/png;base64,{_PNG_B64}"
        )
        # Recorded body: redacted.
        recorded = json.dumps(out.invocation_request_payload)
        self.assertNotIn(_PNG_B64, recorded)
        self.assertIn("image_redacted", recorded)

    def test_recording_adapter_never_persists_base64(self) -> None:
        captured: list[dict] = []

        class FakeCompletion:
            def __init__(self) -> None:
                self.choices = [
                    SimpleNamespace(
                        message=SimpleNamespace(content="[]", tool_calls=None)
                    )
                ]

            def model_dump(self, *, mode: str = "json"):  # noqa: ARG002
                return {"id": "chatcmpl-fake", "choices": []}

        class FakeCompletions:
            def create(self, **kwargs):  # noqa: ARG002
                return FakeCompletion()

        inner = OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.1,
            max_tokens=64,
            timeout_seconds=30,
        )
        inner.sync_client = SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        )
        adapter = RecordingModelAdapter(
            inner,
            provider="openai_compatible",
            provider_kind="openai_compatible",
            model="gpt-test",
            recorder=captured.append,
        )
        adapter.generate(
            ModelRequest(
                system_prompt="S", user_prompt="U", image_parts=(_image_part(),)
            )
        )
        self.assertEqual(len(captured), 1)
        serialized_row = json.dumps(captured[0]["request_payload"])
        self.assertNotIn(_PNG_B64, serialized_row)
        self.assertIn("image_redacted", serialized_row)

    def test_recording_defensive_layer_redacts_leaky_inner(self) -> None:
        """Even if an inner adapter leaks raw image blocks in its payload,
        the recording entry point must strip them."""
        captured: list[dict] = []

        leaky_payload = {
            "model": "leaky",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{_PNG_B64}"},
                        }
                    ],
                }
            ],
        }

        from doyoutrade.models.base import ModelAdapter

        class LeakyInner(ModelAdapter):
            def generate(self, request: ModelRequest) -> ModelResponse:
                return ModelResponse(
                    text="ok",
                    raw=None,
                    invocation_request_payload=leaky_payload,
                )

        adapter = RecordingModelAdapter(
            LeakyInner(),
            provider="test",
            provider_kind="test",
            model="leaky",
            recorder=captured.append,
        )
        adapter.generate(ModelRequest(system_prompt="S", user_prompt="U"))
        self.assertEqual(len(captured), 1)
        serialized_row = json.dumps(captured[0]["request_payload"])
        self.assertNotIn(_PNG_B64, serialized_row)
        self.assertIn("image_redacted", serialized_row)


class AnthropicWireBodyTests(unittest.TestCase):
    def test_generate_sends_base64_and_records_redacted(self) -> None:
        class FakeMessage:
            content = [SimpleNamespace(type="text", text="[]")]
            usage = None

            def model_dump(self, *, mode: str = "json"):  # noqa: ARG002
                return {"id": "msg-fake", "content": [{"type": "text", "text": "[]"}]}

        class FakeRawResponse:
            def __init__(self) -> None:
                self.http_response = SimpleNamespace(json=lambda: {"id": "msg-fake"})

            def parse(self):
                return FakeMessage()

        class FakeMessages:
            def __init__(self) -> None:
                self.last: dict | None = None
                self.with_raw_response = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                self.last = kwargs
                return FakeRawResponse()

        adapter = AnthropicAdapter(
            model="claude-test",
            api_key="k",
            temperature=0.4,
            max_tokens=256,
            timeout_seconds=30,
        )
        fake_messages = FakeMessages()
        adapter.client = SimpleNamespace(messages=fake_messages)

        out = adapter.generate(
            ModelRequest(
                system_prompt="S", user_prompt="U", image_parts=(_image_part(),)
            )
        )
        # Wire body carries the real base64 source block.
        wire_user = fake_messages.last["messages"][1]["content"]
        self.assertEqual(wire_user[0]["source"]["data"], _PNG_B64)
        # Recorded payload is redacted.
        recorded = json.dumps(out.invocation_request_payload)
        self.assertNotIn(_PNG_B64, recorded)
        self.assertIn("image_redacted", recorded)


if __name__ == "__main__":
    unittest.main()
