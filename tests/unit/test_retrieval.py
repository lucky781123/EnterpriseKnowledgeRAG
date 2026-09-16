"""方案 B 最小 Dense 检索闭环的失败测试。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from enterprise_rag.db.models import (
    DocumentModel,
    DocumentVersionModel,
    EmbeddingRecordModel,
    TenantModel,
)
from enterprise_rag.db.session import Database
from enterprise_rag.embedding.ollama import OllamaEmbeddingProvider
from enterprise_rag.retrieval.indexer import DenseIndexer
from enterprise_rag.retrieval.repository import PostgresVectorRepository
from enterprise_rag.retrieval.service import DenseRetrievalService, EmbeddingRecord


class _ManifestStorage:
    """只返回固定 parsed manifest 的最小对象存储替身。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        """保存待返回的 manifest，避免测试依赖文件系统。"""

        self.payload = payload

    async def get(self, _key: str) -> bytes:
        """按生产对象存储协议返回 UTF-8 JSON 字节。"""

        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class _RecordingEmbeddingProvider:
    """记录实际送入 embedding 的 child 文本。"""

    def __init__(self) -> None:
        """初始化空调用记录。"""

        self.calls: list[list[str]] = []

    async def embed(self, texts) -> list[list[float]]:
        """为每条 child 返回固定二维向量，并保留调用参数供断言。"""

        values = list(texts)
        self.calls.append(values)
        return [[1.0, 0.0] for _ in values]


class _RecordingRepository:
    """记录 indexer 交给持久化层的版本范围与向量记录。"""

    def __init__(self) -> None:
        """初始化尚未发生替换的状态。"""

        self.replacement: tuple[str, str, str, tuple[EmbeddingRecord, ...]] | None = None

    async def replace_version(
        self,
        _session,
        *,
        tenant_id: str,
        document_id: str,
        version_id: str,
        records,
    ) -> int:
        """保存一次版本级替换，模拟真实 repository 的返回值。"""

        normalized = tuple(records)
        self.replacement = (tenant_id, document_id, version_id, normalized)
        return len(normalized)


@pytest.mark.asyncio
async def test_ollama_embedding_parses_embeddings_and_checks_dimension() -> None:
    """Provider 应解析 Ollama 返回值，并拒绝维度不一致的响应。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embed"
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0], [0.0, 1.0]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OllamaEmbeddingProvider(client, "http://ollama", "demo")
        result = await provider.embed(["a", "b"])

    assert result == [[1.0, 0.0], [0.0, 1.0]]
    with pytest.raises(ValueError, match="维度"):
        provider.validate_dimension([[1.0, 0.0], [0.0]])


def test_dense_retrieval_filters_tenant_and_version_and_returns_parent_evidence() -> None:
    """检索必须隔离 tenant/version，并按 cosine 相似度返回 child 与 parent。"""

    records = [
        EmbeddingRecord(
            "t1", "d1", "v1", "c1", "p1", "alpha", ("A",), (), (1.0, 0.0), "parent alpha"
        ),
        EmbeddingRecord(
            "t1", "d1", "v1", "c2", "p1", "beta", ("A",), (), (0.9, 0.1), "parent alpha"
        ),
        EmbeddingRecord(
            "t2", "d2", "v1", "c3", "p3", "secret", (), (), (1.0, 0.0), "private parent"
        ),
    ]
    service = DenseRetrievalService(records)
    results = service.search("t1", "v1", (1.0, 0.0), top_k=2)

    assert [item.chunk_id for item in results] == ["c1", "c2"]
    assert results[0].parent_id == "p1"
    assert results[0].text == "parent alpha"


def test_dense_retrieval_missing_parent_falls_back_to_child_without_cross_version_lookup() -> None:
    """同 ID Parent 只存在于其他版本时，当前版本必须稳定回退 child 文本。"""

    records = [
        EmbeddingRecord("t1", "d1", "v1", "c1", "p1", "child-v1", (), (), (1.0, 0.0)),
        EmbeddingRecord(
            "t1",
            "d1",
            "v2",
            "c2",
            "p1",
            "child-v2",
            (),
            (),
            (1.0, 0.0),
            "parent-secret-v2",
        ),
    ]

    result = DenseRetrievalService(records).search("t1", "v1", (1.0, 0.0), top_k=1)

    assert result[0].text == "child-v1"
    assert "secret" not in result[0].text


@pytest.mark.asyncio
async def test_indexer_reads_real_nested_manifest_and_materializes_parent_evidence() -> None:
    """Indexer 应读取真实 ``chunks`` 层级，只 embed child，并固化同版本 Parent 文本。"""

    payload = {
        "tenant_id": "t1",
        "document_id": "d1",
        "version_id": "v1",
        "chunks": {
            "strategy_version": "parent-child-v1",
            "parent_size": 800,
            "child_size": 320,
            "parents": [
                {
                    "chunk_id": "p1",
                    "parent_id": None,
                    "version_id": "v1",
                    "text": "完整 Parent 证据",
                }
            ],
            "children": [
                {
                    "chunk_id": "c1",
                    "parent_id": "p1",
                    "version_id": "v1",
                    "text": "child 片段",
                    "heading_path": ["标题"],
                    "source_locators": [{"kind": "line", "start": 1, "end": 2}],
                }
            ],
        },
    }
    provider = _RecordingEmbeddingProvider()
    repository = _RecordingRepository()
    indexer = DenseIndexer(provider, repository, _ManifestStorage(payload))

    count = await indexer.index_manifest(
        object(),
        tenant_id="t1",
        document_id="d1",
        version_id="v1",
        parsed_object_key="parsed/t1/d1/v1.json",
    )

    assert count == 1
    assert provider.calls == [["child 片段"]]
    assert repository.replacement is not None
    scope = repository.replacement[:3]
    records = repository.replacement[3]
    assert scope == ("t1", "d1", "v1")
    assert records[0].text == "child 片段"
    assert records[0].parent_text == "完整 Parent 证据"


@pytest.mark.asyncio
async def test_empty_manifest_replaces_version_and_deletes_stale_vectors(tmp_path) -> None:
    """空 child 清单仍需执行版本级替换，确保重建后没有旧向量残留。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'empty-replace.db'}")
    await database.migrate()
    try:
        async with database.transaction() as session:
            session.add(TenantModel(id="t1", name="租户一"))
            session.add(DocumentModel(tenant_id="t1", id="d1", source_id="s1", title="文档一"))
            session.add(
                DocumentVersionModel(
                    tenant_id="t1", document_id="d1", id="v1", content_hash="hash-v1"
                )
            )

        # 先提交基础 lineage，再写入向量记录，明确模拟线上已完成版本持久化后的索引阶段。
        async with database.transaction() as session:
            session.add(
                EmbeddingRecordModel(
                    tenant_id="t1",
                    document_id="d1",
                    version_id="v1",
                    chunk_id="c1",
                    parent_id="p1",
                    text="旧 child",
                    parent_text="旧 parent",
                    heading_path=[],
                    source_locators=[],
                    embedding=[1.0, 0.0],
                    dimension=2,
                )
            )

        async with database.transaction() as session:
            deleted_count = await PostgresVectorRepository().replace_version(
                session,
                tenant_id="t1",
                document_id="d1",
                version_id="v1",
                records=(),
            )

        async with database.session() as session:
            remaining = (await session.scalars(select(EmbeddingRecordModel))).all()

        assert deleted_count == 0
        assert remaining == []
    finally:
        await database.dispose()
