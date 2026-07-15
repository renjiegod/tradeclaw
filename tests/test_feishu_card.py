"""Tests for CardKitClient."""
import unittest
from unittest.mock import MagicMock, patch

from doyoutrade.assistant.channels.feishu.card.cardkit import CardKitClient


class TestCardKitClientInit(unittest.TestCase):
    def test_domain_defaults_to_feishu(self):
        client = CardKitClient("cli_aaa", "secret_xyz")
        self.assertEqual(client.domain, "feishu")
        self.assertEqual(client._base_url, "https://open.feishu.cn")

    def test_domain_lark_sets_larksuite_url(self):
        client = CardKitClient("cli_aaa", "secret_xyz", domain="lark")
        self.assertEqual(client.domain, "lark")
        self.assertEqual(client._base_url, "https://open.larksuite.com")

    def test_credentials_stored(self):
        client = CardKitClient("cli_abc", "secret_123", domain="lark")
        self.assertEqual(client.app_id, "cli_abc")
        self.assertEqual(client.app_secret, "secret_123")

    def test_token_initially_none(self):
        client = CardKitClient("cli_aaa", "secret")
        self.assertIsNone(client._token)
        self.assertEqual(client._token_expires_at, 0)


class TestCardKitClientToken(unittest.TestCase):
    def setUp(self):
        self.client = CardKitClient("cli_aaa", "secret_xyz")

    def test_get_tenant_access_token_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 0,
            "msg": "success",
            "tenant_access_token": "test_token_abc",
        }

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            token = self.client._get_tenant_access_token()

        self.assertEqual(token, "test_token_abc")
        self.assertEqual(self.client._token, "test_token_abc")

    def test_get_tenant_access_token_cached(self):
        # Pre-set token with future expiry — should return cached without HTTP call
        self.client._token = "cached_token"
        self.client._token_expires_at = 9999999999

        with patch("httpx.Client") as mock_client_cls:
            token = self.client._get_tenant_access_token()

        self.assertEqual(token, "cached_token")
        mock_client_cls.assert_not_called()

    def test_get_tenant_access_token_failure(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 99999999, "msg": "error"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            token = self.client._get_tenant_access_token()

        self.assertIsNone(token)

    def test_refresh_token_clears_cache_and_fetches_new(self):
        self.client._token = "old_token"
        self.client._token_expires_at = 0  # Already expired — forces refresh

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 0,
            "msg": "success",
            "tenant_access_token": "new_token_xyz",
        }

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            token = self.client._refresh_token()

        # Verify HTTP was called (cache was bypassed) and new token is stored
        mock_client_instance.post.assert_called_once()
        self.assertEqual(token, "new_token_xyz")
        self.assertEqual(self.client._token, "new_token_xyz")


class TestCardKitClientCreateCard(unittest.TestCase):
    def setUp(self):
        self.client = CardKitClient("cli_aaa", "secret_xyz")

    def test_create_card_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 0,
            "msg": "success",
            "data": {"card_id": "card_abc_123"},
        }

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            card_id = self.client.create_card({"card": {"a": 1}})

        self.assertEqual(card_id, "card_abc_123")
        call_args = mock_client_instance.post.call_args
        self.assertEqual(
            call_args.args[0],
            "https://open.feishu.cn/open-apis/cardkit/v1/cards",
        )
        payload = call_args.kwargs["json"]
        self.assertEqual(payload["type"], "card_json")
        self.assertEqual(payload["data"], '{"card": {"a": 1}}')

    def test_create_card_failure_returns_none(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 99999, "msg": "failed"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            card_id = self.client.create_card({"card": {}})

        self.assertIsNone(card_id)


class TestCardKitClientSendCard(unittest.TestCase):
    def setUp(self):
        self.client = CardKitClient("cli_aaa", "secret_xyz")

    def test_send_card_by_card_id_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 0,
            "msg": "success",
            "data": {"message_id": "msg_xyz_789"},
        }

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            message_id = self.client.send_card_by_card_id(
                "card_abc", "ou_user_001", "open_id"
            )

        self.assertEqual(message_id, "msg_xyz_789")
        # Verify params
        call_args = mock_client_instance.post.call_args
        self.assertEqual(call_args.kwargs["params"]["receive_id_type"], "open_id")
        payload = call_args.kwargs["json"]
        self.assertEqual(payload["receive_id"], "ou_user_001")
        self.assertEqual(payload["msg_type"], "interactive")
        self.assertEqual(
            payload["content"],
            '{"type": "card", "data": {"card_id": "card_abc"}}',
        )

    def test_send_card_by_card_id_failure_returns_none(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 88888, "msg": "error"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            message_id = self.client.send_card_by_card_id("card_abc", "ou_user")

        self.assertIsNone(message_id)

    def test_send_card_by_card_id_threads_reply_to_message_id(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 0,
            "msg": "success",
            "data": {"message_id": "msg_xyz_789"},
        }

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            self.client.send_card_by_card_id(
                "card_abc",
                "ou_user_001",
                "open_id",
                reply_to_message_id="om_origin_001",
            )

        call_args = mock_client_instance.post.call_args
        self.assertEqual(call_args.kwargs["params"]["reply_to_message_id"], "om_origin_001")


class TestCardKitClientUpdateCard(unittest.TestCase):
    def setUp(self):
        self.client = CardKitClient("cli_aaa", "secret_xyz")

    def test_update_card_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "msg": "success"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            mock_client_instance.put.return_value = mock_resp

            result = self.client.update_card("card_xyz", {"updated": True}, sequence=2)

        self.assertTrue(result)
        call_args = mock_client_instance.put.call_args
        self.assertEqual(
            call_args.args[0],
            "https://open.feishu.cn/open-apis/cardkit/v1/cards/card_xyz",
        )
        payload = call_args.kwargs["json"]
        self.assertEqual(payload["card"]["type"], "card_json")
        self.assertEqual(payload["card"]["data"], '{"updated": true}')
        self.assertEqual(payload["sequence"], 2)

    def test_update_card_table_limit_returns_false(self):
        for limit_code in (230099, 11310):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"code": limit_code, "msg": "table limit"}

            with patch("httpx.Client") as mock_client_cls:
                mock_client_instance = MagicMock()
                mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
                mock_client_instance.__exit__ = MagicMock(return_value=False)
                mock_client_instance.put.return_value = mock_resp
                mock_client_cls.return_value = mock_client_instance

                result = self.client.update_card("card_xyz", {})

            self.assertFalse(result)


