"""真实 PostgreSQL 摄取状态集成测试。

本文件只在设置专用开发/CI ``POSTGRES_DSN`` 时运行。每个测试创建随机临时
数据库，结束时仅删除该临时数据库，不触碰现有开发容器或 named volume。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from enterprise_rag.db.models import (
    ArtifactKind,
    ArtifactStatus,
    DocumentModel,
    DocumentVersionModel,
    IngestionJobModel,
    JobStatus,
    VersionArtifactModel,
    VersionStatus,
)
from enterprise_rag.db.session import Database, normalize_database_url
from enterprise_rag.ingestion.contracts import IngestionRequest
from enterprise_rag.ingestion.service import IngestionService
from enterprise_rag.storage.local import LocalObjectStorage


POSTGRES_DSN = os.getenv("POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 POSTGRES_DSN，跳过真实 PostgreSQL 摄取集成测试",
)


def _render_database_url(base_url, database_name: str) -> str:
    """替换数据库名并保留密码；只在内存中构造 URL，不打印敏感值。"""

    return base_url.set(database=database_name).render_as_string(hide_password=False)


@pytest.fixture
async def ingestion_database() -> AsyncIterator[Database]:
    """为每个测试创建隔离数据库，并在 finally 中强制删除。"""

    if not POSTGRES_DSN:
        pytest.skip("未设置 POSTGRES_DSN")
    base_url = make_url(normalize_database_url(POSTGRES_DSN))
    database_name = f"enterprise_rag_ingest_{uuid4().hex}"
    admin = Database(_render_database_url(base_url, "postgres"))
    database = Database(_render_database_url(base_url, database_name))

    async def admin_sql(statement: str) -> None:
        """在 AUTOCOMMIT 下执行 CREATE/DROP DATABASE。"""

        async with admin.engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(statement))

    try:
        await admin_sql(f'CREATE DATABASE "{database_name}"')
        await database.migrate()
        yield database
    finally:
        await database.dispose()
        await admin_sql(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        await admin.dispose()


def _request(**overrides) -> IngestionRequest:
    """构造真实数据库测试使用的最小 Markdown 请求。"""

    values = {
        "tenant_id": "tenant-postgres",
        "source_id": "git:repo/postgres.md",
        "title": "PostgreSQL 摄取验证",
        "filename": "postgres.md",
        "content": "# 数据库\n\n真实摄取状态。".encode(),
        "idempotency_key": "pg-request-1",
    }
    values.update(overrides)
    return IngestionRequest(**values)


class _FirstRawPutBarrierStorage:
    """让两个 PostgreSQL 请求在首次原文写入处会合，稳定覆盖并发窗口。"""

    def __init__(self, inner: LocalObjectStorage) -> None:
        self.inner = inner
        # 两个请求都到达 barrier 后才继续，避免测试偶然变成先后执行，
        # 从而确保它确实覆盖“先查询后提交”的数据库竞态窗口。
        self.barrier = asyncio.Barrier(2)

    async def put(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        if key.startswith("raw/"):
            await self.barrier.wait()
        await self.inner.put(key, data, content_type)

    async def get(self, key: str) -> bytes:
        return await self.inner.get(key)

    async def delete(self, key: str) -> None:
        await self.inner.delete(key)

    async def exists(self, key: str) -> bool:
        return await self.inner.exists(key)


class _RawGetReleaseStorage(_FirstRawPutBarrierStorage):
    """让两个 PostgreSQL job 都登记后再开始读取原文。"""

    def __init__(self, inner: LocalObjectStorage) -> None:
        super().__init__(inner)
        # 释放点由测试根据数据库状态触发，避免用固定 sleep 猜测两个事务何时完成。
        self.raw_get_release = asyncio.Event()

    async def get(self, key: str) -> bytes:
        if key.startswith("raw/"):
            await self.raw_get_release.wait()
        return await super().get(key)


@pytest.mark.asyncio
async def test_postgres_ingestion_persists_ready_raw_and_parsed_artifacts(
    ingestion_database: Database, tmp_path
):
    """真实 PostgreSQL 应保存成功 job/version 及正确的 artifact 状态。"""

    service = IngestionService(ingestion_database, LocalObjectStorage(tmp_path / "objects"))

    result = await service.ingest(_request())

    assert result.status is JobStatus.SUCCEEDED
    assert result.version_status is VersionStatus.READY
    async with ingestion_database.session() as session:
        artifacts = (
            await session.scalars(
                select(VersionArtifactModel).where(
                    VersionArtifactModel.tenant_id == result.tenant_id,
                    VersionArtifactModel.version_id == result.version_id,
                )
            )
        ).all()
        document = await session.get(DocumentModel, (result.tenant_id, result.document_id))
        version = await session.get(
            DocumentVersionModel, (result.tenant_id, result.document_id, result.version_id)
        )
    statuses = {artifact.kind: artifact.status for artifact in artifacts}
    assert statuses[ArtifactKind.RAW] is ArtifactStatus.READY
    assert statuses[ArtifactKind.PARSED] is ArtifactStatus.READY
    assert statuses[ArtifactKind.EMBEDDING] is ArtifactStatus.PENDING
    assert statuses[ArtifactKind.SPARSE] is ArtifactStatus.PENDING
    assert document is not None and document.current_version_id is None
    assert version is not None and version.status is VersionStatus.READY


@pytest.mark.asyncio
async def test_postgres_ingestion_reuses_same_idempotency_job_and_version(
    ingestion_database: Database, tmp_path
):
    """同一个 PostgreSQL 租户内重复请求不得产生第二个 job 或 version。"""

    service = IngestionService(ingestion_database, LocalObjectStorage(tmp_path / "objects"))
    first = await service.ingest(_request())
    second = await service.ingest(_request())

    assert (first.job_id, first.version_id) == (second.job_id, second.version_id)
    async with ingestion_database.session() as session:
        assert len((await session.scalars(select(IngestionJobModel))).all()) == 1
        assert len((await session.scalars(select(DocumentVersionModel))).all()) == 1


@pytest.mark.asyncio
async def test_postgres_concurrent_same_idempotency_request_has_one_winner(
    ingestion_database: Database, tmp_path
):
    """真实 PostgreSQL 并发相同幂等键必须收敛到一个 job/version。"""

    storage = _FirstRawPutBarrierStorage(LocalObjectStorage(tmp_path / "objects"))
    service = IngestionService(ingestion_database, storage)

    # barrier 确保两个调用都完成第一次原文写入前置步骤，再由 PostgreSQL
    # 的唯一约束和租户行锁决定唯一赢家；测试不依赖进程内锁或固定 sleep。
    results = await asyncio.gather(service.ingest(_request()), service.ingest(_request()))

    assert len({(result.job_id, result.version_id) for result in results}) == 1
    assert all(result.status is JobStatus.SUCCEEDED for result in results)
    async with ingestion_database.session() as session:
        assert len((await session.scalars(select(IngestionJobModel))).all()) == 1
        assert len((await session.scalars(select(DocumentVersionModel))).all()) == 1


@pytest.mark.asyncio
async def test_postgres_concurrent_shared_version_failure_is_processed_once(
    ingestion_database: Database, tmp_path
):
    """真实 PostgreSQL 中共享 version 失败也只能有一个 parser owner。"""

    storage = _RawGetReleaseStorage(LocalObjectStorage(tmp_path / "objects"))
    service = IngestionService(ingestion_database, storage)
    parse_calls = 0

    def fail_parser(_data: bytes, _filename: str):
        """用确定性失败替身验证失败状态不会被并发 job 互相覆盖。"""

        nonlocal parse_calls
        parse_calls += 1
        raise ValueError("测试用解析失败")

    service.parsers[".md"] = fail_parser
    first_task = asyncio.create_task(
        service.ingest(_request(idempotency_key="pg-failure-a"))
    )
    second_task = asyncio.create_task(
        service.ingest(_request(idempotency_key="pg-failure-b"))
    )
    try:
        # 只有当两个 job 已登记、且同一 version 已进入 PROCESSING，才允许
        # parser 继续；这能稳定覆盖跨事务的共享 version 竞态窗口。
        for _ in range(300):
            async with ingestion_database.session() as session:
                jobs = (await session.scalars(select(IngestionJobModel))).all()
                versions = (await session.scalars(select(DocumentVersionModel))).all()
            if (
                len(jobs) == 2
                and len(versions) == 1
                and versions[0].status is VersionStatus.PROCESSING
            ):
                storage.raw_get_release.set()
                break
            await asyncio.sleep(0.01)
        else:
            storage.raw_get_release.set()
            pytest.fail("两个 PostgreSQL job 未在共享 version PROCESSING 时完成注册")

        first, second = await asyncio.gather(first_task, second_task)
        assert first.status is JobStatus.FAILED
        assert second.status is JobStatus.FAILED
        assert first.version_id == second.version_id
        assert parse_calls == 1
        async with ingestion_database.session() as session:
            jobs = (await session.scalars(select(IngestionJobModel))).all()
            version = (await session.scalars(select(DocumentVersionModel))).one()
        assert len(jobs) == 2
        assert all(job.status is JobStatus.FAILED for job in jobs)
        assert version.status is VersionStatus.FAILED
    finally:
        # 若测试在轮询或断言阶段提前失败，也要解除 barrier，避免后台 task 悬挂。
        storage.raw_get_release.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
