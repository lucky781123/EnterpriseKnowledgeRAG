"""真实业务 handlers 的离线闭环；SQLite/内存索引不是服务验收证据。"""

from contextlib import asynccontextmanager
from io import BytesIO

import httpx
import pytest
from fastapi import UploadFile
from sqlalchemy import delete, select

from enterprise_rag.api.app import create_app
from enterprise_rag.config.settings import Settings
from enterprise_rag.db.async_ingestion import AsyncOutboxModel
from enterprise_rag.db.migrations import ASYNC_INGESTION_VERSION, upgrade_database
from enterprise_rag.db.models import (
    DenseGenerationModel,
    DocumentACLModel,
    DocumentModel,
    DocumentVersionModel,
    TenantModel,
)
from enterprise_rag.db.session import Database
from enterprise_rag.infrastructure.write_gate import MaintenanceBlocked
from enterprise_rag.ingestion.async_service import AsyncIngestionService
from enterprise_rag.ingestion.stage_worker import StageWorker
from enterprise_rag.ingestion.task_ledger import TaskLedger
from enterprise_rag.retrieval.contracts import IndexProfile, WeaviateBatchResult
from enterprise_rag.retrieval.generation import GenerationService
from enterprise_rag.retrieval.sparse import SparseProfile
from enterprise_rag.security.context import RequestContext
from enterprise_rag.security.dev_token import DevTokenCodec
from enterprise_rag.storage.local import LocalObjectStorage


def identity(tenant="a", actor="editor"):
    """模拟已认证且仍有效的服务端身份。"""
    return RequestContext(
        tenant_id=tenant, actor_id=actor, roles=frozenset({"editor", "reader"}),
        issued_at=1, expires_at=2**62, token_version=1,
    )


class Embedding:
    """记录模型调用，不伪造真实 Embedding 验收。"""

    def __init__(self):
        """保存计数以验证重复消息不重复嵌入。"""
        self.calls = 0

    async def embed(self, texts):
        """为每个文本生成固定二维向量。"""
        self.calls += 1
        return [[1.0, 0.0] for _ in texts]


class Dense:
    """用内存保存 Dense 产物，仅验证编排。"""

    def __init__(self):
        """记录对象与写入次数。"""
        self.objects = {}

    async def ensure_collection(self, *args, **kwargs):
        """离线替身无需创建物理 collection。"""

    async def upsert_batch(self, collection, objects):
        """模拟稳定 ID 的幂等写入。"""
        self.objects.update({item.object_id: item for item in objects})
        return WeaviateBatchResult(accepted=len(objects))

    async def list_chunk_ids(self, collection, *, generation_id, version_id):
        """按完整 generation/version 过滤。"""
        return {
            obj.properties["chunk_id"] for obj in self.objects.values()
            if obj.properties["generation_id"] == generation_id
            and obj.properties["version_id"] == version_id
        }

    async def count_generation(self, collection, **kwargs):
        """返回同一 scope 的条数。"""
        return len(await self.list_chunk_ids(collection, **kwargs))


class Sparse(Dense):
    """Sparse 复用幂等存储，保留完整性接口。"""

    async def upsert_batch(self, collection, objects):
        """Sparse adapter 返回接受条数。"""
        result = await super().upsert_batch(collection, objects)
        return result.accepted

    async def inspect_generation(self, collection, **kwargs):
        """返回与业务接口一致的完整性快照。"""
        from enterprise_rag.retrieval.contracts import SparseGenerationSnapshot

        ids = await self.list_chunk_ids(
            collection, generation_id=kwargs["generation_id"], version_id=kwargs["version_id"],
        )
        return SparseGenerationSnapshot(
            item_count=len(ids), chunk_ids=frozenset(ids),
            sparse_profile_key=kwargs["sparse_profile_key"],
            tenant_id=kwargs["tenant_id"], generation_id=kwargs["generation_id"],
            version_id=kwargs["version_id"],
        )


@pytest.fixture
async def pipeline(tmp_path):
    """装配临时账本、文件对象与真实分阶段 handler。"""
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    await upgrade_database(database.engine, target_version=ASYNC_INGESTION_VERSION)
    async with database.transaction() as session:
        session.add_all([TenantModel(id="a", name="A"), TenantModel(id="b", name="B")])
    ledger = TaskLedger(database)
    storage = LocalObjectStorage(tmp_path / "objects")
    profile = IndexProfile("test", 2, "parent-child-v1", "dense-v1")
    embedding = Embedding()

    async def resolver(tenant, actor):
        """模拟实时身份重载，不使用消息里的角色。"""
        return identity(tenant, actor)

    service = AsyncIngestionService(ledger, storage, profile, spool_root=tmp_path / "spool")
    worker = StageWorker(
        ledger, storage, embedding, Dense(), Sparse(), profile,
        SparseProfile("word", "identity", "sparse-v1", {
            "body": 1.0, "title": 2.0, "section_title": 2.0, "api_symbols": 3.0,
        }),
        GenerationService(), identity_resolver=resolver,
    )
    try:
        yield service, worker
    finally:
        await database.dispose()


async def accept(service, key="one", data=b"# policy\n\napproval takes three days."):
    """受理公开合成文本，文件不含私人数据。"""
    return await service.accept(
        identity(), UploadFile(filename="sample.md", file=BytesIO(data)),
        source_id="sample", title="Sample", idempotency_key=key,
    )


async def advance(worker, task_id):
    """取 PG 当前阶段事件，不自行生成 stage 参数。"""
    async with worker.ledger.database.session() as session:
        events = (await session.scalars(select(AsyncOutboxModel).where(
            AsyncOutboxModel.task_id == task_id,
        ).order_by(AsyncOutboxModel.created_at))).all()
    return await worker.run_event(events[-1].id)


