"""新增异步受理，只保存原文与任务事实，不在上传请求内运行模型。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from uuid import uuid4

from sqlalchemy import select

from enterprise_rag.db.models import DocumentModel, TenantModel
from enterprise_rag.ingestion.service import _document_id, _version_id
from enterprise_rag.security.authorization import AuthorizationService, ResourceNotFound
from enterprise_rag.storage.streaming import persist_upload


class AsyncIngestionService:
    """tenant/actor 只取已验证身份，API 不能选择模型或对象 locator。"""

    def __init__(self, ledger, storage, profile, *, spool_root: Path, max_bytes=104857600):
        """受理上限默认 100 MiB，仅影响新入口，保留旧同步 API 语义。"""
        self.ledger, self.storage, self.profile = ledger, storage, profile
        self.spool_root, self.max_bytes = spool_root, max_bytes

    async def accept(self, context, upload, *, source_id, title, idempotency_key):
        """先角色/租户门禁，再流式落对象，最后事务创建任务/outbox。"""
        AuthorizationService().require_any_role(context, {"editor", "admin"})
        if not source_id.strip() or len(source_id) > 512 or not title.strip() or len(title) > 500:
            raise ValueError("source_id/title 非法")
        filename = Path(upload.filename or "").name
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix not in {".txt", ".md", ".markdown", ".html", ".htm", ".pdf"}:
            raise ValueError("文档格式不支持")
        async with self.ledger.database.session() as session:
            tenant = await session.scalar(select(TenantModel).where(TenantModel.id == context.tenant_id))
            if tenant is None or not tenant.is_active:
                raise ResourceNotFound("资源不存在")
            policy_version = tenant.policy_version
            document_id = _document_id(context.tenant_id, source_id)
            document = await session.get(DocumentModel, (context.tenant_id, document_id))
            if (
                document is not None
                and not context.has_role("admin")
                and not await AuthorizationService()._has_read_acl(session, context, document_id)
            ):
                raise ResourceNotFound("资源不存在")
        tenant_hash = hashlib.sha256(context.tenant_id.encode()).hexdigest()
        key = f"async-raw/{tenant_hash}/{uuid4().hex}{suffix}"
        content_hash, _ = await persist_upload(
            self.storage, upload, key, root=self.spool_root, max_bytes=self.max_bytes,
        )
        document_id = _document_id(context.tenant_id, source_id)
        metadata = {
            "filename": filename, "source_id": source_id, "title": title,
            "content_hash": content_hash, "document_id": document_id,
            "version_id": _version_id(document_id, content_hash), "profile": self.profile.key,
            "policy_version": str(policy_version), "roles": ",".join(sorted(context.roles)),
        }
        fingerprint = hashlib.sha256(json.dumps(
            {name: value for name, value in metadata.items() if name not in {"roles", "policy_version"}},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        # 对象落盘后数据库失败可能留孤儿，但不删除赢家原文；需后续审计回收。
        result = await self.ledger.submit(
            context, idempotency_key=idempotency_key, fingerprint=fingerprint,
            raw_object_key=key, metadata=metadata,
        )
        # 幂等命中只回收本次随机原文副本，不删除已提交任务的赢家对象。
        async with self.ledger.database.session() as session:
            from enterprise_rag.db.async_ingestion import AsyncTaskModel

            task = await session.get(AsyncTaskModel, (context.tenant_id, result.task_id))
            retained_key = task.raw_object_key
        if retained_key != key:
            await self.storage.delete(key)
        return result

    async def get(self, context, task_id):
        """从 PG 长期事实查询，backend 结果过期不影响状态。"""
        return await self.ledger.get(context, task_id)

    async def replay(self, context, task_id):
        """人工 replay 不隐式清空旧执行记录。"""
        return await self.ledger.replay(context, task_id)
