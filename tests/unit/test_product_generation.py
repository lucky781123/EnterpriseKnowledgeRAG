"""本地 P0 双路 generation 完整性与发布前置条件测试。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import select

from enterprise_rag.db.models import (
    ACLPermission,
    ACLPrincipalType,
    ArtifactKind,
    ArtifactStatus,
    DenseGenerationPointerModel,
    DocumentACLModel,
    DocumentModel,
    DocumentVersionModel,
    TenantModel,
    VersionArtifactModel,
)
from enterprise_rag.db.publish import create_generation, publish_product_generation
from enterprise_rag.db.session import Database
from enterprise_rag.domain.entities import DenseGenerationStatus, VersionStatus
from enterprise_rag.retrieval.contracts import (
    GenerationScope,
    IndexProfile,
    sparse_collection_name_for_tenant,
)
from enterprise_rag.retrieval.generation import GenerationService
from enterprise_rag.retrieval.sparse import SparseProfile


def _dense_profile() -> IndexProfile:
    """返回双路 generation 使用的固定 Dense profile。"""

    return IndexProfile("demo-embedding", 2, "parent-child-v1", "dense-v1")


def _sparse_profile() -> SparseProfile:
    """返回产品默认 ``word + identity`` Sparse profile。"""

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


@dataclass(slots=True)
class _DenseTransport:
    """返回可控 Dense 数量和 chunk identity 的 transport 替身。"""

    chunk_ids: set[str]
    generation_id: str

    async def count_generation(
        self,
        _collection_name: str,
        *,
        generation_id: str,
        version_id: str,
    ) -> int:
        """校验调用方提供稳定 scope 后返回对象数量。"""

        assert generation_id == self.generation_id
        assert version_id == "version-1"
        return len(self.chunk_ids)

    async def list_chunk_ids(
        self,
        _collection_name: str,
        *,
        generation_id: str,
        version_id: str,
    ) -> set[str]:
        """返回当前 Dense generation 的 chunk identity 集合。"""

        assert generation_id == self.generation_id
        assert version_id == "version-1"
        return set(self.chunk_ids)


@dataclass(slots=True)
class _SparseTransport:
    """返回可控 Sparse 数量、identity 和 profile 的 transport 替身。"""

    chunk_ids: set[str]
    observed_profile_key: str
    generation_id: str

    async def inspect_generation(
        self,
        _collection_name: str,
        *,
        tenant_id: str,
        generation_id: str,
        version_id: str,
        sparse_profile_key: str,
    ):
        """返回 Sparse generation 的实际完整性快照。"""

        from enterprise_rag.retrieval.contracts import SparseGenerationSnapshot

        assert tenant_id == "tenant-a"
        assert generation_id == self.generation_id
        assert version_id == "version-1"
        assert sparse_profile_key == _sparse_profile().key
        return SparseGenerationSnapshot(
            tenant_id=tenant_id,
            version_id=version_id,
            generation_id=generation_id,
            sparse_profile_key=self.observed_profile_key,
            item_count=len(self.chunk_ids),
            chunk_ids=frozenset(self.chunk_ids),
        )


async def _seed_product_generation(database: Database):
    """创建 READY version、四类 artifact、上传者 ACL 和 BUILDING generation。"""

    dense_profile = _dense_profile()
    sparse_profile = _sparse_profile()
    scope = GenerationScope("tenant-a", "doc-1", "version-1", dense_profile.key)
    # 摄取服务先提交 tenant/document/version/artifact 事实，索引阶段随后创建 generation；
    # 分两笔事务也避免 SQLite 在缺少 ORM relationship 时自行猜测跨表插入顺序。
    async with database.transaction() as session:
        session.add_all(
            [
                TenantModel(id="tenant-a", name="租户 A"),
                DocumentModel(
                    tenant_id="tenant-a",
                    id="doc-1",
                    source_id="upload:doc-1",
                    title="产品文档",
                ),
                DocumentVersionModel(
                    tenant_id="tenant-a",
                    document_id="doc-1",
                    id="version-1",
                    content_hash="hash-1",
                    status=VersionStatus.READY,
                ),
            ]
        )
        session.add_all(
            [
                VersionArtifactModel(
                    tenant_id="tenant-a",
                    document_id="doc-1",
                    version_id="version-1",
                    kind=kind,
                    status=(
                        ArtifactStatus.READY
                        if kind in {ArtifactKind.RAW, ArtifactKind.PARSED}
                        else ArtifactStatus.PENDING
                    ),
                    item_count=2 if kind is ArtifactKind.PARSED else 0,
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
            scope,
            dense_profile,
            manifest_hash="manifest-1",
            expected_item_count=2,
            build_attempt=1,
            worker_lease="local-request-1",
            sparse_profile=sparse_profile,
        )
    return generation


def test_sparse_collection_name_is_stable_and_does_not_disclose_tenant() -> None:
    """Sparse collection 必须稳定、合法，并且不能直接暴露原始 tenant ID。"""

    first = sparse_collection_name_for_tenant("tenant/a")
    second = sparse_collection_name_for_tenant("tenant/a")

    assert first == second
    assert first.startswith("SparseTenant_")
    assert "tenant" not in first.removeprefix("SparseTenant_").lower()
    assert "/" not in first


@pytest.mark.asyncio
async def test_product_generation_marks_both_artifacts_ready_only_after_identity_match(
    tmp_path,
) -> None:
    """Dense/Sparse 数量和 identity 全部一致后才允许进入 READY。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'product-generation.db'}")
    try:
        await database.migrate()
        generation = await _seed_product_generation(database)

        async with database.transaction() as session:
            generation = await GenerationService().validate_product_generation(
                session,
                generation.generation_id,
                dense_transport=_DenseTransport(
                    {"child-1", "child-2"}, generation.generation_id
                ),
                sparse_transport=_SparseTransport(
                    {"child-1", "child-2"},
                    _sparse_profile().key,
                    generation.generation_id,
                ),
                sparse_profile=_sparse_profile(),
            )

        async with database.session() as session:
            artifacts = (
                await session.scalars(
                    select(VersionArtifactModel).where(
                        VersionArtifactModel.version_id == "version-1"
                    )
                )
            ).all()

        statuses = {artifact.kind: artifact.status for artifact in artifacts}
        assert generation.status is DenseGenerationStatus.READY
        assert generation.actual_item_count == 2
        assert statuses[ArtifactKind.EMBEDDING] is ArtifactStatus.READY
        assert statuses[ArtifactKind.SPARSE] is ArtifactStatus.READY
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sparse_ids", "observed_profile", "message"),
    [
        ({"child-1", "child-3"}, _sparse_profile().key, "chunk identity"),
        ({"child-1", "child-2"}, "unexpected-profile", "Sparse profile"),
    ],
)
async def test_product_generation_rejects_sparse_mismatch_without_ready_artifacts(
    tmp_path,
    sparse_ids: set[str],
    observed_profile: str,
    message: str,
) -> None:
    """Sparse identity 或 profile 不一致时保持 BUILDING 和 PENDING artifact。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'product-generation-reject.db'}")
    try:
        await database.migrate()
        generation = await _seed_product_generation(database)

        with pytest.raises(ValueError, match=message):
            async with database.transaction() as session:
                await GenerationService().validate_product_generation(
                    session,
                    generation.generation_id,
                    dense_transport=_DenseTransport(
                        {"child-1", "child-2"}, generation.generation_id
                    ),
                    sparse_transport=_SparseTransport(
                        sparse_ids,
                        observed_profile,
                        generation.generation_id,
                    ),
                    sparse_profile=_sparse_profile(),
                )

        async with database.session() as session:
            statuses = {
                artifact.kind: artifact.status
                for artifact in (
                    await session.scalars(
                        select(VersionArtifactModel).where(
                            VersionArtifactModel.version_id == "version-1"
                        )
                    )
                ).all()
            }
        assert statuses[ArtifactKind.EMBEDDING] is ArtifactStatus.PENDING
        assert statuses[ArtifactKind.SPARSE] is ArtifactStatus.PENDING
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_product_publish_switches_version_and_generation_in_one_transaction(
    tmp_path,
) -> None:
    """双路 READY 且存在 READ ACL 时才切换文档版本和 generation pointer。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'product-publish.db'}")
    try:
        await database.migrate()
        generation = await _seed_product_generation(database)
        async with database.transaction() as session:
            await GenerationService().validate_product_generation(
                session,
                generation.generation_id,
                dense_transport=_DenseTransport(
                    {"child-1", "child-2"}, generation.generation_id
                ),
                sparse_transport=_SparseTransport(
                    {"child-1", "child-2"},
                    _sparse_profile().key,
                    generation.generation_id,
                ),
                sparse_profile=_sparse_profile(),
            )
            pointer = await publish_product_generation(
                session,
                generation.generation_id,
            )

        async with database.session() as session:
            document = await session.get(DocumentModel, ("tenant-a", "doc-1"))
            version = await session.get(
                DocumentVersionModel,
                ("tenant-a", "doc-1", "version-1"),
            )
            stored_pointer = await session.get(
                DenseGenerationPointerModel,
                (
                    "tenant-a",
                    "doc-1",
                    "version-1",
                    _dense_profile().key,
                ),
            )

        assert document is not None
        assert version is not None
        assert stored_pointer is not None
        assert document.current_version_id == "version-1"
        assert version.status is VersionStatus.PUBLISHED
        assert pointer.active_generation_id == generation.generation_id
        assert stored_pointer.active_generation_id == generation.generation_id
    finally:
        await database.dispose()