class TestCardKitClientStreamCardContent(unittest.TestCase):
    def setUp(self):
        self.client = CardKitClient("cli_aaa", "secret_xyz")

    def test_stream_card_content_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "msg": "success"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.put.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            result = self.client.stream_card_content(
                "card_abc", "element_1", "new content", sequence=3
            )

        self.assertTrue(result)
        call_args = mock_client_instance.put.call_args
        self.assertEqual(
            call_args.args[0],
            "https://open.feishu.cn/open-apis/cardkit/v1/cards/card_abc/elements/element_1/content",
        )
        payload = call_args.kwargs["json"]
        self.assertEqual(payload["content"], "new content")
        self.assertEqual(payload["sequence"], 3)

    def test_stream_card_content_failure_returns_false(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 9999, "msg": "error"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.put.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            result = self.client.stream_card_content("card", "elem", "content")

        self.assertFalse(result)


class TestCardKitClientSetStreamingMode(unittest.TestCase):
    def setUp(self):
        self.client = CardKitClient("cli_aaa", "secret_xyz")

    def test_set_streaming_mode_enable_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "msg": "success"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.patch.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            result = self.client.set_streaming_mode("card_xyz", streaming_mode=True)

        self.assertTrue(result)
        call_args = mock_client_instance.patch.call_args
        self.assertEqual(
            call_args.args[0],
            "https://open.feishu.cn/open-apis/cardkit/v1/cards/card_xyz/settings",
        )
        payload = call_args.kwargs["json"]
        self.assertEqual(payload["settings"], '{"config": {"streaming_mode": true}}')

    def test_set_streaming_mode_disable_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "msg": "success"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.patch.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            result = self.client.set_streaming_mode("card_xyz", streaming_mode=False)

        self.assertTrue(result)
        call_args = mock_client_instance.patch.call_args
        payload = call_args.kwargs["json"]
        self.assertEqual(payload["settings"], '{"config": {"streaming_mode": false}}')

    def test_set_streaming_mode_table_limit_returns_false(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 230099, "msg": "limit"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.patch.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            result = self.client.set_streaming_mode("card", True)

        self.assertFalse(result)


class TestCardKitClientPatchMessage(unittest.TestCase):
    def setUp(self):
        self.client = CardKitClient("cli_aaa", "secret_xyz")

    def test_patch_message_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "msg": "success"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.patch.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            result = self.client.patch_message("msg_abc", {"key": "value"})

        self.assertTrue(result)
        call_args = mock_client_instance.patch.call_args
        payload = call_args.kwargs["json"]
        self.assertEqual(payload["content"], '{"key": "value"}')

    def test_patch_message_failure_returns_false(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 11111, "msg": "error"}

        with patch("httpx.Client") as mock_client_cls:
            mock_client_instance = MagicMock()
            mock_client_instance.__enter__ = MagicMock(return_value=mock_client_instance)
            mock_client_instance.__exit__ = MagicMock(return_value=False)
            mock_client_instance.patch.return_value = mock_resp
            mock_client_cls.return_value = mock_client_instance

            result = self.client.patch_message("msg_abc", {})

        self.assertFalse(result)


class TestCardBuilder(unittest.TestCase):
    @staticmethod
    def _all_tags(value):
        if isinstance(value, dict):
            tags = [value["tag"]] if "tag" in value else []
            for child in value.values():
                tags.extend(TestCardBuilder._all_tags(child))
            return tags
        if isinstance(value, list):
            tags = []
            for child in value:
                tags.extend(TestCardBuilder._all_tags(child))
            return tags
        return []

    @staticmethod
    def _all_buttons(card):
        buttons = []

        def _walk(elements):
            for el in elements or []:
                tag = el.get("tag")
                if tag == "button":
                    buttons.append(el)
                elif tag == "column_set":
                    for col in el.get("columns", []):
                        _walk(col.get("elements", []))

        _walk(card.get("body", {}).get("elements", []))
        return buttons

    def test_build_streaming_card_schema_v2(self):
        from doyoutrade.assistant.channels.feishu.card import build_streaming_card
        card = build_streaming_card("Hello world", show_tool_use=False)
        self.assertEqual(card["schema"], "2.0")
        self.assertIn("body", card)
        self.assertIn("config", card)

    def test_all_sample_cards_satisfy_local_v2_contract(self):
        from doyoutrade.assistant.channels.feishu.card.validation import (
            sample_feishu_cards,
            validate_card_json_v2,
        )

        failures = []
        for name, card in sample_feishu_cards().items():
            for error in validate_card_json_v2(card, name=name):
                failures.append(error)

        self.assertEqual(failures, [])

    def test_build_streaming_card_with_reasoning_only(self):
        from doyoutrade.assistant.channels.feishu.card import build_streaming_card
        card = build_streaming_card("", show_tool_use=False, reasoning_text="Thinking...")
        self.assertEqual(card["schema"], "2.0")
        # reasoning content should be present
        elements = card["body"]["elements"]
        self.assertTrue(len(elements) > 0)

    def test_build_streaming_card_renders_answer_only(self):
        from doyoutrade.assistant.channels.feishu.card import STREAMING_ELEMENT_ID, build_streaming_card

        card = build_streaming_card(
            "Final **answer** with `code`",
            show_tool_use=True,
            reasoning_text="Need to inspect data",
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "data_bars_relative",
                    "category": "kline",
                    "input": {"symbol": "600000.SH"},
                    "status": "completed",
                    "result": {"output": {"rows": 3}, "is_error": False},
                }
            ],
        )

        elements = card["body"]["elements"]
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["tag"], "markdown")
        self.assertEqual(elements[0]["element_id"], STREAMING_ELEMENT_ID)
        self.assertIn("**answer**", elements[0]["content"])
        self.assertIn("`code`", elements[0]["content"])

    def test_build_streaming_card_omits_stop_button_without_session_id(self):
        from doyoutrade.assistant.channels.feishu.card import build_streaming_card

        card = build_streaming_card("partial", show_tool_use=False)
        self.assertEqual(len(card["body"]["elements"]), 1)
        self.assertNotIn("stop_attempt", str(card))

    def test_build_streaming_card_appends_stop_button_with_session_id(self):
        from doyoutrade.assistant.channels.feishu.card import build_streaming_card
        from doyoutrade.assistant.channels.feishu.card.validation import (
            validate_card_json_v2,
        )

        card = build_streaming_card("partial", show_tool_use=False, session_id="asst-1")
        elements = card["body"]["elements"]
        self.assertEqual(elements[0]["tag"], "markdown")
        # The stop button lives in a column_set row appended after the markdown.
        button_row = elements[-1]
        self.assertEqual(button_row["tag"], "column_set")
        button = button_row["columns"][0]["elements"][0]
        self.assertEqual(button["tag"], "button")
        self.assertEqual(button["type"], "danger")
        self.assertEqual(
            button["value"], {"action": "stop_attempt", "session_id": "asst-1"}
        )
        self.assertTrue(button["text"]["content"].strip())
        # Adding the button must not break the local V2 contract.
        self.assertEqual(validate_card_json_v2(card, name="streaming_stop"), [])

    def test_build_thinking_card_does_not_include_tool_panel(self):
        from doyoutrade.assistant.channels.feishu.card import build_thinking_card

        card = build_thinking_card("Need to inspect data")

        elements = card["body"]["elements"]
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["tag"], "markdown")
        self.assertIn("Need to inspect data", elements[0]["content"])
        self.assertNotIn("Tool Use", str(card))
        self.assertNotIn("工具使用", str(card))

    def test_build_complete_card_schema_v2(self):
        from doyoutrade.assistant.channels.feishu.card import build_complete_card
        card = build_complete_card("Final answer", show_tool_use=False)
        self.assertEqual(card["schema"], "2.0")
        self.assertIn("body", card)

    def test_build_complete_card_uses_only_v2_supported_tags(self):
        from doyoutrade.assistant.channels.feishu.card import build_complete_card

        card = build_complete_card(
            "Final answer",
            show_tool_use=True,
            reasoning_text="Thought process",
            elapsed_ms=1234,
            reasoning_elapsed_ms=567,
        )

        def collect_tags(value):
            if isinstance(value, dict):
                tags = [value["tag"]] if "tag" in value else []
                for child in value.values():
                    tags.extend(collect_tags(child))
                return tags
            if isinstance(value, list):
                tags = []
                for child in value:
                    tags.extend(collect_tags(child))
                return tags
            return []

        self.assertNotIn("note", collect_tags(card))

    def test_build_complete_card_with_reasoning(self):
        from doyoutrade.assistant.channels.feishu.card import build_complete_card
        card = build_complete_card("Answer", show_tool_use=False, reasoning_text="Thought process")
        self.assertEqual(card["schema"], "2.0")

    def test_build_confirm_card_has_header(self):
        from doyoutrade.assistant.channels.feishu.card import build_confirm_card, ConfirmData
        data = ConfirmData(operation_description="Execute trade?", pending_operation_id="op123")
        card = build_confirm_card(data)
        self.assertIn("header", card)
        self.assertEqual(card["header"]["template"], "orange")

    def test_build_confirm_card_has_confirm_button(self):
        from doyoutrade.assistant.channels.feishu.card import build_confirm_card, ConfirmData
        data = ConfirmData(operation_description="Execute trade?", pending_operation_id="op123")
        card = build_confirm_card(data)
        self.assertNotIn("action", self._all_tags(card))
        button_tags = [button.get("tag") for button in self._all_buttons(card)]
        self.assertIn("button", button_tags)

    def test_build_ask_user_card_pending_payload(self):
        # build_ask_user_card now takes the pending_user_question payload
        # persisted by the ask_user_question tool (option buttons + free-text
        # input fallback). Detailed shape assertions live in
        # tests/test_assistant_ask_user_question.py.
        from doyoutrade.assistant.channels.feishu.card import build_ask_user_card
        card = build_ask_user_card(
            {
                "question_id": "uq-ask123",
                "question": "What is your name?",
                "options": [{"label": "Option A"}, {"label": "Option B"}],
            }
        )
        self.assertEqual(card["schema"], "2.0")
        self.assertIn("header", card)
        option_labels = [
            b["text"]["content"] for b in self._all_buttons(card)
            if b.get("value", {}).get("action") == "ask_user_select"
        ]
        self.assertEqual(option_labels, ["Option A", "Option B"])
        self.assertNotIn("action", self._all_tags(card))
        elements = card["body"]["elements"]
        self.assertTrue(any(e.get("tag") == "input" for e in elements))

    def test_build_ask_user_answered_card_is_terminal(self):
        from doyoutrade.assistant.channels.feishu.card.builder import (
            build_ask_user_answered_card,
        )

        card = build_ask_user_answered_card("Option A")
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["template"], "green")
        self.assertEqual(self._all_buttons(card), [])
        elements = card["body"]["elements"]
        self.assertFalse(any(e.get("tag") == "input" for e in elements))
        self.assertIn("Option A", str(elements))

    def test_build_ask_user_answered_card_marks_submitted(self):
        from doyoutrade.assistant.channels.feishu.card.builder import (
            build_ask_user_answered_card,
        )

        card = build_ask_user_answered_card("自定义答复", submitted=True)
        self.assertIn("已回答", card["header"]["title"]["content"])
        self.assertIn("自定义答复", str(card["body"]["elements"]))

    def test_build_approval_card_uses_card_json_v2_button_layout(self):
        from doyoutrade.assistant.channels.feishu.card.builder import build_approval_card

        card = build_approval_card(
            {
                "approval_id": "appr-1",
                "description": "停止交易任务",
                "command_preview": "doyoutrade-cli task stop task-1",
                "timeout_seconds": 300,
            }
        )

        self.assertEqual(card["schema"], "2.0")
        self.assertNotIn("action", self._all_tags(card))
        labels = [button["text"]["content"] for button in self._all_buttons(card)]
        self.assertEqual(labels, ["允许一次", "总是允许", "拒绝"])
        decisions = [button["value"]["decision"] for button in self._all_buttons(card)]
        self.assertEqual(decisions, ["approve_once", "approve_always", "reject"])
        button_rows = [
            element
            for element in card["body"]["elements"]
            if element.get("tag") == "column_set"
        ]
        self.assertEqual(len(button_rows), 3)
        self.assertTrue(
            all(len(row.get("columns", [])) == 1 for row in button_rows)
        )

    def test_build_approval_resolved_card_has_no_buttons(self):
        from doyoutrade.assistant.channels.feishu.card import build_approval_resolved_card

        card = build_approval_resolved_card(
            {
                "approval_id": "appr-1",
                "description": "停止交易任务",
                "command_preview": "doyoutrade-cli task stop task-1",
            },
            decision="approve_once",
            resolver="ou_user",
        )

        self.assertEqual(card["header"]["template"], "green")
        self.assertNotIn("button", self._all_tags(card))
        self.assertIn("停止交易任务", str(card))
        self.assertIn("doyoutrade-cli task stop task-1", str(card))

    def test_build_thinking_card(self):
        from doyoutrade.assistant.channels.feishu.card import build_thinking_card
        card = build_thinking_card()
        self.assertEqual(card["schema"], "2.0")

    def test_streaming_element_id_constant(self):
        from doyoutrade.assistant.channels.feishu.card import STREAMING_ELEMENT_ID
        self.assertEqual(STREAMING_ELEMENT_ID, "streaming_text")

    def test_confirm_data_dataclass(self):
        from doyoutrade.assistant.channels.feishu.card import ConfirmData
        data = ConfirmData(operation_description="Test", pending_operation_id="op1", preview="preview text")
        self.assertEqual(data.operation_description, "Test")
        self.assertEqual(data.pending_operation_id, "op1")
        self.assertEqual(data.preview, "preview text")


