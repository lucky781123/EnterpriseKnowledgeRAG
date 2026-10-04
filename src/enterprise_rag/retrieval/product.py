"""本地 P0 产品查询的授权混合检索编排。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from enterprise_rag.retrieval.contracts import IndexProfile
from enterprise_rag.retrieval.hybrid import (
    HybridCandidate,
    RerankerUnavailable,
    collapse_parent_candidates,
    fuse_retrieval_candidates,
    rerank_retrieval_candidates,
)
from enterprise_rag.retrieval.scope import AuthorizationScopeService
from enterprise_rag.retrieval.service import RetrievalResult
from enterprise_rag.retrieval.sparse import SparseProfile
from enterprise_rag.retrieval.weaviate import WeaviateTransportError
from enterprise_rag.security.authorization import AuthorizationService, ResourceNotFound
from enterprise_rag.security.context import RequestContext


def _retryable_transport_failure(error: Exception) -> bool:
    """仅网络、超时、限流和服务端故障可降级；认证及请求错误必须停止。"""

    if isinstance(error, WeaviateTransportError):
        return error.retryable
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {408, 429} or error.response.status_code >= 500
    return isinstance(error, httpx.RequestError)


@dataclass(frozen=True, slots=True)
class ProductRetrievalTrace:
    """只保存计数和固定策略的检索摘要，不暴露 collection 或对象存储 key。"""

    scopes: int
    dense_hits: int
    sparse_hits: int
    fused_candidates: int
    reranked_candidates: int
    parent_candidates: int
    rrf_k: int
    reranker_used: bool
    mode: str = "normal"
    fallback_reasons: tuple[str, ...] = ()

    @classmethod
    def empty(cls, *, rrf_k: int = 60) -> "ProductRetrievalTrace":
        """构造无授权或无命中时使用的零计数摘要。"""

        return cls(0, 0, 0, 0, 0, 0, rrf_k, False)


@dataclass(frozen=True, slots=True)
class ProductRetrievalResult:
    """保存通过 ACL 复核后的 Parent 候选和检索摘要。"""

    candidates: tuple[HybridCandidate, ...]
    trace: ProductRetrievalTrace


class ProductHybridRetrievalService:
    """执行授权 scope、Dense/BM25F、RRF、Reranker 和 Parent 复核。"""

    def __init__(
        self,
        *,
        scope_service: AuthorizationScopeService,
        embedding_provider: Any,
        dense_transport: Any,
        sparse_transport: Any,
        reranker: Any,
        authorization_service: AuthorizationService,
        index_profile: IndexProfile,
        sparse_profile: SparseProfile,
        rrf_k: int = 60,
    ) -> None:
        """保存服务端固定依赖，禁止请求方动态切换 profile 或 RRF 参数。"""

        if rrf_k <= 0:
            raise ValueError("rrf_k 必须大于 0")
        self.scope_service = scope_service
        self.embedding_provider = embedding_provider
        self.dense_transport = dense_transport
        self.sparse_transport = sparse_transport
        self.reranker = reranker
        self.authorization_service = authorization_service
        self.index_profile = index_profile
        self.sparse_profile = sparse_profile
        self.rrf_k = rrf_k

    async def search(
        self,
        session: Any,
        context: RequestContext,
        *,
        question: str,
        candidate_k: int,
        top_k: int,
    ) -> ProductRetrievalResult:
        """只在授权 scope 内执行一次 embedding 和双路召回。"""

        if not question.strip():
            raise ValueError("question 不能为空")
        if candidate_k <= 0 or top_k <= 0:
            raise ValueError("candidate_k 和 top_k 必须大于 0")

        scopes = await self.scope_service.list_scopes(
            session,
            context,
            index_profile=self.index_profile,
        )
        if not scopes:
            return ProductRetrievalResult(
                candidates=(),
                trace=ProductRetrievalTrace.empty(rrf_k=self.rrf_k),
            )

        reasons: list[str] = []
        query_vector = None
        try:
            vectors = await self.embedding_provider.embed([question])
            if len(vectors) != 1 or not vectors[0]:
                raise ValueError("查询 embedding 响应数量不正确")
            query_vector = vectors[0]
            # 查询也校验实际维度，配置写错不能作为Dense故障悄悄转向关键词兜底。
            if len(query_vector) != self.index_profile.dimension:
                raise ValueError("查询 embedding dimension 与 index profile 不一致")
        except httpx.HTTPError as exc:
            # 仅网络故障允许转向 BM25F；维度、协议和授权错误不能被兜底掩盖。
            if not _retryable_transport_failure(exc):
                raise
            reasons.append("embedding_unavailable")
        dense_hits: list[RetrievalResult] = []
        sparse_hits: list[RetrievalResult] = []
        successful_channels = 0
        for scope in scopes:
            if query_vector is not None:
                try:
                    dense_hits.extend(
                        await self.dense_transport.search(
                            scope.dense_collection_name,
                            generation_id=scope.generation_id,
                            version_id=scope.version_id,
                            query=query_vector,
                            top_k=candidate_k,
                        )
                    )
                    successful_channels += 1
                except (httpx.HTTPError, WeaviateTransportError) as exc:
                    if not _retryable_transport_failure(exc):
                        raise
                    reasons.append("dense_unavailable")
            try:
                sparse_rows = await self.sparse_transport.search(
                    scope.sparse_collection_name,
                    profile=self.sparse_profile,
                    tenant_id=scope.tenant_id,
                    generation_id=scope.generation_id,
                    version_id=scope.version_id,
                    query=question,
                    top_k=candidate_k,
                )
                sparse_hits.extend(self._sparse_rows_to_results(sparse_rows, scope))
                successful_channels += 1
            except (httpx.HTTPError, WeaviateTransportError) as exc:
                if not _retryable_transport_failure(exc):
                    raise
                reasons.append("sparse_unavailable")

        if not successful_channels:
            # 两路均失效时禁止纯模型回答，避免以模型常识冒充企业证据。
            raise RuntimeError("授权检索通道全部不可用")

        # scope逐文档查询只保证文档内部排序；同一profile/租户collection的分数
        # 必须先合并为通道全局排名，避免文档遍历顺序挤掉后来上传的高相关候选。
        dense_hits.sort(key=lambda item: (-item.score, item.chunk_id))
        sparse_hits.sort(key=lambda item: (-item.score, item.chunk_id))
        fused = fuse_retrieval_candidates(
            dense_hits,
            sparse_hits,
            rrf_k=self.rrf_k,
        )[:candidate_k]
        if not fused:
            return ProductRetrievalResult(
                candidates=(),
                trace=ProductRetrievalTrace(
                    scopes=len(scopes),
                    dense_hits=len(dense_hits),
                    sparse_hits=len(sparse_hits),
                    fused_candidates=0,
                    reranked_candidates=0,
                    parent_candidates=0,
                    rrf_k=self.rrf_k,
                    reranker_used=False,
                    mode="degraded" if reasons else "normal",
                    fallback_reasons=tuple(dict.fromkeys(reasons)),
                ),
            )

        reranker_used = True
        try:
            reranked = await rerank_retrieval_candidates(
                question,
                fused,
                reranker=self.reranker,
                top_k=min(candidate_k, len(fused)),
            )
        except RerankerUnavailable:
            # BGE 失败保留 RRF 顺序；最终仍做 ACL 复核，再交本地 LLM 引用回答。
            reranked = fused
            reranker_used = False
            reasons.append("reranker_unavailable")
        collapsed = collapse_parent_candidates(reranked)[:top_k]
        authorized = await self._recheck_parent_acl(session, context, collapsed)
        return ProductRetrievalResult(
            candidates=tuple(authorized),
            trace=ProductRetrievalTrace(
                scopes=len(scopes),
                dense_hits=len(dense_hits),
                sparse_hits=len(sparse_hits),
                fused_candidates=len(fused),
                reranked_candidates=len(reranked),
                parent_candidates=len(authorized),
                rrf_k=self.rrf_k,
                reranker_used=reranker_used,
                mode="degraded" if reasons else "normal",
                fallback_reasons=tuple(dict.fromkeys(reasons)),
            ),
        )

    async def _recheck_parent_acl(
        self,
        session: Any,
        context: RequestContext,
        candidates: Sequence[HybridCandidate],
    ) -> list[HybridCandidate]:
        """在 Parent 回溯后再次检查当前版本和文档 ACL，撤权时丢弃候选。"""

        authorized: list[HybridCandidate] = []
        for candidate in candidates:
            try:
                document = await self.authorization_service.require_document_read(
                    session,
                    context,
                    candidate.result.document_id,
                )
            except ResourceNotFound:
                # 资源在检索期间撤权时只丢弃该候选；不能把旧文本或旧版本继续交给模型。
                continue
            if document.version_id != candidate.result.version_id:
                continue
            authorized.append(candidate)
        return authorized

    @staticmethod
    def _sparse_rows_to_results(rows: Sequence[dict[str, Any]], scope: Any) -> list[RetrievalResult]:
        """把 BM25F GraphQL 行转换成统一 RetrievalResult，并严格校验 scope。"""

        results: list[RetrievalResult] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Sparse 检索结果格式非法")
            if any(
                row.get(name) != expected
                for name, expected in (
                    ("tenant_id", scope.tenant_id),
                    ("version_id", scope.version_id),
                    ("generation_id", scope.generation_id),
                )
            ):
                raise ValueError("Sparse 检索结果 scope 不一致")
            chunk_id = row.get("chunk_id")
            document_id = row.get("document_id")
            body = row.get("body")
            if not all(isinstance(value, str) and value.strip() for value in (chunk_id, document_id, body)):
                raise ValueError("Sparse 检索结果缺少稳定 identity 或正文")
            additional = row.get("_additional")
            score = additional.get("score", 0.0) if isinstance(additional, dict) else 0.0
            try:
                numeric_score = float(score)
            except (TypeError, ValueError) as exc:
                raise ValueError("Sparse 检索 score 非法") from exc
            heading_path = row.get("heading_path") or []
            source_locators = row.get("source_locators") or []
            if not isinstance(heading_path, list) or not all(
                isinstance(value, str) for value in heading_path
            ):
                raise ValueError("Sparse heading_path 格式非法")
            if not isinstance(source_locators, list) or not all(
                isinstance(value, dict) for value in source_locators
            ):
                raise ValueError("Sparse source_locators 格式非法")
            results.append(
                RetrievalResult(
                    chunk_id=chunk_id,
                    parent_id=row.get("parent_id"),
                    version_id=scope.version_id,
                    document_id=document_id,
                    text=body,
                    heading_path=tuple(heading_path),
                    source_locators=tuple(source_locators),
                    score=numeric_score,
                )
            )
        return results


__all__ = [
    "ProductHybridRetrievalService",
    "ProductRetrievalResult",
    "ProductRetrievalTrace",
]
