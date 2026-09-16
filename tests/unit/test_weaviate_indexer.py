"""Weaviate Dense generation 构建与 checkpoint 的失败优先测试。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import pytest

from enterprise_rag.retrieval.contracts import IndexProfile, WeaviateBatchResult
from enterprise_rag.retrieval.indexer import WeaviateDenseIndexer


class _Storage:
    """返回固定 manifest 的最小对象存储替身。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        """保存测试 manifest。"""

        self.payload = payload

    async def get(self, _key: str) -> bytes:
        """按生产路径返回稳定 JSON 字节。"""

        return json.dumps(self.payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


class _Embedding:
    """记录 child 文本并返回固定维度向量。"""

    def __init__(self, dimension: int = 2) -> None:
        """保存向量维度和调用记录。"""

        self.dimension = dimension
        self.calls: list[list[str]] = []

    async def embed(self, texts) -> list[list[float]]:
        """只为传入的 child 文本生成向量。"""

        values = list(texts)
        self.calls.append(values)
        return [[1.0] + [0.0] * (self.dimension - 1) for _ in values]


@dataclass
class _Generation:
    """测试用 generation 事实快照。"""

    generation_id: str
    tenant_id: str
    document_id: str
    version_id: str
    index_profile_key: str
    collection_name: str
    manifest_hash: str
    model_name: str
    dimension: int
    chunking_version: str
    schema_version: str
    expected_item_count: int
    actual_item_count: int = 0
    checkpoint: int = 0
    build_attempt: int = 1
    worker_lease: str = "lease-1"
    status: str = "building"


class _GenerationService:
    """记录 generation 查询、checkpoint 和 READY 标记。"""

    def __init__(self, generation: _Generation) -> None:
        """保存测试 generation。"""

        self.generation = generation
        self.checkpoints: list[int] = []
        self.validated = False

    async def get_generation(self, _session, generation_id: str) -> _Generation:
        """返回指定 generation，模拟事实源查询。"""

        assert generation_id == self.generation.generation_id
        return self.generation

    async def record_checkpoint(self, _session, generation_id: str, next_child_index: int) -> None:
        """保存批次完成后的下一个 child 下标。"""

        assert generation_id == self.generation.generation_id
        self.generation.checkpoint = next_child_index
        self.checkpoints.append(next_child_index)

    async def validate_generation(self, _session, generation_id: str, transport) -> _Generation:
        """委托给测试 transport 做 scope/count/identity 完整性检查。"""

        assert generation_id == self.generation.generation_id
        count = await transport.count_generation(
            self.generation.collection_name,
            generation_id=generation_id,
            version_id=self.generation.version_id,
        )
        if count != self.generation.expected_item_count:
            raise ValueError("generation 完整性检查失败")
        self.generation.actual_item_count = count
        self.generation.status = "ready"
        self.validated = True
        return self.generation


class _Transport:
    """保存 batch upsert 对象并支持可控的第 N 批失败。"""

    def __init__(self, *, fail_on_call: int | None = None) -> None:
        """初始化批次记录和失败注入点。"""

        self.fail_on_call = fail_on_call
        self.calls = 0
        self.objects: dict[str, Any] = {}

    async def upsert_batch(self, _collection_name: str, objects) -> WeaviateBatchResult:
        """按稳定 object_id 覆盖写入，模拟 Weaviate 幂等 upsert。"""

        self.calls += 1
        if self.fail_on_call == self.calls:
            raise RuntimeError("temporary batch failure")
        for item in objects:
            self.objects[item.object_id] = item
        return WeaviateBatchResult(accepted=len(objects))

    async def count_generation(self, _collection_name: str, *, generation_id: str, version_id: str) -> int:
        """统计 scope 内对象。"""

        return sum(
            1
            for item in self.objects.values()
            if item.properties["generation_id"] == generation_id
            and item.properties["version_id"] == version_id
        )

    async def list_chunk_ids(self, _collection_name: str, *, generation_id: str, version_id: str) -> set[str]:
        """返回 scope 内 chunk identity。"""

        return {
            item.properties["chunk_id"]
            for item in self.objects.values()
            if item.properties["generation_id"] == generation_id
            and item.properties["version_id"] == version_id
        }


def _payload() -> dict[str, Any]:
    """构造两个 child、一个 parent 的固定 manifest。"""

    return {
        "tenant_id": "tenant-a",
        "document_id": "doc-1",
        "version_id": "version-1",
        "chunks": {
            "strategy_version": "parent-child-v1",
            "parents": [
                {
                    "chunk_id": "parent-1",
                    "version_id": "version-1",
                    "text": "parent text",
                    "heading_path": ["Heading"],
                    "source_locators": [{"line": 1}],
                }
            ],
            "children": [
                {
                    "chunk_id": "child-1",
                    "parent_id": "parent-1",
                    "version_id": "version-1",
                    "text": "child one",
                    "heading_path": ["Heading"],
                    "source_locators": [{"line": 1}],
                },
                {
                    "chunk_id": "child-2",
                    "parent_id": "parent-1",
                    "version_id": "version-1",
                    "text": "child two",
                    "heading_path": ["Heading"],
                    "source_locators": [{"line": 2}],
                },
            ],
        },
    }


def _generation(payload: dict[str, Any], profile: IndexProfile) -> _Generation:
    """依据 manifest 构造与生产事实字段一致的 generation。"""

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return _Generation(
        generation_id="generation-1",
        tenant_id="tenant-a",
        document_id="doc-1",
        version_id="version-1",
        index_profile_key=profile.key,
        collection_name="DenseTenant_demo",
        manifest_hash=hashlib.sha256(raw).hexdigest(),
        model_name=profile.model_name,
        dimension=profile.dimension,
        chunking_version=profile.chunking_version,
        schema_version=profile.schema_version,
        expected_item_count=2,
    )


@pytest.mark.asyncio
async def test_build_generation_embeds_children_and_writes_parent_metadata() -> None:
    """构建只 embed child，并把 parent 文本/locator 固化到 Weaviate object。"""

    payload = _payload()
    profile = IndexProfile("demo-model", 2, "parent-child-v1", "dense-v1")
    generation = _generation(payload, profile)
    embedding = _Embedding()
    transport = _Transport()
    service = _GenerationService(generation)
    indexer = WeaviateDenseIndexer(embedding, transport, _Storage(payload), service)

    result = await indexer.build_generation(
        object(),
        generation_id="generation-1",
        tenant_id="tenant-a",
        document_id="doc-1",
        version_id="version-1",
        profile=profile,
        parsed_object_key="parsed.json",
        batch_size=2,
    )

    assert result.actual_item_count == 2
    assert embedding.calls == [["child one", "child two"]]
    objects = list(transport.objects.values())
    assert objects[0].properties["parent_text"] == "parent text"
    assert objects[0].properties["source_locators"] == [{"line": 1}]
    assert service.checkpoints == [2]


@pytest.mark.asyncio
async def test_failed_batch_keeps_building_and_resume_starts_at_checkpoint() -> None:
    """第二批失败时保留 BUILDING，重试只提交 checkpoint 之后的 child。"""

    payload = _payload()
    profile = IndexProfile("demo-model", 2, "parent-child-v1", "dense-v1")
    generation = _generation(payload, profile)
    generation.expected_item_count = 2
    transport = _Transport(fail_on_call=2)
    service = _GenerationService(generation)
    indexer = WeaviateDenseIndexer(_Embedding(), transport, _Storage(payload), service)

    with pytest.raises(RuntimeError, match="temporary batch failure"):
        await indexer.build_generation(
            object(),
            generation_id="generation-1",
            tenant_id="tenant-a",
            document_id="doc-1",
            version_id="version-1",
            profile=profile,
            parsed_object_key="parsed.json",
            batch_size=1,
        )

    assert generation.status == "building"
    assert generation.checkpoint == 1

    transport.fail_on_call = None
    second_embedding = _Embedding()
    resumed = WeaviateDenseIndexer(second_embedding, transport, _Storage(payload), service)
    await resumed.build_generation(
        object(),
        generation_id="generation-1",
        tenant_id="tenant-a",
        document_id="doc-1",
        version_id="version-1",
        profile=profile,
        parsed_object_key="parsed.json",
        batch_size=1,
    )

    assert second_embedding.calls == [["child two"]]
    assert len(transport.objects) == 2


@pytest.mark.asyncio
async def test_manifest_scope_or_hash_mismatch_is_rejected_before_write() -> None:
    """manifest scope/hash 不匹配时必须在调用 Weaviate 前失败。"""

    payload = _payload()
    payload["tenant_id"] = "tenant-b"
    profile = IndexProfile("demo-model", 2, "parent-child-v1", "dense-v1")
    generation = _generation(_payload(), profile)
    transport = _Transport()
    indexer = WeaviateDenseIndexer(
        _Embedding(), transport, _Storage(payload), _GenerationService(generation)
    )

    with pytest.raises(ValueError, match="索引范围"):
        await indexer.build_generation(
            object(),
            generation_id="generation-1",
            tenant_id="tenant-a",
            document_id="doc-1",
            version_id="version-1",
            profile=profile,
            parsed_object_key="parsed.json",
            batch_size=2,
        )
    assert transport.objects == {}
