"""文档版本发布服务；事务由调用方的 Database.transaction 持有。

该服务只原子更新 PostgreSQL 事实源。对象、向量和稀疏索引由后续可重放任务协调，
此处只检查它们在数据库中已达到 ready 状态，不尝试制造跨系统事务。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.db.models import (
    ArtifactKind,
    ArtifactStatus,
    DocumentModel,
    DocumentStatus,
    DocumentVersionModel,
    VersionArtifactModel,
    VersionStatus,
)


class PublishPreconditionError(ValueError):
    """目标文档、版本状态或发布产物不满足发布前置条件。"""


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


__all__ = ["PublishPreconditionError", "publish_version"]
