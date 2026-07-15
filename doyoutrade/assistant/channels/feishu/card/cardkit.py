"""CardKit API Client for Feishu interactive cards.

IMPORTANT: When implementing or debugging CardKit API calls, always cross-reference
the official Feishu CardKit API documentation:
https://open.feishu.cn/document/feishu-cards/card-json-v2-components/component-json-v2-overview

API endpoints used in this module:
- Card creation: POST /cardkit/v1/cards
- Card update: PUT /cardkit/v1/cards/{card_id}
- Element streaming: PUT /cardkit/v1/cards/{card_id}/elements/{element_id}/content
- Streaming settings: PATCH /cardkit/v1/cards/{card_id}/settings
- IM message sending: POST /im.v1.messages
- IM message patching: PATCH /im/v1/messages/{message_id}

Rate limit codes and error handling should be aligned with official docs.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Rate limit codes from CardKit API
_CARDKIT_RATE_LIMIT_CODE = 230020
_CARDKIT_TABLE_LIMIT_CODES = (230099, 11310)

# Token cache TTL (in seconds) — token typically expires in 2 hours
_TOKEN_TTL = 7200


class CardKitClient:
    """Client for Feishu CardKit API.

    CardKit API uses tenant access token (not app access token).
    """

    def __init__(self, app_id: str, app_secret: str, domain: str = "feishu"):
        """Initialize CardKitClient.

        Args:
            app_id: Feishu app ID.
            app_secret: Feishu app secret.
            domain: "feishu" (default) or "lark".
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self.domain = domain
        self._base_url = (
            "https://open.larksuite.com"
            if domain == "lark"
            else "https://open.feishu.cn"
        )
        self._token: str | None = None
        self._token_expires_at: float = 0  # unix timestamp when token expires
        self._token_lock = threading.Lock()
        self.last_error: dict[str, Any] | None = None

    def _record_error(self, operation: str, **fields: Any) -> None:
        self.last_error = {"operation": operation, **fields}

    # -------------------------------------------------------------------------
    # Token management
    # -------------------------------------------------------------------------

    def _get_tenant_access_token(self) -> str | None:
        """Fetch (or return cached) tenant access token.

        Returns:
            Tenant access token string, or None if fetching failed.
        """
        with self._token_lock:
            now = time.time()
            if self._token and now < self._token_expires_at:
                return self._token

            url = f"{self._base_url}/open-apis/auth/v3/tenant_access_token/internal"
            payload = {"app_id": self.app_id, "app_secret": self.app_secret}
            try:
                with httpx.Client(timeout=30.0) as client:
                    resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                logger.exception("CardKit: failed to get tenant access token")
                return None

            code = data.get("code", 0)
            if code != 0:
                logger.warning("CardKit: token response code=%s msg=%s", code, data.get("msg"))
                return None

            self._token = data.get("tenant_access_token")
            # Default expiry is 2 hours; cache with a small buffer
            self._token_expires_at = now + _TOKEN_TTL - 60
            return self._token

    def _refresh_token(self) -> str | None:
        """Force refresh of tenant access token."""
        self._token = None
        self._token_expires_at = 0
        return self._get_tenant_access_token()

    def _headers(self) -> dict[str, str]:
        """Build request headers with current tenant access token."""
        token = self._get_tenant_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    # -------------------------------------------------------------------------
    # Card API
    # -------------------------------------------------------------------------

    def create_card(self, card: dict[str, Any]) -> str | None:
        """Create an interactive card.

        Calls ``POST /cardkit/v1/cards``.

        Args:
            card: Card definition dict.

        Returns:
            Created card_id string, or None on failure.
        """
        self.last_error = None
        url = f"{self._base_url}/open-apis/cardkit/v1/cards"
        payload = {"type": "card_json", "data": json.dumps(card)}
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            resp = locals().get("resp")
            status = resp.status_code if resp else "N/A"
            body = resp.text if resp else str(exc)
            logger.warning("CardKit.create_card HTTP %s: %s", status, body[:500])
            logger.exception("CardKit.create_card failed")
            self._record_error(
                "create_card",
                error_type=type(exc).__name__,
                status=status,
                body=body[:500],
            )
            return None

        code = data.get("code", 0)
        if code == _CARDKIT_RATE_LIMIT_CODE:
            logger.warning("CardKit: rate limited on create_card — retry not implemented")
            self._record_error("create_card", code=code, msg=data.get("msg"))
            return None
        if code != 0:
            logger.warning("CardKit.create_card code=%s msg=%s", code, data.get("msg"))
            self._record_error("create_card", code=code, msg=data.get("msg"))
            return None

        return data.get("data", {}).get("card_id")

    def precreate_cards(
        self,
        card_configs: dict[str, str],
    ) -> dict[str, str]:
        """预创建多张卡片。

        Args:
            card_configs: {content_type: placeholder} 的映射。
                         content_type 如 "thinking", "tool_call", "rich_text"。
                         placeholder 值目前未使用（预留，后续可用于模板自定义）。
                         目前通过 get_template_for_content_type() 获取内部模板。

        Returns:
            {content_type: created_card_id} 的映射。
            created_card_id 是 CardKit API 返回的实际卡片 ID。

        Raises:
            Exception: 任何一张卡片创建失败则抛异常。
        """
        from .templates import get_template_for_content_type

        results = {}
        for content_type, _ in card_configs.items():
            template = get_template_for_content_type(content_type)
            created_id = self.create_card(template)
            if not created_id:
                raise Exception(f"CardKit precreate failed for {content_type}")
            results[content_type] = created_id
        return results

    def send_card_by_card_id(
        self,
        card_id: str,
        receive_id: str,
        receive_id_type: str = "open_id",
        reply_to_message_id: str | None = None,
    ) -> str | None:
        """Send a card to a user by card_id.

        Calls ``POST /im.v1.messages`` with msg_type=interactive.

        Args:
            card_id: The card_id returned by create_card.
            receive_id: The recipient's ID (open_id, user_id, union_id, etc.).
            receive_id_type: Type of receive_id. Defaults to "open_id".

        Returns:
            message_id string, or None on failure.
        """
        self.last_error = None
        url = f"{self._base_url}/open-apis/im/v1/messages"
        params = {"receive_id_type": receive_id_type}
        payload = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps({"type": "card", "data": {"card_id": card_id}}),
        }
        if reply_to_message_id:
            params["reply_to_message_id"] = reply_to_message_id
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, headers=self._headers(), params=params, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            resp = locals().get("resp")
            status = resp.status_code if resp else "N/A"
            body = resp.text if resp else str(exc)
            logger.exception("CardKit.send_card_by_card_id failed")
            self._record_error(
                "send_card_by_card_id",
                error_type=type(exc).__name__,
                status=status,
                body=body[:500],
            )
            return None

        code = data.get("code", 0)
        if code != 0:
            logger.warning(
                "CardKit.send_card_by_card_id code=%s msg=%s", code, data.get("msg")
            )
            self._record_error("send_card_by_card_id", code=code, msg=data.get("msg"))
            return None

        return data.get("data", {}).get("message_id")

    def send_card_json(
        self,
        card: dict[str, Any],
        receive_id: str,
        receive_id_type: str = "open_id",
        reply_to_message_id: str | None = None,
    ) -> str | None:
        """Send a card JSON directly as an interactive message (no CardKit needed).

        Sends ``POST /im.v1.messages`` with msg_type=interactive and the full
        card JSON as content. Used when CardKit is unavailable — the card is
        rendered by Feishu clients without needing a CardKit card entity.

        Args:
            card: Full card JSON (schema 2.0).
            receive_id: The recipient's ID (open_id, user_id, union_id, etc.).
            receive_id_type: Type of receive_id. Defaults to "open_id".
            reply_to_message_id: Optional message ID to reply to (threaded reply).

        Returns:
            message_id string, or None on failure.
        """
        self.last_error = None
        url = f"{self._base_url}/open-apis/im/v1/messages"
        params: dict[str, str] = {"receive_id_type": receive_id_type}
        payload: dict[str, Any] = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card),
        }
        if reply_to_message_id:
            params["reply_to_message_id"] = reply_to_message_id
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, headers=self._headers(), params=params, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            resp = locals().get("resp")
            status = resp.status_code if resp else "N/A"
            body = resp.text if resp else str(exc)
            logger.warning("CardKit.send_card_json HTTP %s: %s", status, body[:500])
            logger.exception("CardKit.send_card_json failed")
            self._record_error(
                "send_card_json",
                error_type=type(exc).__name__,
                status=status,
                body=body[:500],
            )
            return None

        code = data.get("code", 0)
        if code != 0:
            logger.warning("CardKit.send_card_json code=%s msg=%s", code, data.get("msg"))
            self._record_error("send_card_json", code=code, msg=data.get("msg"))
            return None

        return data.get("data", {}).get("message_id")

    def update_card(
        self,
        card_id: str,
        card: dict[str, Any],
        sequence: int = 1,
    ) -> bool:
        """Update an existing card.

        Calls ``PUT /cardkit/v1/cards/{card_id}``.

        Args:
            card_id: Card to update.
            card: Updated card definition.
            sequence: Card version sequence number.

        Returns:
            True on success, False on failure.
        """
        url = f"{self._base_url}/open-apis/cardkit/v1/cards/{card_id}"
        payload = {
            "card": {"type": "card_json", "data": json.dumps(card)},
            "sequence": sequence,
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.put(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("CardKit.update_card failed")
            return False

        code = data.get("code", 0)
        if code in _CARDKIT_TABLE_LIMIT_CODES:
            logger.warning(
                "CardKit: table limit reached on update_card code=%s", code
            )
            return False
        if code != 0:
            logger.warning("CardKit.update_card code=%s msg=%s", code, data.get("msg"))
            return False

        return True

    def stream_card_content(
        self,
        card_id: str,
        element_id: str,
        content: str,
        sequence: int = 1,
    ) -> bool:
        """Stream-update a single element's content inside a card.

        Calls ``PUT /cardkit/v1/cards/{card_id}/elements/{element_id}/content``.

        Args:
            card_id: Card containing the element.
            element_id: ID of the element to update.
            content: New text content for the element.
            sequence: Card version sequence number.

        Returns:
            True on success, False on failure.
        """
        url = (
            f"{self._base_url}/open-apis/cardkit/v1/cards/"
            f"{card_id}/elements/{element_id}/content"
        )
        payload = {"content": content, "sequence": sequence}
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.put(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("CardKit.stream_card_content failed")
            return False

        code = data.get("code", 0)
        if code in _CARDKIT_TABLE_LIMIT_CODES:
            logger.warning(
                "CardKit: table limit reached on stream_card_content code=%s", code
            )
            return False
        if code != 0:
            logger.warning(
                "CardKit.stream_card_content code=%s msg=%s", code, data.get("msg")
            )
            return False

        return True

    def set_streaming_mode(
        self,
        card_id: str,
        streaming_mode: bool,
        sequence: int = 1,
    ) -> bool:
        """Enable or disable streaming mode on a card.

        Calls ``PATCH /cardkit/v1/cards/{card_id}/settings``.

        Args:
            card_id: Card to configure.
            streaming_mode: True to enable streaming, False to disable.
            sequence: Card version sequence number.

        Returns:
            True on success, False on failure.
        """
        url = f"{self._base_url}/open-apis/cardkit/v1/cards/{card_id}/settings"
        settings = {"config": {"streaming_mode": streaming_mode}}
        payload = {"settings": json.dumps(settings), "sequence": sequence}
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.patch(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("CardKit.set_streaming_mode failed")
            return False

        code = data.get("code", 0)
        if code in _CARDKIT_TABLE_LIMIT_CODES:
            logger.warning(
                "CardKit: table limit reached on set_streaming_mode code=%s", code
            )
            return False
        if code != 0:
            logger.warning(
                "CardKit.set_streaming_mode code=%s msg=%s", code, data.get("msg")
            )
            return False

        return True

    def patch_message(
        self,
        message_id: str,
        content: dict[str, Any],
    ) -> bool:
        """Patch an existing message's content (fallback for card updates).

        Calls ``PATCH /im/v1/messages/{message_id}``.

        Args:
            message_id: The message to patch.
            content: New message content dict.

        Returns:
            True on success, False on failure.
        """
        url = f"{self._base_url}/open-apis/im/v1/messages/{message_id}"
        payload = {"content": json.dumps(content)}
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.patch(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("CardKit.patch_message failed")
            return False

        code = data.get("code", 0)
        if code != 0:
            logger.warning("CardKit.patch_message code=%s msg=%s", code, data.get("msg"))
            return False

        return True
