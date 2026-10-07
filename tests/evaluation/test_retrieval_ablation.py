"""P1 检索消融 runner 的契约测试。"""

import json
from pathlib import Path

import pytest

from enterprise_rag.evaluation.retrieval_ablation import (
    load_ablation_fixture,
    run_retrieval_ablation,
)


def _fixture() -> dict:
    """构造包含五种模式所需通道排名和可选延迟的最小固定数据。"""

    return {
        "schema_version": 1,
        "dataset_id": "test-dataset",
        "chunk_ids": ["a", "b", "c", "x"],
        "queries": [
            {
                "query_id": "q-1",
                "relevant_chunk_ids": ["a"],
                "dense": ["a", "b"],
                "bm25f": ["b", "a"],
                "reranker": ["a", "b"],
                "latency_ms": {
                    "dense": 2.0,
                    "bm25f": 3.0,
                    "dense_bm25f": 4.0,
                    "rrf": 5.0,
                    "rrf_reranker": 6.0,
                },
            },
            {
                "query_id": "q-2",
                "relevant_chunk_ids": ["c"],
                "dense": ["x", "c"],
                "bm25f": ["c", "x"],
                "reranker": ["c", "x"],
                "latency_ms": {
                    "dense": 2.0,
                    "bm25f": 3.0,
                    "dense_bm25f": 4.0,
                    "rrf": 5.0,
                    "rrf_reranker": 6.0,
                },
            },
        ],
    }


def test_runner_outputs_five_modes_and_latency_summary(tmp_path: Path) -> None:
    """runner 应输出五种模式、质量指标和可解释的延迟统计。"""

    fixture_path = tmp_path / "fixture.json"
    output_path = tmp_path / "report.json"
    fixture_path.write_text(json.dumps(_fixture()), encoding="utf-8")

    report = run_retrieval_ablation(fixture_path, output_path=output_path, k=2)

    assert report["dataset_id"] == "test-dataset"
    assert list(report["modes"]) == [
        "dense",
        "bm25f",
        "dense_bm25f",
        "rrf",
        "rrf_reranker",
    ]
    assert report["modes"]["dense"]["metrics"]["recall_at_k"] == 1.0
    assert report["modes"]["rrf_reranker"]["metrics"]["mrr"] == 1.0
    assert report["modes"]["rrf_reranker"]["latency_ms"]["measured"] is True
    assert report["modes"]["rrf_reranker"]["latency_ms"]["p50_nearest_rank"] == 6.0
    assert json.loads(output_path.read_text(encoding="utf-8")) == report


def test_runner_rejects_duplicate_query_and_bad_ranking(tmp_path: Path) -> None:
    """重复 query 和包含未知 chunk 的排名必须明确失败。"""

    duplicate = _fixture()
    duplicate["queries"].append(duplicate["queries"][0])
    duplicate_path = tmp_path / "duplicate.json"
    duplicate_path.write_text(json.dumps(duplicate), encoding="utf-8")

    with pytest.raises(ValueError, match="重复 query_id"):
        load_ablation_fixture(duplicate_path)

    invalid = _fixture()
    invalid["queries"][0]["dense"] = ["unknown"]
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")

    with pytest.raises(ValueError, match="未知 chunk_id"):
        load_ablation_fixture(invalid_path)


def test_runner_marks_missing_latency_as_unmeasured(tmp_path: Path) -> None:
    """没有真实或显式延迟样本时，报告必须标记为未测量而非填零。"""

    fixture = _fixture()
    for query in fixture["queries"]:
        query.pop("latency_ms")
    fixture_path = tmp_path / "no-latency.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    report = run_retrieval_ablation(fixture_path, output_path=None, k=2)

    assert report["modes"]["dense"]["latency_ms"] == {
        "measured": False,
        "sample_count": 0,
        "p50_nearest_rank": None,
        "p95_nearest_rank": None,
    }


def test_runner_accepts_empty_retrieval_as_zero_recall(tmp_path: Path) -> None:
    """合法的全部漏召回应计入零分，不应被当作文件格式错误或丢掉样本。"""

    fixture = _fixture()
    fixture["queries"] = [fixture["queries"][0]]
    # 保留真实 gold，将所有通道置空，以复现真实 BM25F miss 与双路无证据。
    for mode in ("dense", "bm25f", "reranker"):
        fixture["queries"][0][mode] = []
    source = tmp_path / "empty.json"
    source.write_text(json.dumps(fixture), encoding="utf-8")

    report = run_retrieval_ablation(source)

    for result in report["modes"].values():
        assert result["metrics"]["recall_at_k"] == 0
        assert result["metrics"]["query_count"] == 1
        assert result["metrics"]["hit_query_count"] == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_runner_rejects_nonfinite_latency(tmp_path: Path, value: float) -> None:
    """非有限数无法作为真实性能观测，也不能进入 JSON 报告。"""

    fixture = _fixture()
    fixture["queries"][0]["latency_ms"]["dense"] = value
    source = tmp_path / "nonfinite.json"
    source.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(ValueError, match="有限"):
        run_retrieval_ablation(source)


def test_runner_rejects_reranker_outside_retrieval_candidates(tmp_path: Path) -> None:
    """重排不得凭空引入只在全集存在、却未进入双路候选的 gold。"""

    fixture = _fixture()
    fixture["queries"][0]["reranker"] = ["c"]
    source = tmp_path / "injected.json"
    source.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(ValueError, match="候选"):
        run_retrieval_ablation(source)
