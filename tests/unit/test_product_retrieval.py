"""本地 P0 产品混合检索服务的行为测试。"""

from __future__ import annotations

import pytest

from enterprise_rag.retrieval.contracts import IndexProfile
from enterprise_rag.retrieval.scope import AuthorizedRetrievalScope
from enterprise_rag.retrieval.service import RetrievalResult
from enterprise_rag.retrieval.sparse import SparseProfile
from enterprise_rag.security.context import RequestContext


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
