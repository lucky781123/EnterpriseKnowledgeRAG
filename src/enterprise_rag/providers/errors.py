"""Provider 错误分类，供重试、降级和观测使用。"""

from enum import StrEnum


class ProviderErrorKind(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    UNAVAILABLE = "unavailable"
    PROTOCOL = "protocol"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ProviderError(RuntimeError):
    def __init__(self, kind: ProviderErrorKind, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
