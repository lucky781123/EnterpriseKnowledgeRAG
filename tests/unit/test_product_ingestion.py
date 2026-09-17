"""本地 P0 产品上传、双路索引和发布编排测试。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import select

from enterprise_rag.db.models import (
    ArtifactKind,
    ArtifactStatus,
    DenseGenerationModel,
    DenseGenerationPointerModel,
    DocumentACLModel,
    DocumentModel,
    DocumentVersionModel,
    VersionArtifactModel,
)
from enterprise_rag.db.session import Database
from enterprise_rag.domain.entities import DenseGenerationStatus, VersionStatus
from enterprise_rag.ingestion.product_service import (
    ProductIndexingError,
    ProductIngestionService,
)
from enterprise_rag.ingestion.service import IdempotencyConflictError, IngestionService
from enterprise_rag.retrieval.contracts import IndexProfile, WeaviateBatchResult
from enterprise_rag.retrieval.generation import GenerationService
from enterprise_rag.retrieval.indexer import WeaviateDenseIndexer
from enterprise_rag.retrieval.sparse import SparseProfile
from enterprise_rag.security.authorization import AuthorizationDenied, AuthorizationService
from enterprise_rag.security.context import RequestContext
from enterprise_rag.storage.local import LocalObjectStorage


def _context(*roles: str) -> RequestContext:
    """构造固定 tenant 的可信开发身份，便于验证角色和 ACL 边界。"""

    return RequestContext(
        actor_id="user-alice",
        tenant_id="tenant-a",
        roles=frozenset(roles),
        issued_at=1_789_600_000,
        expires_at=1_789_603_600,
        token_version=1,
    )


def _dense_profile() -> IndexProfile:
    """返回测试使用的二维 Dense profile。"""

    return IndexProfile("demo-embedding", 2, "parent-child-v1", "dense-v1")


def _sparse_profile() -> SparseProfile:
    """返回产品默认的 ``word + identity`` Sparse profile。"""

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


class _EmbeddingProvider:
    """记录 Dense embedding 调用并返回固定二维向量。"""

    def __init__(self) -> None:
        """初始化调用计数。"""

        self.calls = 0

    async def embed(self, texts) -> list[list[float]]:
        """为每个 child 返回稳定向量，不访问真实 Ollama。"""

        values = list(texts)
        self.calls += 1
        return [[1.0, 0.0] for _ in values]


class _DenseTransport:
    """以内存字典模拟 Dense collection 的幂等写入和完整性查询。"""

    def __init__(self) -> None:
        """初始化 schema 调用和对象集合。"""

        self.ensure_calls: list[tuple[str, str]] = []
        self.objects: dict[str, object] = {}

    async def ensure_collection(self, collection_name: str, schema_version: str) -> None:
        """记录服务端派生的 collection 和 schema。"""

        self.ensure_calls.append((collection_name, schema_version))

    async def upsert_batch(self, _collection_name: str, objects) -> WeaviateBatchResult:
        """按稳定 object ID 覆盖写入，模拟 Weaviate upsert。"""

        for item in objects:
            self.objects[item.object_id] = item
        return WeaviateBatchResult(accepted=len(objects))

    async def count_generation(
        self,
        _collection_name: str,
        *,
        generation_id: str,
        version_id: str,
    ) -> int:
        """统计指定 generation/version 下的 Dense 对象。"""

        return len(await self._objects_for(generation_id, version_id))

    async def list_chunk_ids(
        self,
        _collection_name: str,
        *,
        generation_id: str,
        version_id: str,
    ) -> set[str]:
        """返回指定 scope 的 Dense chunk identity。"""

        return {
            item.properties["chunk_id"]
            for item in await self._objects_for(generation_id, version_id)
        }

    async def _objects_for(self, generation_id: str, version_id: str) -> list:
        """按可信 scope 筛选对象，避免测试替身掩盖跨 generation 查询。"""

        return [
            item
            for item in self.objects.values()
            if item.properties["generation_id"] == generation_id
            and item.properties["version_id"] == version_id
        ]


@dataclass(slots=True)
class _SparseTransport:
    """以内存对象模拟 Sparse 写入，并可注入单路失败。"""

    fail_upsert: bool = False
    ensure_calls: list[tuple[str, str, str]] | None = None
    objects: dict[str, object] | None = None

    def __post_init__(self) -> None:
        """初始化 schema 调用和对象集合。"""

        self.ensure_calls = []
        self.objects = {}

    async def ensure_collection(
        self,
        collection_name: str,
        schema_version: str,
        *,
        tokenization: str = "word",
    ) -> None:
        """记录固定 tokenizer，证明产品默认没有切换到 ``cjk_bigram``。"""

        self.ensure_calls.append((collection_name, schema_version, tokenization))

    async def upsert_batch(self, _collection_name: str, objects) -> int:
        """成功时幂等写入；失败注入用于验证 generation 不会发布。"""

        if self.fail_upsert:
            raise RuntimeError("sparse unavailable")
        for item in objects:
            self.objects[item.object_id] = item
        return len(objects)

    async def inspect_generation(
        self,
        _collection_name: str,
        *,
        tenant_id: str,
        generation_id: str,
        version_id: str,
        sparse_profile_key: str,
    ):
        """返回完整 scope 下的 Sparse 快照。"""

        from enterprise_rag.retrieval.contracts import SparseGenerationSnapshot

        rows = [
            item
            for item in self.objects.values()
            if item.properties["tenant_id"] == tenant_id
            and item.properties["generation_id"] == generation_id
            and item.properties["version_id"] == version_id
            and item.properties["sparse_profile"] == sparse_profile_key
        ]
        return SparseGenerationSnapshot(
            tenant_id=tenant_id,
            version_id=version_id,
            generation_id=generation_id,
            sparse_profile_key=sparse_profile_key,
            item_count=len(rows),
            chunk_ids=frozenset(item.properties["chunk_id"] for item in rows),
        )


def _service(
    database: Database,
    storage: LocalObjectStorage,
    sparse_transport: _SparseTransport,
) -> tuple[ProductIngestionService, _EmbeddingProvider, _DenseTransport]:
    """装配真实摄取/发布服务与内存 Dense/Sparse transport。"""

    embedding = _EmbeddingProvider()
    dense_transport = _DenseTransport()
    generation_service = GenerationService()
    dense_indexer = WeaviateDenseIndexer(
        embedding,
        dense_transport,
        storage,
        generation_service,
    )
    service = ProductIngestionService(
        database=database,
        storage=storage,
        ingestion_service=IngestionService(database, storage),
        authorization_service=AuthorizationService(),
        generation_service=generation_service,
        dense_indexer=dense_indexer,
        dense_transport=dense_transport,
        sparse_transport=sparse_transport,
        dense_profile=_dense_profile(),
        sparse_profile=_sparse_profile(),
    )
    return service, embedding, dense_transport


@pytest.mark.asyncio
async def test_product_upload_publishes_both_indexes_and_reuses_idempotent_result(tmp_path):
    """editor 上传应完成 ACL、双路完整性和联合发布，重复请求复用 active generation。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'product.db'}")
    await database.migrate()
    storage = LocalObjectStorage(tmp_path / "objects")
    sparse_transport = _SparseTransport()
    service, embedding, dense_transport = _service(database, storage, sparse_transport)
    try:
        first = await service.upload(
            _context("editor"),
            source_id="upload:guide",
            title="部署指南",
            filename="guide.md",
            content=b"# Install\n\nUse deploy_app to publish the service.",
            content_type="text/markdown",
            idempotency_key="upload-1",
        )
        second = await service.upload(
            _context("editor"),
            source_id="upload:guide",
            title="部署指南",
            filename="guide.md",
            content=b"# Install\n\nUse deploy_app to publish the service.",
            content_type="text/markdown",
            idempotency_key="upload-1",
        )

        assert first.published is True
        assert first.generation_status is DenseGenerationStatus.ACTIVE
        assert first.artifact_statuses == {
            ArtifactKind.RAW: ArtifactStatus.READY,
            ArtifactKind.PARSED: ArtifactStatus.READY,
            ArtifactKind.EMBEDDING: ArtifactStatus.READY,
            ArtifactKind.SPARSE: ArtifactStatus.READY,
        }
        assert second == first
        assert embedding.calls == 1
        assert len(dense_transport.objects) == len(sparse_transport.objects) > 0
        assert sparse_transport.ensure_calls[0][2] == "word"

        async with database.session() as session:
            document = await session.get(
                DocumentModel, ("tenant-a", first.document_id)
            )
            version = await session.get(
                DocumentVersionModel,
                ("tenant-a", first.document_id, first.version_id),
            )
            pointer = await session.scalar(
                select(DenseGenerationPointerModel).where(
                    DenseGenerationPointerModel.active_generation_id
                    == first.generation_id
                )
            )
            acl = await session.scalar(
                select(DocumentACLModel).where(
                    DocumentACLModel.tenant_id == "tenant-a",
                    DocumentACLModel.document_id == first.document_id,
                    DocumentACLModel.principal_id == "user-alice",
                )
            )

        assert document is not None and document.current_version_id == first.version_id
        assert version is not None and version.status is VersionStatus.PUBLISHED
        assert pointer is not None
        assert acl is not None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_product_upload_rejects_reader_before_writing(tmp_path):
    """reader 不具备上传能力，拒绝必须发生在 tenant/document 写入之前。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'reader.db'}")
    await database.migrate()
    storage = LocalObjectStorage(tmp_path / "objects")
    service, _, _ = _service(database, storage, _SparseTransport())
    try:
        with pytest.raises(AuthorizationDenied):
            await service.upload(
                _context("reader"),
                source_id="upload:guide",
                title="部署指南",
                filename="guide.md",
                content=b"# Install\n",
                content_type="text/markdown",
                idempotency_key="upload-reader",
            )

        async with database.session() as session:
            assert (await session.scalars(select(DocumentModel))).all() == []
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_product_upload_conflict_and_sparse_failure_never_publish(tmp_path):
    """幂等内容冲突应拒绝；Sparse 单路失败必须留下不可查询 generation。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'failure.db'}")
    await database.migrate()
    storage = LocalObjectStorage(tmp_path / "objects")
    service, _, _ = _service(database, storage, _SparseTransport(fail_upsert=True))
    try:
        with pytest.raises(ProductIndexingError):
            await service.upload(
                _context("admin"),
                source_id="upload:failure",
                title="失败文档",
                filename="failure.md",
                content=b"# Failure\n\nSparse should fail safely.",
                content_type="text/markdown",
                idempotency_key="upload-failure",
            )

        async with database.session() as session:
            generation = await session.scalar(select(DenseGenerationModel))
            pointers = (await session.scalars(select(DenseGenerationPointerModel))).all()
            artifacts = (
                await session.scalars(
                    select(VersionArtifactModel).where(
                        VersionArtifactModel.kind == ArtifactKind.SPARSE
                    )
                )
            ).all()

        assert generation is not None
        assert generation.status is DenseGenerationStatus.FAILED
        assert pointers == []
        assert artifacts and artifacts[0].status is ArtifactStatus.FAILED

        with pytest.raises(IdempotencyConflictError):
            await service.upload(
                _context("admin"),
                source_id="upload:failure",
                title="失败文档",
                filename="failure.md",
                content=b"different content",
                content_type="text/markdown",
                idempotency_key="upload-failure",
            )
    finally:
        await database.dispose()
