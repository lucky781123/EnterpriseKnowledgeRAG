"""PostgreSQL 向量记录读写适配器。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.db.models import EmbeddingRecordModel
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
