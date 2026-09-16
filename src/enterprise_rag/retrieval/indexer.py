"""从 parsed manifest 构建 Dense 索引。"""

from __future__ import annotations

import json

from enterprise_rag.embedding.ollama import OllamaEmbeddingProvider
from enterprise_rag.retrieval.repository import PostgresVectorRepository
from enterprise_rag.retrieval.service import EmbeddingRecord
from enterprise_rag.storage.protocols import ObjectStorage


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
