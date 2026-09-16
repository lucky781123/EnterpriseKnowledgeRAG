"""文档版本发布服务；事务由调用方的 Database.transaction 持有。

该服务只原子更新 PostgreSQL 事实源。对象、向量和稀疏索引由后续可重放任务协调，
此处只检查它们在数据库中已达到 ready 状态，不尝试制造跨系统事务。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.db.models import (
    ArtifactKind,
    ArtifactStatus,
    DenseGenerationModel,
    DenseGenerationPointerModel,
    DocumentModel,
    DocumentStatus,
    DocumentVersionModel,
    DenseGenerationStatus,
    VersionArtifactModel,
    VersionStatus,
    TenantDenseCollectionModel,
)
from enterprise_rag.retrieval.contracts import GenerationScope, IndexProfile, collection_name_for_tenant


class PublishPreconditionError(ValueError):
    """目标文档、版本状态或发布产物不满足发布前置条件。"""


class DenseGenerationPublishError(ValueError):
    """Dense generation 未满足构建完整性或发布指针前置条件。"""


async def create_generation(
    session: AsyncSession,
    scope: GenerationScope,
    profile: IndexProfile,
    *,
    manifest_hash: str,
    expected_item_count: int,
    build_attempt: int,
    worker_lease: str,
) -> DenseGenerationModel:
    """创建一个 BUILDING generation，并确保租户 collection 注册事实存在。"""

    if not manifest_hash.strip():
        raise DenseGenerationPublishError("manifest_hash 不能为空")
    if expected_item_count < 0:
        raise DenseGenerationPublishError("expected_item_count 不能为负数")
    if build_attempt <= 0:
        raise DenseGenerationPublishError("build_attempt 必须大于 0")
    if not worker_lease.strip():
        raise DenseGenerationPublishError("worker_lease 不能为空")

    # collection 名称只由可信的租户映射函数产生，避免把原始租户标识直接拼到外部资源名。
    collection_name = collection_name_for_tenant(scope.tenant_id)
    collection = await session.scalar(
        select(TenantDenseCollectionModel)
        .where(TenantDenseCollectionModel.tenant_id == scope.tenant_id)
        .with_for_update()
    )
    if collection is None:
        collection = TenantDenseCollectionModel(
            tenant_id=scope.tenant_id,
            collection_name=collection_name,
            schema_version=profile.schema_version,
            status="pending",
        )
        session.add(collection)
        # generation 通过复合外键引用 registry；没有 ORM relationship 时显式刷入父行，
        # 让 SQLite 和 PostgreSQL 都在写 generation 前看到 collection 事实。
        await session.flush()
    elif collection.collection_name != collection_name:
        raise DenseGenerationPublishError("租户 collection 映射不一致")

    existing = await session.scalar(
        select(DenseGenerationModel)
        .where(
            DenseGenerationModel.tenant_id == scope.tenant_id,
            DenseGenerationModel.document_id == scope.document_id,
            DenseGenerationModel.version_id == scope.version_id,
            DenseGenerationModel.index_profile_key == scope.index_profile_key,
            DenseGenerationModel.status == DenseGenerationStatus.BUILDING,
        )
        .with_for_update()
    )
    if existing is not None:
        raise DenseGenerationPublishError("同一 generation scope 已存在 BUILDING generation")

    generation = DenseGenerationModel(
        generation_id=f"dense_{uuid4().hex}",
        tenant_id=scope.tenant_id,
        document_id=scope.document_id,
        version_id=scope.version_id,
        index_profile_key=scope.index_profile_key,
        collection_name=collection_name,
        manifest_hash=manifest_hash,
        model_name=profile.model_name,
        dimension=profile.dimension,
        chunking_version=profile.chunking_version,
        schema_version=profile.schema_version,
        expected_item_count=expected_item_count,
        actual_item_count=0,
        checkpoint=0,
        build_attempt=build_attempt,
        worker_lease=worker_lease,
        status=DenseGenerationStatus.BUILDING,
    )
    session.add(generation)
    await session.flush()
    return generation


async def mark_generation_ready(
    session: AsyncSession, generation_id: str, actual_item_count: int
) -> DenseGenerationModel:
    """在实际条数与预期一致后，把 BUILDING generation 标记为 READY。"""

    generation = await session.scalar(
        select(DenseGenerationModel)
        .where(DenseGenerationModel.generation_id == generation_id)
        .with_for_update()
    )
    if generation is None:
        raise DenseGenerationPublishError("generation 不存在")
    if generation.status is not DenseGenerationStatus.BUILDING:
        raise DenseGenerationPublishError(
            f"generation 状态不可完成: {generation.status.name}"
        )
    if actual_item_count < 0 or actual_item_count != generation.expected_item_count:
        raise DenseGenerationPublishError("generation 实际条数与预期不一致")

    generation.actual_item_count = actual_item_count
    generation.checkpoint = actual_item_count
    generation.status = DenseGenerationStatus.READY
    await session.flush()
    return generation


async def publish_dense_generation(
    session: AsyncSession, generation_id: str, *, now: datetime | None = None
) -> DenseGenerationPointerModel:
    """在同一 PostgreSQL 事务中把完整 READY generation 切换为 active pointer。"""

    now = now or datetime.now(UTC)
    generation = await session.scalar(
        select(DenseGenerationModel)
        .where(DenseGenerationModel.generation_id == generation_id)
        .with_for_update()
    )
    if generation is None:
        raise DenseGenerationPublishError("generation 不存在")
    if generation.status is not DenseGenerationStatus.READY:
        raise DenseGenerationPublishError(
            f"generation 状态不可发布: {generation.status.name}"
        )
    if generation.actual_item_count != generation.expected_item_count:
        raise DenseGenerationPublishError("generation 尚未通过条数完整性检查")

    version = await session.scalar(
        select(DocumentVersionModel)
        .where(
            DocumentVersionModel.tenant_id == generation.tenant_id,
            DocumentVersionModel.document_id == generation.document_id,
            DocumentVersionModel.id == generation.version_id,
        )
        .with_for_update()
    )
    if version is None:
        raise DenseGenerationPublishError("generation 对应 version 不存在")
    if version.status not in {VersionStatus.READY, VersionStatus.PUBLISHED}:
        raise DenseGenerationPublishError(f"version 状态不可发布: {version.status.value}")

    pointer_key = (
        generation.tenant_id,
        generation.document_id,
        generation.version_id,
        generation.index_profile_key,
    )
    pointer = await session.scalar(
        select(DenseGenerationPointerModel)
        .where(
            DenseGenerationPointerModel.tenant_id == pointer_key[0],
            DenseGenerationPointerModel.document_id == pointer_key[1],
            DenseGenerationPointerModel.version_id == pointer_key[2],
            DenseGenerationPointerModel.index_profile_key == pointer_key[3],
        )
        .with_for_update()
    )
    previous_generation_id = pointer.active_generation_id if pointer else None
    if previous_generation_id == generation.generation_id:
        return pointer

    # 旧 active 只进入保留态，不删除外部 collection，保证失败回滚仍可读。
    if previous_generation_id is not None:
        previous = await session.scalar(
            select(DenseGenerationModel)
            .where(DenseGenerationModel.generation_id == previous_generation_id)
            .with_for_update()
        )
        if previous is not None and previous.status is DenseGenerationStatus.ACTIVE:
            previous.status = DenseGenerationStatus.RETIRED
            previous.retired_at = now

    generation.status = DenseGenerationStatus.ACTIVE
    generation.published_at = now
    if pointer is None:
        pointer = DenseGenerationPointerModel(
            tenant_id=pointer_key[0],
            document_id=pointer_key[1],
            version_id=pointer_key[2],
            index_profile_key=pointer_key[3],
            active_generation_id=generation.generation_id,
            previous_generation_id=previous_generation_id,
            updated_at=now,
        )
        session.add(pointer)
    else:
        pointer.previous_generation_id = previous_generation_id
        pointer.active_generation_id = generation.generation_id
        pointer.updated_at = now
    await session.flush()
    return pointer


async def rollback_dense_generation(
    session: AsyncSession,
    pointer_scope: GenerationScope,
    target_generation_id: str,
    *,
    now: datetime | None = None,
) -> DenseGenerationPointerModel:
    """只切换指定 scope 的 PostgreSQL pointer，不重新写入或删除 Weaviate 对象。"""

    now = now or datetime.now(UTC)
    pointer = await session.scalar(
        select(DenseGenerationPointerModel)
        .where(
            DenseGenerationPointerModel.tenant_id == pointer_scope.tenant_id,
            DenseGenerationPointerModel.document_id == pointer_scope.document_id,
            DenseGenerationPointerModel.version_id == pointer_scope.version_id,
            DenseGenerationPointerModel.index_profile_key == pointer_scope.index_profile_key,
        )
        .with_for_update()
    )
    if pointer is None:
        raise DenseGenerationPublishError("generation pointer 不存在")

    target = await session.scalar(
        select(DenseGenerationModel)
        .where(DenseGenerationModel.generation_id == target_generation_id)
        .with_for_update()
    )
    if target is None:
        raise DenseGenerationPublishError("回滚目标 generation 不存在")
    target_scope = (target.tenant_id, target.document_id, target.version_id, target.index_profile_key)
    if target_scope != (
        pointer_scope.tenant_id,
        pointer_scope.document_id,
        pointer_scope.version_id,
        pointer_scope.index_profile_key,
    ):
        raise DenseGenerationPublishError("回滚目标不属于 pointer scope")
    if target.status not in {DenseGenerationStatus.ACTIVE, DenseGenerationStatus.RETIRED}:
        raise DenseGenerationPublishError("回滚目标 generation 不可读")

    current_generation_id = pointer.active_generation_id
    if current_generation_id != target_generation_id:
        pointer.previous_generation_id = current_generation_id
        pointer.active_generation_id = target_generation_id
        pointer.updated_at = now
        await session.flush()
    return pointer


async def publish_version(
    session: AsyncSession,
    *,
    tenant_id: str,
    document_id: str,
    version_id: str,
    now: datetime | None = None,
) -> None:
    """在调用方已开启的事务中原子切换文档可见版本。

    先锁定 document 再锁定目标 version，调用方统一锁顺序可降低 PostgreSQL 死锁风险。
    SQLite 会忽略 FOR UPDATE，因此真实并发发布仍属于待实验范围。
    """

    now = now or datetime.now(UTC)
    await session.flush()
    document = await session.scalar(
        select(DocumentModel)
        .where(DocumentModel.tenant_id == tenant_id, DocumentModel.id == document_id)
        .with_for_update()
    )
    if document is None:
        raise PublishPreconditionError("document 不存在")

    version = await session.scalar(
        select(DocumentVersionModel)
        .where(
            DocumentVersionModel.tenant_id == tenant_id,
            DocumentVersionModel.document_id == document_id,
            DocumentVersionModel.id == version_id,
        )
        .with_for_update()
    )
    if version is None:
        raise PublishPreconditionError("version 不属于目标 document")
    if version.status is not VersionStatus.READY:
        raise PublishPreconditionError(f"version 状态不可发布: {version.status.value}")

    artifacts = (
        await session.scalars(
            select(VersionArtifactModel).where(
                VersionArtifactModel.tenant_id == tenant_id,
                VersionArtifactModel.document_id == document_id,
                VersionArtifactModel.version_id == version_id,
            )
        )
    ).all()
    ready_kinds = {artifact.kind for artifact in artifacts if artifact.status is ArtifactStatus.READY}
    missing = [kind.value for kind in ArtifactKind if kind not in ready_kinds]
    if missing:
        raise PublishPreconditionError(f"发布前置产物未完成: {', '.join(missing)}")

    # 旧版本先转为 superseded，随后同一事务把目标版本和 document 指针一起切换；
    # 任意后续 flush 或 commit 失败都会由 Database.transaction 自动回滚全部字段。
    await session.execute(
        update(DocumentVersionModel)
        .where(
            DocumentVersionModel.document_id == document_id,
            DocumentVersionModel.tenant_id == tenant_id,
            DocumentVersionModel.status == VersionStatus.PUBLISHED,
        )
        .values(status=VersionStatus.SUPERSEDED)
    )
    version.status = VersionStatus.PUBLISHED
    version.published_at = now
    document.current_version_id = version.id
    document.status = DocumentStatus.READY
    document.updated_at = now


__all__ = [
    "DenseGenerationPublishError",
    "PublishPreconditionError",
    "create_generation",
    "mark_generation_ready",
    "publish_dense_generation",
    "publish_version",
    "rollback_dense_generation",
]
