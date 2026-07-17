from __future__ import annotations

from typing import Any


class QmtProxyError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.payload = payload


class TransportError(QmtProxyError):
    pass


class AuthenticationError(QmtProxyError):
    pass


class RequestValidationError(QmtProxyError):
    pass


class ClientError(QmtProxyError):
    pass


class RateLimitedError(ClientError):
    """HTTP 429：云网关（doyoutrade-cloud）分钟限流 / 日配额触发。

    - ``retry_after``：服务端 ``Retry-After`` 头指示的等待秒数（缺失时为 ``None``）。
    - ``error_code``：响应 JSON 里的 ``error_code``（如 ``"rate_limited"`` /
      ``"daily_quota_exceeded"``），缺失时为 ``None``。

    继承自 :class:`ClientError`，旧代码 ``except ClientError`` 仍能兜住。
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        error_code: str | None = None,
        status_code: int | None = None,
        code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message, status_code=status_code, code=code, payload=payload)
        self.retry_after = retry_after
        self.error_code = error_code


class ServerError(QmtProxyError):
    pass
