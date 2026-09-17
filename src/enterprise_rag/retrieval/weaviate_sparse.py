"""Weaviate BM25F Spike 的最小真实 transport。

该模块只验证 ``SparseFieldProjector`` 产物与 Weaviate 1.27 的 schema、batch、
BM25F query 和 scope filter 兼容性。它不保存 sparse generation 发布事实，也不接入
默认检索 API。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from enterprise_rag.retrieval.sparse import SparseObject, SparseProfile
from enterprise_rag.retrieval.weaviate import (
    WeaviateHTTPXTransport,
    WeaviateTransportError,
)


_BM25F_FIELDS = ("title", "section_title", "api_symbols", "body")


class WeaviateBM25FTransport(WeaviateHTTPXTransport):
    """在 Dense HTTP 基础设施上增加隔离的 BM25F Spike 操作。"""

    @staticmethod
    def _scope_where(
        tenant_id: str,
        generation_id: str,
        version_id: str,
    ) -> dict[str, Any]:
        """构造 tenant、generation 和 version 三重过滤条件。"""

        return {
            "operator": "And",
            "operands": [
                {"path": ["tenant_id"], "operator": "Equal", "valueText": tenant_id},
                {
                    "path": ["generation_id"],
                    "operator": "Equal",
                    "valueText": generation_id,
                },
                {"path": ["version_id"], "operator": "Equal", "valueText": version_id},
            ],
        }

    @staticmethod
    def _weighted_properties(profile: SparseProfile) -> list[str]:
        """按固定字段顺序生成 Weaviate BM25F 的 ``field^weight`` 表达。"""

        return [
            f"{field}^{profile.field_weights[field]:g}" for field in _BM25F_FIELDS
        ]

    async def ensure_collection(
        self,
        collection_name: str,
        schema_version: str,
        *,
        tokenization: str = "word",
    ) -> None:
        """确保隔离 collection 存在，并为四个 BM25F 字段声明同一 tokenizer。"""

        collection_name = self._validate_collection_name(collection_name)
        if not schema_version.strip():
            raise ValueError("schema_version 不能为空")
        tokenization = tokenization.strip()
        if not tokenization:
            raise ValueError("tokenization 不能为空")
        try:
            response = await self._request("GET", f"/v1/schema/{collection_name}")
        except WeaviateTransportError as exc:
            # 只有 collection 不存在时才创建；认证、限流和服务错误继续抛出。
            if exc.status_code != 404:
                raise
            response = None
        if response is not None:
            return

        schema = {
            "class": collection_name,
            "description": f"Enterprise RAG BM25F spike schema {schema_version}",
            "vectorizer": "none",
            "properties": [
                {"name": "tenant_id", "dataType": ["text"]},
                {"name": "document_id", "dataType": ["text"]},
                {"name": "version_id", "dataType": ["text"]},
                {"name": "generation_id", "dataType": ["text"]},
                {"name": "sparse_profile", "dataType": ["text"]},
                {"name": "manifest_hash", "dataType": ["text"]},
                {"name": "chunk_id", "dataType": ["text"]},
                {"name": "parent_id", "dataType": ["text"]},
                {
                    "name": "title",
                    "dataType": ["text"],
                    "tokenization": tokenization,
                },
                {
                    "name": "section_title",
                    "dataType": ["text"],
                    "tokenization": tokenization,
                },
                {
                    "name": "api_symbols",
                    "dataType": ["text"],
                    "tokenization": tokenization,
                },
                {
                    "name": "body",
                    "dataType": ["text"],
                    "tokenization": tokenization,
                },
                {"name": "heading_path", "dataType": ["text[]"]},
                {
                    "name": "source_locators",
                    "dataType": ["object[]"],
                    "nestedProperties": [
                        {"name": "kind", "dataType": ["text"]},
                        {"name": "start", "dataType": ["int"]},
                        {"name": "end", "dataType": ["int"]},
                        {"name": "page", "dataType": ["int"]},
                        {"name": "line", "dataType": ["int"]},
                    ],
                },
            ],
        }
        await self._request("POST", "/v1/schema", json=schema)

    async def upsert_batch(
        self,
        collection_name: str,
        objects: Sequence[SparseObject],
    ) -> int:
        """按 projector 的稳定 object identity 批量写入 sparse object。"""

        collection_name = self._validate_collection_name(collection_name)
        if not objects:
            return 0
        payload = {
            "objects": [
                {
                    "class": collection_name,
                    "id": self._object_uuid(item.object_id),
                    "properties": item.properties,
                }
                for item in objects
            ]
        }
        data = self._json(
            await self._request("POST", "/v1/batch/objects", json=payload)
        )
        if not isinstance(data, list):
            raise WeaviateTransportError(
                "protocol",
                retryable=False,
                safe_message="Weaviate sparse 批量响应格式非法",
            )
        accepted = 0
        for item in data:
            result = item.get("result") if isinstance(item, dict) else None
            status = result.get("status") if isinstance(result, dict) else None
            if status in {"SUCCESS", "UPDATED", "CREATED"}:
                accepted += 1
                continue
            raise WeaviateTransportError(
                "batch_failed",
                retryable=True,
                safe_message="Weaviate sparse 批量写入部分失败",
            )
        return accepted

    async def search(
        self,
        collection_name: str,
        *,
        profile: SparseProfile,
        tenant_id: str,
        generation_id: str,
        version_id: str,
        query: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """使用固定字段权重执行 BM25F，并返回当前 scope 内的最小结果。"""

        collection_name = self._validate_collection_name(collection_name)
        if not query.strip():
            raise ValueError("BM25F query 不能为空")
        if top_k <= 0:
            raise ValueError("top_k 必须大于 0")

        weighted_properties = json.dumps(
            self._weighted_properties(profile),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        where_type = f"GetObjects{collection_name}WhereInpObj!"
        # 与 Dense 查询一致，Weaviate 1.27 的 Int variable 会触发 int64/int 类型断言，
        # 因此只把已校验的 top_k 作为数字字面量内联。
        query_text = (
            f"query($query: String!, $where: {where_type}) {{ "
            f"Get {{ {collection_name}("
            f"bm25: {{query: $query, properties: {weighted_properties}}}, "
            f"where: $where, limit: {top_k}) {{ "
            "tenant_id document_id version_id generation_id chunk_id parent_id "
            "title section_title api_symbols body _additional { score } "
            "} } }"
        )
        payload = {
            "query": query_text,
            "variables": {
                "query": query,
                "where": self._scope_where(tenant_id, generation_id, version_id),
            },
        }
        data = self._json(await self._request("POST", "/v1/graphql", json=payload))
        try:
            rows = data["data"]["Get"][collection_name]
        except (KeyError, TypeError) as exc:
            raise WeaviateTransportError(
                "protocol",
                retryable=False,
                safe_message="Weaviate BM25F 查询响应格式非法",
            ) from exc
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise WeaviateTransportError(
                "protocol",
                retryable=False,
                safe_message="Weaviate BM25F 查询结果格式非法",
            )
        return rows

    async def delete_collection(self, collection_name: str) -> None:
        """删除本次隔离 smoke collection，避免重复实验累积空 schema。"""

        collection_name = self._validate_collection_name(collection_name)
        await self._request("DELETE", f"/v1/schema/{collection_name}")


__all__ = ["WeaviateBM25FTransport"]
