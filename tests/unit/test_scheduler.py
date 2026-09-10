import asyncio

import pytest

from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.protocols import ChatMessage, ChatRequest
from enterprise_rag.providers.scheduler import LocalLLMScheduler


@pytest.mark.asyncio
async def test_scheduler_limits_concurrency():
    active = 0
    maximum = 0

    async def responder(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok"

    class Provider(FakeChatProvider):
        async def complete(self, request):
            self.calls.append(request)
            return type("Response", (), {"content": await responder(request), "model": request.model})()

    scheduler = LocalLLMScheduler(Provider(), max_concurrency=2)
    request = ChatRequest([ChatMessage("user", "q")], "local")
    await asyncio.gather(*(scheduler.complete(request) for _ in range(5)))
    assert maximum == 2
