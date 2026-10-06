"""真实分阶段摄取，外部 I/O 在事务外，发布与 fencing 在同一短事务。"""

from __future__ import annotations

import asyncio
import hashlib
import json

from sqlalchemy import select

from enterprise_rag.db.models import (
    ArtifactKind,
    ArtifactStatus,
    DenseGenerationModel,
    DocumentModel,
    DocumentVersionModel,
    TenantModel,
    VersionArtifactModel,
)
from enterprise_rag.db.publish import mark_product_generation_ready, publish_product_generation
from enterprise_rag.domain.entities import DenseGenerationStatus, DocumentStatus, VersionStatus
from enterprise_rag.infrastructure.redis import TenantEmbeddingCache
from enterprise_rag.ingestion.chunking import chunk_blocks
from enterprise_rag.ingestion.parsers import parse_document
from enterprise_rag.ingestion.service import _insert_if_absent, _json_bytes
from enterprise_rag.ingestion.task_ledger import StageFailure, StaleStageLease
from enterprise_rag.retrieval.contracts import GenerationScope, WeaviateObject
from enterprise_rag.retrieval.indexer import WeaviateDenseIndexer
from enterprise_rag.retrieval.sparse import SparseFieldProjector
from enterprise_rag.security.authorization import (
    AuthorizationDenied,
    AuthorizationService,
    ResourceNotFound,
)
from enterprise_rag.storage.s3 import StorageUnavailable


