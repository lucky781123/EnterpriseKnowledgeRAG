"""本地 P0 产品问答和引用复核 API 测试。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from fastapi.testclient import TestClient

from enterprise_rag.api.app import create_app
from enterprise_rag.config.settings import Settings
from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.registry import ProviderRegistry
from enterprise_rag.retrieval.hybrid import HybridCandidate
from enterprise_rag.retrieval.product import ProductRetrievalResult, ProductRetrievalTrace
from enterprise_rag.retrieval.service import RetrievalResult
from enterprise_rag.security.dev_token import DevTokenCodec
from enterprise_rag.security.authorization import ResourceNotFound


class _Database:
    """提供产品路由所需的最小 session 上下文。"""

    class _Session:
        """模拟异步上下文管理器。"""

        async def __aenter__(self):
            """返回固定 session 对象。"""

            return object()

        async def __aexit__(self, *_args):
            """结束固定 session。"""

            return False

    def session(self):
        """返回最小异步 session 上下文。"""

        return self._Session()


class _Authorization:
    """让 API 测试验证引用复核调用，而不连接真实 PostgreSQL。"""

    def __init__(self, *, revoked: bool = False) -> None:
        """保存是否模拟引用返回前的撤权。"""

        self.revoked = revoked

    async def require_document_read(self, _session, _context, document_id: str):
        """返回当前版本的授权文档事实。"""

        if self.revoked:
            raise ResourceNotFound("资源不存在")
        return type("AuthorizedDocument", (), {"document_id": document_id, "version_id": "version-1"})()


@dataclass
class _RetrievalService:
    """返回固定授权 Parent 的产品检索替身。"""

    result: ProductRetrievalResult
    calls: int = 0

    async def search(self, _session, _context, *, question, candidate_k, top_k):
        """记录调用参数并返回预先构造的结果。"""

        self.calls += 1
        assert question
        assert candidate_k >= top_k
        return self.result


def _candidate() -> HybridCandidate:
    """构造一条可供模型引用的 Parent 证据。"""

    return HybridCandidate(
        result=RetrievalResult(
            chunk_id="chunk-1",
            parent_id="parent-1",
            version_id="version-1",
            document_id="doc-1",
            text="由部署任务执行。",
            heading_path=("部署",),
            source_locators=({"line": 1},),
            score=0.9,
        ),
        rank=1,
        rrf_score=0.03,
        dense_rank=1,
        sparse_rank=1,
        rerank_score=0.9,
    )


def _app(
    *,
    chat_response: str,
    retrieval_result: ProductRetrievalResult,
    revoked: bool = False,
):
    """装配产品问答测试应用和开发身份 token。"""

    registry = ProviderRegistry()
    provider = FakeChatProvider(lambda _request: chat_response)
    registry.register("local_answer", provider, capabilities={"chat.complete"})
    codec = DevTokenCodec("x" * 32, ttl_seconds=3600)
    settings = Settings.from_env()
    app = create_app(
        settings=settings,
        provider_registry=registry,
        dev_token_codec=codec,
        product_retrieval_service=_RetrievalService(retrieval_result),
        authorization_service=_Authorization(revoked=revoked),
    )
    app.state.database = _Database()
    token = codec.issue(
        actor_id="user-alice",
        tenant_id="tenant-a",
        roles={"reader"},
    )
    return app, token, provider


def test_product_rag_query_returns_answer_and_safe_retrieval_trace() -> None:
    """产品入口从服务端身份取得 scope，并返回引用和不含内部 key 的 trace。"""

    candidate = _candidate()
    result = ProductRetrievalResult(
        candidates=(candidate,),
        trace=ProductRetrievalTrace(
            scopes=1,
            dense_hits=1,
            sparse_hits=1,
            fused_candidates=1,
            reranked_candidates=1,
            parent_candidates=1,
            rrf_k=60,
            reranker_used=True,
        ),
    )
    chat_response = json.dumps(
        {
            "answer_status": "answered",
            "message": "由部署任务执行。",
            "excerpts": [{"evidence_id": "retrieved-1", "quote": "部署任务"}],
        },
        ensure_ascii=False,
    )
    app, token, provider = _app(chat_response=chat_response, retrieval_result=result)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/rag/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"question": "谁执行部署？", "top_k": 1, "candidate_k": 5},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["context_source"] == "retrieved"
    assert body["citation_validation"] == "passed"
    assert body["citations"][0]["source"] == "doc-1#chunk-1"
    assert body["retrieval_trace"]["rrf_k"] == 60
    assert "collection" not in json.dumps(body["retrieval_trace"])
    assert len(provider.calls) == 1


def test_product_rag_query_returns_404_without_evidence_and_skips_llm() -> None:
    """没有授权证据时不能调用模型。"""

    result = ProductRetrievalResult(candidates=(), trace=ProductRetrievalTrace.empty())
    app, token, provider = _app(
        chat_response='{"answer_status":"not_found","message":"资料不足","excerpts":[]}',
        retrieval_result=result,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/rag/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"question": "问题"},
        )

    assert response.status_code == 404
    assert response.json() == {
        "error": {"kind": "evidence_not_found", "message": "未检索到可用于回答的证据"}
    }
    assert provider.calls == []


def test_product_rag_query_hides_revoked_citation_and_does_not_return_answer() -> None:
    """引用出站前撤权时返回 404，不能把模型回答和旧引用继续返回。"""

    candidate = _candidate()
    result = ProductRetrievalResult(
        candidates=(candidate,),
        trace=ProductRetrievalTrace(
            scopes=1,
            dense_hits=1,
            sparse_hits=1,
            fused_candidates=1,
            reranked_candidates=1,
            parent_candidates=1,
            rrf_k=60,
            reranker_used=True,
        ),
    )
    app, token, provider = _app(
        chat_response=json.dumps(
            {
                "answer_status": "answered",
                "message": "由部署任务执行。",
                "excerpts": [{"evidence_id": "retrieved-1", "quote": "部署任务"}],
            },
            ensure_ascii=False,
        ),
        retrieval_result=result,
        revoked=True,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/rag/query",
            headers={"Authorization": f"Bearer {token}"},
            json={"question": "谁执行部署？"},
        )

    assert response.status_code == 404
    assert response.json() == {
        "error": {"kind": "evidence_not_found", "message": "引用对应的资源不可见"}
    }
    assert len(provider.calls) == 1


def test_product_rag_query_rejects_client_scope_and_evidence_fields() -> None:
    """产品请求不允许调用方伪造 tenant、version 或 evidence。"""

    result = ProductRetrievalResult(candidates=(), trace=ProductRetrievalTrace.empty())
    app, token, _provider = _app(
        chat_response='{"answer_status":"not_found","message":"资料不足","excerpts":[]}',
        retrieval_result=result,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/rag/query",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "question": "问题",
                "tenant_id": "attacker-tenant",
                "version_id": "attacker-version",
                "evidence": [],
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {"kind": "invalid_request", "message": "请求参数不符合接口契约"}
    }
