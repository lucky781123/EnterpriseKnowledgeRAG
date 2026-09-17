"""BM25F/RRF 质量实验所需确定性融合与指标测试。"""

from __future__ import annotations

import math

import pytest

from enterprise_rag.evaluation.retrieval_quality import (
    ChannelHit,
    evaluate_rankings,
    reciprocal_rank_fusion,
)


def test_rrf_keeps_channel_ranks_and_deduplicates_candidates() -> None:
    """RRF 应保留双路 rank，并对同一通道的重复 chunk 只计一次。"""

    dense = [
        ChannelHit(chunk_id="chunk-a", rank=1, score=0.91),
        ChannelHit(chunk_id="chunk-b", rank=2, score=0.82),
        ChannelHit(chunk_id="chunk-a", rank=3, score=0.75),
    ]
    sparse = [
        ChannelHit(chunk_id="chunk-b", rank=1, score=8.0),
        ChannelHit(chunk_id="chunk-c", rank=2, score=6.0),
    ]

    fused = reciprocal_rank_fusion(dense, sparse, k=20)

    assert [item.chunk_id for item in fused] == ["chunk-b", "chunk-a", "chunk-c"]
    assert fused[0].dense_rank == 2
    assert fused[0].sparse_rank == 1
    assert fused[1].dense_rank == 1
    assert fused[1].sparse_rank is None
    assert fused[1].score == pytest.approx(1 / 21)


def test_rrf_supports_one_missing_channel_and_stable_tie_order() -> None:
    """单路无结果时仍应工作；同分候选按最优 rank 和 chunk_id 稳定排序。"""

    sparse = [
        ChannelHit(chunk_id="chunk-b", rank=1, score=9.0),
        ChannelHit(chunk_id="chunk-a", rank=1, score=8.0),
    ]

    first = reciprocal_rank_fusion([], sparse, k=60)
    second = reciprocal_rank_fusion([], list(reversed(sparse)), k=60)

    assert [item.chunk_id for item in first] == ["chunk-a", "chunk-b"]
    assert [item.chunk_id for item in second] == ["chunk-a", "chunk-b"]
    assert all(item.dense_rank is None for item in first)
    assert all(item.sparse_rank == 1 for item in first)


def test_rrf_rejects_invalid_rank_and_k() -> None:
    """RRF 参数错误必须显式失败，避免实验悄悄产生无效分数。"""

    with pytest.raises(ValueError, match="k"):
        reciprocal_rank_fusion([], [], k=0)
    with pytest.raises(ValueError, match="rank"):
        reciprocal_rank_fusion(
            [ChannelHit(chunk_id="chunk-a", rank=0, score=1.0)],
            [],
            k=60,
        )


def test_evaluate_rankings_matches_known_recall_mrr_and_ndcg() -> None:
    """聚合指标应与手工可核对的二元相关性样例一致。"""

    rankings = {
        "query-1": ["chunk-a", "chunk-x", "chunk-b"],
        "query-2": ["chunk-x", "chunk-c"],
    }
    relevant = {
        "query-1": {"chunk-a", "chunk-b"},
        "query-2": {"chunk-c"},
    }

    metrics = evaluate_rankings(rankings, relevant, k=2)

    # query-1 Recall@2=1/2，query-2 Recall@2=1，均值为 0.75。
    assert metrics.recall_at_k == pytest.approx(0.75)
    # query-1 首位命中，query-2 第二位命中，MRR 均值为 0.75。
    assert metrics.mrr == pytest.approx(0.75)
    # query-1 DCG=1、IDCG=1+1/log2(3)；query-2 DCG=1/log2(3)、IDCG=1。
    expected_ndcg = (1 / (1 + 1 / math.log2(3)) + 1 / math.log2(3)) / 2
    assert metrics.ndcg_at_k == pytest.approx(expected_ndcg)
    assert metrics.query_count == 2
    assert metrics.hit_query_count == 2


def test_evaluate_rankings_rejects_missing_or_empty_ground_truth() -> None:
    """缺少 query 或空相关集属于数据集错误，不能按零分吞掉。"""

    with pytest.raises(ValueError, match="ground truth"):
        evaluate_rankings({"query-1": []}, {}, k=3)
    with pytest.raises(ValueError, match="相关"):
        evaluate_rankings({"query-1": []}, {"query-1": set()}, k=3)
