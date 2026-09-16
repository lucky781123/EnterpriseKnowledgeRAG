"""方案 B 的检索问答 API 契约测试。

这些测试使用确定性 Embedding、Repository 和 Chat Provider，只证明 HTTP 编排、
tenant/version 范围、Parent 回溯与 F2-G 复用契约，不把 Fake 结果当成真实检索质量证据。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from enterprise_rag.api.app import create_app
from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.registry import ProviderRegistry
from enterprise_rag.retrieval.service import RetrievalResult


class _FakeDatabase:
    """只提供只读 session 边界的数据库替身。"""

    def __init__(self) -> None:
        """创建可用于校验 Repository 调用参数的固定 session 对象。"""

        self.session_value = object()

    @asynccontextmanager
    async def session(self):
        """返回固定只读 session，不执行真实数据库操作。"""

        yield self.session_value


class _RecordingEmbeddingProvider:
    """记录查询文本并返回固定二维向量。"""

    def __init__(self) -> None:
        """初始化空调用记录。"""

        self.calls: list[list[str]] = []

    async def embed(self, texts) -> list[list[float]]:
        """保存一次批量调用，并为唯一问题返回确定性向量。"""

        normalized = list(texts)
        self.calls.append(normalized)
        return [[1.0, 0.0] for _ in normalized]


class _RecordingVectorRepository:
    """返回固定检索结果并记录完整范围参数。"""

    def __init__(self, results: list[RetrievalResult]) -> None:
        """保存待返回结果和空调用记录。"""

        self.results = results
        self.calls: list[tuple[Any, str, str, tuple[float, ...], int]] = []

    async def search(
        self,
        session,
        tenant_id: str,
        version_id: str,
        query,
        top_k: int,
    ) -> list[RetrievalResult]:
        """记录 session、tenant/version、查询向量和 top_k 后返回固定结果。"""

        self.calls.append((session, tenant_id, version_id, tuple(query), top_k))
        return list(self.results)


def _build_app(
    *,
    results: list[RetrievalResult],
    chat_response: str,
) -> tuple[Any, _FakeDatabase, _RecordingEmbeddingProvider, _RecordingVectorRepository, FakeChatProvider]:
    """装配检索入口测试所需的全部确定性依赖。"""

    chat_provider = FakeChatProvider(lambda _request: chat_response)
    registry = ProviderRegistry()
    registry.register("local_answer", chat_provider, capabilities={"chat.complete"})
    embedding_provider = _RecordingEmbeddingProvider()
    repository = _RecordingVectorRepository(results)
    app = create_app(
        provider_registry=registry,
        embedding_provider=embedding_provider,
        vector_repository=repository,
    )
    # 测试只验证路由使用 app.state 中的只读数据库边界，不连接真实 PostgreSQL。
    database = _FakeDatabase()
    app.state.database = database
    return app, database, embedding_provider, repository, chat_provider


def test_retrieval_query_runs_single_embedding_and_reuses_parent_evidence() -> None:
    """入口应限定 tenant/version、去重 Parent，并把 Parent 正文交给 F2-G。"""

    results = [
        RetrievalResult(
            chunk_id="child-low",
            parent_id="parent-1",
            version_id="version-1",
            document_id="document-1",
            text="低分 Parent 正文",
            heading_path=("部署",),
            source_locators=(),
            score=0.5,
        ),
        RetrievalResult(
            chunk_id="child-high",
            parent_id="parent-1",
            version_id="version-1",
            document_id="document-1",
            text="高分 Parent 正文，由部署任务执行。",
            heading_path=("部署",),
            source_locators=(),
            score=0.9,
        ),
        RetrievalResult(
            chunk_id="child-2",
            parent_id="parent-2",
            version_id="version-1",
            document_id="document-2",
            text="第二条 Parent 正文",
            heading_path=("运行",),
            source_locators=(),
            score=0.8,
        ),
    ]
    chat_response = json.dumps(
        {
            "answer_status": "answered",
            "message": "由部署任务执行",
            "excerpts": [
                {
                    "evidence_id": "retrieved-1",
                    "quote": "部署任务",
                }
            ],
        },
        ensure_ascii=False,
    )
    app, database, embedding, repository, chat_provider = _build_app(
        results=results,
        chat_response=chat_response,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/retrieval/query",
            json={
                "tenant_id": "tenant-a",
                "version_id": "version-1",
                "question": "谁执行部署？",
                "top_k": 3,
            },
        )

    assert response.status_code == 200
    assert embedding.calls == [["谁执行部署？"]]
    assert repository.calls == [
        (database.session_value, "tenant-a", "version-1", (1.0, 0.0), 3)
    ]
    assert len(chat_provider.calls) == 1
    prompt = chat_provider.calls[0].messages[1].content
    assert "高分 Parent 正文，由部署任务执行。" in prompt
    assert "第二条 Parent 正文" in prompt
    assert "低分 Parent 正文" not in prompt

    body = response.json()
    assert body["context_source"] == "retrieved"
    assert body["citation_validation"] == "passed"
    assert body["citations"] == [
        {
            "evidence_id": "retrieved-1",
            "quote": "部署任务",
            "start": 14,
            "end": 18,
            "source": "document-1#child-high",
        }
    ]


def test_retrieval_query_returns_404_without_calling_chat_when_no_evidence() -> None:
    """空检索结果应稳定返回 404，且不能消耗 Chat Provider。"""

    app, _database, embedding, repository, chat_provider = _build_app(
        results=[],
        chat_response='{"answer_status":"not_found","message":"资料不足","excerpts":[]}',
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/retrieval/query",
            json={
                "tenant_id": "tenant-a",
                "version_id": "version-1",
                "question": "问题",
            },
        )

    assert response.status_code == 404
    assert response.json() == {
        "error": {"kind": "evidence_not_found", "message": "未检索到可用于回答的证据"}
    }
    assert embedding.calls == [["问题"]]
    assert len(repository.calls) == 1
    assert chat_provider.calls == []


@pytest.mark.parametrize(
    "missing_dependency",
    ["database", "embedding_provider", "vector_repository"],
)
def test_retrieval_query_returns_503_when_dependency_is_missing(missing_dependency: str) -> None:
    """数据库、Embedding 或 Repository 任一缺失时都应 fail closed。"""

    app, _database, _embedding, _repository, chat_provider = _build_app(
        results=[],
        chat_response='{"answer_status":"not_found","message":"资料不足","excerpts":[]}',
    )
    setattr(app.state, missing_dependency, None)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/retrieval/query",
            json={
                "tenant_id": "tenant-a",
                "version_id": "version-1",
                "question": "问题",
            },
        )

    assert response.status_code == 503
    assert response.json() == {
        "error": {"kind": "retrieval_unavailable", "message": "检索依赖未就绪"}
    }
    assert chat_provider.calls == []


@pytest.mark.parametrize("forbidden_field", ["evidence", "base_url", "api_key"])
def test_retrieval_query_rejects_caller_supplied_context_and_provider_fields(
    forbidden_field: str,
) -> None:
    """调用方不能绕过检索提交 evidence、endpoint 或密钥。"""

    app, _database, _embedding, _repository, _chat_provider = _build_app(
        results=[],
        chat_response='{"answer_status":"not_found","message":"资料不足","excerpts":[]}',
    )
    payload: dict[str, Any] = {
        "tenant_id": "tenant-a",
        "version_id": "version-1",
        "question": "问题",
        forbidden_field: "forbidden",
    }

    with TestClient(app) as client:
        response = client.post("/api/v1/retrieval/query", json=payload)

    assert response.status_code == 422
    assert response.json() == {
        "error": {"kind": "invalid_request", "message": "请求参数不符合接口契约"}
    }
