import asyncio

import pytest

from enterprise_rag.providers.errors import ProviderError, ProviderErrorKind
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


@pytest.mark.asyncio
async def test_scheduler_rejects_when_bounded_waiting_queue_is_full() -> None:
    """并发槽位满且等待队列达到上限时，新的请求必须快速限流。"""

    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(FakeChatProvider):
        async def complete(self, request):
            started.set()
            await release.wait()
            return await super().complete(request)

    scheduler = LocalLLMScheduler(BlockingProvider(), max_concurrency=1, max_queue_size=1)
    request = ChatRequest([ChatMessage("user", "q")], "local")
    first = asyncio.create_task(scheduler.complete(request))
    await started.wait()
    second = asyncio.create_task(scheduler.complete(request))
    await asyncio.sleep(0)

    with pytest.raises(ProviderError) as raised:
        await scheduler.complete(request)

    assert raised.value.kind is ProviderErrorKind.RATE_LIMITED
    release.set()
    await asyncio.gather(first, second)
    assert scheduler.active_count == 0
    assert scheduler.waiting_count == 0


@pytest.mark.asyncio
async def test_scheduler_queue_timeout_is_normalized_to_rate_limited() -> None:
    """等待超过准入窗口时不能留下悬挂 waiter，也不能误报为上游错误。"""

    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(FakeChatProvider):
        async def complete(self, request):
            started.set()
            await release.wait()
            return await super().complete(request)

    scheduler = LocalLLMScheduler(BlockingProvider(), max_concurrency=1, max_queue_size=1)
    request = ChatRequest([ChatMessage("user", "q")], "local")
    first = asyncio.create_task(scheduler.complete(request))
    await started.wait()

    with pytest.raises(ProviderError) as raised:
        await scheduler.complete(request, queue_timeout=0.001)

    assert raised.value.kind is ProviderErrorKind.RATE_LIMITED
    assert scheduler.waiting_count == 0
    release.set()
    await first
    assert scheduler.active_count == 0


@pytest.mark.asyncio
async def test_scheduler_cancelling_waiter_does_not_consume_future_slot() -> None:
    """取消排队请求后，释放槽位应能被后续请求正常获得。"""

    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(FakeChatProvider):
        async def complete(self, request):
            started.set()
            await release.wait()
            return await super().complete(request)

    scheduler = LocalLLMScheduler(BlockingProvider(), max_concurrency=1, max_queue_size=1)
    request = ChatRequest([ChatMessage("user", "q")], "local")
    first = asyncio.create_task(scheduler.complete(request))
    await started.wait()
    cancelled = asyncio.create_task(scheduler.complete(request))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert scheduler.waiting_count == 0

    release.set()
    await first
    # 如果取消路径错误地占用了 permit，下面的请求会永久等待或超时。
    await scheduler.complete(request)
    assert scheduler.active_count == 0


@pytest.mark.asyncio
async def test_scheduler_acquire_reports_queue_time_and_release_is_idempotent() -> None:
    """调度器公开的 lease 必须记录排队耗时，并且重复释放不能减少 active 计数两次。"""

    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(FakeChatProvider):
        async def complete(self, request):
            started.set()
            await release.wait()
            return await super().complete(request)

    scheduler = LocalLLMScheduler(BlockingProvider(), max_concurrency=1, max_queue_size=1)
    request = ChatRequest([ChatMessage("user", "q")], "local")
    first = asyncio.create_task(scheduler.complete(request))
    await started.wait()

    lease_task = asyncio.create_task(scheduler.acquire(queue_timeout=1.0))
    await asyncio.sleep(0)
    release.set()
    await first
    lease = await lease_task

    assert lease.queue_ms >= 0
    await lease.release()
    await lease.release()
    assert scheduler.active_count == 0
    assert scheduler.waiting_count == 0
