"""PostgreSQL 向量记录读写适配器。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.db.models import (
    DenseGenerationModel,
    DenseGenerationPointerModel,
    DenseGenerationStatus,
    EmbeddingRecordModel,
    TenantDenseCollectionModel,
)
from enterprise_rag.retrieval.contracts import IndexProfile, WeaviateTransport
from enterprise_rag.retrieval.service import EmbeddingRecord, RetrievalResult, _cosine


class PostgresVectorRepository:
    """使用 JSONB/JSON 保存向量，首期在应用层完成 cosine 计算。"""

    async def replace_version(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        document_id: str,
        version_id: str,
        records: Sequence[EmbeddingRecord],
    ) -> int:
        """按显式版本范围替换 child 向量；空清单同样删除旧索引。"""

        # 删除范围来自调用方的显式 scope，而不是 records[0]。因此即使重建结果为空，
        # 也能清除该版本历史遗留的向量记录。
        await session.execute(
            delete(EmbeddingRecordModel).where(
                EmbeddingRecordModel.tenant_id == tenant_id,
                EmbeddingRecordModel.document_id == document_id,
                EmbeddingRecordModel.version_id == version_id,
            )
        )
        if not records:
            return 0

        # 所有待写记录必须属于同一显式 scope，避免一次替换意外混入其他租户或版本。
        if any(
            (record.tenant_id, record.document_id, record.version_id)
            != (tenant_id, document_id, version_id)
            for record in records
        ):
            raise ValueError("向量记录与替换范围不一致")

        session.add_all(
            EmbeddingRecordModel(
                tenant_id=record.tenant_id,
                document_id=record.document_id,
                version_id=record.version_id,
                chunk_id=record.chunk_id,
                parent_id=record.parent_id,
                text=record.text,
                parent_text=record.parent_text,
                heading_path=list(record.heading_path),
                source_locators=list(record.source_locators),
                embedding=list(record.embedding),
                dimension=len(record.embedding),
            )
            for record in records
        )
        return len(records)

    async def search(
        self,
        session: AsyncSession,
        tenant_id: str,
        version_id: str,
        query: Sequence[float],
        top_k: int,
    ) -> list[RetrievalResult]:
        """先在 SQL 中完成租户和版本过滤，再在应用层排序 cosine。"""

        rows = (
            await session.execute(
                select(EmbeddingRecordModel).where(
                    EmbeddingRecordModel.tenant_id == tenant_id,
                    EmbeddingRecordModel.version_id == version_id,
                )
            )
        ).scalars()
        ranked = sorted(
            rows,
            key=lambda row: _cosine(query, row.embedding),
            reverse=True,
        )[:top_k]
        return [
            RetrievalResult(
                chunk_id=row.chunk_id,
                parent_id=row.parent_id,
                version_id=row.version_id,
                document_id=row.document_id,
                # parent_text 与 child 来自同一版本的固化记录；缺失时稳定回退 child。
                text=row.parent_text or row.text,
                heading_path=tuple(row.heading_path),
                source_locators=tuple(row.source_locators),
                score=_cosine(query, row.embedding),
            )
            for row in ranked
        ]


class WeaviateRetrievalUnavailable(RuntimeError):
    """表示 PostgreSQL 没有可读的 active Dense generation。"""


class WeaviateVectorRepository:
    """通过 PostgreSQL active pointer 读取 Weaviate Dense generation。"""

    def __init__(self, transport: WeaviateTransport, index_profile: IndexProfile) -> None:
        """保存外部 transport 和固定 profile；请求方不能覆盖 collection 或 profile。"""

        self.transport = transport
        self.index_profile = index_profile

    async def search(
        self,
        session: AsyncSession,
        tenant_id: str,
        version_id: str,
        query: Sequence[float],
        top_k: int,
    ) -> list[RetrievalResult]:
        """先读取 active pointer，再以 generation/version 双过滤查询 Weaviate。"""

        if top_k <= 0:
            raise ValueError("top_k 必须大于 0")
        pointers = (
            await session.scalars(
                select(DenseGenerationPointerModel).where(
                    DenseGenerationPointerModel.tenant_id == tenant_id,
                    DenseGenerationPointerModel.version_id == version_id,
                    DenseGenerationPointerModel.index_profile_key == self.index_profile.key,
                )
            )
        ).all()
        if len(pointers) != 1 or not pointers[0].active_generation_id:
            raise WeaviateRetrievalUnavailable("active generation 不存在")
        pointer = pointers[0]
        generation = await session.scalar(
            select(DenseGenerationModel).where(
                DenseGenerationModel.generation_id == pointer.active_generation_id,
                DenseGenerationModel.tenant_id == tenant_id,
                DenseGenerationModel.version_id == version_id,
                DenseGenerationModel.index_profile_key == self.index_profile.key,
            )
        )
        if generation is None or generation.status is not DenseGenerationStatus.ACTIVE:
            raise WeaviateRetrievalUnavailable("active generation 不可读")
        collection = await session.scalar(
            select(TenantDenseCollectionModel).where(
                TenantDenseCollectionModel.tenant_id == tenant_id,
                TenantDenseCollectionModel.collection_name == generation.collection_name,
            )
        )
        if collection is None:
            raise WeaviateRetrievalUnavailable("tenant collection 不存在")
        return await self.transport.search(
            collection.collection_name,
            generation_id=generation.generation_id,
            version_id=version_id,
            query=query,
            top_k=top_k,
        )


__all__ = [
    "PostgresVectorRepository",
    "WeaviateRetrievalUnavailable",
    "WeaviateVectorRepository",
]
