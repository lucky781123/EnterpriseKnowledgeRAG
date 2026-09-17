"""本地 P0 产品状态、ACL 管理和 readiness API 的 RED/GREEN 测试。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from enterprise_rag.api.app import create_app
from enterprise_rag.api.product_readiness import ProductReadiness
from enterprise_rag.db.models import (
    ACLPermission,
    ACLPrincipalType,
    ArtifactKind,
    ArtifactStatus,
    DenseGenerationModel,
    DenseGenerationPointerModel,
    DocumentACLModel,
    DocumentModel,
    DocumentVersionModel,
    IngestionJobModel,
    TenantDenseCollectionModel,
    TenantModel,
    VersionArtifactModel,
)
from enterprise_rag.db.session import Database
from enterprise_rag.domain.entities import (
    DenseGenerationStatus,
    DocumentStatus,
    JobStatus,
    VersionStatus,
)
from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.registry import ProviderRegistry
from enterprise_rag.security.dev_token import DevTokenCodec

SECRET = "0123456789abcdef0123456789abcdef"


@dataclass(slots=True)
class _ReadinessStub:
    """提供固定 readiness 结果，隔离 HTTP 响应契约与真实依赖探测。"""

    result: ProductReadiness

    async def check(self) -> ProductReadiness:
        """返回预置依赖状态，验证路由会正确设置 HTTP 状态码和响应摘要。"""

        return self.result


def _token(codec: DevTokenCodec, *roles: str, tenant_id: str = "tenant-a") -> str:
    """签发当前测试使用的可信开发身份 token。"""

    return codec.issue(
        actor_id="user-alice",
        tenant_id=tenant_id,
        roles=list(roles),
    )


def _registry() -> ProviderRegistry:
    """构造不访问真实 LM Studio 的最小 Provider registry。"""

    registry = ProviderRegistry()
    registry.register(
        "local_answer",
        FakeChatProvider(),
        capabilities={"chat.complete", "chat.stream"},
    )
    return registry


def _seed_management_database(database: Database) -> None:
    """为状态和 ACL API 建立一组可查询的已发布产品事实。"""

    async def seed() -> None:
        """执行 migration 和最小租户/文档/版本/generation/ACL 写入。"""

        await database.migrate()
        async with database.transaction() as session:
            session.add(TenantModel(id="tenant-a", name="租户 A"))
            session.add(TenantModel(id="tenant-b", name="租户 B"))

        async with database.transaction() as session:
            session.add(
                TenantDenseCollectionModel(
                    tenant_id="tenant-a",
                    collection_name="TenantACollection",
                    schema_version="dense-v1",
                )
            )
            session.add(
                TenantDenseCollectionModel(
                    tenant_id="tenant-b",
                    collection_name="TenantBCollection",
                    schema_version="dense-v1",
                )
            )

        async with database.transaction() as session:
            session.add_all(
                [
                    DocumentModel(
                        tenant_id="tenant-a",
                        id="doc-1",
                        source_id="upload:doc-1",
                        title="产品文档",
                        status=DocumentStatus.READY,
                        current_version_id="version-1",
                    ),
                    DocumentModel(
                        tenant_id="tenant-b",
                        id="doc-foreign",
                        source_id="upload:foreign",
                        title="其他租户文档",
                        status=DocumentStatus.READY,
                        current_version_id="version-foreign",
                    ),
                ]
            )
            session.add_all(
                [
                    DocumentVersionModel(
                        tenant_id="tenant-a",
                        document_id="doc-1",
                        id="version-1",
                        content_hash="hash-1",
                        status=VersionStatus.PUBLISHED,
                        published_at=datetime.now(UTC),
                    ),
                    DocumentVersionModel(
                        tenant_id="tenant-b",
                        document_id="doc-foreign",
                        id="version-foreign",
                        content_hash="hash-foreign",
                        status=VersionStatus.PUBLISHED,
                        published_at=datetime.now(UTC),
                    ),
                ]
            )

        async with database.transaction() as session:
            session.add(
                IngestionJobModel(
                    tenant_id="tenant-a",
                    id="job-1",
                    document_id="doc-1",
                    idempotency_key="upload-1",
                    status=JobStatus.SUCCEEDED,
                    attempt=1,
                )
            )
            session.add_all(
                [
                    VersionArtifactModel(
                        tenant_id="tenant-a",
                        document_id="doc-1",
                        version_id="version-1",
                        kind=kind,
                        status=ArtifactStatus.READY,
                        item_count=2,
                    )
                    for kind in ArtifactKind
                ]
            )
            session.add(
                DocumentACLModel(
                    tenant_id="tenant-a",
                    document_id="doc-1",
                    principal_type=ACLPrincipalType.USER,
                    principal_id="user-alice",
                    permission=ACLPermission.READ,
                )
            )
            session.add(
                DenseGenerationModel(
                    generation_id="generation-1",
                    tenant_id="tenant-a",
                    document_id="doc-1",
                    version_id="version-1",
                    index_profile_key="demo-embedding:2:parent-child-v1:dense-v1",
                    collection_name="TenantACollection",
                    manifest_hash="manifest-1",
                    model_name="demo-embedding",
                    dimension=2,
                    chunking_version="parent-child-v1",
                    schema_version="dense-v1",
                    sparse_collection_name="TenantASparseCollection",
                    sparse_profile_key="word:identity:sparse-v1",
                    sparse_schema_version="sparse-v1",
                    expected_item_count=2,
                    actual_item_count=2,
                    checkpoint=2,
                    worker_lease="test-lease",
                    status=DenseGenerationStatus.ACTIVE,
                    published_at=datetime.now(UTC),
                )
            )
            session.add(
                DenseGenerationPointerModel(
                    tenant_id="tenant-a",
                    document_id="doc-1",
                    version_id="version-1",
                    index_profile_key="demo-embedding:2:parent-child-v1:dense-v1",
                    active_generation_id="generation-1",
                )
            )

    asyncio.run(seed())


def _management_client(tmp_path):
    """创建绑定 SQLite 事实库和开发 token 的管理 API 客户端。"""

    database_path = tmp_path / "product-management.db"
    database = Database(f"sqlite+aiosqlite:///{database_path}")
    _seed_management_database(database)
    codec = DevTokenCodec(SECRET, ttl_seconds=3600)
    app = create_app(
        provider_registry=_registry(),
        database=database,
        dev_token_codec=codec,
        embedding_provider=object(),
        product_readiness_manager=_ReadinessStub(
            ProductReadiness(
                status="ready",
                dependencies={
                    "database": "ready",
                    "weaviate_dense": "ready",
                    "weaviate_sparse": "ready",
                    "ollama_embedding": "ready",
                    "bge_reranker": "ready",
                    "lmstudio": "ready",
                    "object_storage": "ready",
                    "dev_identity": "ready",
                    "product_services": "ready",
                },
            )
        ),
    )
    return TestClient(app), codec, database


def test_document_status_returns_safe_product_state(tmp_path) -> None:
    """状态接口应返回文档链路摘要，但不得泄露正文、对象 key 或 worker lease。"""

    client, codec, database = _management_client(tmp_path)
    try:
        response = client.get(
            "/api/v1/documents/doc-1/status",
            headers={"Authorization": f"Bearer {_token(codec, 'reader')}",},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["document_id"] == "doc-1"
        assert body["document_status"] == "ready"
        assert body["current_version"]["version_id"] == "version-1"
        assert body["current_version"]["status"] == "published"
        assert body["latest_job"]["job_id"] == "job-1"
        assert body["latest_job"]["status"] == "succeeded"
        assert body["generation"]["generation_id"] == "generation-1"
        assert body["generation"]["status"] == "active"
        assert body["artifacts"]["embedding"]["status"] == "ready"
        assert "manifest-1" not in response.text
        assert "TenantACollection" not in response.text
        assert "test-lease" not in response.text
    finally:
        asyncio.run(database.dispose())


def test_admin_can_grant_and_revoke_acl_but_reader_cannot(tmp_path) -> None:
    """ACL 管理必须区分 admin、跨 tenant 资源和非法 principal。"""

    client, codec, database = _management_client(tmp_path)
    try:
        admin_headers = {"Authorization": f"Bearer {_token(codec, 'admin')}"}
        reader_headers = {"Authorization": f"Bearer {_token(codec, 'reader')}"}

        granted = client.put(
            "/api/v1/documents/doc-1/acl/user/user-bob",
            headers=admin_headers,
        )
        denied = client.put(
            "/api/v1/documents/doc-1/acl/user/user-carol",
            headers=reader_headers,
        )
        revoked = client.delete(
            "/api/v1/documents/doc-1/acl/user/user-bob",
            headers=admin_headers,
        )
        foreign = client.put(
            "/api/v1/documents/doc-foreign/acl/user/user-bob",
            headers=admin_headers,
        )
        invalid_type = client.put(
            "/api/v1/documents/doc-1/acl/group/group-engineering",
            headers=admin_headers,
        )
        invalid_id = client.put(
            "/api/v1/documents/doc-1/acl/role/   ",
            headers=admin_headers,
        )

        assert granted.status_code == 200
        assert granted.json() == {
            "document_id": "doc-1",
            "principal_type": "user",
            "principal_id": "user-bob",
            "permission": "read",
            "granted": True,
        }
        assert denied.status_code == 403
        assert denied.json()["detail"]["kind"] == "forbidden"
        assert revoked.status_code == 200
        assert revoked.json()["revoked"] is True
        assert foreign.status_code == 404
        assert foreign.json()["detail"]["kind"] == "resource_not_found"
        assert invalid_type.status_code == 422
        assert invalid_id.status_code == 422
    finally:
        asyncio.run(database.dispose())


def test_product_readiness_reports_each_dependency_without_secrets() -> None:
    """产品 readiness 缺依赖时返回稳定 503 和逐项状态，不回显配置值。"""

    codec = DevTokenCodec(SECRET, ttl_seconds=3600)
    result = ProductReadiness(
        status="not_ready",
        dependencies={
            "database": "ready",
            "weaviate_dense": "unavailable",
            "weaviate_sparse": "not_configured",
            "ollama_embedding": "ready",
            "bge_reranker": "not_configured",
            "lmstudio": "ready",
            "object_storage": "ready",
            "dev_identity": "ready",
            "product_services": "not_configured",
        },
    )
    app = create_app(
        provider_registry=_registry(),
        dev_token_codec=codec,
        product_readiness_manager=_ReadinessStub(result),
    )

    with TestClient(app) as client:
        response = client.get("/health/product/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["dependencies"]["weaviate_dense"] == {"status": "unavailable"}
    assert body["dependencies"]["product_services"] == {"status": "not_configured"}
    assert SECRET not in response.text
    assert "127.0.0.1" not in response.text


def test_default_product_readiness_is_stable_when_services_are_missing() -> None:
    """默认 readiness 缺少可选产品服务时应返回稳定 503，而不是抛内部异常。"""

    codec = DevTokenCodec(SECRET, ttl_seconds=3600)
    app = create_app(provider_registry=_registry(), dev_token_codec=codec)

    with TestClient(app) as client:
        response = client.get("/health/product/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["dependencies"]["database"] == {"status": "not_configured"}
    assert body["dependencies"]["product_services"] == {"status": "not_configured"}
    assert body["dependencies"]["dev_identity"] == {"status": "ready"}
