"""Weaviate BM25F 真实 smoke runner 的状态与环境守卫测试。"""

from __future__ import annotations

import pytest

from scripts.weaviate_bm25f_smoke import build_bm25f_report, run_bm25f_smoke


def test_bm25f_report_has_explicit_filter_and_claim_boundaries() -> None:
    """报告必须区分正确查询、负过滤查询和非生产结论。"""

    report = build_bm25f_report()

    assert report["experiment"] == "weaviate_bm25f_smoke"
    assert report["retrieval_quality_claim"] is False
    assert report["production_performance_claim"] is False
    assert set(report["stages"]) == {
        "collection",
        "projection",
        "generation_write",
        "query_correct_scope",
        "query_wrong_tenant",
        "query_wrong_version",
        "cleanup",
    }


@pytest.mark.asyncio
async def test_bm25f_smoke_skips_without_explicit_enablement(monkeypatch) -> None:
    """未显式启用时不得访问真实 Weaviate。"""

    monkeypatch.setenv("WEAVIATE_ENABLED", "false")
    monkeypatch.delenv("WEAVIATE_BASE_URL", raising=False)

    report = await run_bm25f_smoke()

    assert report["run_status"] == "skipped"
    assert report["stages"]["collection"] == {
        "status": "skipped",
        "reason": "WEAVIATE_ENABLED=false",
    }
