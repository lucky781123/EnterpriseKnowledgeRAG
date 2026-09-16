"""从 parsed manifest 构建 Dense 索引。"""

from __future__ import annotations

import json
import hashlib
from typing import Any

from enterprise_rag.embedding.ollama import OllamaEmbeddingProvider
from enterprise_rag.retrieval.repository import PostgresVectorRepository
from enterprise_rag.retrieval.service import EmbeddingRecord
from enterprise_rag.storage.protocols import ObjectStorage
from enterprise_rag.retrieval.contracts import IndexProfile, WeaviateObject, WeaviateTransport
from enterprise_rag.retrieval.generation import GenerationService


class DenseIndexer:
    """读取 parsed manifest，生成 child embedding 并写入 PostgreSQL。"""

    def __init__(
        self,
        embedding_provider: OllamaEmbeddingProvider,
        repository: PostgresVectorRepository,
        storage: ObjectStorage,
    ) -> None:
        """保存 embedding、向量库和对象存储适配器。"""

        self.embedding_provider = embedding_provider
        self.repository = repository
        self.storage = storage

    async def index_manifest(
        self,
        session,
        *,
        tenant_id: str,
        document_id: str,
        version_id: str,
        parsed_object_key: str,
    ) -> int:
        """校验 parsed manifest 范围，只为 child 建向量并固化 Parent 正文。"""

        payload = json.loads((await self.storage.get(parsed_object_key)).decode("utf-8"))
        expected_scope = (tenant_id, document_id, version_id)
        actual_scope = (
            payload.get("tenant_id"),
            payload.get("document_id"),
            payload.get("version_id"),
        )
        if actual_scope != expected_scope:
            raise ValueError("parsed manifest 与索引范围不一致")

        chunks = payload.get("chunks")
        if not isinstance(chunks, dict):
            raise ValueError("parsed manifest 缺少 chunks")
        parents = chunks.get("parents", [])
        children = chunks.get("children", [])
        if not isinstance(parents, list) or not isinstance(children, list):
            raise ValueError("parsed manifest 的 parents/children 必须是列表")

        # Parent map 只从当前 manifest 构建，从数据入口上阻断跨版本 parent 回溯。
        parent_text_by_id = {
            item["chunk_id"]: item["text"]
            for item in parents
            if item.get("version_id") == version_id
        }
        vectors = (
            await self.embedding_provider.embed([item["text"] for item in children])
            if children
            else []
        )
        records = [
            EmbeddingRecord(
                tenant_id=tenant_id,
                document_id=document_id,
                version_id=version_id,
                chunk_id=item["chunk_id"],
                parent_id=item.get("parent_id"),
                text=item["text"],
                heading_path=tuple(item.get("heading_path", [])),
                source_locators=tuple(item.get("source_locators", [])),
                embedding=tuple(vector),
                parent_text=parent_text_by_id.get(item.get("parent_id")),
            )
            for item, vector in zip(children, vectors, strict=True)
        ]
        return await self.repository.replace_version(
            session,
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            records=records,
        )


