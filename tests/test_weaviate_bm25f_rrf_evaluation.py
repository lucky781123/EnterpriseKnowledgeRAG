"""BM25F/RRF 真实质量实验 runner 的确定性辅助逻辑测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from enterprise_rag.retrieval.service import RetrievalResult
from scripts.weaviate_bm25f_rrf_evaluation import (
    _channel_hits_from_dense,
    _channel_hits_from_sparse,
    _exclusive_relevant_hits,
    load_quality_dataset,
)


def test_quality_dataset_has_fixed_documents_queries_and_categories() -> None:
    """版本化小数据集必须引用完整，并覆盖三类查询。"""

    dataset = load_quality_dataset(
        Path("docs/evaluation/bm25f-rrf-smoke-dataset.json")
    )

    assert dataset.dataset_id == "bm25f-rrf-smoke-v1"
    assert len(dataset.documents) == 10
    assert len(dataset.queries) == 8
    assert {query.category for query in dataset.queries} == {
        "semantic",
        "lexical",
        "mixed",
    }
    chunk_ids = {document.chunk_id for document in dataset.documents}
    assert all(set(query.relevant_chunk_ids) <= chunk_ids for query in dataset.queries)


def test_channel_hit_conversion_preserves_rank_and_native_score() -> None:
    """双路结果转换必须保留通道内 rank 和原始 score，仅 RRF 使用 rank。"""

    dense_rows = [
        RetrievalResult(
            chunk_id="chunk-a",
            parent_id="parent-a",
            version_id="version-1",
            document_id="doc-1",
            text="正文",
            heading_path=("标题",),
            source_locators=({"line": 1},),
            score=0.9,
        )
    ]
    sparse_rows = [
        {"chunk_id": "chunk-b", "_additional": {"score": "7.5"}},
        {"chunk_id": "chunk-c", "_additional": {}},
    ]

    dense_hits = _channel_hits_from_dense(dense_rows)
    sparse_hits = _channel_hits_from_sparse(sparse_rows)

    assert dense_hits[0].rank == 1
    assert dense_hits[0].score == pytest.approx(0.9)
    assert sparse_hits[0].rank == 1
    assert sparse_hits[0].score == pytest.approx(7.5)
    assert sparse_hits[1].score is None


def test_exclusive_relevant_hits_are_reported_by_query_id() -> None:
    """独有命中只比较相同 cutoff 下是否命中相关 chunk。"""

    relevant = {
        "query-1": {"chunk-a"},
        "query-2": {"chunk-b"},
        "query-3": {"chunk-c"},
    }
    dense = {
        "query-1": ["chunk-a"],
        "query-2": ["chunk-x"],
        "query-3": ["chunk-c"],
    }
    sparse = {
        "query-1": ["chunk-x"],
        "query-2": ["chunk-b"],
        "query-3": ["chunk-c"],
    }

    exclusive = _exclusive_relevant_hits(dense, sparse, relevant, k=1)

    assert exclusive == {
        "dense_only_query_ids": ["query-1"],
        "sparse_only_query_ids": ["query-2"],
        "both_query_ids": ["query-3"],
        "neither_query_ids": [],
    }


def test_experiment_readme_contains_executable_reproduction_command() -> None:
    """实验 README 的续行命令不能混入 diff 标记等无效字符。"""

    readme = Path(
        "artifacts/experiments/2026-09-17_bm25f-rrf-quality/README.md"
    ).read_text(encoding="utf-8")

    assert "\nOLLAMA_BASE_URL=" in readme
    assert "\n/Users/liuhui/opt/anaconda3" in readme
    assert "\n+OLLAMA_BASE_URL=" not in readme
    assert "\n+/Users/liuhui/opt/anaconda3" not in readme
