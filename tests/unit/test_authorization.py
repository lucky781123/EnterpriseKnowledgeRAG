from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enterprise_rag.db.models import (
    ACLPermission,
    ACLPrincipalType,
    DocumentACLModel,
    DocumentModel,
    DocumentStatus,
    DocumentVersionModel,
    TenantModel,
    VersionStatus,
)
from enterprise_rag.db.session import Database
from enterprise_rag.security.authorization import (
    AuthorizationDenied,
    AuthorizationService,
    ResourceNotFound,
)
from enterprise_rag.security.context import RequestContext


def _context(actor_id: str, tenant_id: str, *roles: str) -> RequestContext:
    """构造固定有效期的可信上下文，测试不依赖系统时钟。"""

    return RequestContext(
        actor_id=actor_id,
        tenant_id=tenant_id,
        roles=frozenset(roles),
        issued_at=1_789_600_000,
        expires_at=1_789_603_600,
        token_version=1,
    )


async def _seed_published_documents(database: Database) -> None:
    """创建两个租户的已发布文档，作为跨租户与 ACL 测试基线。"""

    published_at = datetime(2026, 9, 17, tzinfo=UTC)
    async with database.transaction() as session:
        session.add_all(
            [
                TenantModel(id="tenant-a", name="租户 A"),
                TenantModel(id="tenant-b", name="租户 B"),
                DocumentModel(
                    tenant_id="tenant-a",
                    id="doc-a",
                    source_id="upload:a.md",
                    title="A 文档",
                    status=DocumentStatus.READY,
                    current_version_id="version-a",
                ),
                DocumentVersionModel(
                    tenant_id="tenant-a",
                    document_id="doc-a",
                    id="version-a",
                    content_hash="hash-a",
                    status=VersionStatus.PUBLISHED,
                    published_at=published_at,
                ),
                DocumentModel(
                    tenant_id="tenant-b",
                    id="doc-b",
                    source_id="upload:b.md",
                    title="B 文档",
                    status=DocumentStatus.READY,
                    current_version_id="version-b",
                ),
                DocumentVersionModel(
                    tenant_id="tenant-b",
                    document_id="doc-b",
                    id="version-b",
                    content_hash="hash-b",
                    status=VersionStatus.PUBLISHED,
                    published_at=published_at,
                ),
                DocumentACLModel(
                    tenant_id="tenant-a",
                    document_id="doc-a",
                    principal_type=ACLPrincipalType.USER,
                    principal_id="user-alice",
                    permission=ACLPermission.READ,
                ),
            ]
        )


def test_role_gate_requires_one_of_the_declared_roles() -> None:
    """上传等操作必须显式声明允许角色，不做隐式权限扩大。"""

    service = AuthorizationService()

    service.require_any_role(_context("alice", "tenant-a", "editor"), {"editor", "admin"})
    service.require_any_role(_context("admin", "tenant-a", "admin"), {"editor", "admin"})
    with pytest.raises(AuthorizationDenied):
        service.require_any_role(_context("reader", "tenant-a", "reader"), {"editor", "admin"})


@pytest.mark.asyncio
async def test_document_read_is_tenant_scoped_and_hides_idor(tmp_path) -> None:
    """未授权和跨租户资源都返回同一 not found 领域结果。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'authorization.db'}")
    await database.migrate()
    await _seed_published_documents(database)
    service = AuthorizationService()
    try:
        async with database.session() as session:
            allowed = await service.require_document_read(
                session,
                _context("user-alice", "tenant-a", "reader"),
                "doc-a",
            )
            assert allowed.document_id == "doc-a"
            assert allowed.version_id == "version-a"

            with pytest.raises(ResourceNotFound):
                await service.require_document_read(
                    session,
                    _context("user-bob", "tenant-a", "reader"),
                    "doc-a",
                )
            with pytest.raises(ResourceNotFound):
                await service.require_document_read(
                    session,
                    _context("user-alice", "tenant-a", "reader"),
                    "doc-b",
                )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_admin_can_grant_and_revoke_read_access_immediately(tmp_path) -> None:
    """ACL 撤销提交后下一次授权查询必须立即拒绝，不等待索引清理。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'acl-management.db'}")
    await database.migrate()
    await _seed_published_documents(database)
    service = AuthorizationService()
    admin = _context("tenant-admin", "tenant-a", "admin")
    bob = _context("user-bob", "tenant-a", "reader")
    try:
        async with database.transaction() as session:
            await service.grant_read(
                session,
                admin,
                document_id="doc-a",
                principal_type=ACLPrincipalType.USER,
                principal_id="user-bob",
            )

        async with database.session() as session:
            assert (await service.require_document_read(session, bob, "doc-a")).document_id == "doc-a"

        async with database.transaction() as session:
            removed = await service.revoke_read(
                session,
                admin,
                document_id="doc-a",
                principal_type=ACLPrincipalType.USER,
                principal_id="user-bob",
            )
            assert removed is True

        async with database.session() as session:
            with pytest.raises(ResourceNotFound):
                await service.require_document_read(session, bob, "doc-a")
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_non_admin_cannot_manage_acl_and_uploader_gets_read_acl(tmp_path) -> None:
    """editor 不能管理他人 ACL，但上传编排可为当前 actor 创建自己的 READ ACL。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'acl-uploader.db'}")
    await database.migrate()
    await _seed_published_documents(database)
    service = AuthorizationService()
    editor = _context("user-editor", "tenant-a", "editor")
    try:
        async with database.transaction() as session:
            with pytest.raises(AuthorizationDenied):
                await service.grant_read(
                    session,
                    editor,
                    document_id="doc-a",
                    principal_type=ACLPrincipalType.USER,
                    principal_id="user-bob",
                )

            await service.ensure_actor_read_acl(session, editor, document_id="doc-a")

        async with database.session() as session:
            allowed = await service.require_document_read(session, editor, "doc-a")
            assert allowed.document_id == "doc-a"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_role_acl_and_authorized_document_listing_share_the_same_scope(tmp_path) -> None:
    """角色 ACL 与单文档检查必须产生一致的授权文档集合。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'acl-role.db'}")
    await database.migrate()
    await _seed_published_documents(database)
    service = AuthorizationService()
    admin = _context("tenant-admin", "tenant-a", "admin")
    reader = _context("user-charlie", "tenant-a", "reader")
    try:
        async with database.transaction() as session:
            await service.grant_read(
                session,
                admin,
                document_id="doc-a",
                principal_type=ACLPrincipalType.ROLE,
                principal_id="reader",
            )

        async with database.session() as session:
            documents = await service.list_authorized_documents(session, reader)
            assert [(item.document_id, item.version_id) for item in documents] == [
                ("doc-a", "version-a")
            ]
    finally:
        await database.dispose()
