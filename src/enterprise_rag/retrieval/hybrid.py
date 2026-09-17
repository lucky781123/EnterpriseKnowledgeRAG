"""Dense+BM25F 混合检索的候选融合、Reranker 映射和 Parent 去重。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from enterprise_rag.evaluation.retrieval_quality import ChannelHit, reciprocal_rank_fusion
from enterprise_rag.reranking.protocols import RerankerProvider, RerankRequest
from enterprise_rag.retrieval.service import RetrievalResult


class RerankerUnavailable(RuntimeError):
    """Reranker 未配置、健康检查失败或返回了非法结果。"""


@dataclass(frozen=True, slots=True)
class HybridCandidate:
    """保存候选原始证据、双路排名、RRF 分数和可选重排分数。"""

    result: RetrievalResult
    rank: int
    rrf_score: float
    dense_rank: int | None
    sparse_rank: int | None
    rerank_score: float | None = None

    @property
    def chunk_id(self) -> str:
        """暴露稳定 chunk identity，便于 API/测试不接触内部嵌套对象。"""

        return self.result.chunk_id

    @property
    def parent_id(self) -> str | None:
        """暴露 Parent lineage identity。"""

        return self.result.parent_id

    @property
    def text(self) -> str:
        """暴露已经授权过滤后的证据文本。"""

        return self.result.text


def _ranked_channel_hits(results: Sequence[RetrievalResult]) -> list[ChannelHit]:
    """按输入顺序生成去重后的通道 rank，避免重复对象放大融合分数。"""

    seen: set[str] = set()
    hits: list[ChannelHit] = []
    for rank, result in enumerate(results, start=1):
        if result.chunk_id in seen:
            continue
        seen.add(result.chunk_id)
        hits.append(ChannelHit(result.chunk_id, rank, result.score))
    return hits


def fuse_retrieval_candidates(
    dense_hits: Sequence[RetrievalResult],
    sparse_hits: Sequence[RetrievalResult],
    *,
    rrf_k: int = 60,
) -> list[HybridCandidate]:
    """使用确定性 RRF 融合两路结果，并保留 rank provenance。"""

    dense_by_id = {item.chunk_id: item for item in dense_hits}
    sparse_by_id = {item.chunk_id: item for item in sparse_hits}
    fused = reciprocal_rank_fusion(
        _ranked_channel_hits(dense_hits),
        _ranked_channel_hits(sparse_hits),
        k=rrf_k,
    )
    candidates: list[HybridCandidate] = []
    for item in fused:
        result = dense_by_id.get(item.chunk_id) or sparse_by_id.get(item.chunk_id)
        if result is None:
            raise ValueError("RRF candidate 缺少原始检索结果")
        candidates.append(
            HybridCandidate(
                result=result,
                rank=item.rank,
                rrf_score=item.score,
                dense_rank=item.dense_rank,
                sparse_rank=item.sparse_rank,
            )
        )
    return candidates


async def rerank_retrieval_candidates(
    query: str,
    candidates: Sequence[HybridCandidate],
    *,
    reranker: RerankerProvider | None,
    top_k: int,
) -> list[HybridCandidate]:
    """只向已授权候选调用 Reranker，并把 provider index 映射回 chunk identity。"""

    if not query.strip():
        raise ValueError("Reranker query 不能为空")
    if top_k <= 0:
        raise ValueError("Reranker top_k 必须大于 0")
    if reranker is None:
        raise RerankerUnavailable("Reranker 未配置")
    try:
        if not await reranker.health_check():
            raise RerankerUnavailable("Reranker 不可用")
        response = await reranker.rerank(
            RerankRequest(
                query=query,
                documents=[candidate.text for candidate in candidates],
                top_k=min(top_k, len(candidates)),
            )
        )
    except RerankerUnavailable:
        raise
    except Exception as exc:
        raise RerankerUnavailable("Reranker 执行失败") from exc

    reranked: list[HybridCandidate] = []
    for item in response:
        if item.index < 0 or item.index >= len(candidates):
            raise RerankerUnavailable("Reranker 返回了越界候选")
        candidate = candidates[item.index]
        if item.text != candidate.text:
            raise RerankerUnavailable("Reranker 返回文本与候选不一致")
        reranked.append(replace(candidate, rerank_score=float(item.score)))
    reranked.sort(key=lambda item: (-float(item.rerank_score or 0.0), item.rank, item.chunk_id))
    return reranked[:top_k]


def collapse_parent_candidates(
    candidates: Sequence[HybridCandidate],
) -> list[HybridCandidate]:
    """按 Parent identity 去重，保留输入顺序中的最高优先级候选。"""

    seen: set[tuple[str, str]] = set()
    collapsed: list[HybridCandidate] = []
    for candidate in candidates:
        identity = (
            ("parent", candidate.parent_id)
            if candidate.parent_id is not None
            else ("chunk", candidate.chunk_id)
        )
        if identity in seen:
            continue
        seen.add(identity)
        collapsed.append(candidate)
    return collapsed


__all__ = [
    "HybridCandidate",
    "RerankerUnavailable",
    "collapse_parent_candidates",
    "fuse_retrieval_candidates",
    "rerank_retrieval_candidates",
]
