"""Weaviate REST/GraphQL transport 适配器。

该模块只负责把稳定的 Dense transport 契约映射到 Weaviate HTTP API。它不保存
租户、版本或发布事实，也不接受请求方传入任意 URL；这些边界由上层 generation
服务和 PostgreSQL 事实模型负责。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

import httpx

from enterprise_rag.retrieval.contracts import (
    WeaviateBatchResult,
    WeaviateObject,
)
from enterprise_rag.retrieval.service import RetrievalResult


_COLLECTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class WeaviateTransportError(RuntimeError):
    """表示已脱敏的 Weaviate transport 错误。"""

    def __init__(
        self,
        kind: str,
        *,
        retryable: bool,
        safe_message: str,
        status_code: int | None = None,
    ) -> None:
        """只保存稳定分类和安全消息，禁止携带上游 body、URL 或请求头。"""

        super().__init__(safe_message)
        self.kind = kind
        self.retryable = retryable
        self.safe_message = safe_message
        self.status_code = status_code


class WeaviateHTTPXTransport:
    """使用共享 HTTPX client 调用固定 Weaviate API 的适配器。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        api_key: str | None = None,
        request_timeout_seconds: float = 10.0,
    ) -> None:
        """保存服务端配置；调用方只能提供服务根地址而不能改变请求路径。"""

        normalized = base_url.strip().rstrip("/")
        if not normalized:
            raise ValueError("Weaviate base_url 不能为空")
        if request_timeout_seconds <= 0:
            raise ValueError("Weaviate request_timeout_seconds 必须大于 0")
        self._client = client
        self._base_url = normalized
        self._api_key = api_key
        self._timeout = httpx.Timeout(request_timeout_seconds)

    @staticmethod
    def _validate_collection_name(collection_name: str) -> str:
        """限制 collection 名称，阻止路径注入和跨 endpoint 访问。"""

        if not _COLLECTION_RE.fullmatch(collection_name):
            raise ValueError("Weaviate collection_name 格式非法")
        return collection_name

    def _headers(self) -> dict[str, str]:
        """构造仅包含必要认证头的请求头。"""

        headers = {"accept": "application/json", "content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    def _url(self, path: str) -> str:
        """把固定 API 相对路径拼接到服务端配置地址。"""

        return f"{self._base_url}{path}"

    @staticmethod
    def _status_error(status_code: int) -> WeaviateTransportError:
        """将 HTTP 状态映射成不泄露上游内容的稳定错误。"""

        if status_code in {401, 403}:
            return WeaviateTransportError(
                "authentication",
                retryable=False,
                safe_message="Weaviate 认证失败",
                status_code=status_code,
            )
        if status_code == 429:
            return WeaviateTransportError(
                "rate_limited",
                retryable=True,
                safe_message="Weaviate 请求被限流",
                status_code=status_code,
            )
        if 400 <= status_code < 500:
            return WeaviateTransportError(
                "invalid_request",
                retryable=False,
                safe_message="Weaviate 请求被拒绝",
                status_code=status_code,
            )
        if status_code >= 500:
            return WeaviateTransportError(
                "unavailable",
                retryable=True,
                safe_message="Weaviate 服务暂不可用",
                status_code=status_code,
            )
        return WeaviateTransportError(
            "protocol",
            retryable=False,
            safe_message="Weaviate 返回了非法状态",
            status_code=status_code,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """发送请求并统一处理 HTTPX/HTTP 错误，绝不读取敏感响应正文。"""

        try:
            response = await self._client.request(
                method,
                self._url(path),
                headers=self._headers(),
                timeout=self._timeout,
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise WeaviateTransportError(
                "timeout", retryable=True, safe_message="Weaviate 请求超时"
            ) from exc
        except httpx.HTTPError as exc:
            raise WeaviateTransportError(
                "unavailable", retryable=True, safe_message="Weaviate 网络请求失败"
            ) from exc
        if response.status_code >= 400:
            raise self._status_error(response.status_code)
        return response

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        """解析 JSON；解析失败时只抛出固定协议错误。"""

        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise WeaviateTransportError(
                "protocol", retryable=False, safe_message="Weaviate 响应格式非法"
            ) from exc

    async def ensure_collection(self, collection_name: str, schema_version: str) -> None:
        """确保 collection 存在并拥有固定的无内置向量化 Dense schema。"""

        collection_name = self._validate_collection_name(collection_name)
        if not schema_version.strip():
            raise ValueError("schema_version 不能为空")
        try:
            response = await self._request("GET", f"/v1/schema/{collection_name}")
        except WeaviateTransportError as exc:
            # 只有 404 是“尚未创建”的正常分支；其他错误必须继续抛出。
            if exc.status_code != 404:
                raise
            response = None
        if response is not None:
            return
        schema = {
            "class": collection_name,
            "description": f"Enterprise RAG Dense schema {schema_version}",
            "vectorizer": "none",
            "properties": [
                {"name": "tenant_id", "dataType": ["text"]},
                {"name": "document_id", "dataType": ["text"]},
                {"name": "version_id", "dataType": ["text"]},
                {"name": "generation_id", "dataType": ["text"]},
                {"name": "index_profile", "dataType": ["text"]},
                {"name": "chunk_id", "dataType": ["text"]},
                {"name": "parent_id", "dataType": ["text"]},
                {"name": "text", "dataType": ["text"]},
                {"name": "parent_text", "dataType": ["text"]},
                {"name": "heading_path", "dataType": ["text[]"]},
                {"name": "source_locators", "dataType": ["object[]"]},
            ],
        }
        await self._request("POST", "/v1/schema", json=schema)

    async def upsert_batch(
        self, collection_name: str, objects: Sequence[WeaviateObject]
    ) -> WeaviateBatchResult:
        """按稳定 object_id 批量 upsert，并将任意部分失败归类为可重试错误。"""

        collection_name = self._validate_collection_name(collection_name)
        if not objects:
            return WeaviateBatchResult(accepted=0)
        payload = {
            "objects": [
                {
                    "class": collection_name,
                    "id": item.object_id,
                    "vector": list(item.vector),
                    "properties": item.properties,
                }
                for item in objects
            ]
        }
        response = await self._request("POST", "/v1/batch/objects", json=payload)
        data = self._json(response)
        if not isinstance(data, list):
            raise WeaviateTransportError(
                "protocol", retryable=False, safe_message="Weaviate 批量响应格式非法"
            )
        failed: list[str] = []
        accepted = 0
        for item in data:
            result = item.get("result") if isinstance(item, dict) else None
            status = result.get("status") if isinstance(result, dict) else None
            object_id = item.get("id") if isinstance(item, dict) else None
            if status in {"SUCCESS", "UPDATED", "CREATED"}:
                accepted += 1
            elif isinstance(object_id, str):
                failed.append(object_id)
        if failed:
            raise WeaviateTransportError(
                "batch_failed", retryable=True, safe_message="Weaviate 批量写入部分失败"
            )
        return WeaviateBatchResult(accepted=accepted)

    @staticmethod
    def _where(generation_id: str, version_id: str) -> dict[str, Any]:
        """构造同时约束 generation/version 的 Weaviate where 过滤器。"""

        return {
            "operator": "And",
            "operands": [
                {"path": ["generation_id"], "operator": "Equal", "valueText": generation_id},
                {"path": ["version_id"], "operator": "Equal", "valueText": version_id},
            ],
        }

    async def _list_objects(
        self, collection_name: str, *, generation_id: str, version_id: str
    ) -> list[dict[str, Any]]:
        """读取指定 generation/version 的对象，用于完整性检查和清理。"""

        collection_name = self._validate_collection_name(collection_name)
        params = httpx.QueryParams(
            {
                "class": collection_name,
                "where": json.dumps(self._where(generation_id, version_id), separators=(",", ":")),
                "limit": "10000",
            }
        )
        path = f"/v1/objects?{params}"
        data = self._json(await self._request("GET", path))
        objects = data.get("objects") if isinstance(data, dict) else None
        if not isinstance(objects, list):
            raise WeaviateTransportError(
                "protocol", retryable=False, safe_message="Weaviate 对象列表格式非法"
            )
        return [item for item in objects if isinstance(item, dict)]

    async def count_generation(
        self, collection_name: str, *, generation_id: str, version_id: str
    ) -> int:
        """统计指定 generation/version 的对象数量。"""

        return len(
            await self._list_objects(
                collection_name, generation_id=generation_id, version_id=version_id
            )
        )

    async def list_chunk_ids(
        self, collection_name: str, *, generation_id: str, version_id: str
    ) -> set[str]:
        """列出指定 generation/version 下 properties 中的 chunk_id。"""

        chunk_ids: set[str] = set()
        for item in await self._list_objects(
            collection_name, generation_id=generation_id, version_id=version_id
        ):
            properties = item.get("properties")
            chunk_id = properties.get("chunk_id") if isinstance(properties, dict) else None
            if isinstance(chunk_id, str):
                chunk_ids.add(chunk_id)
        return chunk_ids

    async def search(
        self,
        collection_name: str,
        *,
        generation_id: str,
        version_id: str,
        query: Sequence[float],
        top_k: int,
    ) -> list[RetrievalResult]:
        """通过 GraphQL nearVector 查询，并映射为现有 RetrievalResult。"""

        collection_name = self._validate_collection_name(collection_name)
        if top_k <= 0:
            raise ValueError("top_k 必须大于 0")
        selection = " ".join(
            (
                "tenant_id document_id version_id generation_id chunk_id parent_id",
                "text parent_text heading_path source_locators _additional { distance }",
            )
        )
        query_text = (
            "query($vector: [Float!]!, $where: WhereFilter!, $limit: Int!) { "
            f"Get {{ {collection_name}(nearVector: {{vector: $vector}}, where: $where, limit: $limit) {{ "
            f"{selection} }} }} }}"
        )
        payload = {
            "query": query_text,
            "variables": {
                "vector": list(query),
                "where": self._where(generation_id, version_id),
                "limit": top_k,
            },
        }
        data = self._json(await self._request("POST", "/v1/graphql", json=payload))
        try:
            rows = data["data"]["Get"][collection_name]
        except (KeyError, TypeError) as exc:
            raise WeaviateTransportError(
                "protocol", retryable=False, safe_message="Weaviate 查询响应格式非法"
            ) from exc
        if not isinstance(rows, list):
            raise WeaviateTransportError(
                "protocol", retryable=False, safe_message="Weaviate 查询结果格式非法"
            )
        results: list[RetrievalResult] = []
        for row in rows:
            additional = row.get("_additional") if isinstance(row, dict) else None
            distance = additional.get("distance") if isinstance(additional, dict) else 0.0
            results.append(
                RetrievalResult(
                    chunk_id=str(row.get("chunk_id", "")),
                    parent_id=row.get("parent_id"),
                    version_id=str(row.get("version_id", version_id)),
                    document_id=str(row.get("document_id", "")),
                    text=str(row.get("parent_text") or row.get("text") or ""),
                    heading_path=tuple(row.get("heading_path") or ()),
                    source_locators=tuple(row.get("source_locators") or ()),
                    score=1.0 - float(distance),
                )
            )
        return results

    async def delete_generation(
        self, collection_name: str, *, generation_id: str, version_id: str
    ) -> int:
        """删除指定 generation/version 的对象，并返回上游报告的删除数量。"""

        collection_name = self._validate_collection_name(collection_name)
        payload = {
            "match": {
                "class": collection_name,
                "where": self._where(generation_id, version_id),
            },
            "output": "minimal",
        }
        data = self._json(await self._request("DELETE", "/v1/batch/objects", json=payload))
        if isinstance(data, dict):
            deleted = data.get("objects", data.get("matches", 0))
            if isinstance(deleted, int):
                return deleted
        return 0


__all__ = ["WeaviateHTTPXTransport", "WeaviateTransportError"]
