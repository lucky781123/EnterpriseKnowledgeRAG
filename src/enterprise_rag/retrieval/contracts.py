"""检索后端共享的稳定值对象和跨组件契约。"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from enterprise_rag.domain.entities import DenseGenerationStatus

if TYPE_CHECKING:
    from enterprise_rag.retrieval.service import RetrievalResult


@dataclass(frozen=True, slots=True)
class IndexProfile:
    """描述一组不可混用的 embedding、切块和 Weaviate schema 参数。"""

    model_name: str
    dimension: int
    chunking_version: str
    schema_version: str

    def __post_init__(self) -> None:
        """校验 profile 字段，避免生成含糊或不可持久化的 profile key。"""

        if not self.model_name.strip():
            raise ValueError("model_name 不能为空")
        if self.dimension <= 0:
            raise ValueError("dimension 必须大于 0")
        if not self.chunking_version.strip():
            raise ValueError("chunking_version 不能为空")
        if not self.schema_version.strip():
            raise ValueError("schema_version 不能为空")

    @property
    def key(self) -> str:
        """按固定字段顺序生成可比较、可持久化的 profile key。"""

        return ":".join(
            (self.model_name, str(self.dimension), self.chunking_version, self.schema_version)
        )


@dataclass(frozen=True, slots=True)
class GenerationScope:
    """限定 generation 可覆盖的租户、文档版本和索引 profile。"""

    tenant_id: str
    document_id: str
    version_id: str
    index_profile_key: str

    def __post_init__(self) -> None:
        """拒绝空 scope，防止跨租户或未命名 generation 进入持久化层。"""

        if any(not value.strip() for value in (self.tenant_id, self.document_id, self.version_id)):
            raise ValueError("generation scope 的 tenant/document/version 不能为空")
        if not self.index_profile_key.strip():
            raise ValueError("index_profile_key 不能为空")


def collection_name_for_tenant(tenant_id: str) -> str:
    """把租户 ID 映射成不泄露原始标识且符合 Weaviate 命名约束的 collection 名。"""

    if not tenant_id.strip():
        raise ValueError("tenant_id 不能为空")
    digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:24]
    return f"DenseTenant_{digest}"


def sparse_collection_name_for_tenant(tenant_id: str) -> str:
    """把租户 ID 映射成稳定且不暴露原始标识的 Sparse collection 名。"""

    if not tenant_id.strip():
        raise ValueError("tenant_id 不能为空")
    digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:24]
    return f"SparseTenant_{digest}"


@dataclass(frozen=True, slots=True)
class CollectionRegistryRecord:
    """描述 PostgreSQL 中 tenant 到 Weaviate collection 的稳定映射。"""

    tenant_id: str
    collection_name: str
    schema_version: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DenseGenerationRecord:
    """描述一个 Dense generation 的 PostgreSQL 事实快照。"""

    generation_id: str
    scope: GenerationScope
    collection_name: str
    manifest_hash: str
    model_name: str
    dimension: int
    chunking_version: str
    schema_version: str
    expected_item_count: int
    actual_item_count: int
    checkpoint: int
    build_attempt: int
    worker_lease: str
    status: DenseGenerationStatus
    created_at: datetime
    published_at: datetime | None = None
    retired_at: datetime | None = None
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ActiveGenerationPointer:
    """描述一个 scope 当前 active 与 previous generation 的发布指针。"""

    scope: GenerationScope
    active_generation_id: str | None
    previous_generation_id: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SparseGenerationSnapshot:
    """描述 Weaviate 中一个 Sparse generation 的完整性检查结果。"""

    tenant_id: str
    version_id: str
    generation_id: str
    sparse_profile_key: str
    item_count: int
    chunk_ids: frozenset[str]

    def __post_init__(self) -> None:
        """拒绝空 scope、负数量和重复导致的数量不一致。"""

        if any(
            not value.strip()
            for value in (
                self.tenant_id,
                self.version_id,
                self.generation_id,
                self.sparse_profile_key,
            )
        ):
            raise ValueError("Sparse generation snapshot scope 不能为空")
        if self.item_count < 0:
            raise ValueError("Sparse generation item_count 不能为负数")
        if len(self.chunk_ids) != self.item_count:
            raise ValueError("Sparse generation chunk identity 数量不一致")


@dataclass(frozen=True, slots=True)
class WeaviateObject:
    """描述一个可幂等写入 Weaviate 的 Dense 对象。"""

    object_id: str
    vector: tuple[float, ...]
    properties: dict[str, Any]

    def __post_init__(self) -> None:
        """校验对象 identity 和向量，避免把不可重试的数据交给 transport。"""

        if not self.object_id.strip():
            raise ValueError("Weaviate object_id 不能为空")
        if not self.vector:
            raise ValueError("Weaviate vector 不能为空")
        if any(not isinstance(value, (int, float)) for value in self.vector):
            raise ValueError("Weaviate vector 必须只包含数字")


@dataclass(frozen=True, slots=True)
class WeaviateBatchResult:
    """表示一次批量 upsert 的稳定统计结果。"""

    accepted: int
    failed_ids: tuple[str, ...] = ()


class WeaviateTransport(Protocol):
    """定义 generation 构建和查询所需的最小 Weaviate transport。"""

    async def ensure_collection(self, collection_name: str, schema_version: str) -> None:
        """确保指定 collection 使用固定 Dense schema。"""

    async def upsert_batch(
        self, collection_name: str, objects: Sequence[WeaviateObject]
    ) -> WeaviateBatchResult:
        """按对象稳定 identity 批量写入 Dense 数据。"""

    async def count_generation(
        self, collection_name: str, *, generation_id: str, version_id: str
    ) -> int:
        """统计指定 generation/version 下的对象数量。"""

    async def list_chunk_ids(
        self, collection_name: str, *, generation_id: str, version_id: str
    ) -> set[str]:
        """列出指定 generation/version 下的 chunk identity。"""

    async def search(
        self,
        collection_name: str,
        *,
        generation_id: str,
        version_id: str,
        query: Sequence[float],
        top_k: int,
    ) -> list[RetrievalResult]:
        """在指定 collection 和 generation 范围内执行向量查询。"""

    async def delete_generation(
        self, collection_name: str, *, generation_id: str, version_id: str
    ) -> int:
        """删除指定 generation/version 的派生对象。"""


class SparseGenerationTransport(Protocol):
    """定义产品双路发布所需的最小 Sparse generation transport。"""

    async def inspect_generation(
        self,
        collection_name: str,
        *,
        tenant_id: str,
        generation_id: str,
        version_id: str,
        sparse_profile_key: str,
    ) -> SparseGenerationSnapshot:
        """读取完整 scope 下的数量、chunk identity 和 Sparse profile。"""

    async def delete_generation(
        self,
        collection_name: str,
        *,
        tenant_id: str,
        generation_id: str,
        version_id: str,
        sparse_profile_key: str,
    ) -> int:
        """删除完整 scope 下的 Sparse 派生对象。"""


__all__ = [
    "ActiveGenerationPointer",
    "CollectionRegistryRecord",
    "DenseGenerationRecord",
    "GenerationScope",
    "IndexProfile",
    "SparseGenerationSnapshot",
    "SparseGenerationTransport",
    "WeaviateBatchResult",
    "WeaviateObject",
    "WeaviateTransport",
    "collection_name_for_tenant",
    "sparse_collection_name_for_tenant",
]
