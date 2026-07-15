import unittest

import httpx

from doyoutrade.models.invoke_errors import (
    adapter_invoke_endpoint_url,
    exception_to_invoke_error,
    failure_message_from_error,
    model_invocation_failure_response_payload,
    otel_status_description_from_error,
)


class _EmptyStrExc(Exception):
    def __str__(self) -> str:
        return ""


class InvokeErrorsTests(unittest.TestCase):
    def test_failure_message_from_error_prefers_type_and_message(self) -> None:
        msg = failure_message_from_error(
            {"code": "chat_ainvoke_failed", "type": "RuntimeError", "message": "boom"}
        )
        self.assertEqual(msg, "RuntimeError: boom")

    def test_failure_message_from_error_includes_http_status(self) -> None:
        msg = failure_message_from_error(
            {"code": "x", "type": "HTTPError", "message": "nope", "http_status": 503}
        )
        self.assertEqual(msg, "HTTPError: nope (http 503)")

    def test_failure_message_from_error_includes_url(self) -> None:
        msg = failure_message_from_error(
            {
                "code": "chat_ainvoke_failed",
                "type": "ReadTimeout",
                "message": "ReadTimeout('')",
                "url": "http://127.0.0.1:1234",
            }
        )
        self.assertIn("127.0.0.1:1234", msg)
        self.assertTrue(msg.endswith("@ http://127.0.0.1:1234"))

    def test_exception_to_invoke_error_includes_url_from_httpx_request(self) -> None:
        req = httpx.Request("GET", "http://probe.local/v1/x")
        try:
            raise httpx.ReadTimeout("slow", request=req)
        except httpx.ReadTimeout as exc:
            err = exception_to_invoke_error(exc, code="chat_ainvoke_failed")
        self.assertEqual(err.get("url"), "http://probe.local/v1/x")

    def test_exception_to_invoke_error_falls_back_to_adapter_url(self) -> None:
        class _Ad:
            api_host = "http://127.0.0.1:9999"

        try:
            raise httpx.ReadTimeout("")
        except httpx.ReadTimeout as exc:
            err = exception_to_invoke_error(exc, code="chat_ainvoke_failed", adapter=_Ad())
        self.assertEqual(err.get("url"), "http://127.0.0.1:9999")

    def test_adapter_invoke_endpoint_url_unwraps_recording(self) -> None:
        from doyoutrade.models.recording import RecordingModelAdapter

        class _Inner:
            base_url = "https://api.example/v1"

        inner = _Inner()
        rec = RecordingModelAdapter(inner, provider="x", provider_kind="openai", model="m", recorder=None)  # type: ignore[arg-type]
        self.assertEqual(adapter_invoke_endpoint_url(rec), "https://api.example/v1")

    def test_exception_to_invoke_error_non_empty_message_when_str_empty(self) -> None:
        try:
            raise _EmptyStrExc()
        except _EmptyStrExc as exc:
            err = exception_to_invoke_error(exc, code="chat_ainvoke_failed")
        self.assertTrue(str(err.get("message", "")).strip())
        self.assertEqual(err["code"], "chat_ainvoke_failed")
        self.assertEqual(err["type"], "_EmptyStrExc")

    def test_otel_status_description_bounded(self) -> None:
        long_msg = "x" * 2000
        err = {"code": "c", "type": "E", "message": long_msg}
        desc = otel_status_description_from_error(err)
        self.assertLessEqual(len(desc), 512)

    def test_exception_to_invoke_error_finds_body_on_cause_chain(self) -> None:
        req = httpx.Request("POST", "http://127.0.0.1:1234/v1/x")
        resp = httpx.Response(422, content=b'{"detail":"bad enum"}', request=req)
        http_exc = httpx.HTTPStatusError("upstream", request=req, response=resp)
        try:
            raise RuntimeError("wrapper") from http_exc
        except RuntimeError as exc:
            err = exception_to_invoke_error(exc, code="chat_ainvoke_failed")
        self.assertEqual(err.get("http_status"), 422)
        self.assertIn("bad enum", str(err.get("body_preview") or ""))

    def test_model_invocation_failure_payload_uses_large_body_cap(self) -> None:
        req = httpx.Request("POST", "http://127.0.0.1:1/x")
        big = "Z" * 5000
        resp = httpx.Response(500, content=('{"x":"' + big + '"}').encode(), request=req)
        http_exc = httpx.HTTPStatusError("e", request=req, response=resp)
        try:
            raise RuntimeError("wrap") from http_exc
        except RuntimeError as exc:
            payload = model_invocation_failure_response_payload(exc)
        inner = payload.get("error") or {}
        preview = str(inner.get("body_preview") or "")
        self.assertGreater(len(preview), 2000)
        self.assertIn("ZZZZ", preview)


if __name__ == "__main__":
    unittest.main()
