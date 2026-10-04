"""本地 P0 产品混合检索服务的行为测试。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from enterprise_rag.retrieval.contracts import IndexProfile
from enterprise_rag.retrieval.scope import AuthorizedRetrievalScope
from enterprise_rag.retrieval.service import RetrievalResult
from enterprise_rag.retrieval.sparse import SparseProfile
from enterprise_rag.security.context import RequestContext


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["dense", "sparse"])
async def test_multi_document_ranking_precedes_candidate_limit(channel, monkeypatch) -> None:
    """后加载文档的更高分命中必须进入融合，不能先按文档顺序截断候选。"""
    from enterprise_rag.retrieval.product import ProductHybridRetrievalService

    scopes = [
        _scope(),
        replace(_scope(), document_id="doc-2", version_id="version-2", generation_id="generation-2"),
    ]
    dense = _DenseTransport()
    sparse = _SparseTransport(_sparse_profile())
    authorization = _Authorization()

    async def dense_search(_collection, *, generation_id, version_id, **_kwargs):
        """模拟同一profile下两个文档的可比较向量分数。"""
        if channel != "dense":
            return []
        second = generation_id == "generation-2"
        return [RetrievalResult(
            chunk_id=f"chunk-{generation_id}", parent_id=f"parent-{generation_id}",
            version_id=version_id, document_id="doc-2" if second else "doc-1",
            text="公开测试条文", heading_path=(), source_locators=(),
            score=0.95 if second else 0.2,
        )]

    async def sparse_search(_collection, *, generation_id, version_id, tenant_id, **_kwargs):
        """模拟同一租户collection中两个文档的BM25F分数。"""
        if channel != "sparse":
            return []
        second = generation_id == "generation-2"
        return [{
            "chunk_id": f"chunk-{generation_id}", "parent_id": f"parent-{generation_id}",
            "version_id": version_id, "document_id": "doc-2" if second else "doc-1",
            "tenant_id": tenant_id, "generation_id": generation_id, "body": "公开测试条文",
            "_additional": {"score": 12.0 if second else 1.0},
        }]

    async def authorize(_session, _context, document_id):
        """保留两个文档的授权和版本复核，不绕过查询末端校验。"""
        return SimpleNamespace(version_id="version-2" if document_id == "doc-2" else "version-1")

    monkeypatch.setattr(dense, "search", dense_search)
    monkeypatch.setattr(sparse, "search", sparse_search)
    monkeypatch.setattr(authorization, "require_document_read", authorize)
    service = ProductHybridRetrievalService(
        scope_service=_ScopeService(scopes), embedding_provider=_Embedding(),
        dense_transport=dense, sparse_transport=sparse, reranker=_Reranker(),
        authorization_service=authorization, index_profile=_dense_profile(),
        sparse_profile=_sparse_profile(),
    )
    result = await service.search(
        object(), _context(), question="公开条文", candidate_k=1, top_k=1,
    )
    assert result.candidates[0].result.document_id == "doc-2"
    assert result.trace.scopes == 2
    assert result.trace.fused_candidates == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["both_channels", "authentication", "cancel"])
async def test_product_retrieval_does_not_hide_terminal_failures(failure, monkeypatch) -> None:
    """双路全失效、认证错误和主动取消必须停止，不进入无证据降级。"""

    from enterprise_rag.retrieval.product import ProductHybridRetrievalService
    from enterprise_rag.retrieval.weaviate import WeaviateTransportError

    dense = _DenseTransport()
    sparse = _SparseTransport(_sparse_profile())

    async def fail(*_args, **_kwargs):
        """按场景注入不同故障，验证可用性不能覆盖硬约束。"""
        if failure == "authentication":
            raise WeaviateTransportError(
                "authentication", retryable=False, safe_message="认证失败",
            )
        if failure == "cancel":
            raise asyncio.CancelledError
        raise httpx.ConnectError("dependency unavailable")

    monkeypatch.setattr(dense, "search", fail)
    if failure == "both_channels":
        monkeypatch.setattr(sparse, "search", fail)
    service = ProductHybridRetrievalService(
        scope_service=_ScopeService([_scope()]),
        embedding_provider=_Embedding(),
        dense_transport=dense,
        sparse_transport=sparse,
        reranker=_Reranker(),
        authorization_service=_Authorization(),
        index_profile=_dense_profile(),
        sparse_profile=_sparse_profile(),
    )
    expected = {
        "both_channels": RuntimeError,
        "authentication": WeaviateTransportError,
        "cancel": asyncio.CancelledError,
    }[failure]
    with pytest.raises(expected):
        await service.search(object(), _context(), question="部署", candidate_k=5, top_k=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_component", ["embedding", "dense", "sparse", "reranker"])
async def test_product_retrieval_falls_back_without_bypassing_acl(
    failed_component, monkeypatch,
) -> None:
    """单依赖故障保留可用授权证据，降级仍必须经过最终 ACL 复核。"""

    from enterprise_rag.retrieval.product import ProductHybridRetrievalService

    embedding = _Embedding()
    dense = _DenseTransport()
    sparse = _SparseTransport(_sparse_profile())
    reranker = _Reranker()
    authorization = _Authorization()

    async def fail(*args, **kwargs):
        """模拟可恢复的网络故障，不伪造任何检索证据。"""
        raise httpx.ConnectError("dependency unavailable")

    dependency = {"embedding": embedding, "dense": dense, "sparse": sparse, "reranker": reranker}
    method = {"embedding": "embed", "dense": "search", "sparse": "search", "reranker": "rerank"}
    monkeypatch.setattr(dependency[failed_component], method[failed_component], fail)
    service = ProductHybridRetrievalService(
        scope_service=_ScopeService([_scope()]),
        embedding_provider=embedding,
        dense_transport=dense,
        sparse_transport=sparse,
        reranker=reranker,
        authorization_service=authorization,
        index_profile=_dense_profile(),
        sparse_profile=_sparse_profile(),
    )
    result = await service.search(
        object(), _context(), question="谁执行部署？", candidate_k=5, top_k=2,
    )
    assert result.candidates
    assert authorization.calls
    assert result.trace.mode == "degraded"
    assert f"{failed_component}_unavailable" in result.trace.fallback_reasons
    if failed_component == "reranker":
        assert result.trace.reranker_used is False


@pytest.mark.asyncio
async def test_product_retrieval_rejects_scope_corruption(monkeypatch) -> None:
    """协议或授权范围错误不能被视为普通故障，也不能进入模型兜底。"""

    from enterprise_rag.retrieval.product import ProductHybridRetrievalService

    sparse = _SparseTransport(_sparse_profile())
    original = sparse.search

    async def corrupt(*args, **kwargs):
        """构造跨租户结果，验证安全校验不会被 fallback 吞掉。"""
        rows = await original(*args, **kwargs)
        rows[0]["tenant_id"] = "other-tenant"
        return rows

    monkeypatch.setattr(sparse, "search", corrupt)
    service = ProductHybridRetrievalService(
        scope_service=_ScopeService([_scope()]),
        embedding_provider=_Embedding(),
        dense_transport=_DenseTransport(),
        sparse_transport=sparse,
        reranker=_Reranker(),
        authorization_service=_Authorization(),
        index_profile=_dense_profile(),
        sparse_profile=_sparse_profile(),
    )
    with pytest.raises(ValueError, match="scope"):
        await service.search(object(), _context(), question="部署", candidate_k=5, top_k=2)


def _context() -> RequestContext:
    """构造测试用的已验证开发身份。"""

    return RequestContext(
        actor_id="user-alice",
        tenant_id="tenant-a",
        roles=frozenset({"reader"}),
        issued_at=1_789_600_000,
        expires_at=1_789_603_600,
        token_version=1,
    )


def _dense_profile() -> IndexProfile:
    """返回产品查询使用的固定 Dense profile。"""

    return IndexProfile("demo-embedding", 2, "parent-child-v1", "dense-v1")


def _sparse_profile() -> SparseProfile:
    """返回产品默认的 word + identity Sparse profile。"""

    return SparseProfile(
        tokenizer_version="word",
        preprocessing_version="identity",
        schema_version="sparse-v1",
        field_weights={
            "title": 2.0,
            "section_title": 2.0,
            "api_symbols": 3.0,
            "body": 1.0,
        },
    )


def _scope() -> AuthorizedRetrievalScope:
    """构造一个已经通过 PostgreSQL ACL 和 active pointer 校验的 scope。"""

    return AuthorizedRetrievalScope(
        tenant_id="tenant-a",
        document_id="doc-1",
        version_id="version-1",
        generation_id="generation-1",
        dense_collection_name="DenseTenant_demo",
        sparse_collection_name="SparseTenant_demo",
        sparse_profile_key=_sparse_profile().key,
    )


class _ScopeService:
    """返回固定 scope，并记录调用参数。"""

    def __init__(self, scopes: list[AuthorizedRetrievalScope]) -> None:
        """保存待返回的 scope。"""

        self.scopes = scopes
        self.calls = 0

    async def list_scopes(self, _session, _context, *, index_profile):
        """记录一次 scope 解析并返回授权结果。"""

        self.calls += 1
        assert index_profile == _dense_profile()
        return list(self.scopes)


class _Embedding:
    """只接受一次问题 embedding 调用的确定性替身。"""

    def __init__(self) -> None:
        """初始化调用记录。"""

        self.calls: list[list[str]] = []

    async def embed(self, texts):
        """记录问题并返回固定二维向量。"""

        values = list(texts)
        self.calls.append(values)
        return [[1.0, 0.0] for _ in values]


class _DenseTransport:
    """返回一个 Parent 命中和一个独立文档命中的 Dense 替身。"""

    def __init__(self) -> None:
        """初始化调用记录。"""

        self.calls: list[tuple[str, str, str, int]] = []

    async def search(self, collection_name, *, generation_id, version_id, query, top_k):
        """记录服务端 scope，并返回带 lineage 的 Dense 结果。"""

        self.calls.append((collection_name, generation_id, version_id, top_k))
        return [
            RetrievalResult(
                chunk_id="chunk-a",
                parent_id="parent-a",
                version_id=version_id,
                document_id="doc-1",
                text="Parent A 的正文，部署任务执行。",
                heading_path=("部署",),
                source_locators=({"line": 1},),
                score=0.9,
            ),
            RetrievalResult(
                chunk_id="chunk-b",
                parent_id="parent-b",
                version_id=version_id,
                document_id="doc-1",
                text="Parent B 的正文。",
                heading_path=("运行",),
                source_locators=({"line": 8},),
                score=0.8,
            ),
        ]


class _SparseTransport:
    """返回与 Dense 部分重叠的 BM25F 结果。"""

    def __init__(self, profile: SparseProfile) -> None:
        """保存 profile 以断言产品查询不接受请求方覆盖。"""

        self.profile = profile
        self.calls: list[tuple[str, str, str, str, int]] = []

    async def search(
        self,
        collection_name,
        *,
        profile,
        tenant_id,
        generation_id,
        version_id,
        query,
        top_k,
    ):
        """记录 scope 和问题，并返回 Weaviate BM25F 行。"""

        assert profile == self.profile
        self.calls.append((collection_name, tenant_id, generation_id, version_id, top_k))
        return [
            {
                "tenant_id": tenant_id,
                "document_id": "doc-1",
                "version_id": version_id,
                "generation_id": generation_id,
                "chunk_id": "chunk-a",
                "parent_id": "parent-a",
                "body": "Parent A 的正文，部署任务执行。",
                "heading_path": ["部署"],
                "source_locators": [{"line": 1}],
                "_additional": {"score": 12.0},
            }
        ]


class _Reranker:
    """返回原候选顺序的确定性 Reranker。"""

    def __init__(self) -> None:
        """初始化调用记录。"""

        self.calls = 0

    async def health_check(self) -> bool:
        """报告 Reranker 可用。"""

        return True

    async def rerank(self, request):
        """为每个候选返回稳定分数。"""

        self.calls += 1
        return [
            type("Result", (), {"index": index, "text": text, "score": 1.0 - index * 0.1})()
            for index, text in enumerate(request.documents)
        ]


class _Authorization:
    """记录 Parent 回溯阶段的再次授权检查。"""

    def __init__(self) -> None:
        """初始化调用记录。"""

        self.calls: list[str] = []

    async def require_document_read(self, _session, _context, document_id: str):
        """确认测试文档仍属于当前授权范围。"""

        self.calls.append(document_id)
        return type(
            "AuthorizedDocument",
            (),
            {"document_id": document_id, "version_id": "version-1"},
        )()


@pytest.mark.asyncio
async def test_query_dimension_mismatch_fails_before_retrieval(monkeypatch):
    """Embedding维度和配置不一致时，不能借Sparse-only掩盖错误。"""
    from enterprise_rag.retrieval.product import ProductHybridRetrievalService
    embedding = _Embedding()
    dense, sparse, reranker = _DenseTransport(), _SparseTransport(_sparse_profile()), _Reranker()
    async def wrong_dimension(_texts):
        """模拟配置维度2但模型输出维度3。"""
        return [[1.0, 0.0, 0.0]]
    monkeypatch.setattr(embedding, "embed", wrong_dimension)
    service = ProductHybridRetrievalService(
        scope_service=_ScopeService([_scope()]), embedding_provider=embedding,
        dense_transport=dense, sparse_transport=sparse, reranker=reranker,
        authorization_service=_Authorization(), index_profile=_dense_profile(),
        sparse_profile=_sparse_profile(),
    )
    with pytest.raises(ValueError, match="dimension"):
        await service.search(object(), _context(), question="问题", candidate_k=20, top_k=5)
    assert not dense.calls and not sparse.calls and reranker.calls == 0


@pytest.mark.asyncio
async def test_product_retrieval_searches_dense_sparse_once_and_rechecks_parent_acl() -> None:
    """产品查询应只生成一次 embedding，融合双路结果并再次校验 Parent ACL。"""

    from enterprise_rag.retrieval.product import ProductHybridRetrievalService

    scope_service = _ScopeService([_scope()])
    embedding = _Embedding()
    dense = _DenseTransport()
    sparse = _SparseTransport(_sparse_profile())
    reranker = _Reranker()
    authorization = _Authorization()
    service = ProductHybridRetrievalService(
        scope_service=scope_service,
        embedding_provider=embedding,
        dense_transport=dense,
        sparse_transport=sparse,
        reranker=reranker,
        authorization_service=authorization,
        index_profile=_dense_profile(),
        sparse_profile=_sparse_profile(),
    )

    result = await service.search(
        object(),
        _context(),
        question="谁执行部署？",
        candidate_k=5,
        top_k=2,
    )

    assert embedding.calls == [["谁执行部署？"]]
    assert len(dense.calls) == 1
    assert len(sparse.calls) == 1
    assert reranker.calls == 1
    assert authorization.calls == ["doc-1", "doc-1"]
    assert [item.parent_id for item in result.candidates] == ["parent-a", "parent-b"]
    assert result.trace.dense_hits == 2
    assert result.trace.sparse_hits == 1
    assert result.trace.fused_candidates == 2
    assert result.trace.parent_candidates == 2


@pytest.mark.asyncio
async def test_product_retrieval_with_no_scope_does_not_call_embedding_or_reranker() -> None:
    """无授权 scope 时必须 fail closed，不能访问索引或模型。"""

    from enterprise_rag.retrieval.product import ProductHybridRetrievalService

    embedding = _Embedding()
    reranker = _Reranker()
    service = ProductHybridRetrievalService(
        scope_service=_ScopeService([]),
        embedding_provider=embedding,
        dense_transport=_DenseTransport(),
        sparse_transport=_SparseTransport(_sparse_profile()),
        reranker=reranker,
        authorization_service=_Authorization(),
        index_profile=_dense_profile(),
        sparse_profile=_sparse_profile(),
    )

    result = await service.search(
        object(),
        _context(),
        question="问题",
        candidate_k=5,
        top_k=2,
    )

    assert result.candidates == ()
    assert embedding.calls == []
    assert reranker.calls == 0
    assert result.trace.scopes == 0
