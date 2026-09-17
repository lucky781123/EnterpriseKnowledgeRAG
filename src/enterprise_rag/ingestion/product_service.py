"""本地 P0 产品上传、双路索引和发布编排。

本模块只负责把已经存在的摄取、generation、Dense、Sparse 和 ACL 组件串成同步
产品流程。PostgreSQL 仍是发布事实源，Weaviate 写入失败时只保留不可查询的
generation，不尝试伪造跨系统原子事务。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select

from enterprise_rag.db.models import (
    ArtifactKind,
    ArtifactStatus,
    DenseGenerationModel,
    DenseGenerationPointerModel,
    DocumentVersionModel,
    VersionArtifactModel,
)
from enterprise_rag.db.publish import publish_product_generation
from enterprise_rag.db.session import Database
from enterprise_rag.domain.entities import (
    DenseGenerationStatus,
    JobStatus,
    VersionStatus,
)
from enterprise_rag.ingestion.contracts import IngestionRequest, IngestionResult
from enterprise_rag.ingestion.service import IdempotencyConflictError, IngestionService
from enterprise_rag.retrieval.contracts import GenerationScope, IndexProfile
from enterprise_rag.retrieval.generation import GenerationService
from enterprise_rag.retrieval.indexer import WeaviateDenseIndexer
from enterprise_rag.retrieval.sparse import SparseFieldProjector, SparseProfile
from enterprise_rag.security.authorization import AuthorizationService
from enterprise_rag.security.context import RequestContext
from enterprise_rag.storage.protocols import ObjectStorage


class ProductIndexingError(RuntimeError):
    """产品索引或发布阶段失败，错误消息不包含上游正文或凭据。"""


@dataclass(frozen=True, slots=True)
class ProductIngestionResult:
    """同步上传返回的安全状态摘要，不携带文档正文或对象存储 key。"""

    document_id: str
    version_id: str
    job_id: str
    generation_id: str
    job_status: JobStatus
    version_status: VersionStatus
    generation_status: DenseGenerationStatus
    artifact_statuses: dict[ArtifactKind, ArtifactStatus]
    published: bool


class ProductIngestionService:
    """编排 token 已验证后的同步摄取、双路索引和 PostgreSQL 发布。"""

    def __init__(
        self,
        *,
        database: Database,
        storage: ObjectStorage,
        ingestion_service: IngestionService,
        authorization_service: AuthorizationService,
        generation_service: GenerationService,
        dense_indexer: WeaviateDenseIndexer,
        dense_transport,
        sparse_transport,
        dense_profile: IndexProfile,
        sparse_profile: SparseProfile,
        sparse_batch_size: int = 64,
    ) -> None:
        """保存已有组件和固定产品 profile，拒绝请求方动态改变索引策略。"""

        if sparse_batch_size <= 0:
            raise ValueError("sparse_batch_size 必须大于 0")
        self.database = database
        self.storage = storage
        self.ingestion_service = ingestion_service
        self.authorization_service = authorization_service
        self.generation_service = generation_service
        self.dense_indexer = dense_indexer
        self.dense_transport = dense_transport
        self.sparse_transport = sparse_transport
        self.dense_profile = dense_profile
        self.sparse_profile = sparse_profile
        self.sparse_batch_size = sparse_batch_size
        self.sparse_projector = SparseFieldProjector(sparse_profile)

    async def upload(
        self,
        context: RequestContext,
        *,
        source_id: str,
        title: str,
        filename: str,
        content: bytes,
        content_type: str | None,
        idempotency_key: str,
        source_revision: str | None = None,
    ) -> ProductIngestionResult:
        """执行一次同步产品上传，并在双路完整后发布当前版本。"""

        # 角色校验必须先于对象存储和数据库写入，reader 不能制造任何业务事实。
        self.authorization_service.require_any_role(context, {"editor", "admin"})
        ingestion = await self.ingestion_service.ingest(
            IngestionRequest(
                tenant_id=context.tenant_id,
                source_id=source_id,
                title=title,
                filename=filename,
                content=content,
                content_type=content_type,
                idempotency_key=idempotency_key,
                source_revision=source_revision,
            )
        )
        if ingestion.status is JobStatus.FAILED or ingestion.version_id is None:
            raise ProductIndexingError("同步摄取失败")

        # 幂等重放优先读取已经 active 的产品事实，避免再次调用 embedding 或外部索引。
        existing = await self._existing_published_result(ingestion)
        if existing is not None:
            return existing
        if ingestion.parsed_object_key is None:
            raise ProductIndexingError("摄取结果缺少 parsed manifest")

        raw_manifest = await self.storage.get(ingestion.parsed_object_key)
        payload = self._load_manifest(raw_manifest)
        children = payload["chunks"]["children"]
        manifest_hash = hashlib.sha256(raw_manifest).hexdigest()

        generation_id: str | None = None
        failure_artifact_kind: ArtifactKind | None = ArtifactKind.EMBEDDING
        try:
            # ACL 和 generation 事实先进入 PostgreSQL，之后的外部写入始终挂在同一 scope。
            async with self.database.transaction() as session:
                await self.authorization_service.ensure_actor_read_acl(
                    session,
                    context,
                    document_id=ingestion.document_id,
                )
                generation = await self.generation_service.start_generation(
                    session,
                    GenerationScope(
                        context.tenant_id,
                        ingestion.document_id,
                        ingestion.version_id,
                        self.dense_profile.key,
                    ),
                    self.dense_profile,
                    manifest_hash=manifest_hash,
                    expected_item_count=len(children),
                    worker_lease=f"product:{uuid4().hex}",
                    sparse_profile=self.sparse_profile,
                )
                generation_id = generation.generation_id

            # Dense collection 和 Sparse collection 都由服务端固定 schema/profile 派生。
            await self.dense_transport.ensure_collection(
                generation.collection_name,
                self.dense_profile.schema_version,
            )
            await self._ensure_sparse_collection(generation.sparse_collection_name)
            failure_artifact_kind = ArtifactKind.SPARSE

            # Dense build 延迟 finalize，等待 Sparse 完成后统一进行双路完整性校验。
            async with self.database.transaction() as session:
                await self.dense_indexer.build_generation(
                    session,
                    generation_id=generation.generation_id,
                    tenant_id=context.tenant_id,
                    document_id=ingestion.document_id,
                    version_id=ingestion.version_id,
                    profile=self.dense_profile,
                    parsed_object_key=ingestion.parsed_object_key,
                    finalize=False,
                )

            await self._build_sparse(
                generation.sparse_collection_name,
                payload,
                context=context,
                document_id=ingestion.document_id,
                version_id=ingestion.version_id,
                generation_id=generation.generation_id,
            )

            # 只有 Dense/Sparse count、scope、profile 和 chunk identity 全部一致才 READY。
            async with self.database.transaction() as session:
                await self.generation_service.validate_product_generation(
                    session,
                    generation.generation_id,
                    dense_transport=self.dense_transport,
                    sparse_transport=self.sparse_transport,
                    sparse_profile=self.sparse_profile,
                )

            # 发布事务只更新 PostgreSQL 事实源；失败时旧 active pointer 保持不变。
            async with self.database.transaction() as session:
                await publish_product_generation(session, generation.generation_id)
            failure_artifact_kind = None
        except Exception as exc:
            if generation_id is not None:
                await self._mark_generation_failed(
                    generation_id,
                    ingestion,
                    artifact_kind=failure_artifact_kind,
                    error=exc,
                )
            raise ProductIndexingError("同步索引发布失败") from exc

        result = await self._result_for_generation(
            ingestion,
            generation_id,
            expected_status=DenseGenerationStatus.ACTIVE,
        )
        if result is None:
            raise ProductIndexingError("发布后无法读取产品事实")
        return result

    @staticmethod
    def _load_manifest(raw_manifest: bytes) -> dict:
        """读取并验证 parsed manifest 的最小产品字段。"""

        try:
            payload = json.loads(raw_manifest.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProductIndexingError("parsed manifest 格式非法") from exc
        chunks = payload.get("chunks") if isinstance(payload, dict) else None
        children = chunks.get("children") if isinstance(chunks, dict) else None
        if not isinstance(children, list) or any(not isinstance(item, dict) for item in children):
            raise ProductIndexingError("parsed manifest 缺少有效 child chunks")
        return payload

    async def _ensure_sparse_collection(self, collection_name: str | None) -> None:
        """创建固定 Sparse collection；缺少能力时明确失败而不静默降级。"""

        if not collection_name:
            raise ProductIndexingError("generation 缺少 Sparse collection")
        ensure_collection = getattr(self.sparse_transport, "ensure_collection", None)
        if ensure_collection is None:
            raise ProductIndexingError("Sparse transport 不支持 collection 初始化")
        await ensure_collection(
            collection_name,
            self.sparse_profile.schema_version,
            tokenization=self.sparse_profile.tokenizer_version,
        )

    async def _build_sparse(
        self,
        collection_name: str | None,
        payload: dict,
        *,
        context: RequestContext,
        document_id: str,
        version_id: str,
        generation_id: str,
    ) -> None:
        """投影并幂等写入 Sparse child 对象，校验每批 accepted 数量。"""

        if not collection_name:
            raise ProductIndexingError("generation 缺少 Sparse collection")
        objects = self.sparse_projector.project_manifest(
            payload,
            generation_id=generation_id,
            tenant_id=context.tenant_id,
            document_id=document_id,
            version_id=version_id,
        )
        for start in range(0, len(objects), self.sparse_batch_size):
            batch = objects[start : start + self.sparse_batch_size]
            accepted = await self.sparse_transport.upsert_batch(collection_name, batch)
            accepted_count = getattr(accepted, "accepted", accepted)
            if accepted_count != len(batch):
                raise ProductIndexingError("Sparse 批量写入数量不一致")

    async def _existing_published_result(
        self,
        ingestion: IngestionResult,
    ) -> ProductIngestionResult | None:
        """读取同一 version 的 active generation，用于安全幂等复用。"""

        if ingestion.version_id is None:
            return None
        async with self.database.session() as session:
            pointer = await session.scalar(
                select(DenseGenerationPointerModel).where(
                    DenseGenerationPointerModel.tenant_id == ingestion.tenant_id,
                    DenseGenerationPointerModel.document_id == ingestion.document_id,
                    DenseGenerationPointerModel.version_id == ingestion.version_id,
                    DenseGenerationPointerModel.index_profile_key == self.dense_profile.key,
                )
            )
            if pointer is None or pointer.active_generation_id is None:
                return None
            generation = await session.get(DenseGenerationModel, pointer.active_generation_id)
            if generation is None or generation.status is not DenseGenerationStatus.ACTIVE:
                return None
            version = await session.get(
                DocumentVersionModel,
                (ingestion.tenant_id, ingestion.document_id, ingestion.version_id),
            )
            if version is None or version.status is not VersionStatus.PUBLISHED:
                return None
            artifacts = (
                await session.scalars(
                    select(VersionArtifactModel).where(
                        VersionArtifactModel.tenant_id == ingestion.tenant_id,
                        VersionArtifactModel.document_id == ingestion.document_id,
                        VersionArtifactModel.version_id == ingestion.version_id,
                    )
                )
            ).all()
        return self._make_result(
            ingestion,
            generation,
            {artifact.kind: artifact.status for artifact in artifacts},
            published=True,
        )

    async def _result_for_generation(
        self,
        ingestion: IngestionResult,
        generation_id: str | None,
        *,
        expected_status: DenseGenerationStatus,
    ) -> ProductIngestionResult | None:
        """按 generation 读取最终状态并构造安全响应。"""

        if generation_id is None:
            return None
        async with self.database.session() as session:
            generation = await session.get(DenseGenerationModel, generation_id)
            if generation is None or generation.status is not expected_status:
                return None
            artifacts = (
                await session.scalars(
                    select(VersionArtifactModel).where(
                        VersionArtifactModel.tenant_id == ingestion.tenant_id,
                        VersionArtifactModel.document_id == ingestion.document_id,
                        VersionArtifactModel.version_id == ingestion.version_id,
                    )
                )
            ).all()
            version = await session.get(
                DocumentVersionModel,
                (ingestion.tenant_id, ingestion.document_id, ingestion.version_id),
            )
        if version is None:
            return None
        return self._make_result(
            ingestion,
            generation,
            {artifact.kind: artifact.status for artifact in artifacts},
            published=version.status is VersionStatus.PUBLISHED,
            version_status=version.status,
        )

    async def _mark_generation_failed(
        self,
        generation_id: str,
        ingestion: IngestionResult,
        *,
        artifact_kind: ArtifactKind | None,
        error: Exception,
    ) -> None:
        """把失败写入 generation/artifact 事实，确保失败对象没有 active pointer。"""

        reason = f"{type(error).__name__}: 产品索引阶段失败"
        async with self.database.transaction() as session:
            generation = await session.get(DenseGenerationModel, generation_id)
            if generation is not None and generation.status is DenseGenerationStatus.BUILDING:
                generation.status = DenseGenerationStatus.FAILED
                generation.failure_reason = reason
            if artifact_kind is not None:
                artifact = await session.get(
                    VersionArtifactModel,
                    (
                        ingestion.tenant_id,
                        ingestion.document_id,
                        ingestion.version_id,
                        artifact_kind,
                    ),
                )
                if artifact is not None:
                    artifact.status = ArtifactStatus.FAILED
                    artifact.failure_reason = reason

    @staticmethod
    def _make_result(
        ingestion: IngestionResult,
        generation: DenseGenerationModel,
        artifact_statuses: dict[ArtifactKind, ArtifactStatus],
        *,
        published: bool,
        version_status: VersionStatus | None = None,
    ) -> ProductIngestionResult:
        """把 ingestion、generation 和 artifact 事实合并成产品响应。"""

        return ProductIngestionResult(
            document_id=ingestion.document_id,
            version_id=ingestion.version_id or generation.version_id,
            job_id=ingestion.job_id,
            generation_id=generation.generation_id,
            job_status=ingestion.status,
            version_status=version_status or ingestion.version_status or VersionStatus.READY,
            generation_status=generation.status,
            artifact_statuses=artifact_statuses,
            published=published,
        )


__all__ = [
    "IdempotencyConflictError",
    "ProductIndexingError",
    "ProductIngestionResult",
    "ProductIngestionService",
]
