"""模型 Provider 的最小业务契约。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ChatRequest:
    messages: Sequence[ChatMessage]
    model: str
    temperature: float = 0.0
    max_tokens: int = 512


@dataclass(frozen=True, slots=True)
class ChatUsage:
    """上游可选的 token 使用量；缺失时保留 None，不伪造零值。"""

    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ChatStreamEvent:
    """Provider 内部流事件。

    ``text`` 只允许放可展示的正文增量；上游可能返回的
    ``reasoning``/``reasoning_content`` 永远不会被映射到这里。终态元数据
    单独放在 ``finish_reason``、``usage`` 和 ``done`` 中，避免 API 把内部
    推理或原始 SSE JSON 直接透传给调用方。
    """

    text: str = ""
    finish_reason: str | None = None
    usage: ChatUsage | None = None
    done: bool = False


@dataclass(frozen=True, slots=True)
class ChatResponse:
    content: str
    model: str
    finish_reason: str | None = None
    usage: ChatUsage | None = None

    @property
    def prompt_tokens(self) -> int | None:
        """兼容既有调用方的旧字段名。"""

        return self.usage.input_tokens if self.usage else None

    @property
    def completion_tokens(self) -> int | None:
        """兼容既有调用方的旧字段名。"""

        return self.usage.output_tokens if self.usage else None


class ChatProvider(Protocol):
    async def complete(self, request: ChatRequest) -> ChatResponse:
        ...

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        ...

    async def health_check(self) -> bool:
        ...
