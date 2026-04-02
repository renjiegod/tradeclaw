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


class ServerError(QmtProxyError):
    pass
