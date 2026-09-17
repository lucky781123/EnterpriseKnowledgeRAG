"""Weaviate Dense smoke 报告的脱敏和状态契约测试。"""

from __future__ import annotations

import json

import pytest

from scripts.weaviate_smoke import (
    build_smoke_report,
    finalize_smoke_report,
    redact_smoke_report,
    run_transport_smoke,
)
from enterprise_rag.retrieval.weaviate import WeaviateTransportError


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


@pytest.mark.asyncio
async def test_transport_smoke_attributes_query_failure_to_query_stage(monkeypatch) -> None:
    """真实 query 失败必须归因到 query，不能误标成尚未执行的 publish 阶段。"""

    class _QueryFailingTransport:
        """模拟 collection、写入和完整性成功，仅在 query 阶段失败的 transport。"""

        def __init__(self, *args, **kwargs) -> None:
            """接受真实构造参数，但测试不发出网络请求。"""

        async def ensure_collection(self, *args, **kwargs) -> None:
            """模拟 collection 已准备完成。"""

        async def upsert_batch(self, collection_name, objects):
            """模拟两条对象均成功写入。"""

            return type("BatchResult", (), {"accepted": len(objects)})()

        async def count_generation(self, *args, **kwargs) -> int:
            """返回 smoke fixture 的固定对象数。"""

            return 2

        async def list_chunk_ids(self, *args, **kwargs) -> set[str]:
            """返回 smoke fixture 的固定 chunk identity。"""

            return {"smoke-chunk-1", "smoke-chunk-2"}

        async def search(self, *args, **kwargs):
            """在 query 阶段抛出脱敏协议错误。"""

            raise WeaviateTransportError(
                "protocol",
                retryable=False,
                safe_message="Weaviate 查询响应格式非法",
            )

        async def delete_generation(self, *args, **kwargs) -> int:
            """query 失败后不应进入 cleanup；保留方法只满足测试对象契约。"""

            return 0

    monkeypatch.setenv("WEAVIATE_ENABLED", "true")
    monkeypatch.setenv("WEAVIATE_BASE_URL", "http://weaviate.test")
    monkeypatch.setattr(
        "scripts.weaviate_smoke.WeaviateHTTPXTransport",
        _QueryFailingTransport,
    )

    report = await run_transport_smoke()

    assert report["run_status"] == "blocked"
    assert report["stages"]["query"] == {
        "status": "blocked",
        "error_kind": "protocol",
    }
    assert report["stages"]["publish"]["status"] == "skipped"
