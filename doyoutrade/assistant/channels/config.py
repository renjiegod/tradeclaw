"""Channel configuration models."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class BaseChannelConfig(BaseModel):
    """所有 Channel 配置的基类。"""
    enabled: bool = False
    bot_prefix: str = ""
    dm_policy: Literal["open", "allowlist"] = "open"
    group_policy: Literal["open", "allowlist"] = "open"
    allow_from: list[str] = Field(default_factory=list)
    deny_message: str = ""
    require_mention: bool = False


class FeishuChannelConfig(BaseChannelConfig):
    """飞书 Channel 配置。

    参考: https://open.feishu.cn/document/server-side-sdk/python--sdk/preparations-before-development
    """
    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    domain: Literal["feishu", "lark"] = "feishu"
    # Per-content-type CardKit card_ids
    thinking_card_id: str = ""
    tool_call_card_id: str = ""
    rich_text_card_id: str = ""

    def has_any_card_id(self) -> bool:
        return bool(self.thinking_card_id or self.tool_call_card_id or self.rich_text_card_id)

    def get_card_id_for_content_type(self, content_type: str) -> str:
        mapping = {
            "thinking": self.thinking_card_id,
            "tool_call": self.tool_call_card_id,
            "rich_text": self.rich_text_card_id,
        }
        return mapping.get(content_type, "")


class HttpChannelConfig(BaseChannelConfig):
    """HTTP Channel 配置（用于调试/手动触发）。"""
    pass


# --- Outbound push channels (email / wecom / dingtalk / telegram / slack) ---
#
# These are primarily *outbound* channels: cron pushes and assistant replies are
# forwarded to them; inbound is not implemented (build_agent_request_from_native
# raises). Secret-bearing fields (passwords / tokens / webhook URLs that embed a
# key) are persisted under the channel row's ``secrets`` and injected at
# bootstrap; non-secret fields live under ``config``. The field is declared on
# the config model regardless (mirrors FeishuChannelConfig), so ``from_config``
# / ``clone`` round-trips work.


class EmailChannelConfig(BaseChannelConfig):
    """SMTP 邮件推送配置。"""
    smtp_host: str = ""
    smtp_port: int = 465
    use_tls: bool = True          # implicit TLS (SMTPS, port 465)
    use_starttls: bool = False    # STARTTLS (port 587); mutually exclusive w/ use_tls
    username: str = ""            # secret
    password: str = ""           # secret
    from_addr: str = ""
    to_addrs: list[str] = Field(default_factory=list)
    subject_prefix: str = "[Doyoutrade]"


class WecomChannelConfig(BaseChannelConfig):
    """企业微信群机器人配置。``webhook_url`` embeds the bot key → secret."""
    webhook_url: str = ""         # secret
    msg_type: Literal["markdown", "text"] = "markdown"


class DingtalkChannelConfig(BaseChannelConfig):
    """钉钉群机器人配置。"""
    webhook_url: str = ""         # secret
    sign_secret: str = ""        # secret (加签密钥); empty = no signing
    msg_type: Literal["markdown", "text"] = "markdown"


class TelegramChannelConfig(BaseChannelConfig):
    """Telegram Bot 配置。"""
    bot_token: str = ""          # secret
    chat_id: str = ""
    message_thread_id: str = ""
    api_base: str = "https://api.telegram.org"


class SlackChannelConfig(BaseChannelConfig):
    """Slack 配置。支持 incoming webhook 或 bot token + channel。"""
    webhook_url: str = ""         # secret (incoming webhook)
    bot_token: str = ""          # secret (chat.postMessage)
    channel_id: str = ""         # required when using bot_token
    api_base: str = "https://slack.com/api"
