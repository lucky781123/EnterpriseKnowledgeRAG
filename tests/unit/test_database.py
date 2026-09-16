"""PostgreSQL 持久化切片的离线契约测试。

这些测试使用 SQLite + aiosqlite，只验证 SQLAlchemy 模型、显式事务边界和迁移
协议。SQLite 不具备 PostgreSQL 的并发锁和 DDL 行为，因此测试结果不能写成
PostgreSQL 集成验证结论。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import MetaData, inspect, select, text
from sqlalchemy.exc import IntegrityError

from enterprise_rag.db.migrations import (
    EMBEDDING_VERSION,
    EXPECTED_TABLES,
    INITIAL_VERSION,
    LATEST_VERSION,
    MigrationError,
    upgrade_database,
)
from enterprise_rag.db.models import (
    ACLPermission,
    ACLPrincipalType,
    ArtifactKind,
    ArtifactStatus,
    DocumentACLModel,
    DocumentModel,
    DocumentStatus,
    DocumentVersionModel,
    EmbeddingRecordModel,
    IngestionJobModel,
    JobStatus,
    TenantModel,
    VersionArtifactModel,
    VersionStatus,
)
from enterprise_rag.db.publish import publish_version
from enterprise_rag.db.session import Database


@pytest.mark.asyncio
async def test_postgres_database_uses_small_pool_with_bounded_burst():
    """PostgreSQL 默认连接池必须限制常驻连接、突发连接和等待时间。"""

    database = Database("postgresql+asyncpg://user:pass@localhost/rag")
    try:
        pool = database.engine.sync_engine.pool
        assert pool.size() == 3
        assert pool._max_overflow == 2
        assert pool._timeout == 10
        assert pool._recycle == 1800
        assert pool._pre_ping is True
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_schema_readiness_distinguishes_unmigrated_and_ready_database(tmp_path):
    """readiness 只读 schema 状态；它不会替调用方执行 migration。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'readiness.db'}")
    try:
        before_migration = await database.schema_readiness()
        assert before_migration.ready is False
        assert before_migration.status == "migration_required"

        await database.migrate()

        after_migration = await database.schema_readiness()
        assert after_migration.ready is True
        assert after_migration.status == "ready"
        assert after_migration.target_version == LATEST_VERSION
        assert after_migration.applied_versions == (INITIAL_VERSION, EMBEDDING_VERSION, LATEST_VERSION)
    finally:
        await database.dispose()


@asynccontextmanager
async def _migrated_database(path: Path):
    """创建独立文件数据库，并保证测试结束后释放 engine 连接。"""

    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.migrate()
    try:
        yield database
    finally:
        await database.dispose()


