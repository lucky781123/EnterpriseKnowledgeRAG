"""从 PostgreSQL 事实源解析当前身份可检索的 active generation scope。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.db.models import (
    DenseGenerationModel,
    DenseGenerationPointerModel,
    DenseGenerationStatus,
    TenantDenseCollectionModel,
)
from enterprise_rag.retrieval.contracts import IndexProfile
from enterprise_rag.security.authorization import AuthorizationService
from enterprise_rag.security.context import RequestContext


class AuthorizationScopeUnavailable(RuntimeError):
    """授权 scope 无法安全解析；调用方必须 fail closed。"""


@dataclass(frozen=True, slots=True)
class AuthorizedRetrievalScope:
    """一个已通过 tenant、ACL、版本和 active generation 校验的检索范围。"""

    tenant_id: str
    document_id: str
    version_id: str
    generation_id: str
    dense_collection_name: str
    sparse_collection_name: str
    sparse_profile_key: str


class AuthorizationScopeService:
    """把授权文档解析成 Dense/Sparse 共用的最小检索 scope。"""

    def __init__(self, authorization_service: AuthorizationService | None = None) -> None:
        """保存无状态 ACL 服务，便于 API 和单测注入不同授权实现。"""

        self.authorization_service = authorization_service or AuthorizationService()

    async def list_scopes(
        self,
        session: AsyncSession,
        context: RequestContext,
        *,
        index_profile: IndexProfile,
    ) -> list[AuthorizedRetrievalScope]:
        """列出当前身份可读且同时拥有 active Dense/Sparse generation 的文档。"""

        documents = await self.authorization_service.list_authorized_documents(session, context)
        scopes: list[AuthorizedRetrievalScope] = []
        for document in documents:
            pointer = await session.scalar(
                select(DenseGenerationPointerModel).where(
                    DenseGenerationPointerModel.tenant_id == context.tenant_id,
                    DenseGenerationPointerModel.document_id == document.document_id,
                    DenseGenerationPointerModel.version_id == document.version_id,
                    DenseGenerationPointerModel.index_profile_key == index_profile.key,
                )
            )
            if pointer is None or pointer.active_generation_id is None:
                continue
            generation = await session.scalar(
                select(DenseGenerationModel).where(
                    DenseGenerationModel.generation_id == pointer.active_generation_id,
                    DenseGenerationModel.tenant_id == context.tenant_id,
                    DenseGenerationModel.document_id == document.document_id,
                    DenseGenerationModel.version_id == document.version_id,
                    DenseGenerationModel.index_profile_key == index_profile.key,
                )
            )
            if generation is None or generation.status is not DenseGenerationStatus.ACTIVE:
                continue
            if not generation.sparse_collection_name or not generation.sparse_profile_key:
                continue
            collection = await session.scalar(
                select(TenantDenseCollectionModel).where(
                    TenantDenseCollectionModel.tenant_id == context.tenant_id,
                    TenantDenseCollectionModel.collection_name == generation.collection_name,
                )
            )
            if collection is None:
                continue
            scopes.append(
                AuthorizedRetrievalScope(
                    tenant_id=context.tenant_id,
                    document_id=document.document_id,
                    version_id=document.version_id,
                    generation_id=generation.generation_id,
                    dense_collection_name=generation.collection_name,
                    sparse_collection_name=generation.sparse_collection_name,
                    sparse_profile_key=generation.sparse_profile_key,
                )
            )
        return scopes


__all__ = [
    "AuthorizationScopeService",
    "AuthorizationScopeUnavailable",
    "AuthorizedRetrievalScope",
]
