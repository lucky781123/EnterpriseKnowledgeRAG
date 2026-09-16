"""本地模型的有界并发调度。

这里不直接使用 ``asyncio.Semaphore`` 的私有计数来判断队列是否已满。
调度器需要同时维护 active/waiting 两个对外可观测计数，并在 waiter 被
取消或超时时把它从队列中移除，因此使用显式的 FIFO waiter 队列更容易
证明不会泄漏槽位或把已取消请求算入容量。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .errors import ProviderError, ProviderErrorKind
from .protocols import ChatProvider, ChatRequest, ChatResponse


@dataclass(slots=True)
class _Waiter:
    """一个尚未取得模型执行槽位的内部 waiter。"""

    future: asyncio.Future[None]
    started_at: float


class SchedulerLease:
    """代表一个已取得的执行槽位。

    lease 的 ``release`` 是幂等的。这样上层即使在多个异常清理分支中
    共同调用释放逻辑，也不会把 ``active_count`` 减少两次；真正的模型
    请求仍由 ``LocalLLMScheduler.complete/stream`` 负责自动使用该 lease。
    """

    def __init__(self, scheduler: "LocalLLMScheduler", queue_ms: float) -> None:
        self._scheduler = scheduler
        self.queue_ms = queue_ms
        self._released = False

    async def release(self) -> None:
        """释放槽位，并唤醒最早的有效 waiter。"""

        if self._released:
            return
        self._released = True
        await self._scheduler._release_slot()


class LocalLLMScheduler:
    """限制本地 LLM 的并发数和等待队列长度。"""

    def __init__(
        self,
        provider: ChatProvider,
        max_concurrency: int = 2,
        max_queue_size: int = 8,
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency 必须大于 0")
        if max_queue_size < 0:
            raise ValueError("max_queue_size 不能小于 0")
        self.provider = provider
        self.max_concurrency = max_concurrency
        self.max_queue_size = max_queue_size
        self._condition = asyncio.Condition()
        self._active_count = 0
        self._waiting: deque[_Waiter] = deque()

    @property
    def active_count(self) -> int:
        """当前持有模型执行槽位的请求数。"""

        return self._active_count

    @property
    def waiting_count(self) -> int:
        """当前仍在等待槽位的请求数，不包含已取消或已超时 waiter。"""

        return len(self._waiting)

    async def acquire(self, *, queue_timeout: float | None = None) -> SchedulerLease:
        """取得一个槽位，并返回包含实际排队耗时的 lease。

        ``queue_timeout`` 只约束等待调度槽位的阶段，不会把上游异常误报
        成 timeout；按照 F2-G 契约，排队超时和队列已满都统一归一化为
        ``ProviderErrorKind.RATE_LIMITED``。等待 future 使用 ``shield``，
        防止 ``wait_for`` 的取消把内部 future 留在队列中或错误消耗槽位。
        """

        started_at = time.monotonic()
        loop = asyncio.get_running_loop()
        async with self._condition:
            if self._active_count < self.max_concurrency:
                self._active_count += 1
                return SchedulerLease(self, _elapsed_ms(started_at))
            if len(self._waiting) >= self.max_queue_size:
                raise ProviderError(
                    ProviderErrorKind.RATE_LIMITED,
                    "本地模型等待队列已满",
                    retryable=True,
                )
            waiter = _Waiter(loop.create_future(), started_at)
            self._waiting.append(waiter)

        try:
            if queue_timeout is None:
                await asyncio.shield(waiter.future)
            else:
                await asyncio.wait_for(asyncio.shield(waiter.future), timeout=queue_timeout)
        except asyncio.TimeoutError as exc:
            # 如果释放槽位与 timeout 恰好竞态，future 已经完成时说明槽位
            # 已转移给本请求；这时不能再抛错，否则会泄漏一个 active slot。
            if waiter.future.done() and not waiter.future.cancelled():
                return SchedulerLease(self, _elapsed_ms(waiter.started_at))
            await self._remove_waiter(waiter)
            raise ProviderError(
                ProviderErrorKind.RATE_LIMITED,
                "等待本地模型槽位超时",
                retryable=True,
            ) from exc
        except asyncio.CancelledError:
            if waiter.future.done() and not waiter.future.cancelled():
                # 槽位已在取消竞态中交给该请求；立即归还，避免取消路径
                # 留下 active_count 永远大于实际执行数的假象。
                await self._release_slot()
            else:
                await self._remove_waiter(waiter)
            raise

        return SchedulerLease(self, _elapsed_ms(waiter.started_at))

    async def _remove_waiter(self, waiter: _Waiter) -> None:
        """在锁内安全删除仍处于等待状态的 waiter。"""

        async with self._condition:
            try:
                self._waiting.remove(waiter)
            except ValueError:
                # waiter 已经被 release 路径取出，不能重复减少等待计数。
                return
            if not waiter.future.done():
                waiter.future.cancel()

    async def _release_slot(self) -> None:
        """释放一个 active 槽位，并把它公平转移给首个有效 waiter。"""

        async with self._condition:
            if self._active_count <= 0:
                # 只有绕过 lease 直接调用内部方法才会到达这里；保持
                # 计数不变比产生负数更安全，公开 release 已由 lease 幂等保护。
                return
            self._active_count -= 1
            while self._waiting:
                waiter = self._waiting.popleft()
                if waiter.future.cancelled() or waiter.future.done():
                    continue
                self._active_count += 1
                waiter.future.set_result(None)
                break
            self._condition.notify_all()

    async def complete(self, request: ChatRequest, *, queue_timeout: float | None = None) -> ChatResponse:
        """在有界槽位内执行一次非流式 Provider 调用。"""

        lease = await self.acquire(queue_timeout=queue_timeout)
        try:
            return await self.provider.complete(request)
        finally:
            await lease.release()

    async def stream(self, request: ChatRequest, *, queue_timeout: float | None = None) -> AsyncIterator[str]:
        """在整个流生命周期内持有一个槽位，并保证结束时释放。"""

        lease = await self.acquire(queue_timeout=queue_timeout)
        try:
            async for chunk in self.provider.stream(request):
                yield chunk
        finally:
            await lease.release()

    async def health_check(self) -> bool:
        """健康探测不占用生成槽位，也不改变等待队列。"""

        return await self.provider.health_check()


def _elapsed_ms(started_at: float) -> float:
    """使用 monotonic clock 计算排队耗时，避免系统时钟回拨影响指标。"""

    return max(0.0, (time.monotonic() - started_at) * 1000)


__all__ = ["LocalLLMScheduler", "SchedulerLease"]
