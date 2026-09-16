"""显式 Weaviate repository 注入时的检索 API 兼容测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

from enterprise_rag.api.app import create_app
from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.registry import ProviderRegistry
from enterprise_rag.retrieval.service import RetrievalResult
from enterprise_rag.retrieval.weaviate import WeaviateTransportError


class _Database:
    """提供 API 所需的最小只读 session 边界。"""

    @asynccontextmanager
    async def session(self):
        """返回不参与本测试断言的固定 session。"""

        yield object()


class _Embedding:
    """为 query 返回固定二维向量。"""

    async def embed(self, texts):
        """为每个问题返回相同确定性向量。"""

        return [[1.0, 0.0] for _ in texts]


class _WeaviateRepository:
    """记录显式注入查询，并返回一个合法的 Parent 证据。"""

    def __init__(self, *, fail: bool = False) -> None:
        """保存是否模拟未发布 generation。"""

        self.fail = fail
        self.calls: list[tuple[str, str, int]] = []

    async def search(self, _session, tenant_id, version_id, _query, top_k):
        """记录 tenant/version/top_k，失败时使用稳定 transport 错误。"""

        self.calls.append((tenant_id, version_id, top_k))
        if self.fail:
            raise WeaviateTransportError(
                "generation_unavailable",
                retryable=False,
                safe_message="Weaviate generation 未发布",
            )
        return [
            RetrievalResult(
                chunk_id="child-1",
                parent_id="parent-1",
                version_id=version_id,
                document_id="document-1",
                text="部署任务执行。",
                heading_path=("部署",),
                source_locators=(),
                score=0.9,
            )
        ]


def _app(repository: _WeaviateRepository):
    """构造显式 Weaviate repository 注入的应用。"""

    registry = ProviderRegistry()
    registry.register(
        "local_answer",
        FakeChatProvider(
            lambda _request: '{"answer_status":"answered","message":"部署任务执行。","excerpts":[]}'
        ),
        capabilities={"chat.complete"},
    )
    app = create_app(
        provider_registry=registry,
        embedding_provider=_Embedding(),
        weaviate_vector_repository=repository,
    )
    app.state.database = _Database()
    return app


def test_explicit_weaviate_injection_keeps_retrieval_api_contract() -> None:
    """显式注入只替换 repository，既有请求/响应字段保持不变。"""

    repository = _WeaviateRepository()
    with TestClient(_app(repository)) as client:
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
    assert repository.calls == [("tenant-a", "version-1", 3)]
    assert {"request_id", "trace_id", "context_source", "answer_status", "citations"} <= response.json().keys()


def test_unpublished_generation_fails_closed_without_leaking_transport_message() -> None:
    """未发布 generation 的稳定错误应映射为 503，不能回显 transport 细节。"""

    repository = _WeaviateRepository(fail=True)
    with TestClient(_app(repository)) as client:
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
