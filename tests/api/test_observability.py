"""Observability middleware 和指标契约测试。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from enterprise_rag.observability import (
    MetricsRegistry,
    RequestObservabilityMiddleware,
    TraceRecorder,
)


def test_metrics_registry_is_bounded_and_serializable():
    """指标只保留有界 histogram，并能安全转换为 JSON 结构。"""

    metrics = MetricsRegistry(histogram_limit=2)
    metrics.increment("test_total")
    metrics.observe("latency", 1)
    metrics.observe("latency", 2)
    metrics.observe("latency", 3)
    snapshot = metrics.snapshot()
    assert snapshot["counters"]["test_total"] == 1
    assert snapshot["histograms"]["latency"]["count"] == 2
    assert snapshot["histograms"]["latency"]["last_ms"] == 3


def test_metrics_registry_exposes_nearest_rank_percentiles() -> None:
    """有界 histogram 应提供可解释的 p50/p95，而不是只返回最后一个样本。"""

    metrics = MetricsRegistry(histogram_limit=4)
    for value in (10, 20, 30, 40):
        metrics.observe("latency", value)

    histogram = metrics.snapshot()["histograms"]["latency"]

    assert histogram["sample_count"] == 4
    assert histogram["p50_nearest_rank"] == 20
    assert histogram["p95_nearest_rank"] == 40


def test_metrics_registry_rejects_non_finite_observation() -> None:
    """NaN/无穷值不能进入性能分位数，避免污染健康指标。"""

    metrics = MetricsRegistry()
    with pytest.raises(ValueError, match="观测值非法"):
        metrics.observe("latency", float("nan"))


def test_request_observability_adds_ids_and_safe_metrics():
    """请求应有稳定响应头和聚合指标，不能把 query 参数写入指标。"""

    app = FastAPI()
    metrics = MetricsRegistry()
    app.add_middleware(RequestObservabilityMiddleware, metrics=metrics)

    @app.get("/documents/{document_id}")
    async def read_document(document_id: str):
        """返回固定安全响应，验证路径 ID 会被聚合。"""

        return {"status": "ok"}

    with TestClient(app) as client:
        response = client.get("/documents/doc_secret?question=private")
    assert response.status_code == 200
    assert response.headers["x-request-id"]
    assert response.headers["x-trace-id"]
    snapshot = metrics.snapshot()
    assert "http_requests_total:GET:/documents/:id:200" in snapshot["counters"]
    assert "private" not in str(snapshot)


def test_trace_recorder_records_stage_without_content():
    """阶段 trace 只保存低基数属性和耗时，不保存模型正文。"""

    metrics = MetricsRegistry()
    trace = TraceRecorder("trace-1", metrics)
    with trace.span("retrieval", candidates=5):
        trace.set_attribute("answer_status", "answered")
    payload = trace.snapshot()
    assert payload["trace_id"] == "trace-1"
    assert payload["spans"][0]["name"] == "retrieval"
    assert "model answer" not in str(payload)
