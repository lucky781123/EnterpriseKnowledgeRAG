"""Dense generation PostgreSQL 事实模型的 SQLite contract 测试。

这些测试使用 SQLite 验证 migration 和显式事务语义；SQLite 不提供 PostgreSQL
行锁并发证据，真实锁行为留给设置专用 ``POSTGRES_DSN`` 的集成 smoke。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, text

from enterprise_rag.db.migrations import (
    EMBEDDING_VERSION,
    INITIAL_VERSION,
    WEAVIATE_DENSE_VERSION,
    WEAVIATE_HYBRID_VERSION,
)
from enterprise_rag.db.models import (
    DenseGenerationPointerModel,
    DocumentModel,
    DocumentVersionModel,
    TenantModel,
)
from enterprise_rag.db.publish import (
    DenseGenerationPublishError,
    create_generation,
    mark_generation_ready,
    publish_dense_generation,
    rollback_dense_generation,
)
from enterprise_rag.db.session import Database
from enterprise_rag.domain.entities import DenseGenerationStatus, VersionStatus
from enterprise_rag.retrieval.contracts import (
    GenerationScope,
    IndexProfile,
    collection_name_for_tenant,
)
from enterprise_rag.retrieval.generation import cleanup_generation, retire_generation


def _profile() -> IndexProfile:
    """返回测试使用的稳定 index profile。"""

    return IndexProfile("demo-embedding", 2, "parent-child-v1", "dense-v1")


def _scope(profile: IndexProfile) -> GenerationScope:
    """返回测试使用的单租户、单文档版本 scope。"""

    return GenerationScope("tenant-a", "doc-1", "version-1", profile.key)


async def _seed_version(database: Database, status: VersionStatus = VersionStatus.READY) -> None:
    """创建 generation 外键所需的租户、文档和版本事实。"""

    async with database.transaction() as session:
        session.add(TenantModel(id="tenant-a", name="租户 A"))
        session.add(
            DocumentModel(
                tenant_id="tenant-a",
                id="doc-1",
                source_id="source-1",
                title="测试文档",
            )
        )
        session.add(
            DocumentVersionModel(
                tenant_id="tenant-a",
                document_id="doc-1",
                id="version-1",
                content_hash="hash-1",
                source_revision="rev-1",
                status=status,
                created_at=datetime.now(UTC),
            )
        )


@pytest.mark.asyncio
async def test_generation_tables_and_pointer_are_created_after_0003(tmp_path):
    """默认 migration 必须按顺序创建三张 Dense generation 事实表。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'generation-migration.db'}")
    try:
        await database.migrate()
        async with database.engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
            versions = (
                await connection.execute(text("SELECT version FROM schema_migrations ORDER BY version"))
            ).scalars().all()
        assert {
            "tenant_dense_collections",
            "dense_generations",
            "dense_generation_pointers",
        } <= tables
        assert versions == [
            INITIAL_VERSION,
            EMBEDDING_VERSION,
            WEAVIATE_DENSE_VERSION,
            WEAVIATE_HYBRID_VERSION,
        ]
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_dense_generation_publish_and_rollback_are_pointer_scoped(tmp_path):
    """generation 必须完整后才能发布，回滚只切换对应 scope 的 pointer。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'generation-publish.db'}")
    profile = _profile()
    scope = _scope(profile)
    try:
        await database.migrate()
        await _seed_version(database)
        async with database.transaction() as session:
            generation = await create_generation(
                session,
                scope,
                profile,
                manifest_hash="manifest-1",
                expected_item_count=2,
                build_attempt=1,
                worker_lease="lease-1",
            )
            with pytest.raises(DenseGenerationPublishError, match="BUILDING"):
                await publish_dense_generation(session, generation.generation_id)

        async with database.transaction() as session:
            generation = await mark_generation_ready(session, generation.generation_id, 2)
            assert generation.status is DenseGenerationStatus.READY
            await publish_dense_generation(session, generation.generation_id)

        async with database.transaction() as session:
            pointer = await session.get(
                DenseGenerationPointerModel,
                (scope.tenant_id, scope.document_id, scope.version_id, scope.index_profile_key),
            )
            assert pointer is not None
            assert pointer.active_generation_id == generation.generation_id

            second = await create_generation(
                session,
                scope,
                profile,
                manifest_hash="manifest-2",
                expected_item_count=1,
                build_attempt=2,
                worker_lease="lease-2",
            )
            await mark_generation_ready(session, second.generation_id, 1)
            await publish_dense_generation(session, second.generation_id)

        async with database.transaction() as session:
            await rollback_dense_generation(session, scope, generation.generation_id)

        async with database.session() as session:
            pointer = await session.get(
                DenseGenerationPointerModel,
                (scope.tenant_id, scope.document_id, scope.version_id, scope.index_profile_key),
            )
            assert pointer is not None
            assert pointer.active_generation_id == generation.generation_id
            assert pointer.previous_generation_id == second.generation_id
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_retire_generation_and_cleanup_block_active_pointer(tmp_path):
    """active pointer 仍指向 generation 时，retire 后也不能立即清理外部对象。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'generation-cleanup-active.db'}")
    profile = _profile()
    scope = _scope(profile)

    class _Transport:
        """记录清理调用的最小 transport 替身。"""

        async def delete_generation(self, collection_name: str, *, generation_id: str, version_id: str) -> int:
            """返回固定删除数量，验证调用参数由数据库事实提供。"""

            raise AssertionError("active generation 不应调用外部删除")

    try:
        await database.migrate()
        await _seed_version(database)
        async with database.transaction() as session:
            generation = await create_generation(
                session,
                scope,
                profile,
                manifest_hash="manifest-1",
                expected_item_count=1,
                build_attempt=1,
                worker_lease="lease-1",
            )
            await mark_generation_ready(session, generation.generation_id, 1)
            await publish_dense_generation(session, generation.generation_id)
            retired = await retire_generation(session, generation.generation_id)
            assert retired.status is DenseGenerationStatus.RETIRED
            with pytest.raises(ValueError, match="active generation"):
                await cleanup_generation(session, generation.generation_id, _Transport())
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_cleanup_non_active_retired_generation_uses_transport(tmp_path):
    """generation 切换后，旧 RETIRED generation 才允许调用外部清理。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'generation-cleanup-retired.db'}")
    profile = _profile()
    scope = _scope(profile)
    calls: list[tuple[str, str, str]] = []

    class _Transport:
        """记录非 active generation 的删除参数。"""

        async def delete_generation(self, collection_name: str, *, generation_id: str, version_id: str) -> int:
            """返回外部服务报告的删除数量。"""

            calls.append((collection_name, generation_id, version_id))
            return 1

    try:
        await database.migrate()
        await _seed_version(database)
        async with database.transaction() as session:
            first = await create_generation(
                session,
                scope,
                profile,
                manifest_hash="manifest-1",
                expected_item_count=1,
                build_attempt=1,
                worker_lease="lease-1",
            )
            await mark_generation_ready(session, first.generation_id, 1)
            await publish_dense_generation(session, first.generation_id)

            second = await create_generation(
                session,
                scope,
                profile,
                manifest_hash="manifest-2",
                expected_item_count=1,
                build_attempt=2,
                worker_lease="lease-2",
            )
            await mark_generation_ready(session, second.generation_id, 1)
            await publish_dense_generation(session, second.generation_id)
            deleted = await cleanup_generation(session, first.generation_id, _Transport())

        assert deleted == 1
        assert calls == [
            (collection_name_for_tenant("tenant-a"), first.generation_id, "version-1")
        ]
    finally:
        await database.dispose()
