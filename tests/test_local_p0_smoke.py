"""本地 P0 产品 smoke runner 的脱敏和响应分类测试。"""

from __future__ import annotations

from scripts.local_p0_smoke import (
    build_smoke_report,
    redact_report,
    summarize_response,
)


def test_build_smoke_report_contains_only_safe_stage_fields() -> None:
    """报告骨架必须包含固定阶段，但不能预置 token、正文或凭据字段。"""

    report = build_smoke_report()

    assert report["experiment"] == "local_p0_product_smoke"
    assert report["run_status"] == "pending"
    assert report["production_capacity_claim"] is False
    assert set(report["stages"]) == {
        "readiness",
        "upload",
        "status",
        "cross_tenant_deny",
        "cross_tenant_query_deny",
        "grant",
        "query",
        "revoke",
        "revoked_query_deny",
        "regrant",
        "regranted_query",
        "cleanup",
    }
    assert "token" not in report
    assert "content" not in report
    assert "password" not in report


def test_redact_report_removes_sensitive_values_recursively() -> None:
    """嵌套报告中的敏感字段必须统一替换为固定占位符。"""

    value = {
        "token": "secret-token",
        "nested": {"answer": "document text", "status": "passed"},
        "items": [{"quote": "private quote", "count": 1}],
    }

    redacted = redact_report(value)

    assert redacted == {
        "token": "[REDACTED]",
        "nested": {"answer": "[REDACTED]", "status": "passed"},
        "items": [{"quote": "[REDACTED]", "count": 1}],
    }


def test_summarize_response_keeps_status_and_safe_error_kind() -> None:
    """HTTP 响应摘要只保留状态码、业务状态和固定错误类别。"""

    assert summarize_response(201, {"published": True}) == {
        "status_code": 201,
        "status": "passed",
        "published": True,
    }
    assert summarize_response(
        404,
        {"error": {"kind": "evidence_not_found", "message": "不要写入报告"}},
    ) == {
        "status_code": 404,
        "status": "blocked",
        "error_kind": "evidence_not_found",
    }


def test_query_http_200_is_not_sufficient_for_acceptance() -> None:
    """截断或无有效引用的 HTTP 200 必须标记失败，不能宣称模型链路完成。"""
    summary = summarize_response(
        200, {"answer_status": "incomplete", "citation_validation": "failed", "citations": []},
    )
    assert summary["status"] == "failed"


def test_summarize_response_keeps_safe_product_counts_and_artifact_statuses() -> None:
    """上传和状态摘要应记录发布状态、artifact 状态与 generation 计数。"""

    assert summarize_response(
        201,
        {
            "document_id": "doc-1",
            "version_id": "version-1",
            "job_id": "job-1",
            "generation_id": "generation-1",
            "job_status": "succeeded",
            "version_status": "published",
            "generation_status": "active",
            "artifacts": {
                "raw": "ready",
                "parsed": "ready",
                "embedding": "ready",
                "sparse": "ready",
            },
            "published": True,
        },
    ) == {
        "status_code": 201,
        "status": "passed",
        "document_id": "doc-1",
        "version_id": "version-1",
        "job_id": "job-1",
        "generation_id": "generation-1",
        "job_status": "succeeded",
        "version_status": "published",
        "generation_status": "active",
        "artifacts": {
            "raw": "ready",
            "parsed": "ready",
            "embedding": "ready",
            "sparse": "ready",
        },
        "published": True,
    }

    assert summarize_response(
        200,
        {
            "document_id": "doc-1",
            "document_status": "ready",
            "generation": {
                "generation_id": "generation-1",
                "status": "active",
                "expected_item_count": 2,
                "actual_item_count": 2,
                "checkpoint": 2,
                "collection_name": "must-not-leak",
            },
            "artifacts": {
                "embedding": {"status": "ready", "item_count": 2},
                "sparse": {"status": "ready", "item_count": 2},
            },
        },
    ) == {
        "status_code": 200,
        "status": "passed",
        "document_id": "doc-1",
        "document_status": "ready",
        "generation": {
            "generation_id": "generation-1",
            "status": "active",
            "expected_item_count": 2,
            "actual_item_count": 2,
            "checkpoint": 2,
        },
        "artifacts": {
            "embedding": {"status": "ready", "item_count": 2},
            "sparse": {"status": "ready", "item_count": 2},
        },
    }
