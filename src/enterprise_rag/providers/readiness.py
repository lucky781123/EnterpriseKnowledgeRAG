"""Provider readiness 的合并、缓存和生命周期管理。

readiness 只执行轻量、只读的 ``health_check``，不触发模型生成、下载或
队列排队。多个 HTTP 请求同时到达时共享同一个 probe task，避免健康检查
本身把本地服务压垮；调用方取消也不会取消这个共享 task。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .registry import ProviderRegistry


@dataclass(frozen=True, slots=True)
class ProviderReadiness:
    """对外暴露的最小 Provider readiness 摘要。"""

    status: str


class ProviderReadinessManager:
    """合并并缓存 Provider health probe。

    ``ttl_seconds`` 和 ``timeout_seconds`` 是服务端保护值，不由请求体控制。
    probe 失败只产生 ``unavailable`` 摘要，不把上游异常、URL 或认证信息
    写入响应。
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        ttl_seconds: float = 5.0,
        timeout_seconds: float = 2.0,
    ) -> None:
        if ttl_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("readiness 的缓存和超时时间必须大于 0")
        self.registry = registry
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._cached: tuple[float, ProviderReadiness] | None = None
        self._probe_task: asyncio.Task[ProviderReadiness] | None = None
        self._lock = asyncio.Lock()

    async def check(self) -> ProviderReadiness:
        """返回缓存结果，或等待一个共享 probe task 完成。"""

        now = time.monotonic()
        cached = self._cached
        if cached is not None and cached[0] > now:
            return cached[1]

        async with self._lock:
            now = time.monotonic()
            cached = self._cached
            if cached is not None and cached[0] > now:
                return cached[1]
            task = self._probe_task
            if task is None or task.done():
                task = asyncio.create_task(self._run_probe())
                self._probe_task = task

        # shield 很关键：某个 HTTP 请求被取消时，只取消它自己的等待，
        # 不能把同一时刻其他请求共享的 probe 一并取消。
        return await asyncio.shield(task)

    async def _run_probe(self) -> ProviderReadiness:
        """并行探测全部已注册 Provider，并缓存统一摘要。"""

        entries = self.registry.items()
        if not entries:
            result = ProviderReadiness("unavailable")
        else:
            statuses = await asyncio.gather(
                *(self._probe_one(registered.provider) for _, registered in entries),
                return_exceptions=False,
            )
            result = ProviderReadiness("ready" if all(statuses) else "unavailable")
        self._cached = (time.monotonic() + self.ttl_seconds, result)
        return result

    async def _probe_one(self, provider: object) -> bool:
        """在独立的两秒预算内执行单个只读探测。"""

        health_check = getattr(provider, "health_check", None)
        if health_check is None:
            return False
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return bool(await health_check())
        except asyncio.CancelledError:
            # 取消表示应用生命周期正在关闭，必须继续向上传播，让共享
            # probe task 能够被正确回收；不能把取消误报成普通 unavailable。
            raise
        except TimeoutError:
            # 单个 Provider 超过健康检查预算时只影响 readiness 摘要，
            # 不应让 health endpoint 抛出 traceback 或泄露上游异常。
            return False
        except Exception:
            return False

    async def close(self) -> None:
        """取消并等待未完成 probe，避免 lifespan 退出后遗留后台任务。"""

        task = self._probe_task
        self._probe_task = None
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                # probe 的失败已经被归一化；关闭阶段不应再把它升级为
                # 应用退出异常。
                pass


__all__ = ["ProviderReadiness", "ProviderReadinessManager"]
