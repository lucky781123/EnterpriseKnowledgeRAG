"""Reranker 的统一业务契约。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RerankRequest:
    query: str
    documents: Sequence[str]
    top_k: int | None = None


@dataclass(frozen=True, slots=True)
class RerankResult:
    index: int
    text: str
    score: float


class RerankerProvider(Protocol):
    async def rerank(self, request: RerankRequest) -> list[RerankResult]:
        ...

    async def health_check(self) -> bool:
        ...