class WeaviateDenseIndexer:
    """按 generation 将 parsed manifest 的 child Dense 对象写入 Weaviate。"""

    def __init__(
        self,
        embedding_provider: Any,
        transport: WeaviateTransport,
        storage: ObjectStorage,
        generation_service: GenerationService,
    ) -> None:
        """保存 embedding、外部 transport、对象存储和事实服务。"""

        self.embedding_provider = embedding_provider
        self.transport = transport
        self.storage = storage
        self.generation_service = generation_service

    @staticmethod
    def _object_id(generation_id: str, chunk_id: str, profile_key: str) -> str:
        """为 generation、chunk 和 profile 组合生成稳定 Weaviate object identity。"""

        value = f"{generation_id}:{chunk_id}:{profile_key}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _scope(payload: dict[str, Any]) -> tuple[Any, Any, Any]:
        """读取 manifest 顶层 scope，避免从 child 猜测租户或版本。"""

        return payload.get("tenant_id"), payload.get("document_id"), payload.get("version_id")

    async def build_generation(
        self,
        session,
        *,
        generation_id: str,
        tenant_id: str,
        document_id: str,
        version_id: str,
        profile: IndexProfile,
        parsed_object_key: str,
        batch_size: int = 32,
    ):
        """分批构建 generation；批次成功后推进 checkpoint，失败保留 BUILDING。"""

        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")
        generation = await self.generation_service.get_generation(session, generation_id)
        if (
            generation.tenant_id,
            generation.document_id,
            generation.version_id,
        ) != (tenant_id, document_id, version_id):
            raise ValueError("generation scope 与请求不一致")
        if generation.index_profile_key != profile.key:
            raise ValueError("generation index profile 与请求不一致")
        if (
            generation.model_name,
            generation.dimension,
            generation.chunking_version,
            generation.schema_version,
        ) != (
            profile.model_name,
            profile.dimension,
            profile.chunking_version,
            profile.schema_version,
        ):
            raise ValueError("generation profile 参数不一致")

        raw_manifest = await self.storage.get(parsed_object_key)
        payload = json.loads(raw_manifest.decode("utf-8"))
        if self._scope(payload) != (tenant_id, document_id, version_id):
            raise ValueError("parsed manifest 与索引范围不一致")
        actual_manifest_hash = hashlib.sha256(raw_manifest).hexdigest()
        if actual_manifest_hash != generation.manifest_hash:
            raise ValueError("parsed manifest hash 与 generation 不一致")
        chunks = payload.get("chunks")
        if not isinstance(chunks, dict):
            raise ValueError("parsed manifest 缺少 chunks")
        parents = chunks.get("parents", [])
        children = chunks.get("children", [])
        if not isinstance(parents, list) or not isinstance(children, list):
            raise ValueError("parsed manifest 的 parents/children 必须是列表")
        if len(children) != generation.expected_item_count:
            raise ValueError("child 数量与 generation expected_item_count 不一致")
        start = generation.checkpoint
        if start < 0 or start > len(children):
            raise ValueError("generation checkpoint 非法")
        parent_by_id = {
            item.get("chunk_id"): item
            for item in parents
            if item.get("version_id") == version_id
        }
        for batch_start in range(start, len(children), batch_size):
            batch = children[batch_start : batch_start + batch_size]
            vectors = await self.embedding_provider.embed([item["text"] for item in batch])
            if len(vectors) != len(batch):
                raise ValueError("embedding 返回条数与 child 数量不一致")
            objects: list[WeaviateObject] = []
            for child, vector in zip(batch, vectors, strict=True):
                if len(vector) != profile.dimension:
                    raise ValueError("embedding dimension 与 index profile 不一致")
                parent = parent_by_id.get(child.get("parent_id"), {})
                objects.append(
                    WeaviateObject(
                        object_id=self._object_id(
                            generation_id, child["chunk_id"], profile.key
                        ),
                        vector=tuple(float(value) for value in vector),
                        properties={
                            "tenant_id": tenant_id,
                            "document_id": document_id,
                            "version_id": version_id,
                            "generation_id": generation_id,
                            "index_profile": profile.key,
                            "manifest_hash": generation.manifest_hash,
                            "model_name": profile.model_name,
                            "dimension": profile.dimension,
                            "chunk_id": child["chunk_id"],
                            "parent_id": child.get("parent_id"),
                            "text": child["text"],
                            "parent_text": parent.get("text"),
                            "heading_path": child.get("heading_path", []),
                            "source_locators": child.get("source_locators", []),
                        },
                    )
                )
            await self.transport.upsert_batch(generation.collection_name, objects)
            await self.generation_service.record_checkpoint(
                session, generation_id, batch_start + len(batch)
            )

        actual_ids = await self.transport.list_chunk_ids(
            generation.collection_name,
            generation_id=generation_id,
            version_id=version_id,
        )
        expected_ids = {child.get("chunk_id") for child in children}
        if actual_ids != expected_ids:
            raise ValueError("generation chunk identity 完整性检查失败")
        return await self.generation_service.validate_generation(
            session, generation_id, self.transport
        )