async def test_real_handlers_publish_and_duplicate_event_does_not_embed(pipeline):
    """离线跑完整六阶段，校验发布、canonical manifest 与 hash lineage。"""
    service, worker = pipeline
    task = await accept(service)
    assert (await accept(service)).task_id == task.task_id
    for _ in range(6):
        result = await advance(worker, task.task_id)
        assert result == "completed", await service.get(identity(), task.task_id)
    assert (await service.get(identity(), task.task_id)).status == "succeeded"
    assert await advance(worker, task.task_id) == "ignored"
    assert worker.embedding.calls == 1
    async with worker.ledger.database.session() as session:
        document = (await session.scalars(select(DocumentModel))).one()
        version = (await session.scalars(select(DocumentVersionModel))).one()
        generation = (await session.scalars(select(DenseGenerationModel))).one()
        assert document.current_version_id == version.id
        assert version.status.value == "published" and generation.status.value == "active"
        from enterprise_rag.ingestion.service import IngestionService

        raw = await worker.storage.get(IngestionService._parsed_key("a", document.id, version.id))
        import hashlib

        assert hashlib.sha256(raw).hexdigest() == generation.manifest_hash


async def test_worker_reloads_acl_before_publish(pipeline):
    """撤权后不能继续发布，也不能借 parse replay 重授权限。"""
    service, worker = pipeline
    task = await accept(service)
    assert await advance(worker, task.task_id) == "completed"
    async with worker.ledger.database.transaction() as session:
        await session.execute(delete(DocumentACLModel))
    assert await advance(worker, task.task_id) == "failed"
    assert (await service.get(identity(), task.task_id)).error_code == "permission_denied"


async def test_async_api_is_registered_and_owner_tenant_hidden(pipeline):
    """验证新增 HTTP 入口的 202、状态查询、越权隐藏和原同步入口保留。"""
    service, _ = pipeline
    secret = "test-secret-at-least-32-characters-long"
    codec = DevTokenCodec(secret, ttl_seconds=3600)
    app = create_app(
        settings=Settings.from_env(),
        database=service.ledger.database, dev_token_codec=codec,
        async_ingestion_service=service,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        headers = {"Authorization": f"Bearer {codec.issue(tenant_id='a', actor_id='editor', roles={'editor', 'reader'})}"}
        response = await client.post(
            "/api/v1/ingestion/tasks", headers=headers,
            files={"file": ("sample.md", b"# public\n\nhello")},
            data={"source_id": "sample", "title": "Sample", "idempotency_key": "api"},
        )
        assert response.status_code == 202
        task_id = response.json()["task_id"]
        assert (await client.get(f"/api/v1/ingestion/tasks/{task_id}", headers=headers)).status_code == 200
        for tenant, actor in (("b", "editor"), ("a", "other")):
            other = {"Authorization": f"Bearer {codec.issue(tenant_id=tenant, actor_id=actor, roles={'editor'})}"}
            assert (await client.get(f"/api/v1/ingestion/tasks/{task_id}", headers=other)).status_code == 404
        assert "/api/v1/documents" in app.openapi()["paths"]


@pytest.mark.parametrize("blocked", ["size", "maintenance"])
async def test_async_admission_rejects_before_task_creation(pipeline, blocked):
    """超限和维护期请求不能创建任务或推进阶段事实。"""
    service, _ = pipeline
    service.max_bytes = 16

    class Gate:
        """只模拟维护拒绝，不冒充真实 PG advisory lock。"""

        @asynccontextmanager
        async def writer(self):
            """拒绝在维护期进入受理逻辑。"""
            raise MaintenanceBlocked("maintenance")
            yield  # pragma: no cover

    codec = DevTokenCodec("boundary-test-secret-at-least-32bytes", ttl_seconds=3600)
    application = create_app(
        settings=Settings.from_env(), database=service.ledger.database,
        dev_token_codec=codec, async_ingestion_service=service,
        write_gate=Gate() if blocked == "maintenance" else None,
    )
    token = codec.issue(tenant_id="a", actor_id="editor", roles={"editor"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/ingestion/tasks", headers={"Authorization": f"Bearer {token}"},
            files={"file": ("sample.txt", b"x" * 70000)},
            data={"source_id": "boundary", "title": "Boundary", "idempotency_key": blocked},
        )
    assert response.status_code == (503 if blocked == "maintenance" else 413)
    async with service.ledger.database.session() as session:
        assert not (await session.scalars(select(AsyncOutboxModel))).all()


async def test_async_api_replay_only_dead_letter_and_keeps_history(pipeline):
    """业务解析失败后支持显式 HTTP replay，queued 状态禁止再次重放。"""
    service, worker = pipeline
    task = await accept(service, data=b"\xff\xfe")
    assert await advance(worker, task.task_id) == "failed"
    assert (await service.get(identity(), task.task_id)).status == "dead_letter"
    codec = DevTokenCodec("replay-test-secret-at-least-32bytes", ttl_seconds=3600)
    application = create_app(
        settings=Settings.from_env(), database=service.ledger.database,
        dev_token_codec=codec, async_ingestion_service=service,
    )
    token = codec.issue(tenant_id="a", actor_id="editor", roles={"editor"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        first = await client.post(
            f"/api/v1/ingestion/tasks/{task.task_id}/replay",
            headers={"Authorization": f"Bearer {token}"},
        )
        second = await client.post(
            f"/api/v1/ingestion/tasks/{task.task_id}/replay",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert first.status_code == 202 and first.json()["execution"] == 2
    assert second.status_code == 409
