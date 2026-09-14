"""真实 PostgreSQL 集成验证。

本文件只在设置 ``POSTGRES_DSN`` 时运行；未设置时自动跳过，不让普通离线测试依赖
Docker。测试使用专用的临时数据库，启动前请确认 DSN 不指向生产库，因为每个测试会
清理业务表数据。SQLite 契约测试继续承担无服务环境下的快速回归职责。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from enterprise_rag.db.migrations import EXPECTED_TABLES
from enterprise_rag.db.models import (
    ACLPermission,
    ACLPrincipalType,
    ArtifactKind,
    ArtifactStatus,
    DocumentACLModel,
    DocumentModel,
    DocumentStatus,
    DocumentVersionModel,
    IngestionJobModel,
    TenantModel,
    VersionArtifactModel,
    VersionStatus,
)
from enterprise_rag.db.publish import publish_version
from enterprise_rag.db.session import Database, normalize_database_url


POSTGRES_DSN = os.getenv("POSTGRES_DSN")

if POSTGRES_DSN:
    pytest.importorskip("asyncpg")

pytestmark = pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 POSTGRES_DSN，跳过真实 PostgreSQL 集成测试",
)


def test_database_url_override_preserves_password():
    """替换数据库名时，生成的连接串必须保留真实密码而不是 ``***``。"""

    base_url = make_url("postgresql+asyncpg://rag:secret@127.0.0.1:54329/enterprise_rag")
    rendered_url = _render_database_url(base_url, "postgres")

    # 该断言复现测试夹具的实际边界：SQLAlchemy URL 的默认字符串表示会隐藏密码，
    # 如果直接传给 Database，asyncpg 会收到字面量 ``***`` 并认证失败。
    assert make_url(rendered_url).password == "secret"


def _render_database_url(base_url, database_name: str) -> str:
    """替换数据库名并显式保留密码，避免 SQLAlchemy 默认脱敏破坏测试连接。"""

    # ``str(URL)`` 等价于 ``render_as_string(hide_password=True)``，会把密码渲染成
    # ``***``。这里仅在内存中为 SQLAlchemy engine 构造真实连接串，不打印或记录该值。
    return base_url.set(database=database_name).render_as_string(hide_password=False)


@pytest.fixture
async def postgres_database() -> AsyncIterator[Database]:
    """在同一 PostgreSQL 容器内为每个测试创建并销毁独立临时数据库。"""

    if not POSTGRES_DSN:
        pytest.skip("未设置 POSTGRES_DSN")

    base_url = make_url(normalize_database_url(POSTGRES_DSN))
    database_name = f"enterprise_rag_test_{uuid4().hex}"
    admin_database = Database(_render_database_url(base_url, "postgres"))
    database = Database(_render_database_url(base_url, database_name))

    async def execute_admin(statement: str) -> None:
        """使用 AUTOCOMMIT 执行 CREATE/DROP DATABASE，避免落入事务。"""

        async with admin_database.engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(statement))

    try:
        await execute_admin(f'CREATE DATABASE "{database_name}"')
        if not await database.health_check():
            pytest.fail("POSTGRES_DSN 无法通过 SELECT 1 健康检查")
        yield database
    finally:
        await database.dispose()
        await execute_admin(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        await admin_database.dispose()


@pytest.fixture
async def database(postgres_database: Database) -> AsyncIterator[Database]:
    """完成 migration 后提供测试数据库。"""

    await postgres_database.migrate()
    yield postgres_database


def _ready_artifacts(tenant_id: str, document_id: str, version_id: str):
    """构造发布要求的全部 ready 产物。"""

    return [
        VersionArtifactModel(
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            kind=kind,
            status=ArtifactStatus.READY,
            item_count=1,
        )
        for kind in ArtifactKind
    ]


@pytest.mark.asyncio
async def test_postgres_migration_is_repeatable_and_schema_is_expected(
    postgres_database: Database,
):
    """空库 migration 应可重复执行，并生成预期表、约束、索引和版本记录。"""

    await postgres_database.migrate()
    await postgres_database.migrate()

    async with postgres_database.engine.connect() as connection:
        table_names = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )
        migration_versions = (
            await connection.execute(
                text("SELECT version FROM schema_migrations ORDER BY version")
            )
        ).scalars().all()
        constraint_rows = (
            await connection.execute(
                text(
                    """
                    SELECT conname, condeferrable, condeferred
                    FROM pg_constraint
                    WHERE conname IN (
                        'fk_documents_current_version_lineage',
                        'uq_documents_tenant_source',
                        'uq_versions_document_hash'
                    )
                    ORDER BY conname
                    """
                )
            )
        ).mappings().all()
        index_rows = (
            await connection.execute(
                text(
                    """
                    SELECT indexname, indexdef
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND tablename = 'document_versions'
                    """
                )
            )
        ).mappings().all()

    assert table_names == EXPECTED_TABLES
    assert migration_versions == ["0001_initial"]

    constraints = {row["conname"]: row for row in constraint_rows}
    assert set(constraints) == {
        "fk_documents_current_version_lineage",
        "uq_documents_tenant_source",
        "uq_versions_document_hash",
    }
    assert constraints["fk_documents_current_version_lineage"]["condeferrable"] is True
    assert constraints["fk_documents_current_version_lineage"]["condeferred"] is True

    published_index = next(
        row for row in index_rows if row["indexname"] == "uq_versions_one_published"
    )
    index_definition = published_index["indexdef"].lower()
    assert "unique index" in index_definition
    assert "where" in index_definition
    assert "published" in index_definition


@pytest.mark.asyncio
async def test_postgres_transaction_sets_local_timeouts_and_reuses_connection(
    postgres_database: Database,
):
    """事务超时必须只影响当前事务，超时回滚后连接仍能执行后续查询。"""

    await postgres_database.migrate()
    database = Database(
        postgres_database.url,
        statement_timeout_ms=50,
        lock_timeout_ms=100,
    )
    try:
        with pytest.raises(SQLAlchemyError):
            async with database.transaction() as session:
                await session.execute(text("SELECT pg_sleep(0.2)"))

        async with database.transaction() as session:
            timeout_values = (
                await session.execute(
                    text(
                        "SELECT current_setting('statement_timeout'), "
                        "current_setting('lock_timeout')"
                    )
                )
            ).one()
            assert timeout_values == ("50ms", "100ms")
            assert (await session.execute(text("SELECT 1"))).scalar_one() == 1

        async with database.engine.connect() as connection:
            statement_timeout = (
                await connection.execute(text("SHOW statement_timeout"))
            ).scalar_one()
            assert statement_timeout != "50ms"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_postgres_constraints_enforce_lineage_and_tenant_idempotency(database: Database):
    """复合外键和租户内唯一键必须由 PostgreSQL 本身拒绝越界写入。"""

    async with database.transaction() as session:
        session.add_all(
            [
                TenantModel(id="tenant-a", name="租户 A"),
                TenantModel(id="tenant-b", name="租户 B"),
                DocumentModel(
                    tenant_id="tenant-a",
                    id="doc-a",
                    source_id="git:a/doc.md",
                    title="A 文档",
                ),
                DocumentModel(
                    tenant_id="tenant-b",
                    id="doc-b",
                    source_id="git:b/doc.md",
                    title="B 文档",
                ),
                DocumentVersionModel(
                    tenant_id="tenant-a",
                    document_id="doc-a",
                    id="version-a",
                    content_hash="hash-a",
                ),
            ]
        )

    # version、ACL 都必须同时匹配 tenant_id 和 document_id，不能只依赖服务层检查。
    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            session.add(
                DocumentVersionModel(
                    tenant_id="tenant-a",
                    document_id="doc-b",
                    id="cross-tenant-version",
                    content_hash="cross-tenant-hash",
                )
            )

    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            session.add(
                DocumentACLModel(
                    tenant_id="tenant-a",
                    document_id="doc-b",
                    principal_type=ACLPrincipalType.USER,
                    principal_id="user-a",
                    permission=ACLPermission.READ,
                )
            )

    # current_version_id 使用延迟复合外键；错误指针在事务提交时仍必须失败。
    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            document = await session.get(DocumentModel, ("tenant-b", "doc-b"))
            assert document is not None
            document.current_version_id = "version-a"

    async with database.transaction() as session:
        session.add_all(
            [
                IngestionJobModel(
                    tenant_id="tenant-a",
                    id="job-a-1",
                    document_id="doc-a",
                    idempotency_key="request-1",
                ),
                IngestionJobModel(
                    tenant_id="tenant-b",
                    id="job-b-1",
                    document_id="doc-b",
                    idempotency_key="request-1",
                ),
            ]
        )

    # 相同外部请求键可以跨租户复用，但同一租户内必须冲突。
    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            session.add(
                IngestionJobModel(
                    tenant_id="tenant-a",
                    id="job-a-2",
                    document_id="doc-a",
                    idempotency_key="request-1",
                )
            )

    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            session.add(
                IngestionJobModel(
                    tenant_id="tenant-a",
                    id="invalid-job",
                    document_id="doc-a",
                    idempotency_key="   ",
                )
            )


@pytest.mark.asyncio
async def test_postgres_partial_unique_index_and_check_constraints(database: Database):
    """发布版本部分唯一索引和发布时间 CHECK 必须在数据库层生效。"""

    published_at = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    async with database.transaction() as session:
        session.add(TenantModel(id="tenant-a", name="租户 A"))
        session.add(
            DocumentModel(
                tenant_id="tenant-a",
                id="doc-a",
                source_id="git:a/doc.md",
                title="A 文档",
            )
        )

    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            session.add_all(
                [
                    DocumentVersionModel(
                        tenant_id="tenant-a",
                        document_id="doc-a",
                        id="version-1",
                        content_hash="hash-1",
                        status=VersionStatus.PUBLISHED,
                        published_at=published_at,
                    ),
                    DocumentVersionModel(
                        tenant_id="tenant-a",
                        document_id="doc-a",
                        id="version-2",
                        content_hash="hash-2",
                        status=VersionStatus.PUBLISHED,
                        published_at=published_at,
                    ),
                ]
            )

    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            session.add(
                DocumentVersionModel(
                    tenant_id="tenant-a",
                    document_id="doc-a",
                    id="version-without-time",
                    content_hash="hash-3",
                    status=VersionStatus.PUBLISHED,
                )
            )


@pytest.mark.asyncio
async def test_postgres_publish_rolls_back_visibility_on_later_write_failure(database: Database):
    """发布事务后续写入失败时，document 指针和 version 状态必须一起回滚。"""

    async with database.transaction() as session:
        session.add(TenantModel(id="tenant-a", name="租户 A"))
        session.add(
            DocumentModel(
                tenant_id="tenant-a",
                id="doc-a",
                source_id="git:a/doc.md",
                title="A 文档",
            )
        )
        session.add(
            DocumentVersionModel(
                tenant_id="tenant-a",
                document_id="doc-a",
                id="version-1",
                content_hash="hash-1",
                status=VersionStatus.READY,
            )
        )
        session.add_all(_ready_artifacts("tenant-a", "doc-a", "version-1"))

    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            await publish_version(
                session,
                tenant_id="tenant-a",
                document_id="doc-a",
                version_id="version-1",
            )
            # 故意让事务提交阶段触发 CHECK，验证前面的发布写入不会单独落库。
            session.add(
                IngestionJobModel(
                    tenant_id="tenant-a",
                    id="invalid-job",
                    document_id="doc-a",
                    idempotency_key="   ",
                )
            )

    async with database.session() as session:
        document = await session.get(DocumentModel, ("tenant-a", "doc-a"))
        version = await session.get(DocumentVersionModel, ("tenant-a", "doc-a", "version-1"))

    assert document is not None
    assert document.current_version_id is None
    assert document.status is DocumentStatus.DRAFT
    assert version is not None
    assert version.status is VersionStatus.READY
    assert version.published_at is None


@pytest.mark.asyncio
async def test_postgres_concurrent_publishers_serialize_on_document_lock(database: Database):
    """两个独立事务并发发布时应按 document 锁顺序串行完成，不产生死锁。"""

    published_at = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    async with database.transaction() as session:
        session.add(TenantModel(id="tenant-a", name="租户 A"))
        session.add(
            DocumentModel(
                tenant_id="tenant-a",
                id="doc-a",
                source_id="git:a/doc.md",
                title="A 文档",
                status=DocumentStatus.READY,
                current_version_id="version-0",
            )
        )
        session.add_all(
            [
                DocumentVersionModel(
                    tenant_id="tenant-a",
                    document_id="doc-a",
                    id="version-0",
                    content_hash="hash-0",
                    status=VersionStatus.PUBLISHED,
                    published_at=published_at,
                ),
                DocumentVersionModel(
                    tenant_id="tenant-a",
                    document_id="doc-a",
                    id="version-1",
                    content_hash="hash-1",
                    status=VersionStatus.READY,
                ),
                DocumentVersionModel(
                    tenant_id="tenant-a",
                    document_id="doc-a",
                    id="version-2",
                    content_hash="hash-2",
                    status=VersionStatus.READY,
                ),
                *_ready_artifacts("tenant-a", "doc-a", "version-1"),
                *_ready_artifacts("tenant-a", "doc-a", "version-2"),
            ]
        )

    second_started = asyncio.Event()
    second_task: asyncio.Task[None] | None = None

    async def publish_second_candidate() -> None:
        """启动第二个独立事务；publish_version 会在 document 锁上等待。"""

        second_started.set()
        async with database.transaction() as session:
            await publish_version(
                session,
                tenant_id="tenant-a",
                document_id="doc-a",
                version_id="version-2",
                now=published_at,
            )

    try:
        async with database.transaction() as first_session:
            # 先由第一个事务显式持有 document 行锁，制造可观测的真实锁等待窗口。
            document = await first_session.scalar(
                select(DocumentModel)
                .where(DocumentModel.tenant_id == "tenant-a", DocumentModel.id == "doc-a")
                .with_for_update()
            )
            assert document is not None

            second_task = asyncio.create_task(publish_second_candidate())
            await second_started.wait()
            await asyncio.sleep(0.1)
            assert not second_task.done(), "第二个发布事务没有在 document 行锁上等待"

            await publish_version(
                first_session,
                tenant_id="tenant-a",
                document_id="doc-a",
                version_id="version-1",
                now=published_at,
            )
        # 第一个事务提交释放 document 锁；第二个事务随后应取得锁并完成发布。
        await asyncio.wait_for(second_task, timeout=5)
    finally:
        if second_task is not None and not second_task.done():
            await asyncio.wait_for(second_task, timeout=5)

    async with database.session() as session:
        document = await session.get(DocumentModel, ("tenant-a", "doc-a"))
        versions = (
            await session.scalars(
                select(DocumentVersionModel)
                .where(
                    DocumentVersionModel.tenant_id == "tenant-a",
                    DocumentVersionModel.document_id == "doc-a",
                )
                .order_by(DocumentVersionModel.id)
            )
        ).all()

    assert document is not None and document.current_version_id == "version-2"
    assert {version.status for version in versions} == {
        VersionStatus.SUPERSEDED,
        VersionStatus.PUBLISHED,
    }
    assert sum(version.status is VersionStatus.PUBLISHED for version in versions) == 1
    published_version = next(
        version for version in versions if version.status is VersionStatus.PUBLISHED
    )
    assert published_version.id == "version-2"
