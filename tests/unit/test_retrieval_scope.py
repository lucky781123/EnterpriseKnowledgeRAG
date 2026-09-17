"""Task 5 授权检索 scope 的 SQLite 契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enterprise_rag.db.models import (
    ACLPermission,
    ACLPrincipalType,
    DenseGenerationModel,
    DenseGenerationPointerModel,
    DenseGenerationStatus,
    DocumentACLModel,
    DocumentModel,
    DocumentStatus,
    DocumentVersionModel,
    TenantDenseCollectionModel,
    TenantModel,
    VersionStatus,
)
from enterprise_rag.db.session import Database
from enterprise_rag.retrieval.contracts import IndexProfile, collection_name_for_tenant
from enterprise_rag.retrieval.scope import AuthorizationScopeService
from enterprise_rag.retrieval.sparse import SparseProfile
from enterprise_rag.security.authorization import AuthorizationService
from enterprise_rag.security.context import RequestContext


def _context() -> RequestContext:
    """构造固定开发身份。"""

    return RequestContext(
        actor_id="user-alice",
        tenant_id="tenant-a",
        roles=frozenset({"reader"}),
        issued_at=1_789_600_000,
        expires_at=1_789_603_600,
        token_version=1,
    )


def _profiles() -> tuple[IndexProfile, SparseProfile]:
    """返回测试使用的固定 Dense/Sparse profile。"""

    return (
        IndexProfile("demo-embedding", 2, "parent-child-v1", "dense-v1"),
        SparseProfile(
            tokenizer_version="word",
            preprocessing_version="identity",
            schema_version="sparse-v1",
            field_weights={
                "title": 2.0,
                "section_title": 2.0,
                "api_symbols": 3.0,
                "body": 1.0,
            },
        ),
    )


@pytest.mark.asyncio
async def test_scope_requires_current_acl_and_active_generation(tmp_path) -> None:
    """只有当前发布版本、READ ACL 和 active generation 同时满足才返回 scope。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'scope.db'}")
    dense_profile, sparse_profile = _profiles()
    now = datetime.now(UTC)
    collection_name = collection_name_for_tenant("tenant-a")
    try:
        await database.migrate()
        async with database.transaction() as session:
            session.add(TenantModel(id="tenant-a", name="租户 A"))

        async with database.transaction() as session:
            session.add_all(
                [
                    TenantDenseCollectionModel(
                        tenant_id="tenant-a",
                        collection_name=collection_name,
                        schema_version=dense_profile.schema_version,
                        status="ready",
                    ),
                    DocumentModel(
                        tenant_id="tenant-a",
                        id="doc-1",
                        source_id="upload:doc-1",
                        title="产品文档",
                        status=DocumentStatus.READY,
                        current_version_id="version-1",
                    ),
                    DocumentVersionModel(
                        tenant_id="tenant-a",
                        document_id="doc-1",
                        id="version-1",
                        content_hash="hash-1",
                        status=VersionStatus.PUBLISHED,
                        published_at=now,
                    ),
                    DocumentACLModel(
                        tenant_id="tenant-a",
                        document_id="doc-1",
                        principal_type=ACLPrincipalType.USER,
                        principal_id="user-alice",
                        permission=ACLPermission.READ,
                    ),
                ]
            )

        async with database.transaction() as session:
            session.add(
                DenseGenerationModel(
                    generation_id="generation-1",
                    tenant_id="tenant-a",
                    document_id="doc-1",
                    version_id="version-1",
                    index_profile_key=dense_profile.key,
                    collection_name=collection_name,
                    manifest_hash="manifest-1",
                    model_name=dense_profile.model_name,
                    dimension=dense_profile.dimension,
                    chunking_version=dense_profile.chunking_version,
                    schema_version=dense_profile.schema_version,
                    sparse_collection_name="SparseTenant_demo",
                    sparse_profile_key=sparse_profile.key,
                    sparse_schema_version=sparse_profile.schema_version,
                    expected_item_count=1,
                    actual_item_count=1,
                    checkpoint=1,
                    worker_lease="lease-1",
                    status=DenseGenerationStatus.ACTIVE,
                    published_at=now,
                )
            )

        async with database.transaction() as session:
            session.add(
                DenseGenerationPointerModel(
                    tenant_id="tenant-a",
                    document_id="doc-1",
                    version_id="version-1",
                    index_profile_key=dense_profile.key,
                    active_generation_id="generation-1",
                    updated_at=now,
                )
            )

        async with database.session() as session:
            scopes = await AuthorizationScopeService().list_scopes(
                session,
                _context(),
                index_profile=dense_profile,
            )
        assert len(scopes) == 1
        assert scopes[0].generation_id == "generation-1"

        async with database.transaction() as session:
            await AuthorizationService().revoke_read(
                session,
                RequestContext(
                    actor_id="user-admin",
                    tenant_id="tenant-a",
                    roles=frozenset({"admin"}),
                    issued_at=1_789_600_000,
                    expires_at=1_789_603_600,
                    token_version=1,
                ),
                document_id="doc-1",
                principal_type=ACLPrincipalType.USER,
                principal_id="user-alice",
            )
        async with database.session() as session:
            assert await AuthorizationScopeService().list_scopes(
                session,
                _context(),
                index_profile=dense_profile,
            ) == []
    finally:
        await database.dispose()
