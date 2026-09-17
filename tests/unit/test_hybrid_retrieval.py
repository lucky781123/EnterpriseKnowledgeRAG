"""Task 5 混合检索纯函数和 Reranker 映射测试。"""

from __future__ import annotations

import pytest

from enterprise_rag.retrieval.hybrid import (
    RerankerUnavailable,
    collapse_parent_candidates,
    fuse_retrieval_candidates,
    rerank_retrieval_candidates,
)
from enterprise_rag.retrieval.service import RetrievalResult


def _hit(chunk_id: str, parent_id: str | None, score: float) -> RetrievalResult:
    """构造带稳定 lineage 的最小检索结果。"""

    return RetrievalResult(
        chunk_id=chunk_id,
        parent_id=parent_id,
        version_id="version-1",
        document_id="doc-1",
        text=f"text-{chunk_id}",
        heading_path=("Guide",),
        source_locators=({"line": 1},),
        score=score,
    )


def test_fusion_keeps_channel_provenance_and_deduplicates_chunk() -> None:
    """Dense/Sparse 同一 chunk 只能保留一条，并保留两路 rank。"""

    fused = fuse_retrieval_candidates(
        dense_hits=[_hit("chunk-a", "parent-a", 0.9), _hit("chunk-b", "parent-b", 0.8)],
        sparse_hits=[_hit("chunk-a", "parent-a", 12.0), _hit("chunk-c", "parent-c", 11.0)],
        rrf_k=60,
    )

    by_id = {item.chunk_id: item for item in fused}
    assert set(by_id) == {"chunk-a", "chunk-b", "chunk-c"}
    assert by_id["chunk-a"].dense_rank == 1
    assert by_id["chunk-a"].sparse_rank == 1
    assert by_id["chunk-b"].dense_rank == 2
    assert by_id["chunk-b"].sparse_rank is None


@pytest.mark.asyncio
async def test_reranker_maps_provider_indexes_back_to_authorized_candidates() -> None:
    """Reranker 只返回候选子集时，结果仍映射到原始 chunk identity。"""

    class _Reranker:
        """按传入文本长度排序的确定性 Reranker 替身。"""

        async def rerank(self, request):
            """返回第二条候选，验证 index 映射而不是 mock 调用次数。"""

            return [type("Result", (), {"index": 1, "text": request.documents[1], "score": 0.9})()]

        async def health_check(self) -> bool:
            """报告替身可用。"""

            return True

    candidates = fuse_retrieval_candidates(
        dense_hits=[_hit("chunk-a", "parent-a", 0.9), _hit("chunk-b", "parent-b", 0.8)],
        sparse_hits=[],
        rrf_k=60,
    )
    reranked = await rerank_retrieval_candidates(
        "问题",
        candidates,
        reranker=_Reranker(),
        top_k=1,
    )

    assert [item.chunk_id for item in reranked] == ["chunk-b"]
    assert reranked[0].rerank_score == 0.9


@pytest.mark.asyncio
async def test_reranker_failure_is_explicit_and_parent_collapse_is_stable() -> None:
    """Reranker 不可用时 fail closed；Parent 去重保留最高排序项。"""

    class _Unavailable:
        """始终不可用的 Reranker 替身。"""

        async def health_check(self) -> bool:
            """报告不可用。"""

            return False

        async def rerank(self, _request):
            """不应被调用。"""

            raise AssertionError("不可用 Reranker 不应执行")

    candidates = fuse_retrieval_candidates(
        dense_hits=[_hit("child-a", "parent-a", 0.9), _hit("child-b", "parent-a", 0.8)],
        sparse_hits=[],
        rrf_k=60,
    )
    with pytest.raises(RerankerUnavailable):
        await rerank_retrieval_candidates(
            "问题",
            candidates,
            reranker=_Unavailable(),
            top_k=2,
        )

    collapsed = collapse_parent_candidates(candidates)
    assert [item.parent_id for item in collapsed] == ["parent-a"]

