"""摄取服务的离线状态、幂等和 replay 契约测试。

测试使用 SQLite 只验证应用层状态转换与对象存储产物；真实 PostgreSQL 证据另由
集成测试提供，不能把 SQLite 并发行为外推到 PostgreSQL。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from enterprise_rag.db.models import (
    ArtifactKind,
    ArtifactStatus,
    DocumentModel,
    DocumentVersionModel,
    IngestionJobModel,
    JobStatus,
    VersionStatus,
    VersionArtifactModel,
)
from enterprise_rag.db.session import Database
from enterprise_rag.ingestion.contracts import IngestionRequest
from enterprise_rag.ingestion.service import IdempotencyConflictError, IngestionService
from enterprise_rag.storage.local import LocalObjectStorage


class _FirstRawPutBarrierStorage:
    """让两个并发请求在首次原文写入处会合，稳定重现幂等竞态。"""

    def __init__(self, inner: LocalObjectStorage) -> None:
        self.inner = inner
        # 两个 ingest 调用都必须抵达 barrier，才能继续写入各自的 raw object；
        # 这样可以证明测试覆盖的是“先查后插入”的并发窗口，而不是偶然调度顺序。
        self.barrier = asyncio.Barrier(2)

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
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
    """在两个 job 都注册后再释放原文读取，稳定覆盖共享 version 竞态。"""

    def __init__(self, inner: LocalObjectStorage) -> None:
        super().__init__(inner)
        # 第一个真正执行的 job 会在 parser 前读取 raw；测试先让这一步暂停，
        # 以便第二个 job 有机会观察到同一 version 已经处于 PROCESSING。
        self.raw_get_release = asyncio.Event()

    async def get(self, key: str) -> bytes:
        if key.startswith("raw/"):
            await self.raw_get_release.wait()
        return await super().get(key)


def _request(content: bytes = "# 标题\n\n正文".encode(), **overrides) -> IngestionRequest:
    """构造服务测试请求；所有测试使用同一租户和稳定 source identity。"""

    values = {
        "tenant_id": "tenant-a",
        "source_id": "git:repo/docs/guide.md",
        "title": "开发指南",
        "filename": "guide.md",
        "content": content,
        "idempotency_key": "request-1",
    }
    values.update(overrides)
    return IngestionRequest(**values)


@pytest.mark.asyncio
async def test_ingest_writes_raw_and_parsed_artifacts_without_publishing(tmp_path):
    """成功摄取应完成 raw/parsed，但不能偷偷切换当前可见版本。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'ingestion.db'}")
    await database.migrate()
    storage = LocalObjectStorage(tmp_path / "objects")
    service = IngestionService(database, storage)
    try:
        result = await service.ingest(_request())

        assert result.status is JobStatus.SUCCEEDED
        assert result.version_status is VersionStatus.READY
        assert result.raw_object_key and await storage.exists(result.raw_object_key)
        assert result.parsed_object_key and await storage.exists(result.parsed_object_key)

        manifest = json.loads(await storage.get(result.parsed_object_key))
        assert manifest["version_id"] == result.version_id
        assert manifest["chunks"]["strategy_version"] == "parent-child-v1"

        async with database.session() as session:
            job = await session.get(IngestionJobModel, ("tenant-a", result.job_id))
            version = await session.get(
                DocumentVersionModel, ("tenant-a", result.document_id, result.version_id)
            )
            document = await session.get(DocumentModel, ("tenant-a", result.document_id))
            artifacts = (
                await session.scalars(
                    select(VersionArtifactModel).where(
                        VersionArtifactModel.tenant_id == "tenant-a",
                        VersionArtifactModel.version_id == result.version_id,
                    )
                )
            ).all()

        assert job is not None and job.status is JobStatus.SUCCEEDED
        assert version is not None and version.status is VersionStatus.READY
        assert document is not None and document.current_version_id is None
        statuses = {artifact.kind: artifact.status for artifact in artifacts}
        assert statuses[ArtifactKind.RAW] is ArtifactStatus.READY
        assert statuses[ArtifactKind.PARSED] is ArtifactStatus.READY
        assert statuses[ArtifactKind.EMBEDDING] is ArtifactStatus.PENDING
        assert statuses[ArtifactKind.SPARSE] is ArtifactStatus.PENDING
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_same_idempotency_request_reuses_job_and_version(tmp_path):
    """同租户同幂等键重复提交不得创建第二个可见 job/version。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'idempotent.db'}")
    await database.migrate()
    service = IngestionService(database, LocalObjectStorage(tmp_path / "objects"))
    try:
        first = await service.ingest(_request())
        second = await service.ingest(_request())

        assert (second.job_id, second.version_id) == (first.job_id, first.version_id)
        async with database.session() as session:
            jobs = (await session.scalars(select(IngestionJobModel))).all()
            versions = (await session.scalars(select(DocumentVersionModel))).all()
        assert len(jobs) == 1
        assert len(versions) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_new_idempotency_key_reuses_ready_version_without_reparsing(tmp_path):
    """同一内容已完成后，新 job 应复用 READY version 和 parsed manifest。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'ready-version-reuse.db'}")
    await database.migrate()
    service = IngestionService(database, LocalObjectStorage(tmp_path / "objects"))
    original_parser = service.parsers[".md"]
    parse_calls = 0

    def counting_parser(data: bytes, filename: str):
        """记录真实 parser 调用次数，防止 READY version 被重复解析。"""

        nonlocal parse_calls
        parse_calls += 1
        return original_parser(data, filename)

    service.parsers[".md"] = counting_parser
    try:
        first = await service.ingest(_request(idempotency_key="ready-first"))
        second = await service.ingest(_request(idempotency_key="ready-second"))

        assert first.status is JobStatus.SUCCEEDED
        assert second.status is JobStatus.SUCCEEDED
        assert second.job_id != first.job_id
        assert second.version_id == first.version_id
        assert second.parsed_object_key == first.parsed_object_key
        # 第二个幂等键表示独立的任务事实，但原文内容已对应一个完整 READY version；
        # 此时再次调用 parser 会浪费资源，并可能用新执行结果覆盖不可变 manifest。
        assert parse_calls == 1
        async with database.session() as session:
            jobs = (await session.scalars(select(IngestionJobModel))).all()
            versions = (await session.scalars(select(DocumentVersionModel))).all()
        assert len(jobs) == 2
        assert all(job.status is JobStatus.SUCCEEDED for job in jobs)
        assert len(versions) == 1
        assert versions[0].status is VersionStatus.READY
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_concurrent_same_idempotency_request_returns_one_winner(tmp_path):
    """并发相同幂等键必须收敛到同一个 job/version，而不是泄露唯一键异常。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'concurrent-idempotent.db'}")
    await database.migrate()
    storage = _FirstRawPutBarrierStorage(LocalObjectStorage(tmp_path / "objects"))
    service = IngestionService(database, storage)
    try:
        # barrier 确保两个调用都在第一次查询后继续执行，从而覆盖真实竞态窗口。
        results = await asyncio.gather(service.ingest(_request()), service.ingest(_request()))

        assert {(result.job_id, result.version_id) for result in results}.__len__() == 1
        assert all(result.status is JobStatus.SUCCEEDED for result in results)
        async with database.session() as session:
            jobs = (await session.scalars(select(IngestionJobModel))).all()
            versions = (await session.scalars(select(DocumentVersionModel))).all()
        assert len(jobs) == 1
        assert len(versions) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_concurrent_same_content_different_idempotency_keys_share_version(tmp_path):
    """相同内容的并发新请求应各有 job，但只能共享一个不可变 version。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'concurrent-version.db'}")
    await database.migrate()
    storage = _FirstRawPutBarrierStorage(LocalObjectStorage(tmp_path / "objects"))
    service = IngestionService(database, storage)
    try:
        # 两个不同幂等键代表两次独立执行尝试；它们仍应通过 content hash 收敛到
        # 同一 version，而不是因并发插入同一内容版本而暴露唯一键异常。
        results = await asyncio.gather(
            service.ingest(_request(idempotency_key="request-a")),
            service.ingest(_request(idempotency_key="request-b")),
        )

        assert all(result.status is JobStatus.SUCCEEDED for result in results)
        assert len({result.version_id for result in results}) == 1
        assert len({result.job_id for result in results}) == 2
        async with database.session() as session:
            jobs = (await session.scalars(select(IngestionJobModel))).all()
            versions = (await session.scalars(select(DocumentVersionModel))).all()
        assert len(jobs) == 2
        assert len(versions) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_concurrent_shared_version_failure_is_processed_once(tmp_path):
    """相同内容并发解析失败时只允许一个 parser，所有 job 复用 FAILED 终态。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'concurrent-version-failure.db'}")
    await database.migrate()
    storage = _RawGetReleaseStorage(LocalObjectStorage(tmp_path / "objects"))
    service = IngestionService(database, storage)
    parse_calls = 0

    def fail_parser(_data: bytes, _filename: str):
        """用确定性失败替身模拟两个执行器都可能遇到的解析错误。"""

        nonlocal parse_calls
        parse_calls += 1
        raise ValueError("测试用解析失败")

    service.parsers[".md"] = fail_parser
    try:
        first_task = asyncio.create_task(
            service.ingest(_request(idempotency_key="failure-a"))
        )
        second_task = asyncio.create_task(
            service.ingest(_request(idempotency_key="failure-b"))
        )

        # 轮询数据库事实源而不是依赖固定 sleep；只有两个 job 已登记且共享
        # version 已被首个执行者抢占为 PROCESSING，才释放 raw 读取继续测试。
        for _ in range(200):
            async with database.session() as session:
                jobs = (await session.scalars(select(IngestionJobModel))).all()
                versions = (await session.scalars(select(DocumentVersionModel))).all()
            if len(jobs) == 2 and len(versions) == 1 and versions[0].status is VersionStatus.PROCESSING:
                storage.raw_get_release.set()
                break
            await asyncio.sleep(0.01)
        else:
            storage.raw_get_release.set()
            pytest.fail("两个 job 未在共享 version PROCESSING 时完成注册")

        first, second = await asyncio.gather(first_task, second_task)
        assert {first.status, second.status} == {JobStatus.FAILED}
        assert first.version_id == second.version_id
        # 关键断言：第二个 job 不能再次执行 parser，否则一次 version 失败会被
        # 多次解析和多次写失败状态，重新暴露共享 version 的先后覆盖风险。
        assert parse_calls == 1
        async with database.session() as session:
            jobs = (await session.scalars(select(IngestionJobModel))).all()
            version = (await session.scalars(select(DocumentVersionModel))).one()
        assert len(jobs) == 2
        assert all(job.status is JobStatus.FAILED for job in jobs)
        assert version.status is VersionStatus.FAILED
    finally:
        storage.raw_get_release.set()
        await database.dispose()


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_content_is_rejected(tmp_path):
    """幂等键复用时 payload 指纹变化必须显式冲突，而不是覆盖原文对象。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'conflict.db'}")
    await database.migrate()
    service = IngestionService(database, LocalObjectStorage(tmp_path / "objects"))
    try:
        await service.ingest(_request())
        with pytest.raises(IdempotencyConflictError):
            await service.ingest(_request(content="# 被替换的内容".encode()))
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_failed_job_requires_explicit_replay_and_can_resume(tmp_path):
    """解析失败要留下 FAILED；只有显式 replay 才能回到成功状态。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'replay.db'}")
    await database.migrate()
    storage = LocalObjectStorage(tmp_path / "objects")
    service = IngestionService(database, storage)
    failing_request = _request(
        content=b"not-a-pdf",
        filename="broken.pdf",
        content_type="application/pdf",
        idempotency_key="request-pdf",
    )
    try:
        failed = await service.ingest(failing_request)
        assert failed.status is JobStatus.FAILED

        # 用可控 parser double 恢复同一个 raw 对象，验证 replay 会重读已保存原文，
        # 而不是偷偷接受新的调用方 payload。
        service.parsers[".pdf"] = lambda _data, _filename: [
            service.make_block("paragraph", "恢复后的文本", {"kind": "page", "page": 1})
        ]

        replayed = await service.replay_failed("tenant-a", failed.job_id)
        assert replayed.status is JobStatus.SUCCEEDED
        assert replayed.version_id == failed.version_id

        async with database.session() as session:
            job = await session.get(IngestionJobModel, ("tenant-a", failed.job_id))
        assert job is not None and job.attempt == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_repeated_failed_request_does_not_implicitly_retry(tmp_path):
    """FAILED job 的普通重复提交只能返回失败事实，不能绕过显式 replay。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'failed-repeat.db'}")
    await database.migrate()
    service = IngestionService(database, LocalObjectStorage(tmp_path / "objects"))
    request = _request(
        content=b"not-a-pdf",
        filename="broken.pdf",
        content_type="application/pdf",
        idempotency_key="failed-repeat",
    )
    try:
        first = await service.ingest(request)
        second = await service.ingest(request)

        assert first.status is JobStatus.FAILED
        assert second.status is JobStatus.FAILED
        assert second.job_id == first.job_id
        async with database.session() as session:
            job = await session.get(IngestionJobModel, ("tenant-a", first.job_id))
        assert job is not None and job.attempt == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_replay_requires_failed_terminal_state(tmp_path):
    """成功任务不能被 replay API 意外退回处理态。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'replay-state.db'}")
    await database.migrate()
    service = IngestionService(database, LocalObjectStorage(tmp_path / "objects"))
    try:
        result = await service.ingest(_request())
        with pytest.raises(ValueError, match="FAILED"):
            await service.replay_failed("tenant-a", result.job_id)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_same_content_after_previous_failure_can_use_new_idempotency_key(tmp_path):
    """相同内容的后续任务可复用 FAILED version，但不能非法跳过 STAGED。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'failed-version-reuse.db'}")
    await database.migrate()
    storage = LocalObjectStorage(tmp_path / "objects")
    service = IngestionService(database, storage)
    failed_request = _request(
        content=b"not-a-pdf",
        filename="broken.pdf",
        content_type="application/pdf",
        idempotency_key="failed-first",
    )
    try:
        failed = await service.ingest(failed_request)
        assert failed.status is JobStatus.FAILED

        # 新幂等键提交相同内容时，服务应先把可复用的 FAILED version 重新置为
        # STAGED，再进入正常处理；这里替换 parser 以模拟修复后的解析器。
        service.parsers[".pdf"] = lambda _data, _filename: [
            service.make_block("page", "恢复文本", {"kind": "page", "page": 1})
        ]
        retried = await service.ingest(
            _request(
                content=b"not-a-pdf",
                filename="broken.pdf",
                content_type="application/pdf",
                idempotency_key="failed-second",
            )
        )

        assert retried.status is JobStatus.SUCCEEDED
        assert retried.version_id == failed.version_id
        async with database.session() as session:
            versions = (await session.scalars(select(DocumentVersionModel))).all()
        assert len(versions) == 1
        assert versions[0].status is VersionStatus.READY
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_replay_unknown_job_has_stable_service_error(tmp_path):
    """不存在的 job 必须返回服务层错误，不把对象存储实现细节泄露给调用方。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'unknown-replay.db'}")
    await database.migrate()
    service = IngestionService(database, LocalObjectStorage(tmp_path / "objects"))
    try:
        with pytest.raises(Exception) as captured:
            await service.replay_failed("tenant-a", "job-missing")
        assert type(captured.value).__name__ == "IngestionServiceError"
        assert "对象存储" not in str(captured.value)
    finally:
        await database.dispose()
