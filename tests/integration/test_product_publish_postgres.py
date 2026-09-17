"""本地 P0 产品发布事务的真实 PostgreSQL 回滚验证。"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

import enterprise_rag.db.publish as publish_module
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
    TenantModel,
    VersionArtifactModel,
)
from enterprise_rag.db.publish import (
    create_generation,
    mark_product_generation_ready,
    publish_product_generation,
)
from enterprise_rag.db.session import Database, normalize_database_url
from enterprise_rag.domain.entities import DenseGenerationStatus, VersionStatus
from enterprise_rag.retrieval.contracts import GenerationScope, IndexProfile
from enterprise_rag.retrieval.sparse import SparseProfile

POSTGRES_DSN = os.getenv("POSTGRES_DSN")

if POSTGRES_DSN:
    pytest.importorskip("asyncpg")

pytestmark = pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 POSTGRES_DSN，跳过真实 PostgreSQL 产品发布测试",
)


def _render_database_url(base_url, database_name: str) -> str:
    """替换数据库名并保留真实密码，仅用于内存中的测试连接。"""

    return base_url.set(database=database_name).render_as_string(hide_password=False)


@pytest.fixture
async def product_database() -> AsyncIterator[Database]:
    """为产品发布测试创建并销毁独立 PostgreSQL 临时数据库。"""

    if not POSTGRES_DSN:
        pytest.skip("未设置 POSTGRES_DSN")
    base_url = make_url(normalize_database_url(POSTGRES_DSN))
    database_name = f"enterprise_rag_product_{uuid4().hex}"
    admin_database = Database(_render_database_url(base_url, "postgres"))
    database = Database(_render_database_url(base_url, database_name))

    async def execute_admin(statement: str) -> None:
        """使用 AUTOCOMMIT 执行临时数据库的 CREATE/DROP。"""

        async with admin_database.engine.connect() as connection:
            connection = await connection.execution_options(
                isolation_level="AUTOCOMMIT"
            )
            await connection.execute(text(statement))

    try:
        await execute_admin(f'CREATE DATABASE "{database_name}"')
        await database.migrate()
        yield database
    finally:
        await database.dispose()
        await execute_admin(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        await admin_database.dispose()


def _dense_profile() -> IndexProfile:
    """返回测试使用的固定 Dense profile。"""

    return IndexProfile("demo-embedding", 2, "parent-child-v1", "dense-v1")


def _sparse_profile() -> SparseProfile:
    """返回测试使用的产品默认 Sparse profile。"""

    return SparseProfile(
        tokenizer_version="word",
        preprocessing_version="identity",
        schema_version="sparse-v1",
        field_weights={
            "title": 2.0,
            "section_title": 2.0,
            "api_symbols": 3.0,
            "body": 1.0,
        },
    )


async def _seed_ready_generation(database: Database) -> DenseGenerationModel:
    """创建具备四类 READY artifact、READ ACL 和 READY generation 的版本。"""

    async with database.transaction() as session:
        session.add(TenantModel(id="tenant-a", name="租户 A"))
    async with database.transaction() as session:
        session.add(
            DocumentModel(
                tenant_id="tenant-a",
                id="doc-1",
                source_id="upload:doc-1",
                title="产品文档",
            )
        )
    async with database.transaction() as session:
        session.add(
            DocumentVersionModel(
                tenant_id="tenant-a",
                document_id="doc-1",
                id="version-1",
                content_hash="hash-1",
                status=VersionStatus.READY,
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
    async with database.transaction() as session:
        generation = await create_generation(
            session,
            GenerationScope("tenant-a", "doc-1", "version-1", _dense_profile().key),
            _dense_profile(),
            manifest_hash="manifest-1",
            expected_item_count=2,
            build_attempt=1,
            worker_lease="local-request-1",
            sparse_profile=_sparse_profile(),
        )
        return await mark_product_generation_ready(
            session,
            generation.generation_id,
            dense_item_count=2,
            sparse_item_count=2,
        )


@pytest.mark.asyncio
async def test_product_publish_failure_rolls_back_version_and_generation_pointer(
    product_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generation 指针阶段失败时，文档版本切换也必须随事务一起回滚。"""

    generation = await _seed_ready_generation(product_database)

    async def fail_pointer_switch(*_args, **_kwargs):
        """在版本已写入 session 后注入 generation pointer 失败。"""

        raise RuntimeError("forced pointer failure")

    monkeypatch.setattr(
        publish_module,
        "publish_dense_generation",
        fail_pointer_switch,
    )
    with pytest.raises(RuntimeError, match="forced pointer failure"):
        async with product_database.transaction() as session:
            await publish_product_generation(session, generation.generation_id)

    async with product_database.session() as session:
        document = await session.get(DocumentModel, ("tenant-a", "doc-1"))
        version = await session.get(
            DocumentVersionModel,
            ("tenant-a", "doc-1", "version-1"),
        )
        stored_generation = await session.get(
            DenseGenerationModel,
            generation.generation_id,
        )
        pointers = (
            await session.scalars(select(DenseGenerationPointerModel))
        ).all()

    assert document is not None
    assert version is not None
    assert stored_generation is not None
    assert document.current_version_id is None
    assert version.status is VersionStatus.READY
    assert stored_generation.status is DenseGenerationStatus.READY
    assert pointers == []