class TestTradeApprovalCard(unittest.TestCase):
    def _payload(self, **overrides):
        payload = {
            "approval_id": "ap-123",
            "intent_id": "intent-9",
            "task_id": "task-7",
            "symbol": "600000.SH",
            "action": "buy",
            "notional": "10000.50",
            "strategy_tag": "ma_cross",
            "created_at": "2026-06-14T09:30:00",
            "timeout_seconds": 300,
        }
        payload.update(overrides)
        return payload

    @staticmethod
    def _all_buttons(card):
        # Card JSON 2.0: buttons live directly in body.elements OR nested inside
        # a column_set's columns (the valid 2.0 horizontal layout). Recurse so
        # the contract tests find them regardless of layout container.
        buttons = []

        def _walk(elements):
            for el in elements or []:
                tag = el.get("tag")
                if tag == "button":
                    buttons.append(el)
                elif tag == "action":  # legacy v1.0 container
                    _walk(el.get("elements", []))
                elif tag == "column_set":
                    for col in el.get("columns", []):
                        _walk(col.get("elements", []))

        _walk(card.get("body", {}).get("elements", []))
        return buttons

    def test_build_trade_approval_card_schema_and_red_header(self):
        from doyoutrade.assistant.channels.feishu.card import build_trade_approval_card
        card = build_trade_approval_card(self._payload())
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["template"], "red")
        self.assertIn("实盘交易审批", card["header"]["title"]["content"])

    def test_build_trade_approval_card_renders_order_facts(self):
        from doyoutrade.assistant.channels.feishu.card import build_trade_approval_card
        card = build_trade_approval_card(self._payload())
        body = str(card)
        self.assertIn("600000.SH", body)
        self.assertIn("买入", body)        # buy -> 买入
        self.assertIn("10000.50", body)    # notional decimal string verbatim
        self.assertIn("名义金额", body)
        self.assertIn("ma_cross", body)
        self.assertIn("task-7", body)
        self.assertIn("2026-06-14T09:30:00", body)

    def test_build_trade_approval_card_sell_maps_to_chinese(self):
        from doyoutrade.assistant.channels.feishu.card import build_trade_approval_card
        card = build_trade_approval_card(self._payload(action="sell"))
        self.assertIn("卖出", str(card))

    def test_build_trade_approval_card_button_value_contract(self):
        from doyoutrade.assistant.channels.feishu.card import build_trade_approval_card
        card = build_trade_approval_card(self._payload())
        buttons = self._all_buttons(card)
        self.assertEqual(len(buttons), 2)

        values = []
        for btn in buttons:
            behaviors = btn["behaviors"]
            self.assertEqual(behaviors[0]["type"], "callback")
            values.append(behaviors[0]["value"])

        approve = next(v for v in values if v["decision"] == "approve")
        reject = next(v for v in values if v["decision"] == "reject")
        for value in (approve, reject):
            self.assertEqual(value["action"], "trade_approval_resolve")
            self.assertEqual(value["approval_id"], "ap-123")
            self.assertEqual(value["task_id"], "task-7")
            self.assertEqual(value["intent_id"], "intent-9")
        # Must NOT reuse the assistant broker action name.
        self.assertNotIn("approval_resolve", [v["action"] for v in values])

    def test_build_trade_approval_card_renders_signal_section(self):
        from doyoutrade.assistant.channels.feishu.card import build_trade_approval_card
        card = build_trade_approval_card(
            self._payload(rationale="网格下轨触发买入", signal_tag="grid_buy_1")
        )
        body = str(card)
        self.assertIn("下单信息（成交以此为准）", body)   # authoritative facts header
        self.assertIn("**审批**", body)                  # 审批 section header
        self.assertIn("理由", body)
        self.assertIn("网格下轨触发买入", body)
        self.assertIn("grid_buy_1", body)

    def test_build_trade_approval_card_names_the_stock(self):
        from doyoutrade.assistant.channels.feishu.card import build_trade_approval_card
        card = build_trade_approval_card(self._payload(symbol_name="工商银行"))
        body = str(card)
        self.assertIn("工商银行", body)
        self.assertIn("600000.SH", body)  # name AND symbol both shown

    def test_build_trade_approval_card_prose_separates_ai_from_facts(self):
        # SAFETY: prose mode shows the Agent narration UNDER a「🤖 AI 解读」caveat,
        # SEPARATED from the deterministic「下单信息（成交以此为准）」facts block which
        # is ALWAYS present — so a hallucination is visibly advisory, not the order.
        from doyoutrade.assistant.channels.feishu.card import build_trade_approval_card
        narration = "【工商银行 600000.SH】建议买入约 ¥10000，网格下轨触发。请在卡片上批准或拒绝。"
        card = build_trade_approval_card(self._payload(symbol_name="工商银行"), narration)
        body = str(card)
        self.assertIn("🤖 AI 解读", body)               # AI section captioned
        self.assertIn("下单不依据此文本", body)          # advisory caveat
        self.assertIn("网格下轨触发", body)              # narration present
        self.assertIn("下单信息（成交以此为准）", body)   # authoritative block label
        # Deterministic facts still render alongside the narration (cross-check).
        self.assertIn("工商银行", body)
        self.assertIn("600000.SH", body)
        self.assertIn("10000.50", body)                 # notional verbatim
        buttons = self._all_buttons(card)
        self.assertEqual(len(buttons), 2)               # buttons preserved (功能不阉割)
        decisions = {b["behaviors"][0]["value"]["decision"] for b in buttons}
        self.assertEqual(decisions, {"approve", "reject"})

    def test_build_trade_approval_card_facts_block_always_present(self):
        # Even without narration the authoritative 下单信息 block is labeled + shown.
        from doyoutrade.assistant.channels.feishu.card import build_trade_approval_card
        body = str(build_trade_approval_card(self._payload()))
        self.assertIn("下单信息（成交以此为准）", body)
        self.assertNotIn("🤖 AI 解读", body)

    def test_build_trade_approval_card_renders_rich_signal_data(self):
        # Parity with the pure signal digest: 现价/涨跌幅 + 限价 + 订单类型·有效期
        # + 方向[signal_tag] must all render.
        from doyoutrade.assistant.channels.feishu.card import build_trade_approval_card
        card = build_trade_approval_card(
            self._payload(
                price_reference="7.80",
                order_type="limit",
                tif="day",
                last_price="7.78",
                pct_change="+1.2%",
                direction="buy",
                signal_tag="grid_buy_1",
            )
        )
        body = str(card)
        self.assertIn("现价", body)
        self.assertIn("7.78", body)
        self.assertIn("+1.2%", body)
        self.assertIn("限价", body)
        self.assertIn("7.80", body)
        self.assertIn("限价单", body)        # order_type label
        self.assertIn("当日有效", body)      # tif label
        self.assertIn("grid_buy_1", body)    # signal_tag in 方向

    def test_build_trade_approval_card_button_value_carries_signal_facts(self):
        # The terminal card is rebuilt from the button value alone, so the value
        # must carry the order facts + signal context (order side under ``side``,
        # NOT ``action`` which is the callback action name).
        from doyoutrade.assistant.channels.feishu.card import build_trade_approval_card
        card = build_trade_approval_card(
            self._payload(rationale="网格下轨触发买入", signal_tag="grid_buy_1")
        )
        value = self._all_buttons(card)[0]["behaviors"][0]["value"]
        self.assertEqual(value["side"], "buy")
        self.assertEqual(value["symbol"], "600000.SH")
        self.assertEqual(value["notional"], "10000.50")
        self.assertEqual(value["strategy_tag"], "ma_cross")
        self.assertEqual(value["signal_tag"], "grid_buy_1")
        self.assertIn("网格下轨触发买入", value["rationale"])
        # ``action`` stays the callback action, never the order side.
        self.assertEqual(value["action"], "trade_approval_resolve")

    def test_build_trade_approval_card_button_types(self):
        from doyoutrade.assistant.channels.feishu.card import build_trade_approval_card
        card = build_trade_approval_card(self._payload())
        buttons = self._all_buttons(card)
        type_by_decision = {
            b["behaviors"][0]["value"]["decision"]: b["type"] for b in buttons
        }
        self.assertEqual(type_by_decision["approve"], "primary")
        self.assertEqual(type_by_decision["reject"], "danger")

    def test_build_trade_approval_card_shows_expiry(self):
        from doyoutrade.assistant.channels.feishu.card import build_trade_approval_card
        card = build_trade_approval_card(self._payload(timeout_seconds=120))
        self.assertIn("120 秒", str(card))

    def test_build_trade_approval_resolved_card_approved_is_green(self):
        from doyoutrade.assistant.channels.feishu.card import (
            build_trade_approval_resolved_card,
        )
        card = build_trade_approval_resolved_card(
            self._payload(), decision="approve", resolver="ou_admin"
        )
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["template"], "green")
        self.assertIn("已批准", card["header"]["title"]["content"])
        # Same order facts + resolver, no buttons.
        self.assertIn("600000.SH", str(card))
        self.assertIn("ou_admin", str(card))
        self.assertEqual(self._all_buttons(card), [])

    def test_build_trade_approval_resolved_card_rejected_is_grey(self):
        from doyoutrade.assistant.channels.feishu.card import (
            build_trade_approval_resolved_card,
        )
        card = build_trade_approval_resolved_card(
            self._payload(), decision="reject", resolver="ou_admin"
        )
        self.assertEqual(card["header"]["template"], "grey")
        self.assertIn("已拒绝", card["header"]["title"]["content"])
        self.assertEqual(self._all_buttons(card), [])

    def test_pending_card_button_value_carries_capped_narration(self):
        # The terminal card is rebuilt from the button value alone, so the AI 解读
        # must travel there (display-only) — capped so the callback value stays small.
        from doyoutrade.assistant.channels.feishu.card.builder import (
            _APPROVAL_CALLBACK_NARRATION_MAX,
            build_trade_approval_card,
        )
        long_narration = "网" * (_APPROVAL_CALLBACK_NARRATION_MAX + 200)
        card = build_trade_approval_card(self._payload(), long_narration)
        value = self._all_buttons(card)[0]["behaviors"][0]["value"]
        self.assertEqual(len(value["narration"]), _APPROVAL_CALLBACK_NARRATION_MAX)
        # card/none mode (no narration) → empty string, not missing key.
        card2 = build_trade_approval_card(self._payload())
        self.assertEqual(self._all_buttons(card2)[0]["behaviors"][0]["value"]["narration"], "")

    def test_resolved_card_preserves_ai_interpretation(self):
        # R1: after approve/reject the「🤖 AI 解读」the operator saw must survive
        # (carried via the button value into payload["narration"]), not vanish.
        from doyoutrade.assistant.channels.feishu.card import (
            build_trade_approval_resolved_card,
        )
        card = build_trade_approval_resolved_card(
            self._payload(narration="工商银行触发网格下轨买入信号，建议买入 780 元。"),
            decision="approve",
            resolver="ou_admin",
        )
        body = str(card)
        self.assertIn("🤖 AI 解读", body)
        self.assertIn("下单不依据此文本", body)        # advisory caveat preserved
        self.assertIn("工商银行触发网格下轨买入信号", body)
        self.assertIn("600000.SH", body)               # deterministic facts still shown
        self.assertEqual(self._all_buttons(card), [])   # terminal: no buttons

    def test_resolved_card_without_narration_has_no_ai_block(self):
        from doyoutrade.assistant.channels.feishu.card import (
            build_trade_approval_resolved_card,
        )
        card = build_trade_approval_resolved_card(
            self._payload(), decision="reject", resolver="ou_admin"
        )
        self.assertNotIn("🤖 AI 解读", str(card))

    def test_build_trade_approval_result_card_filled_is_green(self):
        # R2: post-dispatch receipt — actual fill facts, green 已成交, no buttons.
        from doyoutrade.assistant.channels.feishu.card import (
            build_trade_approval_result_card,
        )
        card = build_trade_approval_result_card(
            self._payload(
                symbol_name="工商银行",
                fill_quantity="100",
                fill_price="7.79",
                fill_amount="779.00",
                fill_time="2026-06-14 13:05:00",
            ),
            outcome="filled",
        )
        body = str(card)
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["template"], "green")
        self.assertIn("已成交", card["header"]["title"]["content"])
        self.assertIn("成交价", body)
        self.assertIn("7.79", body)
        self.assertIn("779.00", body)              # 成交金额
        self.assertIn("成交时间", body)
        self.assertIn("2026-06-14 13:05:00", body)
        self.assertIn("工商银行", body)
        self.assertIn("ap-123", body)              # approval traceability footer
        self.assertEqual(self._all_buttons(card), [])

    def test_build_trade_approval_result_card_abandoned_is_red(self):
        from doyoutrade.assistant.channels.feishu.card import (
            build_trade_approval_result_card,
        )
        card = build_trade_approval_result_card(
            self._payload(error="broker rejected: insufficient funds"),
            outcome="abandoned",
        )
        body = str(card)
        self.assertEqual(card["header"]["template"], "red")
        self.assertIn("未成交", card["header"]["title"]["content"])
        self.assertIn("失败原因", body)
        self.assertIn("insufficient funds", body)
        self.assertIn("计划金额", body)            # planned notional shown
        self.assertIn("10000.50", body)
        self.assertEqual(self._all_buttons(card), [])

    def test_build_trade_approval_result_card_failed_without_error_has_default_reason(self):
        from doyoutrade.assistant.channels.feishu.card import (
            build_trade_approval_result_card,
        )
        card = build_trade_approval_result_card(self._payload(), outcome="failed")
        self.assertIn("零成交", str(card))         # sentinel reason, never blank


