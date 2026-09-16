"""BM25F Spike 的确定性字段投影和 fake transport 契约测试。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from enterprise_rag.retrieval.sparse import (
    SparseFieldProjector,
    SparseProfile,
)


def _payload() -> dict[str, Any]:
    """构造包含标题、错误码、API 名称和 parent lineage 的固定 manifest。"""

    return {
        "tenant_id": "tenant-a",
        "document_id": "doc-1",
        "version_id": "version-1",
        "chunks": {
            "children": [
                {
                    "chunk_id": "child-1",
                    "parent_id": "parent-1",
                    "version_id": "version-1",
                    "text": "调用 CreateUser API 返回 ERR_401，路径 /v1/users。",
                    "heading_path": ["账号", "创建用户"],
                    "source_locators": [{"line": 3}],
                }
            ],
            "parents": [
                {
                    "chunk_id": "parent-1",
                    "version_id": "version-1",
                    "text": "创建用户接口说明",
                    "heading_path": ["账号", "创建用户"],
                    "source_locators": [{"line": 1}],
                }
            ],
        },
    }


def _profile() -> SparseProfile:
    """返回固定的首版 BM25F profile。"""

    return SparseProfile(
        tokenizer_version="tokenizer-v1",
        preprocessing_version="preprocess-v1",
        schema_version="sparse-v1",
        field_weights={
            "title": 2.0,
            "section_title": 2.0,
            "api_symbols": 3.0,
            "body": 1.0,
        },
    )


def test_projector_extracts_stable_bm25f_fields_and_identity() -> None:
    """字段投影必须保留标题、技术标识符、lineage，并生成稳定 object identity。"""

    projector = SparseFieldProjector(_profile())

    first = projector.project_manifest(
        _payload(),
        generation_id="generation-1",
        tenant_id="tenant-a",
        document_id="doc-1",
        version_id="version-1",
    )
    second = projector.project_manifest(
        _payload(),
        generation_id="generation-1",
        tenant_id="tenant-a",
        document_id="doc-1",
        version_id="version-1",
    )

    assert first == second
    assert len(first) == 1
    item = first[0]
    assert item.object_id == SparseFieldProjector.object_id(
        "generation-1", "child-1", _profile().key
    )
    assert item.properties["title"] == "账号"
    assert item.properties["section_title"] == "账号 > 创建用户"
    assert "CreateUser" in item.properties["api_symbols"]
    assert "ERR_401" in item.properties["api_symbols"]
    assert "/v1/users" in item.properties["api_symbols"]
    assert item.properties["body"] == "调用 CreateUser API 返回 ERR_401，路径 /v1/users。"
    assert item.properties["parent_id"] == "parent-1"
    assert item.properties["source_locators"] == [{"line": 3}]


def test_projector_rejects_manifest_scope_mismatch() -> None:
    """manifest 顶层 scope 缺失或不完整时，不能生成跨租户稀疏对象。"""

    payload = _payload()
    payload["version_id"] = "version-2"

    with pytest.raises(ValueError, match="manifest scope"):
        SparseFieldProjector(_profile()).project_manifest(
            payload,
            generation_id="generation-1",
            tenant_id="tenant-a",
            document_id="doc-1",
            version_id="version-1",
        )


def test_projector_rejects_child_without_required_identity() -> None:
    """child 缺少 chunk identity 或正文时，整个投影必须失败。"""

    payload = _payload()
    del payload["chunks"]["children"][0]["chunk_id"]

    with pytest.raises(ValueError, match="child"):
        SparseFieldProjector(_profile()).project_manifest(
            payload,
            generation_id="generation-1",
            tenant_id="tenant-a",
            document_id="doc-1",
            version_id="version-1",
        )


@dataclass
class _FakeSparseTransport:
    """用内存保存 sparse object，模拟 batch upsert 的最小 contract。"""

    objects: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def upsert_batch(self, objects) -> int:
        """按 object identity 幂等写入并返回接受数量。"""

        for item in objects:
            self.objects[item.object_id] = item.properties
        return len(objects)


@pytest.mark.asyncio
async def test_projected_objects_can_be_sent_to_fake_sparse_transport() -> None:
    """投影结果必须能通过最小 fake transport 保留 generation 和 version scope。"""

    projector = SparseFieldProjector(_profile())
    objects = projector.project_manifest(
        _payload(),
        generation_id="generation-1",
        tenant_id="tenant-a",
        document_id="doc-1",
        version_id="version-1",
    )
    transport = _FakeSparseTransport()

    accepted = await transport.upsert_batch(objects)

    assert accepted == 1
    stored = next(iter(transport.objects.values()))
    assert stored["generation_id"] == "generation-1"
    assert stored["version_id"] == "version-1"
    assert stored["sparse_profile"] == _profile().key
