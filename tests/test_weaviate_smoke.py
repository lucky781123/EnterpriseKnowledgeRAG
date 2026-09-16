"""Weaviate Dense smoke 报告的脱敏和状态契约测试。"""

from __future__ import annotations

import json

from scripts.weaviate_smoke import (
    build_smoke_report,
    finalize_smoke_report,
    redact_smoke_report,
)


def test_smoke_report_has_fixed_scope_and_stage_fields() -> None:
    """报告必须固定记录后端、collection 策略、generation scope 和各阶段状态。"""

    report = build_smoke_report()

    assert report["vector_backend"] == "weaviate_dense"
    assert report["collection_strategy"] == "tenant_isolated"
    assert report["generation_scope"] == [
        "tenant_id",
        "document_id",
        "version_id",
        "index_profile_key",
    ]
    assert report["active_generation"] is None
    assert set(report["stages"]) == {
        "collection",
        "generation_write",
        "integrity",
        "publish",
        "query",
        "rollback",
        "cleanup",
    }
    assert all(stage["status"] == "pending" for stage in report["stages"].values())


def test_smoke_report_redaction_excludes_sensitive_values_and_answer_text() -> None:
    """报告序列化不能保存 DSN、API key、密码、Prompt 或完整模型回答。"""

    report = build_smoke_report()
    report["unsafe_probe"] = {
        "dsn": "postgresql://rag:super-secret@db/test",
        "api_key": "weaviate-secret",
        "password": "super-secret",
        "answer": "完整模型回答不应进入报告",
    }

    redacted = redact_smoke_report(report)
    encoded = json.dumps(redacted, ensure_ascii=False)

    assert "postgresql://" not in encoded
    assert "weaviate-secret" not in encoded
    assert "super-secret" not in encoded
    assert "完整模型回答不应进入报告" not in encoded
    assert redacted["unsafe_probe"]["dsn"] == "[REDACTED]"


def test_failed_stage_is_not_rewritten_as_success() -> None:
    """失败阶段必须保留失败状态，旧 active 只能作为回滚目标记录。"""

    report = build_smoke_report()
    report["stages"]["publish"] = {"status": "failed", "error_kind": "unavailable"}
    report["active_generation"] = "generation-old"

    finalized = finalize_smoke_report(report, run_status="blocked")

    assert finalized["run_status"] == "blocked"
    assert finalized["stages"]["publish"]["status"] == "failed"
    assert finalized["active_generation"] == "generation-old"
