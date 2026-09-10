"""本地模型的有界并发调度；不假设运行时会线性扩展。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from .protocols import ChatProvider, ChatRequest, ChatResponse


class LocalLLMScheduler:
    def __init__(self, provider: ChatProvider, max_concurrency: int = 2) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency 必须大于 0")
        self.provider = provider
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def complete(self, request: ChatRequest, *, queue_timeout: float | None = None) -> ChatResponse:
        acquire = self._semaphore.acquire()
        if queue_timeout is None:
            await acquire
        else:
            await asyncio.wait_for(acquire, timeout=queue_timeout)
        try:
            return await self.provider.complete(request)
        finally:
            self._semaphore.release()

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        await self._semaphore.acquire()
        try:
            async for chunk in self.provider.stream(request):
                yield chunk
        finally:
            self._semaphore.release()

    async def health_check(self) -> bool:
        return await self.provider.health_check()