class StageWorker:
    """一次消息只执行一个阶段，下一阶段由 PG outbox 驱动。"""

    def __init__(
        self, ledger, storage, embedding, dense, sparse, profile, sparse_profile,
        generation_service, *, identity_resolver, batch_size=32, write_gate=None, embedding_cache=None,
        coordinator=None,
    ):
        """注入已有 transport/profile；生产必须提供当前身份解析器而非角色快照。"""
        self.ledger, self.storage, self.embedding = ledger, storage, embedding
        self.dense, self.sparse, self.profile = dense, sparse, profile
        self.sparse_profile, self.generations = sparse_profile, generation_service
        self.identity_resolver, self.batch_size, self.write_gate = identity_resolver, batch_size, write_gate
        self.authorization = AuthorizationService()
        self.embedding_cache = embedding_cache
        self.coordinator = coordinator
        if identity_resolver is None:
            raise ValueError("Worker 必须提供可信身份重加载器")
        if type(batch_size) is not int or not 1 <= batch_size <= 128:
            raise ValueError("batch_size 必须在 1 到 128 之间")

    async def run_event(self, event_id):
        """领取、心跳、故障分类与有限重试，取消/失去租约不伪造成功。"""
        if self.write_gate is not None:
            async with self.write_gate.writer():
                return await self._run(event_id)
        return await self._run(event_id)

    async def _run(self, event_id):
        """生命周期 wrapper 不捕获取消，过期任务由持久恢复器处理。"""
        lease = await self.ledger.claim_event(event_id)
        if lease is None:
            return "ignored"
        task = asyncio.create_task(self._execute(lease))

        async def heartbeat():
            """数据库续租失败立即取消本地阶段，旧外部写入不能发布。"""
            while not task.done():
                await asyncio.sleep(max(5, self.ledger.lease_seconds / 3))
                if not task.done():
                    await self.ledger.renew(lease)

        beat = asyncio.create_task(heartbeat())
        try:
            # 即使本机 solo 池没有 hard kill，阶段仍有明确最大执行预算。
            async with asyncio.timeout(self.ledger.lease_seconds):
                done, _ = await asyncio.wait((task, beat), return_when=asyncio.FIRST_COMPLETED)
            if beat in done:
                await beat
                raise StaleStageLease("阶段续租结束")
            commit = await task
            await self.ledger.complete(lease, commit=commit)
            return "completed"
        except (StaleStageLease, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - 分类后只持久化固定错误类别。
            if isinstance(exc, (ResourceNotFound, AuthorizationDenied)):
                failure = StageFailure("permission_denied", False)
            elif isinstance(exc, (ValueError, KeyError, TypeError)):
                failure = StageFailure("invalid_input", False)
            else:
                failure = StageFailure(
                    "timeout" if isinstance(exc, TimeoutError) else "dependency_unavailable", True,
                )
            await self.ledger.fail(lease, failure)
            return "failed"
        finally:
            task.cancel()
            beat.cancel()
            await asyncio.gather(task, beat, return_exceptions=True)

    async def _context(self, lease, inputs, *, session=None):
        """当前 tenant/policy/文档 ACL 复核，不能让 replay 重授已撤销 ACL。"""
        meta = inputs["metadata"]
        if meta["profile"] != self.profile.key:
            raise ValueError("任务 profile 与 Worker 不一致")
        context = await self.identity_resolver(lease.tenant_id, inputs["actor_id"])
        if context.tenant_id != lease.tenant_id or context.actor_id != inputs["actor_id"]:
            raise ResourceNotFound("任务身份不可见")
        self.authorization.require_any_role(context, {"editor", "admin"})
        if session is None:
            async with self.ledger.database.session() as read_session:
                return await self._check_context(read_session, context, meta)
        return await self._check_context(session, context, meta)

    async def _check_context(self, session, context, meta):
        """复核 tenant 状态、授权版本、现有文档状态和上传者 ACL。"""
        tenant = await session.get(TenantModel, context.tenant_id)
        if (
            tenant is None or not tenant.is_active
            or str(tenant.policy_version) != meta["policy_version"]
        ):
            raise ResourceNotFound("任务授权已失效")
        document = await session.get(DocumentModel, (context.tenant_id, meta["document_id"]))
        if document is not None:
            if document.status is DocumentStatus.ARCHIVED:
                raise ResourceNotFound("文档不可见")
            if not context.has_role("admin") and not await self.authorization._has_read_acl(
                session, context, document.id,
            ):
                raise ResourceNotFound("任务权限已撤销")
        return context

    async def _save(self, lease, name, payload):
        """每个 attempt 使用独立不可变对象，提交 hash 后才允许下游读取。"""
        raw = _json_bytes(payload)
        key = f"async-artifacts/{lease.task_id}/{lease.execution}/{lease.stage}/{lease.fence}/{name}.json"
        await self.storage.put(key, raw, "application/json")
        await self.ledger.checkpoint(
            lease, name, object_key=key, content_hash=hashlib.sha256(raw).hexdigest(),
        )
        return key

    async def _read(self, record):
        """产物 hash 不一致时禁止复用或发布，不相信缓存对象。"""
        raw = await self.storage.get(record["object_key"])
        if hashlib.sha256(raw).hexdigest() != record["content_hash"]:
            raise ValueError("checkpoint 产物 hash 不一致")
        return json.loads(raw)

    async def _output(self, inputs, stage):
        """只取本执行该阶段最后一个成功提交的输出 locator。"""
        entries = inputs["checkpoints"][stage]
        names = [name for name in entries if name.startswith("output-")]
        if not names:
            raise ValueError("前置阶段产物缺失")
        name = max(names, key=lambda value: int(value.split("-")[1]))
        return await self._read(entries[name])

    async def _execute(self, lease):
        """选择固定阶段 handler，不执行消息自带函数/地址/正文。"""
        inputs = await self.ledger.task_input(lease)
        await self._context(lease, inputs)
        handler = getattr(self, f"_{lease.stage}")
        if self.coordinator is None:
            return await handler(lease, inputs)
        # Redis 仅协调同一文档阶段；PG lease/fence 才能决定业务提交。
        lock = await self.coordinator.acquire_lock(
            lease.tenant_id, inputs["metadata"]["document_id"],
            ttl_ms=(self.ledger.lease_seconds + 30) * 1000,
        )
        if lock is None:
            raise TimeoutError("文档阶段正在执行")
        try:
            return await handler(lease, inputs)
        finally:
            # 释放失败不伪造阶段成功，checkpoint 仍可由新 attempt 恢复。
            await self.coordinator.release_lock(lock)

    async def _parse(self, lease, inputs):
        """校验原文并在事务外解析/切块，最后短事务写版本与基础产物。"""
        meta = inputs["metadata"]
        raw = await self.storage.get(inputs["raw_object_key"])
        if hashlib.sha256(raw).hexdigest() != meta["content_hash"]:
            raise ValueError("原文 hash 不一致")
        blocks = await asyncio.to_thread(parse_document, raw, filename=meta["filename"])
        manifest = chunk_blocks(blocks, version_id=meta["version_id"])
        payload = {
            "tenant_id": lease.tenant_id, "document_id": meta["document_id"],
            "version_id": meta["version_id"], "content_hash": meta["content_hash"],
            "blocks": [block.to_dict() for block in blocks],
            "chunks": manifest.to_dict(), "parser_version": "structured-block-v1",
        }
        if not payload["chunks"]["children"]:
            raise ValueError("解析无有效文本")
        # 同版本 manifest 必须确定性且与旧同步检索完全兼容，不把 attempt 写入正文。
        key = await self._save(lease, f"output-{lease.attempt}", payload)

        async def commit(session, task):
            """与阶段完成同事务创建事实，原文/manifest I/O 已在事务外结束。"""
            # 首次建文档以 tenant 行短锁串行化，避免并发上传者把已有文档当成新建后重授 ACL。
            await session.scalar(select(TenantModel).where(
                TenantModel.id == lease.tenant_id,
            ).with_for_update())
            context = await self._context(lease, inputs, session=session)
            await _insert_if_absent(session, DocumentModel, {
                "tenant_id": lease.tenant_id, "id": meta["document_id"],
                "source_id": meta["source_id"], "title": meta["title"], "status": DocumentStatus.DRAFT,
            })
            await self.authorization.ensure_actor_read_acl(session, context, document_id=meta["document_id"])
            await _insert_if_absent(session, DocumentVersionModel, {
                "tenant_id": lease.tenant_id, "document_id": meta["document_id"],
                "id": meta["version_id"], "content_hash": meta["content_hash"], "status": VersionStatus.READY,
            })
            version = await session.get(
                DocumentVersionModel, (lease.tenant_id, meta["document_id"], meta["version_id"]),
            )
            if version.status not in {VersionStatus.READY, VersionStatus.PUBLISHED}:
                raise ValueError("异步版本不可构建")
            for kind in ArtifactKind:
                await _insert_if_absent(session, VersionArtifactModel, {
                    "tenant_id": lease.tenant_id, "document_id": meta["document_id"],
                    "version_id": meta["version_id"], "kind": kind,
                    "status": ArtifactStatus.READY if kind in {ArtifactKind.RAW, ArtifactKind.PARSED}
                    else ArtifactStatus.PENDING,
                    "item_count": 1 if kind is ArtifactKind.RAW else len(blocks) if kind is ArtifactKind.PARSED else 0,
                })
            # 兼容现有检索从 canonical manifest 读取；位置在 metadata 保存供发布前搬移。
            task.request_metadata = {**task.request_metadata, "parsed_object_key": key}
        return commit

    async def _embedding(self, lease, inputs):
        """每批向量独立 checkpoint；重试复用已落对象，避免完整重复计算。"""
        payload = await self._output(inputs, "parse")
        children = payload["chunks"]["children"]
        checkpoints = await self.ledger.checkpoints(lease)
        provider = self.embedding
        if self.embedding_cache is not None:
            from enterprise_rag.infrastructure.redis import CachedTenantEmbeddingProvider

            provider = CachedTenantEmbeddingProvider(
                provider, self.embedding_cache, tenant=lease.tenant_id,
                profile=self.profile.key, dimension=self.profile.dimension,
            )
        for start in range(0, len(children), self.batch_size):
            name = f"batch-{start}"
            batch = children[start:start + self.batch_size]
            if name in checkpoints:
                saved = await self._read(checkpoints[name])
                if saved["chunk_ids"] != [child["chunk_id"] for child in batch]:
                    raise ValueError("向量 checkpoint lineage 不一致")
                self._vectors(saved, batch)
                continue
            vectors = await provider.embed([child["text"] for child in batch])
            if len(vectors) != len(batch) or any(
                not TenantEmbeddingCache._valid(vector, self.profile.dimension) for vector in vectors
            ):
                raise ValueError("模型向量维度/数值非法")
            await self._save(lease, name, {
                "chunk_ids": [child["chunk_id"] for child in batch], "vectors": vectors,
                "profile": self.profile.key,
            })

    def _vectors(self, saved, batch):
        """每次从 checkpoint 读取后复核模型空间、数量、维度和数值。"""
        if (
            saved["profile"] != self.profile.key
            or saved["chunk_ids"] != [child["chunk_id"] for child in batch]
            or len(saved["vectors"]) != len(batch)
            or any(not TenantEmbeddingCache._valid(v, self.profile.dimension) for v in saved["vectors"])
        ):
            raise ValueError("向量 checkpoint profile/lineage/数值非法")
        return saved["vectors"]

    async def _dense(self, lease, inputs):
        """每次 Dense attempt 新 generation，旧 Worker 外部迟到写入永不覆盖新 generation。"""
        payload = await self._output(inputs, "parse")
        meta = inputs["metadata"]
        parse_entries = inputs["checkpoints"]["parse"]
        output_name = max(parse_entries, key=lambda value: int(value.split("-")[1]))
        marker = f"async:{lease.task_id}:{lease.execution}:"
        async with self.ledger.database.transaction() as session:
            await self.ledger._leased(session, lease)
            await self._context(lease, inputs, session=session)
            existing = (await session.scalars(select(DenseGenerationModel).where(
                DenseGenerationModel.tenant_id == lease.tenant_id,
                DenseGenerationModel.document_id == meta["document_id"],
                DenseGenerationModel.version_id == meta["version_id"],
                DenseGenerationModel.index_profile_key == self.profile.key,
                DenseGenerationModel.status == DenseGenerationStatus.BUILDING,
            ).with_for_update())).all()
            for old in existing:
                if not old.worker_lease.startswith(f"async:{lease.task_id}:"):
                    from enterprise_rag.db.async_ingestion import AsyncTaskModel

                    parts = old.worker_lease.split(":")
                    previous_task = await session.get(
                        AsyncTaskModel, (lease.tenant_id, parts[1]),
                    ) if len(parts) == 4 and parts[0] == "async" else None
                    if previous_task is None or previous_task.status != "dead_letter":
                        raise TimeoutError("同 scope 其他构建尚未结束")
                old.status, old.failure_reason = DenseGenerationStatus.FAILED, "旧异步 attempt 失效"
            await session.flush()
            generation = await self.generations.start_generation(
                session, GenerationScope(lease.tenant_id, meta["document_id"], meta["version_id"], self.profile.key),
                self.profile, manifest_hash=parse_entries[output_name]["content_hash"],
                expected_item_count=len(payload["chunks"]["children"]),
                worker_lease=marker + str(lease.fence), sparse_profile=self.sparse_profile,
            )
        await self.dense.ensure_collection(generation.collection_name, self.profile.schema_version)
        parents = {parent["chunk_id"]: parent for parent in payload["chunks"]["parents"]}
        children = payload["chunks"]["children"]
        for start in range(0, len(children), self.batch_size):
            batch = children[start:start + self.batch_size]
            vectors = await self._read(inputs["checkpoints"]["embedding"][f"batch-{start}"])
            validated_vectors = self._vectors(vectors, batch)
            objects = [
                WeaviateObject(
                    object_id=WeaviateDenseIndexer._object_id(generation.generation_id, child["chunk_id"], self.profile.key),
                    vector=tuple(vector),
                    properties={
                        "tenant_id": lease.tenant_id, "document_id": meta["document_id"],
                        "version_id": meta["version_id"], "generation_id": generation.generation_id,
                        "index_profile": self.profile.key, "manifest_hash": generation.manifest_hash,
                        "model_name": self.profile.model_name, "dimension": self.profile.dimension,
                        "chunk_id": child["chunk_id"], "parent_id": child.get("parent_id"),
                        "text": child["text"], "parent_text": parents.get(child.get("parent_id"), {}).get("text"),
                        "heading_path": child["heading_path"], "source_locators": child["source_locators"],
                    },
                ) for child, vector in zip(batch, validated_vectors, strict=True)
            ]
            result = await self.dense.upsert_batch(generation.collection_name, objects)
            if result.accepted != len(objects) or result.failed_ids:
                raise StorageUnavailable("Dense 批次写入失败")
        await self._save(lease, f"output-{lease.attempt}", {
            "generation_id": generation.generation_id, "dense_collection": generation.collection_name,
            "sparse_collection": generation.sparse_collection_name,
        })

    async def _sparse(self, lease, inputs):
        """Sparse 使用相同不可变 manifest 和 generation，重放写入内容相同。"""
        payload = await self._output(inputs, "parse")
        generation = await self._output(inputs, "dense")
        await self.sparse.ensure_collection(
            generation["sparse_collection"], self.sparse_profile.schema_version,
            tokenization=self.sparse_profile.tokenizer_version,
        )
        objects = SparseFieldProjector(self.sparse_profile).project_manifest(
            payload, generation_id=generation["generation_id"], tenant_id=lease.tenant_id,
            document_id=inputs["metadata"]["document_id"], version_id=inputs["metadata"]["version_id"],
        )
        for start in range(0, len(objects), self.batch_size):
            batch = objects[start:start + self.batch_size]
            result = await self.sparse.upsert_batch(generation["sparse_collection"], batch)
            if getattr(result, "accepted", result) != len(batch):
                raise StorageUnavailable("Sparse 批次写入失败")

    async def _verify(self, lease, inputs):
        """事务外核验两路完整 identity；事务内只提交 READY。"""
        payload, generation = await self._output(inputs, "parse"), await self._output(inputs, "dense")
        expected = {child["chunk_id"] for child in payload["chunks"]["children"]}
        args = {"generation_id": generation["generation_id"], "version_id": inputs["metadata"]["version_id"]}
        count = await self.dense.count_generation(generation["dense_collection"], **args)
        ids = await self.dense.list_chunk_ids(generation["dense_collection"], **args)
        sparse = await self.sparse.inspect_generation(
            generation["sparse_collection"], tenant_id=lease.tenant_id,
            sparse_profile_key=self.sparse_profile.key, **args,
        )
        if (
            count != len(expected) or ids != expected or sparse.chunk_ids != frozenset(expected)
            or sparse.item_count != len(expected) or sparse.sparse_profile_key != self.sparse_profile.key
        ):
            raise ValueError("双路 generation 完整性失败")

        async def commit(session, task):
            """租约仍有效才记录就绪，不在 DB 事务里访问 Weaviate。"""
            await self._context(lease, inputs, session=session)
            await mark_product_generation_ready(
                session, generation["generation_id"],
                dense_item_count=count, sparse_item_count=sparse.item_count,
            )
        return commit

    async def _publish(self, lease, inputs):
        """canonical manifest 写完后，同一短事务 fencing/ACL/双路发布/任务完成。"""
        generation = await self._output(inputs, "dense")
        payload = await self._output(inputs, "parse")
        meta = inputs["metadata"]
        from enterprise_rag.ingestion.service import IngestionService

        canonical = IngestionService._parsed_key(lease.tenant_id, meta["document_id"], meta["version_id"])
        await self.storage.put(canonical, _json_bytes(payload), "application/json")

        async def commit(session, task):
            """旧 lease 不能调用此回调；发布与账本 complete 共用同一事务。"""
            await self._context(lease, inputs, session=session)
            await publish_product_generation(session, generation["generation_id"])
        return commit
