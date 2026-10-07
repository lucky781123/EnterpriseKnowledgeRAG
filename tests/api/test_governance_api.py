"""API 治理边界测试。"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

from fastapi.testclient import TestClient

from enterprise_rag.api.app import create_app
from enterprise_rag.config.settings import Settings
from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.registry import ProviderRegistry
from enterprise_rag.retrieval.hybrid import HybridCandidate
from enterprise_rag.retrieval.product import ProductRetrievalResult, ProductRetrievalTrace
from enterprise_rag.retrieval.service import RetrievalResult
from enterprise_rag.security.authorization import ResourceNotFound
from enterprise_rag.security.dev_token import DevTokenCodec
from enterprise_rag.security.governance import TenantGovernance


class _Database:
    """提供产品查询所需的最小异步 session。"""

    class _Session:
        """实现异步上下文管理器协议。"""

        async def __aenter__(self):
            """返回测试 session 占位对象。"""

            return object()

        async def __aexit__(self, *_args):
            """结束测试 session。"""

            return False

    def session(self):
        """返回新的测试 session 上下文。"""

        return self._Session()


class _Authorization:
    """对测试候选返回稳定的当前版本授权事实。"""

    async def require_document_read(self, _session, _context, document_id: str):
        """允许固定文档继续出站。"""

        if document_id != "doc-1":
            raise ResourceNotFound("资源不存在")
        return type("AuthorizedDocument", (), {"document_id": document_id, "version_id": "version-1"})()


@dataclass
class _RetrievalService:
    """返回一条可被引用校验的授权证据。"""

    result: ProductRetrievalResult

    async def search(self, _session, _context, *, question, candidate_k, top_k):
        """返回固定检索结果并校验服务端预算参数。"""

        assert question
        assert candidate_k >= top_k
        return self.result


def _candidate() -> HybridCandidate:
    """构造产品问答需要的最小 Parent 候选。"""

    return HybridCandidate(
        result=RetrievalResult(
            chunk_id="chunk-1",
            parent_id="parent-1",
            version_id="version-1",
            document_id="doc-1",
            text="部署任务执行。",
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


def _app(*, governance: TenantGovernance):
    """装配使用 Fake Provider、开发 token 和固定检索结果的测试应用。"""

    registry = ProviderRegistry()
    registry.register(
        "local_answer",
        FakeChatProvider(
            lambda _request: json.dumps(
                {
                    "answer_status": "answered",
                    "message": "部署任务执行。",
                    "excerpts": [{"evidence_id": "retrieved-1", "quote": "部署任务执行"}],
                },
                ensure_ascii=False,
            )
        ),
        capabilities={"chat.complete", "chat.stream"},
    )
    codec = DevTokenCodec("x" * 32, ttl_seconds=3600)
    app = create_app(
        provider_registry=registry,
        dev_token_codec=codec,
        database=_Database(),
        product_retrieval_service=_RetrievalService(
            ProductRetrievalResult(
                candidates=(_candidate(),),
                trace=ProductRetrievalTrace.empty(),
            )
        ),
        authorization_service=_Authorization(),
        tenant_governance=governance,
    )
    token = codec.issue(actor_id="alice", tenant_id="tenant-a", roles={"reader"})
    return app, token


def test_oidc_enabled_requires_verifier_and_membership_resolver() -> None:
    """OIDC 打开但没有可信 verifier/membership 时必须 fail closed。"""

    settings = replace(
        Settings.from_env(),
        oidc_enabled=True,
        oidc_issuer="https://issuer.example",
        oidc_audience="rag-api",
        oidc_jwks_url="https://issuer.example/.well-known/jwks.json",
    )
    registry = ProviderRegistry()
    registry.register("local_answer", FakeChatProvider(), capabilities={"chat.complete"})

    try:
        create_app(settings=settings, provider_registry=registry)
    except ValueError as error:
        assert str(error) == "OIDC_ENABLED=true 时必须注入 verifier 和 membership_resolver"
    else:
        raise AssertionError("OIDC 缺少可信身份依赖时不应创建应用")


def test_tenant_quota_returns_429_and_governance_endpoint_is_redacted() -> None:
    """租户查询超过配额返回429，治理接口只返回固定统计而不泄露输入。"""

    app, token = _app(governance=TenantGovernance(requests_per_minute=1))
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        first = client.post("/api/v1/rag/query", headers=headers, json={"question": "问题"})
        second = client.post("/api/v1/rag/query", headers=headers, json={"question": "第二个问题"})
        governance = client.get("/health/governance?token=do-not-leak")

    assert first.status_code == 200
    assert first.json()["groundedness"] == "grounded"
    assert second.status_code == 429
    assert second.json() == {
        "error": {"kind": "tenant_quota_exceeded", "message": "租户查询频率受限"}
    }
    assert governance.status_code == 200
    assert "do-not-leak" not in governance.text
    assert "问题" not in governance.text
    assert governance.json()["audit_events"] >= 2
