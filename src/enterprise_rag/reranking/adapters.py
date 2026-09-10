"""把现有同步 BGE 实现封装成统一的异步 RerankerProvider。"""

from __future__ import annotations

import asyncio
from typing import Any

from .protocols import RerankRequest, RerankResult


def _normalize_result(value: Any) -> RerankResult:
    if isinstance(value, RerankResult):
        return value
    if isinstance(value, dict):
        return RerankResult(index=int(value["index"]), text=str(value["text"]), score=float(value["score"]))
    return RerankResult(index=int(value.index), text=str(value.text), score=float(value.score))


class SyncRerankerAdapter:
    """在工作线程中调用现有同步 reranker，避免阻塞 FastAPI 事件循环。"""

    def __init__(self, reranker: Any) -> None:
        self._reranker = reranker

    async def rerank(self, request: RerankRequest) -> list[RerankResult]:
        values = await asyncio.to_thread(
            self._reranker.rerank,
            request.query,
            list(request.documents),
            request.top_k,
        )
        return [_normalize_result(value) for value in values]

    async def health_check(self) -> bool:
        return self._reranker is not None
