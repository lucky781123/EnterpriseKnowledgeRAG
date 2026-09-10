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
class ChatResponse:
    content: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class ChatProvider(Protocol):
    async def complete(self, request: ChatRequest) -> ChatResponse:
        ...

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        ...

    async def health_check(self) -> bool:
        ...
