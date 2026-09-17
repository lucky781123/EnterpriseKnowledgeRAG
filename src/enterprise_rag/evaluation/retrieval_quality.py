"""检索通道融合与确定性排序指标。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class ChannelHit:
    """描述单个检索通道返回的候选及其原始排名。"""

    chunk_id: str
    rank: int
    score: float | None = None


@dataclass(frozen=True, slots=True)
class FusedHit:
    """描述 RRF 融合后的候选和双路排名来源。"""

    chunk_id: str
    rank: int
    score: float
    dense_rank: int | None
    sparse_rank: int | None


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    """描述一组查询的宏平均确定性检索指标。"""

    recall_at_k: float
    mrr: float
    ndcg_at_k: float
    query_count: int
    hit_query_count: int


def _deduplicate_channel(hits: Sequence[ChannelHit]) -> dict[str, ChannelHit]:
    """按最小 rank 去重单通道候选，避免重复对象放大 RRF 分数。"""

    deduplicated: dict[str, ChannelHit] = {}
    for hit in hits:
        if not hit.chunk_id.strip():
            raise ValueError("chunk_id 不能为空")
        if hit.rank <= 0:
            raise ValueError("rank 必须大于 0")
        current = deduplicated.get(hit.chunk_id)
        # 同一 chunk 只保留最靠前的真实排名；原始 score 不参与跨通道比较。
        if current is None or hit.rank < current.rank:
            deduplicated[hit.chunk_id] = hit
    return deduplicated


def reciprocal_rank_fusion(
    dense_hits: Sequence[ChannelHit],
    sparse_hits: Sequence[ChannelHit],
    *,
    k: int,
) -> list[FusedHit]:
    """按 ``sum(1 / (k + rank))`` 融合双路结果并保留 rank provenance。"""

    if k <= 0:
        raise ValueError("RRF k 必须大于 0")

    dense_by_id = _deduplicate_channel(dense_hits)
    sparse_by_id = _deduplicate_channel(sparse_hits)
    chunk_ids = set(dense_by_id) | set(sparse_by_id)
    scored: list[tuple[str, float, int | None, int | None]] = []

    for chunk_id in chunk_ids:
        dense_rank = dense_by_id.get(chunk_id).rank if chunk_id in dense_by_id else None
        sparse_rank = sparse_by_id.get(chunk_id).rank if chunk_id in sparse_by_id else None
        # RRF 只使用通道内排名，不直接比较 Dense distance 与 BM25F score。
        score = sum(
            1 / (k + rank)
            for rank in (dense_rank, sparse_rank)
            if rank is not None
        )
        scored.append((chunk_id, score, dense_rank, sparse_rank))

    # 同分时先比较任一通道的最优 rank，最后按稳定 identity 排序，保证可复现。
    scored.sort(
        key=lambda item: (
            -item[1],
            min(rank for rank in (item[2], item[3]) if rank is not None),
            item[0],
        )
    )
    return [
        FusedHit(
            chunk_id=chunk_id,
            rank=index,
            score=score,
            dense_rank=dense_rank,
            sparse_rank=sparse_rank,
        )
        for index, (chunk_id, score, dense_rank, sparse_rank) in enumerate(scored, start=1)
    ]


def _recall_at_k(ranking: Sequence[str], relevant: set[str], k: int) -> float:
    """计算单条查询的二元相关性 Recall@K。"""

    retrieved = set(ranking[:k])
    return len(retrieved & relevant) / len(relevant)


def _reciprocal_rank(ranking: Sequence[str], relevant: set[str]) -> float:
    """计算单条查询首个相关结果的 reciprocal rank。"""

    for rank, chunk_id in enumerate(ranking, start=1):
        if chunk_id in relevant:
            return 1 / rank
    return 0.0


def _ndcg_at_k(ranking: Sequence[str], relevant: set[str], k: int) -> float:
    """按二元相关性计算单条查询的 nDCG@K。"""

    dcg = sum(
        1 / math.log2(rank + 1)
        for rank, chunk_id in enumerate(ranking[:k], start=1)
        if chunk_id in relevant
    )
    ideal_hits = min(len(relevant), k)
    idcg = sum(1 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg


def evaluate_rankings(
    rankings: Mapping[str, Sequence[str]],
    relevant_by_query: Mapping[str, set[str]],
    *,
    k: int,
) -> RetrievalMetrics:
    """对固定查询集合计算宏平均 Recall@K、MRR 和 nDCG@K。"""

    if k <= 0:
        raise ValueError("指标 k 必须大于 0")
    if not rankings:
        raise ValueError("rankings 不能为空")

    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    hit_query_count = 0

    for query_id, ranking in rankings.items():
        if query_id not in relevant_by_query:
            raise ValueError(f"query {query_id} 缺少 ground truth")
        relevant = relevant_by_query[query_id]
        if not relevant:
            raise ValueError(f"query {query_id} 的相关 chunk 集合不能为空")

        # 每个 query 独立计算，再做宏平均，避免多相关项查询支配结果。
        recall = _recall_at_k(ranking, relevant, k)
        recalls.append(recall)
        reciprocal_ranks.append(_reciprocal_rank(ranking, relevant))
        ndcgs.append(_ndcg_at_k(ranking, relevant, k))
        if recall > 0:
            hit_query_count += 1

    query_count = len(rankings)
    return RetrievalMetrics(
        recall_at_k=sum(recalls) / query_count,
        mrr=sum(reciprocal_ranks) / query_count,
        ndcg_at_k=sum(ndcgs) / query_count,
        query_count=query_count,
        hit_query_count=hit_query_count,
    )


__all__ = [
    "ChannelHit",
    "FusedHit",
    "RetrievalMetrics",
    "evaluate_rankings",
    "reciprocal_rank_fusion",
]
