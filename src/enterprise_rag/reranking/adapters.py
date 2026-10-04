"""把现有同步 BGE 实现封装成统一的异步 RerankerProvider。"""

from __future__ import annotations

import asyncio
from threading import Event
from typing import Any

from .protocols import RerankRequest, RerankResult


def _normalize_result(value: Any) -> RerankResult:
    """将同步模型结果转换为统一Provider结果。"""
    if isinstance(value, RerankResult):
        return value
    if isinstance(value, dict):
        return RerankResult(index=int(value["index"]), text=str(value["text"]), score=float(value["score"]))
    return RerankResult(index=int(value.index), text=str(value.text), score=float(value.score))


class SyncRerankerAdapter:
    """在工作线程中调用现有同步 reranker，避免阻塞 FastAPI 事件循环。"""

    def __init__(self, reranker: Any) -> None:
        """保存模型和串行准入锁；锁由底层任务结束回调释放。"""
        self._reranker = reranker
        self._lock = asyncio.Lock()

    def _worker_finished(self, worker: asyncio.Task) -> None:
        """实际线程任务结束后释放占位，并消费取消后的异常避免后台告警。"""
        self._lock.release()
        if not worker.cancelled():
            worker.exception()

    async def rerank(self, request: RerankRequest) -> list[RerankResult]:
        """串行准入；协程取消不释放仍在运行的native任务的模型占位。"""
        # 排队取消只退出锁等待，不提交线程任务。
        await self._lock.acquire()
        cancel_event = Event()
        try:
            method = getattr(self._reranker, "rerank_cancellable", None)
            arguments = (request.query, list(request.documents), request.top_k)
            if method is not None:
                arguments += (cancel_event,)
            else:
                method = self._reranker.rerank
            worker = asyncio.create_task(asyncio.to_thread(method, *arguments))
        except BaseException:
            self._lock.release()
            raise
        worker.add_done_callback(self._worker_finished)
        try:
            # shield保留真实worker；取消只通知批次停止，不将线程“假装结束”。
            values = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        return [_normalize_result(value) for value in values]

    async def health_check(self) -> bool:
        """检查模型是否已装配，不启动推理。"""
        return self._reranker is not None
