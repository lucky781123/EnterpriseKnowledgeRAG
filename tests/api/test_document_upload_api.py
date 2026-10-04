"""产品 multipart 上传 API 的身份、字段和错误映射测试。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

from enterprise_rag.api.app import create_app
from enterprise_rag.domain.entities import (
    ArtifactKind,
    ArtifactStatus,
    DenseGenerationStatus,
    JobStatus,
    VersionStatus,
)
from enterprise_rag.ingestion.product_service import ProductIngestionResult
from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.registry import ProviderRegistry
from enterprise_rag.security.dev_token import DevTokenCodec

SECRET = "0123456789abcdef0123456789abcdef"


@dataclass(slots=True)
class _ProductService:
    """记录 API 传入的可信上下文和 multipart 字段。"""

    calls: list[dict]

    async def upload(self, context, **values) -> ProductIngestionResult:
        """返回固定成功结果，并保存调用参数供断言。"""

        self.calls.append({"context": context, **values})
        return ProductIngestionResult(
            document_id="doc-1",
            version_id="version-1",
            job_id="job-1",
            generation_id="generation-1",
            job_status=JobStatus.SUCCEEDED,
            version_status=VersionStatus.PUBLISHED,
            generation_status=DenseGenerationStatus.ACTIVE,
            artifact_statuses={
                ArtifactKind.RAW: ArtifactStatus.READY,
                ArtifactKind.PARSED: ArtifactStatus.READY,
                ArtifactKind.EMBEDDING: ArtifactStatus.READY,
                ArtifactKind.SPARSE: ArtifactStatus.READY,
            },
            published=True,
        )


def _client(service: _ProductService) -> tuple[TestClient, DevTokenCodec]:
    """创建只使用 Fake Provider 的产品 API 客户端。"""

    registry = ProviderRegistry()
    registry.register(
        "local_answer",
        FakeChatProvider(),
        capabilities={"chat.complete", "chat.stream"},
    )
    codec = DevTokenCodec(SECRET, ttl_seconds=3600)
    app = create_app(
        provider_registry=registry,
        product_ingestion_service=service,
        dev_token_codec=codec,
    )
    return TestClient(app), codec


def _token(codec: DevTokenCodec, *roles: str) -> str:
    """签发当前测试可验证的开发 token。"""

    return codec.issue(
        actor_id="user-alice",
        tenant_id="tenant-a",
        roles=list(roles),
    )


def test_document_upload_accepts_multipart_without_client_tenant() -> None:
    """成功上传返回 201，tenant/actor 只能来自 Bearer token。"""

    service = _ProductService(calls=[])
    client, codec = _client(service)

    response = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {_token(codec, 'editor')}"},
        data={
            "source_id": "upload:guide",
            "title": "部署指南",
            "idempotency_key": "upload-1",
        },
        files={"file": ("guide.md", b"# Install\n", "text/markdown")},
    )

    assert response.status_code == 201
    assert response.json()["published"] is True
    assert response.json()["artifacts"] == {
        "raw": "ready",
        "parsed": "ready",
        "embedding": "ready",
        "sparse": "ready",
    }
    assert service.calls[0]["context"].tenant_id == "tenant-a"
    assert service.calls[0]["context"].actor_id == "user-alice"
    assert service.calls[0]["content"] == b"# Install\n"


def test_document_upload_rejects_unknown_identity_fields_and_reader() -> None:
    """请求不得自报 tenant/actor，reader 也不能触发产品服务。"""

    service = _ProductService(calls=[])
    client, codec = _client(service)
    common = {
        "source_id": "upload:guide",
        "title": "部署指南",
        "idempotency_key": "upload-1",
    }

    extra_response = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {_token(codec, 'editor')}"},
        data={**common, "tenant_id": "tenant-b"},
        files={"file": ("guide.md", b"# Install\n", "text/markdown")},
    )
    reader_response = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {_token(codec, 'reader')}"},
        data=common,
        files={"file": ("guide.md", b"# Install\n", "text/markdown")},
    )

    assert extra_response.status_code == 422
    assert reader_response.status_code == 403
    assert service.calls == []


def test_document_upload_accepts_product_budget_larger_than_default_api_budget() -> None:
    """产品上传预算应与摄取默认 10 MiB 一致，而不是被普通 API 预算提前截断。"""

    service = _ProductService(calls=[])
    client, codec = _client(service)
    content = b"# Install\n" + (b"x" * (256 * 1024))

    response = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {_token(codec, 'editor')}"},
        data={
            "source_id": "upload:large-guide",
            "title": "大文档",
            "idempotency_key": "upload-large-1",
        },
        files={"file": ("large-guide.md", content, "text/markdown")},
    )

    assert response.status_code == 201
    assert service.calls[0]["content"] == content


def test_product_upload_above_10mib_reaches_ingestion() -> None:
    """产品HTTP路径不能在传输层继续使用旧10 MiB上限。"""
    service = _ProductService(calls=[])
    client, codec = _client(service)
    content = b"public\n" + b"x" * (11 * 1024 * 1024)
    response = client.post(
        "/api/v1/documents",
        headers={"Authorization": f"Bearer {_token(codec, 'editor')}"},
        data={"source_id": "large", "title": "large", "idempotency_key": "large"},
        files={"file": ("large.txt", content, "text/plain")},
    )
    assert response.status_code == 201
    assert service.calls[0]["content"] == content
