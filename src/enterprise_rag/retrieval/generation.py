"""Weaviate Dense generation 的 PostgreSQL 事实服务。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.db.models import (
    DenseGenerationModel,
    DenseGenerationPointerModel,
    TenantDenseCollectionModel,
)
from enterprise_rag.db.publish import (
    create_generation,
    mark_generation_ready,
)
from enterprise_rag.domain.entities import DenseGenerationStatus, VersionStatus
from enterprise_rag.retrieval.contracts import (
    GenerationScope,
    IndexProfile,
    WeaviateTransport,
    collection_name_for_tenant,
)


class GenerationService:
    """集中管理 collection registry、generation checkpoint 和完整性状态。"""

    async def ensure_tenant_collection(
        self, session: AsyncSession, tenant_id: str, schema_version: str
    ) -> TenantDenseCollectionModel:
        """创建或读取租户 collection 注册事实，不调用外部 Weaviate。"""

        if not tenant_id.strip():
            raise ValueError("tenant_id 不能为空")
        if not schema_version.strip():
            raise ValueError("schema_version 不能为空")
        collection_name = collection_name_for_tenant(tenant_id)
        record = await session.scalar(
            select(TenantDenseCollectionModel)
            .where(TenantDenseCollectionModel.tenant_id == tenant_id)
            .with_for_update()
        )
        if record is None:
            record = TenantDenseCollectionModel(
                tenant_id=tenant_id,
                collection_name=collection_name,
                schema_version=schema_version,
                status="pending",
            )
            session.add(record)
            await session.flush()
            return record
        if record.collection_name != collection_name or record.schema_version != schema_version:
            raise ValueError("租户 collection registry 与 schema 不一致")
        return record

    async def start_generation(
        self,
        session: AsyncSession,
        scope: GenerationScope,
        profile: IndexProfile,
        *,
        manifest_hash: str,
        expected_item_count: int,
        worker_lease: str,
    ) -> DenseGenerationModel:
        """创建一个新的 BUILDING generation，并复用已有数据库状态语义。"""

        return await create_generation(
            session,
            scope,
            profile,
            manifest_hash=manifest_hash,
            expected_item_count=expected_item_count,
            build_attempt=1,
            worker_lease=worker_lease,
        )

    async def get_generation(
        self, session: AsyncSession, generation_id: str
    ) -> DenseGenerationModel:
        """读取 generation 事实；不存在时返回稳定 ValueError。"""

        generation = await session.scalar(
            select(DenseGenerationModel).where(
                DenseGenerationModel.generation_id == generation_id
            )
        )
        if generation is None:
            raise ValueError("generation 不存在")
        return generation

    async def record_checkpoint(
        self, session: AsyncSession, generation_id: str, next_child_index: int
    ) -> DenseGenerationModel:
        """在 BUILDING 状态下单调推进 checkpoint，供失败后续跑。"""

        if next_child_index < 0:
            raise ValueError("checkpoint 不能为负数")
        generation = await session.scalar(
            select(DenseGenerationModel)
            .where(DenseGenerationModel.generation_id == generation_id)
            .with_for_update()
        )
        if generation is None:
            raise ValueError("generation 不存在")
        if getattr(generation.status, "value", generation.status) != "building":
            raise ValueError("只有 BUILDING generation 可以更新 checkpoint")
        if next_child_index < generation.checkpoint or next_child_index > generation.expected_item_count:
            raise ValueError("checkpoint 必须单调且不超过 expected_item_count")
        generation.checkpoint = next_child_index
        await session.flush()
        return generation

    async def validate_generation(
        self,
        session: AsyncSession,
        generation_id: str,
        transport: WeaviateTransport,
    ) -> DenseGenerationModel:
        """检查外部对象数量和 chunk identity，完整后才标记 READY。"""

        generation = await self.get_generation(session, generation_id)
        if getattr(generation.status, "value", generation.status) != "building":
            raise ValueError("只有 BUILDING generation 可以执行完整性校验")
        count = await transport.count_generation(
            generation.collection_name,
            generation_id=generation.generation_id,
            version_id=generation.version_id,
        )
        chunk_ids = await transport.list_chunk_ids(
            generation.collection_name,
            generation_id=generation.generation_id,
            version_id=generation.version_id,
        )
        if count != generation.expected_item_count or len(chunk_ids) != count:
            raise ValueError("generation 完整性检查失败")
        return await mark_generation_ready(session, generation_id, count)


async def backfill_versions(
    session: AsyncSession,
    versions: Sequence[Any],
    *,
    profile: IndexProfile,
    start_generation: Callable[[Any, IndexProfile], Awaitable[Any]],
    build_generation: Callable[[Any, Any], Awaitable[Any]],
) -> list[Any]:
    """只为 READY version 启动回填，完成后不自动更新 active pointer。"""

    results: list[Any] = []
    for version in versions:
        status = getattr(version, "status", None)
        if status is not VersionStatus.READY:
            continue
        generation = await start_generation(version, profile)
        results.append(await build_generation(generation, version))
    return results


async def retire_generation(session: AsyncSession, generation_id: str) -> DenseGenerationModel:
    """把已 active 的 generation 标记为 RETIRED，保留外部对象供回滚。"""

    generation = await session.scalar(
        select(DenseGenerationModel)
        .where(DenseGenerationModel.generation_id == generation_id)
        .with_for_update()
    )
    if generation is None:
        raise ValueError("generation 不存在")
    if generation.status is not DenseGenerationStatus.ACTIVE:
        raise ValueError("只有 ACTIVE generation 可以 retire")
    generation.status = DenseGenerationStatus.RETIRED
    await session.flush()
    return generation


async def cleanup_generation(
    session: AsyncSession,
    generation_id: str,
    transport: WeaviateTransport,
) -> int:
    """删除非 active 的 RETIRED generation 对象；失败由调用方记录并重试。"""

    generation = await session.scalar(
        select(DenseGenerationModel).where(DenseGenerationModel.generation_id == generation_id)
    )
    if generation is None:
        raise ValueError("generation 不存在")
    if generation.status is not DenseGenerationStatus.RETIRED:
        raise ValueError("只有 RETIRED generation 可以清理")
    active_pointer = await session.scalar(
        select(DenseGenerationPointerModel).where(
            DenseGenerationPointerModel.active_generation_id == generation_id
        )
    )
    if active_pointer is not None:
        raise ValueError("active generation 不允许清理")
    return await transport.delete_generation(
        generation.collection_name,
        generation_id=generation.generation_id,
        version_id=generation.version_id,
    )


__all__ = [
    "GenerationService",
    "backfill_versions",
    "cleanup_generation",
    "retire_generation",
]