def _ready_artifacts(tenant_id: str, document_id: str, version_id: str):
    """构造发布所需的完整产物集合，避免各发布测试重复手写状态。"""

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
async def test_empty_database_migration_is_repeatable(tmp_path):
    """空库升级应生成精确 schema；重复升级不得重复应用版本。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    try:
        await database.migrate()
        await database.migrate()

        async with database.engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
            applied_versions = (
                await connection.execute(text("SELECT version FROM schema_migrations"))
            ).scalars().all()

        assert table_names == EXPECTED_TABLES
        assert applied_versions == [INITIAL_VERSION, EMBEDDING_VERSION, LATEST_VERSION]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_database_can_upgrade_from_initial_schema_to_embedding_schema(tmp_path):
    """真实执行 0001 -> 0002，确认初始 schema 不会提前包含向量表。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'migration-upgrade.db'}")
    try:
        # 先只应用 0001，模拟已经运行旧版本代码的数据库。
        await upgrade_database(database.engine, target_version=INITIAL_VERSION)
        async with database.engine.connect() as connection:
            initial_tables = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
            initial_versions = (
                await connection.execute(text("SELECT version FROM schema_migrations ORDER BY version"))
            ).scalars().all()

        assert "embedding_records" not in initial_tables
        assert initial_versions == [INITIAL_VERSION]

        # 默认升级目标必须推进到最新版本，并且重复执行保持幂等。
        await database.migrate()
        await database.migrate()
        async with database.engine.connect() as connection:
            upgraded_tables = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
            upgraded_versions = (
                await connection.execute(text("SELECT version FROM schema_migrations ORDER BY version"))
            ).scalars().all()

        assert upgraded_tables == EXPECTED_TABLES
        assert upgraded_versions == [INITIAL_VERSION, EMBEDDING_VERSION, LATEST_VERSION]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_database_can_stop_at_embedding_schema_without_dense_tables(tmp_path):
    """显式目标 0002 时不得提前创建 0003 Dense generation 表。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'embedding-only.db'}")
    try:
        await upgrade_database(database.engine, target_version=EMBEDDING_VERSION)
        async with database.engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
        assert "embedding_records" in table_names
        assert not {
            "tenant_dense_collections",
            "dense_generations",
            "dense_generation_pointers",
        } & table_names
    finally:
        await database.dispose()


def test_artifact_and_embedding_models_keep_distinct_columns_and_constraints():
    """产物状态与向量字段必须留在各自表，避免 ORM 映射和数据库约束串位。"""

    artifact_columns = set(VersionArtifactModel.__table__.c.keys())
    embedding_columns = set(EmbeddingRecordModel.__table__.c.keys())
    embedding_constraint_names = {
        constraint.name for constraint in EmbeddingRecordModel.__table__.constraints
    }

    assert {"status", "item_count", "failure_reason", "updated_at"} <= artifact_columns
    assert {"text", "parent_text", "embedding", "dimension"} <= embedding_columns
    assert "status" not in embedding_columns
    assert "fk_embeddings_version_tenant" in embedding_constraint_names
    assert "ck_embeddings_dimension_positive" in embedding_constraint_names
    assert VersionArtifactModel.version.property.back_populates == "artifacts"


@pytest.mark.asyncio
async def test_migration_failure_is_wrapped_and_version_is_not_recorded(tmp_path, monkeypatch):
    """schema 创建异常必须显式上抛，且不能留下虚假的已完成版本。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'broken-migration.db'}")

    # 直接在 metadata 边界注入失败，验证 migration runner 的错误分类与事务语义。
    def fail_create_all(*_args, **_kwargs):
        raise RuntimeError("injected schema failure")

    monkeypatch.setattr(MetaData, "create_all", fail_create_all)
    try:
        with pytest.raises(MigrationError) as captured:
            await database.migrate()

        assert captured.value.target_version == LATEST_VERSION
        assert isinstance(captured.value.cause, RuntimeError)

        async with database.engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
            if "schema_migrations" in table_names:
                applied_versions = (
                    await connection.execute(text("SELECT version FROM schema_migrations"))
                ).scalars().all()
                assert applied_versions == []
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_composite_foreign_keys_reject_cross_tenant_lineage(tmp_path):
    """version 与 ACL 必须同时匹配 tenant_id 和 document_id。"""

    async with _migrated_database(tmp_path / "tenant-boundary.db") as database:
        async with database.transaction() as session:
            session.add_all(
                [
                    TenantModel(id="tenant-a", name="租户 A"),
                    TenantModel(id="tenant-b", name="租户 B"),
                    DocumentModel(
                        tenant_id="tenant-b",
                        id="doc-b",
                        source_id="git:b/doc.md",
                        title="B 文档",
                    ),
                ]
            )

        # tenant-a 虽然存在，但 doc-b 只属于 tenant-b；复合外键必须拒绝该 lineage。
        with pytest.raises(IntegrityError):
            async with database.transaction() as session:
                session.add(
                    DocumentVersionModel(
                        tenant_id="tenant-a",
                        document_id="doc-b",
                        id="version-a",
                        content_hash="hash-a",
                    )
                )

        # current_version_id 也必须遵守同一组 tenant/document lineage，不能只依赖服务层。
        with pytest.raises(IntegrityError):
            async with database.transaction() as session:
                session.add(
                    DocumentModel(
                        tenant_id="tenant-b",
                        id="doc-pointer",
                        source_id="git:b/pointer.md",
                        title="错误指针",
                        current_version_id="version-a",
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


@pytest.mark.asyncio
async def test_ingestion_idempotency_key_is_unique_only_inside_tenant(tmp_path):
    """同租户重复请求应冲突，不同租户可以安全复用外部请求键。"""

    async with _migrated_database(tmp_path / "idempotency.db") as database:
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
                ]
            )

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

        async with database.session() as session:
            jobs = (await session.scalars(select(IngestionJobModel))).all()
        assert {(job.tenant_id, job.id) for job in jobs} == {
            ("tenant-a", "job-a-1"),
            ("tenant-b", "job-b-1"),
        }


@pytest.mark.asyncio
async def test_publish_atomically_switches_current_version(tmp_path):
    """所有产物 ready 后，旧版本与新版本必须在同一事务中切换。"""

    published_at = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    async with _migrated_database(tmp_path / "publish.db") as database:
        async with database.transaction() as session:
            session.add(TenantModel(id="tenant-a", name="租户 A"))
            session.add(
                DocumentModel(
                    tenant_id="tenant-a",
                    id="doc-a",
                    source_id="git:a/doc.md",
                    title="A 文档",
                    status=DocumentStatus.READY,
                    current_version_id="version-1",
                )
            )
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
                        status=VersionStatus.READY,
                    ),
                    *_ready_artifacts("tenant-a", "doc-a", "version-2"),
                ]
            )

        async with database.transaction() as session:
            await publish_version(
                session,
                tenant_id="tenant-a",
                document_id="doc-a",
                version_id="version-2",
                now=published_at,
            )

        async with database.session() as session:
            document = await session.get(DocumentModel, ("tenant-a", "doc-a"))
            old_version = await session.get(
                DocumentVersionModel, ("tenant-a", "doc-a", "version-1")
            )
            new_version = await session.get(
                DocumentVersionModel, ("tenant-a", "doc-a", "version-2")
            )

        assert document is not None and document.current_version_id == "version-2"
        assert old_version is not None and old_version.status is VersionStatus.SUPERSEDED
        assert new_version is not None and new_version.status is VersionStatus.PUBLISHED
        assert new_version.published_at is not None


@pytest.mark.asyncio
async def test_publish_changes_roll_back_when_transaction_commit_fails(tmp_path):
    """发布后的任意 PostgreSQL 写失败，都应回滚同事务内的可见性切换。"""

    async with _migrated_database(tmp_path / "publish-rollback.db") as database:
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
                # 空 idempotency_key 触发数据库 CHECK；失败发生在事务提交时，
                # 因而可以验证此前发布字段也被一起回滚，而不是只验证显式异常。
                session.add(
                    IngestionJobModel(
                        tenant_id="tenant-a",
                        id="invalid-job",
                        document_id="doc-a",
                        idempotency_key="   ",
                        status=JobStatus.PENDING,
                    )
                )

        async with database.session() as session:
            document = await session.get(DocumentModel, ("tenant-a", "doc-a"))
            version = await session.get(
                DocumentVersionModel, ("tenant-a", "doc-a", "version-1")
            )

        assert document is not None and document.current_version_id is None
        assert document.status is DocumentStatus.DRAFT
        assert version is not None and version.status is VersionStatus.READY
        assert version.published_at is None
