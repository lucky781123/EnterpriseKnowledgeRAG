"""PostgreSQL-only 首期 Dense 检索核心。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True, slots=True)
class EmbeddingRecord:
    """一个 child chunk 的向量及其 Parent/引用元数据。"""

    tenant_id: str
    document_id: str
    version_id: str
    chunk_id: str
    parent_id: str | None
    text: str
    heading_path: tuple[str, ...]
    source_locators: tuple[dict, ...]
    embedding: tuple[float, ...]
    parent_text: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """向量检索返回的证据项。"""

    chunk_id: str
    parent_id: str | None
    version_id: str
    document_id: str
    text: str
    heading_path: tuple[str, ...]
    source_locators: tuple[dict, ...]
    score: float


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """计算应用层 cosine；零向量返回零分，避免 NaN 污染排序。"""

    if len(left) != len(right):
        raise ValueError("query 和 embedding 维度不一致")
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    return 0.0 if denominator == 0 else sum(a * b for a, b in zip(left, right)) / denominator


class DenseRetrievalService:
    """对已加载的向量记录执行租户、版本过滤和 cosine 排序。"""

    def __init__(self, records: Sequence[EmbeddingRecord]) -> None:
        """保存只读记录快照，便于单元测试和未来替换数据库适配器。"""

        self.records = tuple(records)

    def search(
        self, tenant_id: str, version_id: str, query: Sequence[float], top_k: int = 5
    ) -> list[RetrievalResult]:
        """返回指定 tenant/version 内最高分的 child evidence。"""

        if top_k <= 0:
            raise ValueError("top_k 必须大于 0")
        matched = [
            record
            for record in self.records
            if record.tenant_id == tenant_id and record.version_id == version_id
        ]
        ranked = sorted(matched, key=lambda record: _cosine(query, record.embedding), reverse=True)
        return [
            RetrievalResult(
                chunk_id=record.chunk_id,
                parent_id=record.parent_id,
                version_id=record.version_id,
                document_id=record.document_id,
                # Parent 正文由索引阶段从同一 manifest 固化到 child 记录。缺失时只
                # 回退当前 child，绝不在检索阶段按 parent_id 跨版本补查。
                text=record.parent_text or record.text,
                heading_path=record.heading_path,
                source_locators=record.source_locators,
                score=_cosine(query, record.embedding),
            )
            for record in ranked[:top_k]
        ]