class TestStreamingController(unittest.TestCase):
    def test_phase_enum_values(self):
        from doyoutrade.assistant.channels.feishu.card.streaming import CardPhase
        self.assertEqual(CardPhase.IDLE, "idle")
        self.assertEqual(CardPhase.CREATING, "creating")
        self.assertEqual(CardPhase.STREAMING, "streaming")
        self.assertEqual(CardPhase.COMPLETED, "completed")
        self.assertEqual(CardPhase.ABORTED, "aborted")
        self.assertEqual(CardPhase.TERMINATED, "terminated")

    def test_phase_transitions_idle_to_creating(self):
        from doyoutrade.assistant.channels.feishu.card.streaming import CardPhase, PHASE_TRANSITIONS
        self.assertIn(CardPhase.CREATING, PHASE_TRANSITIONS[CardPhase.IDLE])

    def test_phase_transitions_streaming_to_completed(self):
        from doyoutrade.assistant.channels.feishu.card.streaming import CardPhase, PHASE_TRANSITIONS
        self.assertIn(CardPhase.COMPLETED, PHASE_TRANSITIONS[CardPhase.STREAMING])

    def test_phase_transitions_streaming_to_aborted(self):
        from doyoutrade.assistant.channels.feishu.card.streaming import CardPhase, PHASE_TRANSITIONS
        self.assertIn(CardPhase.ABORTED, PHASE_TRANSITIONS[CardPhase.STREAMING])

    def test_phase_transitions_invalid_rejected(self):
        from doyoutrade.assistant.channels.feishu.card.streaming import CardPhase, PHASE_TRANSITIONS
        # idle still cannot jump directly into streaming
        self.assertNotIn(CardPhase.STREAMING, PHASE_TRANSITIONS[CardPhase.IDLE])

    def test_flush_controller_has_throttled_update(self):
        from doyoutrade.assistant.channels.feishu.card.streaming import FlushController
        fc = FlushController(lambda: None)
        self.assertTrue(hasattr(fc, "throttled_update"))
        self.assertTrue(callable(fc.throttled_update))

    def test_flush_controller_has_wait_for_flush(self):
        from doyoutrade.assistant.channels.feishu.card.streaming import FlushController
        fc = FlushController(lambda: None)
        self.assertTrue(hasattr(fc, "wait_for_flush"))
        self.assertTrue(callable(fc.wait_for_flush))

    def test_streaming_controller_has_callback_methods(self):
        from doyoutrade.assistant.channels.feishu.card import StreamingCardController
        self.assertTrue(hasattr(StreamingCardController, "on_partial_reply"))
        self.assertTrue(hasattr(StreamingCardController, "on_reasoning_stream"))
        self.assertTrue(hasattr(StreamingCardController, "on_tool_start"))
        self.assertTrue(hasattr(StreamingCardController, "on_tool_result"))
        self.assertTrue(hasattr(StreamingCardController, "on_idle"))
        self.assertTrue(hasattr(StreamingCardController, "abort_card"))

    def test_throttle_constants(self):
        from doyoutrade.assistant.channels.feishu.card.streaming import THROTTLE_CARDKIT_MS, THROTTLE_PATCH_MS
        self.assertEqual(THROTTLE_CARDKIT_MS, 300)
        self.assertEqual(THROTTLE_PATCH_MS, 500)

    def test_streaming_controller_sends_separate_cards_by_event_type(self):
        import asyncio
        from doyoutrade.assistant.channels.feishu.card import StreamingCardController

        class FakeCardKit:
            def __init__(self):
                self.created_cards = []
                self.sent_cards = []
                self.updated_cards = []
                self.streamed_content = []
                self.streaming_settings = []

            def create_card(self, card):
                card_id = f"card_{len(self.created_cards) + 1}"
                self.created_cards.append((card_id, card))
                return card_id

            def send_card_by_card_id(
                self,
                card_id,
                receive_id,
                receive_id_type="open_id",
                reply_to_message_id=None,
            ):
                self.sent_cards.append((card_id, receive_id, receive_id_type))
                return f"msg_{card_id}"

            def update_card(self, card_id, card, sequence=None):
                self.updated_cards.append((card_id, card, sequence))
                return True

            def stream_card_content(self, card_id, element_id, content, sequence=None):
                self.streamed_content.append((card_id, element_id, content, sequence))
                return True

            def set_streaming_mode(self, card_id, streaming_mode, sequence=None):
                self.streaming_settings.append((card_id, streaming_mode, sequence))
                return True

            def send_card_json(self, card, receive_id, receive_id_type="open_id", reply_to_message_id=None):
                self.sent_cards.append(("json", receive_id, receive_id_type, card))
                return f"msg_json_{len(self.sent_cards)}"

            def patch_message(self, message_id, card):
                self.updated_cards.append((message_id, card, None))
                return True

        async def run():
            fake = FakeCardKit()
            controller = StreamingCardController(
                cardkit_client=fake,
                chat_id="ou_user",
                receive_id="ou_user",
            )
            await controller.on_reasoning_stream("thinking text")
            await controller.on_tool_start(
                "data_bars_relative",
                tool_call_id="call_1",
                arguments={"symbol": "600000.SH"},
                category="kline",
            )
            await controller.on_partial_reply("answer text")
            await controller.on_tool_result(
                "call_1",
                name="data_bars_relative",
                preview='{"status":"ok"}',
            )
            await controller.on_idle()
            return fake

        fake = asyncio.run(run())

        # With spec-compliant behavior, on_partial_reply uses send_card_json directly
        # when no precreated_id exists (not create_card + send_card_by_card_id).
        # So only reasoning and tool_call create cards via create_card.
        self.assertGreaterEqual(len(fake.created_cards), 2)
        first_card = fake.created_cards[0][1]
        second_card = fake.created_cards[1][1]
        self.assertIn("thinking text", str(first_card))
        self.assertNotIn("Tool Use", str(first_card))
        self.assertIn("data_bars_relative", str(second_card))
        self.assertNotIn("answer text", str(second_card))
        # answer text is deferred to the final main body card, which must be sent last
        self.assertIn("answer text", str(fake.sent_cards[-1]) + str(fake.updated_cards))
        self.assertEqual([item[0] for item in fake.sent_cards], ["card_1", "card_2", "json"])

    def test_streaming_controller_on_idle_completes_after_partial_reply(self):
        import asyncio
        from doyoutrade.assistant.channels.feishu.card import StreamingCardController

        class FakeCardKit:
            def __init__(self):
                self.updated_cards = []
                self.streaming_settings = []

            def send_card_json(self, card, receive_id, receive_id_type="open_id", reply_to_message_id=None):
                return "msg_json_1"

            def patch_message(self, message_id, card):
                self.updated_cards.append((message_id, card))
                return True

            def set_streaming_mode(self, card_id, streaming_mode, sequence=None):
                self.streaming_settings.append((card_id, streaming_mode, sequence))
                return True

        async def run():
            controller = StreamingCardController(
                cardkit_client=FakeCardKit(),
                chat_id="ou_user",
                receive_id="ou_user",
            )
            await controller.on_partial_reply("answer")
            await asyncio.wait_for(controller.on_idle(), timeout=0.2)

        asyncio.run(run())

    def test_streaming_controller_on_idle_raises_when_final_card_delivery_fails(self):
        import asyncio
        from doyoutrade.assistant.channels.feishu.card import StreamingCardController

        class FakeCardKit:
            def create_card(self, card):
                return None

            def send_card_by_card_id(
                self,
                card_id,
                receive_id,
                receive_id_type="open_id",
                reply_to_message_id=None,
            ):
                return None

            def send_card_json(self, card, receive_id, receive_id_type="open_id", reply_to_message_id=None):
                return None

            def patch_message(self, message_id, card):
                return False

            def set_streaming_mode(self, card_id, streaming_mode, sequence=None):
                return False

            def update_card(self, card_id, card, sequence=None):
                return False

        async def run():
            controller = StreamingCardController(
                cardkit_client=FakeCardKit(),
                chat_id="ou_user",
                receive_id="ou_user",
            )
            await controller.on_tool_start("search", tool_call_id="tc1")
            await controller.on_partial_reply("answer")
            await controller.on_idle()

        with self.assertRaisesRegex(RuntimeError, "final card delivery failed"):
            asyncio.run(run())

    def test_reasoning_stream_after_text_starts_new_thinking_card(self):
        import asyncio
        from doyoutrade.assistant.channels.feishu.card import StreamingCardController

        class FakeCardKit:
            def __init__(self):
                self.created_cards = []
                self.sent_cards = []
                self.patched_messages = []

            def create_card(self, card):
                card_id = f"card_{len(self.created_cards) + 1}"
                self.created_cards.append((card_id, card))
                return card_id

            def send_card_by_card_id(
                self,
                card_id,
                receive_id,
                receive_id_type="open_id",
                reply_to_message_id=None,
            ):
                message_id = f"msg_{len(self.sent_cards) + 1}"
                self.sent_cards.append((card_id, message_id))
                return message_id

            def send_card_json(self, card, receive_id, receive_id_type="open_id", reply_to_message_id=None):
                message_id = f"msg_json_{len(self.sent_cards) + 1}"
                self.sent_cards.append(("json", message_id))
                return message_id

            def patch_message(self, message_id, card):
                self.patched_messages.append((message_id, card))
                return True

            def stream_card_content(self, card_id, element_id, content, sequence=None):
                return True

            def set_streaming_mode(self, card_id, streaming_mode, sequence=None):
                return True

        async def run():
            fake = FakeCardKit()
            controller = StreamingCardController(
                cardkit_client=fake,
                chat_id="ou_user",
                receive_id="ou_user",
            )
            await controller.on_reasoning_stream("first thinking")
            await controller.on_partial_reply("first answer")
            await controller.on_reasoning_stream("second thinking")
            return fake

        fake = asyncio.run(run())

        # Two thinking cards via create_card; the text segment between them is its
        # own card delivered via send_card_json, in chronological order.
        self.assertEqual(len(fake.created_cards), 2)
        self.assertIn("first thinking", str(fake.created_cards[0][1]))
        self.assertIn("second thinking", str(fake.created_cards[1][1]))
        self.assertEqual([item[0] for item in fake.sent_cards], ["card_1", "json", "card_2"])
        self.assertIn("first answer", str(fake.patched_messages))
        self.assertNotIn("second thinking", str(fake.patched_messages))

    def test_tool_start_closes_active_reasoning_stream_before_tool_card(self):
        import asyncio
        from doyoutrade.assistant.channels.feishu.card import StreamingCardController

        class FakeCardKit:
            def __init__(self):
                self.ops = []

            def create_card(self, card):
                card_id = f"card_{len([op for op in self.ops if op[0] == 'create']) + 1}"
                self.ops.append(("create", card_id, card))
                return card_id

            def send_card_by_card_id(
                self,
                card_id,
                receive_id,
                receive_id_type="open_id",
                reply_to_message_id=None,
            ):
                self.ops.append(("send", card_id))
                return f"msg_{card_id}"

            def set_streaming_mode(self, card_id, streaming_mode, sequence=None):
                self.ops.append(("streaming", card_id, streaming_mode, sequence))
                return True

            def stream_card_content(self, card_id, element_id, content, sequence=None):
                self.ops.append(("content", card_id, content, sequence))
                return True

            def update_card(self, card_id, card, sequence=None):
                self.ops.append(("update", card_id, sequence))
                return True

        async def run():
            fake = FakeCardKit()
            controller = StreamingCardController(
                cardkit_client=fake,
                chat_id="ou_user",
                receive_id="ou_user",
            )
            await controller.on_reasoning_stream("thinking")
            await controller.on_tool_start("search", tool_call_id="call_1")
            return fake

        fake = asyncio.run(run())
        close_index = fake.ops.index(("streaming", "card_1", False, 2))
        tool_create_index = fake.ops.index(("create", "card_2", fake.ops[3][2]))
        self.assertLess(close_index, tool_create_index)

    def test_multiturn_thinking_tool_thinking_final_text_sends_final_body_last(self):
        import asyncio
        from doyoutrade.assistant.channels.feishu.card import StreamingCardController

        class FakeCardKit:
            def __init__(self):
                self.created_cards = []
                self.sent_cards = []
                self.updated_cards = []
                self.streaming_settings = []

            def create_card(self, card):
                card_id = f"card_{len(self.created_cards) + 1}"
                self.created_cards.append((card_id, card))
                return card_id

            def send_card_by_card_id(
                self,
                card_id,
                receive_id,
                receive_id_type="open_id",
                reply_to_message_id=None,
            ):
                message_id = f"msg_{len(self.sent_cards) + 1}"
                self.sent_cards.append((card_id, message_id))
                return message_id

            def send_card_json(self, card, receive_id, receive_id_type="open_id", reply_to_message_id=None):
                message_id = f"msg_json_{len(self.sent_cards) + 1}"
                self.sent_cards.append(("json", message_id, card))
                return message_id

            def update_card(self, card_id, card, sequence=None):
                self.updated_cards.append((card_id, card, sequence))
                return True

            def patch_message(self, message_id, card):
                self.updated_cards.append((message_id, card, None))
                return True

            def stream_card_content(self, card_id, element_id, content, sequence=None):
                self.updated_cards.append((card_id, content, sequence))
                return True

            def set_streaming_mode(self, card_id, streaming_mode, sequence=None):
                self.streaming_settings.append((card_id, streaming_mode, sequence))
                return True

        async def run():
            fake = FakeCardKit()
            controller = StreamingCardController(
                cardkit_client=fake,
                chat_id="ou_user",
                receive_id="ou_user",
            )
            await controller.on_reasoning_stream("thinking turn 1")
            await controller.on_tool_start(
                "search",
                tool_call_id="call_1",
                arguments={"q": "AAPL"},
                category="search",
            )
            await controller.on_tool_result(
                "call_1",
                name="search",
                preview='{"status":"ok"}',
            )
            await controller.on_reasoning_stream("thinking turn 2")
            await controller.on_partial_reply("final answer")
            await controller.on_idle()
            return fake

        fake = asyncio.run(run())

        self.assertEqual([item[0] for item in fake.sent_cards], ["card_1", "card_2", "card_3", "json"])
        self.assertIn("thinking turn 1", str(fake.created_cards[0][1]))
        self.assertIn("search", str(fake.created_cards[1][1]))
        self.assertIn("thinking turn 2", str(fake.created_cards[2][1]))
        self.assertIn("final answer", str(fake.sent_cards[-1][2]))
        self.assertNotIn("final answer", str(fake.created_cards[0][1]))
        self.assertNotIn("final answer", str(fake.created_cards[1][1]))
        self.assertNotIn("final answer", str(fake.created_cards[2][1]))

    def test_text_segments_interleaved_with_tools_create_separate_ordered_cards(self):
        """Reproduce the real bug: text streamed *before* and *between* tool calls
        must each land in its own card, in chronological send order — not all
        merged into a single final body card.

        Sequence mirrors a real session: think → preface text → 2 tools →
        middle text → 1 tool → think → final text.
        """
        import asyncio
        from doyoutrade.assistant.channels.feishu.card import StreamingCardController

        class FakeCardKit:
            def __init__(self):
                # ``deliveries`` records every card actually *sent* to the user,
                # in order, as (kind, content_str). Updates patch the latest
                # content for that delivery so segment-leak assertions see the
                # final rendered text.
                self.deliveries = []
                self._card_content = {}

            def create_card(self, card):
                card_id = f"card_{len(self._card_content) + 1}"
                self._card_content[card_id] = str(card)
                return card_id

            def send_card_by_card_id(
                self,
                card_id,
                receive_id,
                receive_id_type="open_id",
                reply_to_message_id=None,
            ):
                self.deliveries.append(["card", self._card_content.get(card_id, "")])
                return f"msg_{card_id}"

            def send_card_json(self, card, receive_id, receive_id_type="open_id", reply_to_message_id=None):
                self.deliveries.append(["json", str(card)])
                return f"msg_json_{len(self.deliveries)}"

            def update_card(self, card_id, card, sequence=None):
                self._card_content[card_id] = str(card)
                for delivery in self.deliveries:
                    if delivery[0] == "card":
                        pass
                return True

            def patch_message(self, message_id, card):
                # Patches always target the most-recently-delivered card.
                if self.deliveries:
                    self.deliveries[-1][1] = str(card)
                return True

            def stream_card_content(self, card_id, element_id, content, sequence=None):
                return True

            def set_streaming_mode(self, card_id, streaming_mode, sequence=None):
                return True

        async def run():
            fake = FakeCardKit()
            controller = StreamingCardController(
                cardkit_client=fake,
                chat_id="ou_user",
                receive_id="ou_user",
            )
            await controller.on_reasoning_stream("think zero")
            await controller.on_partial_reply("preface text")
            await controller.on_tool_start("alpha", tool_call_id="call_1")
            await controller.on_tool_result("call_1", name="alpha", preview='{"status":"ok"}')
            await controller.on_tool_start("beta", tool_call_id="call_2")
            await controller.on_tool_result("call_2", name="beta", preview='{"status":"ok"}')
            await controller.on_partial_reply("middle text")
            await controller.on_tool_start("gamma", tool_call_id="call_3")
            await controller.on_tool_result("call_3", name="gamma", preview='{"status":"ok"}')
            await controller.on_reasoning_stream("think two")
            await controller.on_partial_reply("final text")
            await controller.on_idle()
            return fake

        fake = asyncio.run(run())

        markers = ["think zero", "preface text", "alpha", "middle text", "gamma", "think two", "final text"]
        # Each segment appears in exactly one delivered card, in order.
        positions = []
        for marker in markers:
            hits = [i for i, (_kind, content) in enumerate(fake.deliveries) if marker in content]
            self.assertEqual(len(hits), 1, f"{marker!r} should be in exactly one card, found {hits}")
            positions.append(hits[0])
        self.assertEqual(positions, sorted(positions), "cards must be delivered in chronological order")

        # No text segment leaks into another segment's card.
        for kind, content in fake.deliveries:
            if "preface text" in content:
                self.assertNotIn("middle text", content)
                self.assertNotIn("final text", content)
            if "final text" in content:
                self.assertNotIn("preface text", content)
                self.assertNotIn("middle text", content)

    def test_tool_result_preview_with_error_status_marks_tool_card_failed(self):
        import asyncio
        from doyoutrade.assistant.channels.feishu.card import StreamingCardController

        class FakeCardKit:
            def __init__(self):
                self.created_cards = []
                self.sent_cards = []
                self.updated_cards = []

            def create_card(self, card):
                card_id = f"card_{len(self.created_cards) + 1}"
                self.created_cards.append((card_id, card))
                return card_id

            def send_card_by_card_id(
                self,
                card_id,
                receive_id,
                receive_id_type="open_id",
                reply_to_message_id=None,
            ):
                self.sent_cards.append((card_id, receive_id, receive_id_type))
                return f"msg_{card_id}"

            def update_card(self, card_id, card, sequence=None):
                self.updated_cards.append((card_id, card, sequence))
                return True

            def patch_message(self, message_id, card):
                self.updated_cards.append((message_id, card, None))
                return True

            def send_card_json(self, card, receive_id, receive_id_type="open_id", reply_to_message_id=None):
                self.sent_cards.append(("json", receive_id, receive_id_type, card))
                return "msg_json_1"

        async def run():
            fake = FakeCardKit()
            controller = StreamingCardController(
                cardkit_client=fake,
                chat_id="ou_user",
                receive_id="ou_user",
            )
            await controller.on_tool_start("bind_strategy_instance_to_task", tool_call_id="call_1")
            await controller.on_tool_result(
                "call_1",
                name="bind_strategy_instance_to_task",
                preview='{"status":"error","error":"strategy instance not found: "}',
            )
            return fake

        fake = asyncio.run(run())

        latest_card = fake.updated_cards[-1][1] if fake.updated_cards else fake.created_cards[-1][1]
        self.assertIn("失败", str(latest_card))
        self.assertIn("输出结果（错误）", str(latest_card))

    def test_streaming_controller_has_precreated_cards_parameter(self):
        from doyoutrade.assistant.channels.feishu.card.streaming import StreamingCardController
        import inspect
        sig = inspect.signature(StreamingCardController.__init__)
        param_names = [p.name for p in sig.parameters.values()]
        self.assertIn("precreated_cards", param_names)


class TestCardKitClientPrecreateCards(unittest.TestCase):
    def test_cardkit_precreate_cards_method_exists(self):
        from doyoutrade.assistant.channels.feishu.card.cardkit import CardKitClient
        client = CardKitClient(app_id="test", app_secret="test")
        assert hasattr(client, "precreate_cards")
        assert callable(client.precreate_cards)


class TestCardTemplates(unittest.TestCase):
    def test_templates_module_exists(self):
        from doyoutrade.assistant.channels.feishu.card import templates
        assert hasattr(templates, "THINKING_CARD_JSON")
        assert hasattr(templates, "STREAMING_CARD_JSON")
        assert hasattr(templates, "STREAMING_ELEMENT_ID")

    def test_thinking_card_json_schema(self):
        from doyoutrade.assistant.channels.feishu.card.templates import THINKING_CARD_JSON
        assert THINKING_CARD_JSON["schema"] == "2.0"
        assert "streaming_mode" in THINKING_CARD_JSON["config"]


if __name__ == "__main__":
    unittest.main()
