"""基于 PostgreSQL 事实的角色与文档级 ACL 授权服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Collection

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.db.models import (
    ACLPermission,
    ACLPrincipalType,
    DocumentACLModel,
    DocumentModel,
    DocumentStatus,
    DocumentVersionModel,
    VersionStatus,
)

from .context import RequestContext, SUPPORTED_ROLES


class AuthorizationDenied(PermissionError):
    """表示身份已验证，但角色能力不足；HTTP 层应映射为稳定 403。"""


class ResourceNotFound(LookupError):
    """统一表示资源不存在或不可见，避免 IDOR 泄露资源真实存在性。"""


@dataclass(frozen=True, slots=True)
class AuthorizedDocument:
    """保存一次授权决策确认过的当前文档版本，不携带正文或索引内部信息。"""

    tenant_id: str
    document_id: str
    version_id: str
    title: str


class AuthorizationService:
    """集中执行角色、tenant、发布状态和文档 ACL 判断。"""

    def require_any_role(
        self,
        context: RequestContext,
        allowed_roles: Collection[str],
    ) -> None:
        """要求当前身份至少具有一个声明角色，不实现隐式角色继承。"""

        normalized_roles = {role.strip() for role in allowed_roles if role.strip()}
        if not normalized_roles:
            raise ValueError("allowed_roles 不能为空")
        if not context.roles.intersection(normalized_roles):
            raise AuthorizationDenied("当前身份没有执行该操作的角色")

    async def require_document_read(
        self,
        session: AsyncSession,
        context: RequestContext,
        document_id: str,
    ) -> AuthorizedDocument:
        """检查当前版本、tenant 和 READ ACL；失败统一隐藏为资源不可见。"""

        if not document_id.strip():
            raise ResourceNotFound("资源不存在")
        statement = self._published_document_statement(context).where(
            DocumentModel.id == document_id
        )
        row = (await session.execute(statement)).first()
        if row is None:
            raise ResourceNotFound("资源不存在")
        document, version = row

        # admin 的读取能力仍被 tenant、当前版本和发布状态约束；它只绕过本 tenant
        # 的文档 ACL，不得读取其他 tenant 或未发布版本。
        if not context.has_role("admin") and not await self._has_read_acl(
            session,
            context,
            document_id,
        ):
            raise ResourceNotFound("资源不存在")
        return AuthorizedDocument(
            tenant_id=context.tenant_id,
            document_id=document.id,
            version_id=version.id,
            title=document.title,
        )

    async def list_authorized_documents(
        self,
        session: AsyncSession,
        context: RequestContext,
    ) -> list[AuthorizedDocument]:
        """列出当前身份可读的已发布当前版本，作为检索 scope 的数据库事实。"""

        rows = (await session.execute(self._published_document_statement(context))).all()
        documents: list[AuthorizedDocument] = []
        for document, version in rows:
            if not context.has_role("admin") and not await self._has_read_acl(
                session,
                context,
                document.id,
            ):
                continue
            documents.append(
                AuthorizedDocument(
                    tenant_id=context.tenant_id,
                    document_id=document.id,
                    version_id=version.id,
                    title=document.title,
                )
            )
        return sorted(documents, key=lambda item: item.document_id)

    async def ensure_actor_read_acl(
        self,
        session: AsyncSession,
        context: RequestContext,
        *,
        document_id: str,
    ) -> None:
        """为上传者幂等创建 USER READ ACL；仅允许作用于上下文 tenant 的文档。"""

        await self._require_tenant_document(session, context.tenant_id, document_id)
        await self._ensure_read_acl(
            session,
            tenant_id=context.tenant_id,
            document_id=document_id,
            principal_type=ACLPrincipalType.USER,
            principal_id=context.actor_id,
        )

    async def grant_read(
        self,
        session: AsyncSession,
        context: RequestContext,
        *,
        document_id: str,
        principal_type: ACLPrincipalType,
        principal_id: str,
    ) -> None:
        """由同 tenant admin 幂等授予 USER 或 ROLE 的 READ ACL。"""

        self.require_any_role(context, {"admin"})
        await self._require_tenant_document(session, context.tenant_id, document_id)
        self._validate_principal(principal_type, principal_id)
        await self._ensure_read_acl(
            session,
            tenant_id=context.tenant_id,
            document_id=document_id,
            principal_type=principal_type,
            principal_id=principal_id.strip(),
        )

    async def revoke_read(
        self,
        session: AsyncSession,
        context: RequestContext,
        *,
        document_id: str,
        principal_type: ACLPrincipalType,
        principal_id: str,
    ) -> bool:
        """由同 tenant admin 撤销 READ ACL，并返回是否实际删除授权事实。"""

        self.require_any_role(context, {"admin"})
        await self._require_tenant_document(session, context.tenant_id, document_id)
        self._validate_principal(principal_type, principal_id)
        result = await session.execute(
            delete(DocumentACLModel).where(
                DocumentACLModel.tenant_id == context.tenant_id,
                DocumentACLModel.document_id == document_id,
                DocumentACLModel.principal_type == principal_type,
                DocumentACLModel.principal_id == principal_id.strip(),
                DocumentACLModel.permission == ACLPermission.READ,
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def _published_document_statement(context: RequestContext):
        """构造只包含当前已发布版本的 tenant-scoped 查询。"""

        return (
            select(DocumentModel, DocumentVersionModel)
            .join(
                DocumentVersionModel,
                and_(
                    DocumentVersionModel.tenant_id == DocumentModel.tenant_id,
                    DocumentVersionModel.document_id == DocumentModel.id,
                    DocumentVersionModel.id == DocumentModel.current_version_id,
                ),
            )
            .where(
                DocumentModel.tenant_id == context.tenant_id,
                DocumentModel.status == DocumentStatus.READY,
                DocumentModel.current_version_id.is_not(None),
                DocumentVersionModel.status == VersionStatus.PUBLISHED,
            )
        )

    async def _has_read_acl(
        self,
        session: AsyncSession,
        context: RequestContext,
        document_id: str,
    ) -> bool:
        """检查 USER 或 ROLE allow ACL；开发 token 当前不携带 group 成员关系。"""

        principal_match = or_(
            and_(
                DocumentACLModel.principal_type == ACLPrincipalType.USER,
                DocumentACLModel.principal_id == context.actor_id,
            ),
            and_(
                DocumentACLModel.principal_type == ACLPrincipalType.ROLE,
                DocumentACLModel.principal_id.in_(sorted(context.roles)),
            ),
        )
        statement = select(DocumentACLModel.tenant_id).where(
            DocumentACLModel.tenant_id == context.tenant_id,
            DocumentACLModel.document_id == document_id,
            DocumentACLModel.permission == ACLPermission.READ,
            principal_match,
        )
        return (await session.execute(statement.limit(1))).scalar_one_or_none() is not None

    @staticmethod
    async def _require_tenant_document(
        session: AsyncSession,
        tenant_id: str,
        document_id: str,
    ) -> None:
        """确认文档属于当前 tenant；不存在和跨 tenant 使用相同错误。"""

        statement = select(DocumentModel.id).where(
            DocumentModel.tenant_id == tenant_id,
            DocumentModel.id == document_id,
        )
        if (await session.execute(statement)).scalar_one_or_none() is None:
            raise ResourceNotFound("资源不存在")

    @staticmethod
    def _validate_principal(
        principal_type: ACLPrincipalType,
        principal_id: str,
    ) -> None:
        """限制本地 P0 只管理 token 能解释的 USER/ROLE 主体。"""

        if principal_type not in {ACLPrincipalType.USER, ACLPrincipalType.ROLE}:
            raise ValueError("本地 P0 只支持 USER 或 ROLE ACL")
        normalized_id = principal_id.strip()
        if not normalized_id:
            raise ValueError("principal_id 不能为空")
        if principal_type == ACLPrincipalType.ROLE and normalized_id not in SUPPORTED_ROLES:
            raise ValueError("ROLE principal_id 不受支持")

    @staticmethod
    async def _ensure_read_acl(
        session: AsyncSession,
        *,
        tenant_id: str,
        document_id: str,
        principal_type: ACLPrincipalType,
        principal_id: str,
    ) -> None:
        """幂等创建 READ ACL；调用方负责提供显式事务边界。"""

        statement = select(DocumentACLModel.tenant_id).where(
            DocumentACLModel.tenant_id == tenant_id,
            DocumentACLModel.document_id == document_id,
            DocumentACLModel.principal_type == principal_type,
            DocumentACLModel.principal_id == principal_id,
            DocumentACLModel.permission == ACLPermission.READ,
        )
        if (await session.execute(statement)).scalar_one_or_none() is not None:
            return
        session.add(
            DocumentACLModel(
                tenant_id=tenant_id,
                document_id=document_id,
                principal_type=principal_type,
                principal_id=principal_id,
                permission=ACLPermission.READ,
            )
        )


__all__ = [
    "AuthorizationDenied",
    "AuthorizationService",
    "AuthorizedDocument",
    "ResourceNotFound",
]
