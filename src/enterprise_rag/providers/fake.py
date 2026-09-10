"""确定性 Fake Provider：用于单测、状态机和故障注入。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from .protocols import ChatRequest, ChatResponse


class FakeChatProvider:
    def __init__(self, responder: Callable[[ChatRequest], str] | None = None) -> None:
        self._responder = responder or (lambda request: "[fake] 已收到请求")
        self.calls: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls.append(request)
        return ChatResponse(content=self._responder(request), model=request.model)

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        self.calls.append(request)
        content = self._responder(request)
        for token in content.split(" "):
            yield token + " "

    async def health_check(self) -> bool:
        return True
