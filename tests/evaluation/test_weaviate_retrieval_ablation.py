"""真实检索消融采集入口的离线契约测试。"""

from __future__ import annotations

from pathlib import Path

from scripts.weaviate_retrieval_ablation import (
    CollectorConfig,
    build_ablation_input,
    build_execution_plan,
    safe_error_kind,
)


def _config() -> CollectorConfig:
    """构造不含凭据的本机实验配置，供纯函数契约测试使用。"""

    return CollectorConfig(
        weaviate_base_url="http://127.0.0.1:8080",
        ollama_base_url="http://127.0.0.1:11434",
        embedding_model="embedding-model",
        reranker_base_url="http://127.0.0.1:8003",
        reranker_model="reranker-model",
    )


def test_plan_is_explicit_and_does_not_claim_production_quality() -> None:
    """默认计划必须要求显式执行，并固定隔离 collection 与清理阶段。"""

    plan = build_execution_plan(
        Path("docs/evaluation/bm25f-rrf-smoke-dataset.json"),
        _config(),
        Path("artifacts/experiments/test-real-ablation"),
    )

    assert plan["run_status"] == "planned"
    assert plan["execute_required"] is True
    assert plan["claims"] == {
        "production_quality": False,
        "production_capacity": False,
        "frozen_golden": False,
    }
    assert plan["collection_strategy"] == "two_isolated_ephemeral_collections"
    assert plan["stages"]["cleanup"]["status"] == "pending"
    assert "api_key" not in str(plan)


def test_ablation_input_contains_only_ids_and_observations() -> None:
    """写入既有五模式 runner 的输入不得带 query、正文或模型凭据。"""

    dataset_path = Path("docs/evaluation/bm25f-rrf-smoke-dataset.json")
    query_results = {
        "q-sem-idempotency": {
            "dense": ["chunk-idempotency"],
            "bm25f": [],
            "reranker": ["chunk-idempotency"],
            "latency_ms": {"dense": 1.0, "bm25f": 2.0, "reranker": 3.0},
        }
    }

    payload = build_ablation_input(dataset_path, query_results)
    encoded = str(payload)

    assert payload["dataset_kind"] == "real-local-synthetic"
    assert payload["chunk_ids"] == ["chunk-idempotency"]
    assert payload["queries"][0]["relevant_chunk_ids"] == ["chunk-idempotency"]
    assert payload["queries"][0]["bm25f"] == []
    assert "网络不稳定" not in encoded
    assert "正文" not in encoded
    assert "api_key" not in encoded


def test_error_kind_is_stable_and_does_not_echo_sensitive_exception() -> None:
    """错误摘要只输出分类，不把 URL、响应正文或密钥写入实验产物。"""

    error = RuntimeError("https://secret.example/v1?api_key=do-not-print")

    assert safe_error_kind(error) == "runtime_error"
    assert "secret.example" not in safe_error_kind(error)
