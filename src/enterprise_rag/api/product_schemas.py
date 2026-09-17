"""本地 P0 产品 API 的安全请求和响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enterprise_rag.domain.entities import ArtifactKind, ArtifactStatus
from enterprise_rag.ingestion.product_service import ProductIngestionResult
from enterprise_rag.retrieval.product import ProductRetrievalResult
from enterprise_rag.api.schemas import ExcerptResponse


class ProductUploadResponse(BaseModel):
    """文档上传成功后的状态摘要，不包含正文、token 或内部对象 key。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    job_status: str
    version_status: str
    generation_status: str
    artifacts: dict[str, str]
    published: bool

    @classmethod
    def from_result(cls, result: ProductIngestionResult) -> ProductUploadResponse:
        """将领域结果映射为稳定的小写字符串枚举响应。"""

        return cls(
            document_id=result.document_id,
            version_id=result.version_id,
            job_id=result.job_id,
            generation_id=result.generation_id,
            job_status=result.job_status.value,
            version_status=result.version_status.value,
            generation_status=result.generation_status.value,
            artifacts={
                kind.value: result.artifact_statuses.get(kind, ArtifactStatus.PENDING).value
                for kind in ArtifactKind
            },
            published=result.published,
        )


class ProductRagQueryRequest(BaseModel):
    """产品问答请求；身份和 evidence 必须完全由服务端生成。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4096)
    top_k: int = Field(default=5, ge=1, le=20)
    candidate_k: int = Field(default=20, ge=1, le=100)
    model_alias: str = Field(default="local_answer", pattern="^local_answer$")
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_tokens: int = Field(default=512, ge=1, le=2048)
    timeout_ms: int = Field(default=120_000, ge=1_000, le=300_000)

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        """拒绝只包含空白的产品问题，避免把无效 query 送入索引。"""

        if not value.strip():
            raise ValueError("question 不能为空")
        return value

    @model_validator(mode="after")
    def validate_candidate_budget(self) -> "ProductRagQueryRequest":
        """保留一个稳定的候选预算关系，避免 top_k 大于 candidate_k。"""

        if self.candidate_k < self.top_k:
            raise ValueError("candidate_k 不能小于 top_k")
        return self


class ProductRetrievalTraceResponse(BaseModel):
    """对外可见的检索摘要，只暴露数量和固定策略。"""

    model_config = ConfigDict(extra="forbid")

    scopes: int = Field(ge=0)
    dense_hits: int = Field(ge=0)
    sparse_hits: int = Field(ge=0)
    fused_candidates: int = Field(ge=0)
    reranked_candidates: int = Field(ge=0)
    parent_candidates: int = Field(ge=0)
    rrf_k: int = Field(gt=0)
    reranker_used: bool

    @classmethod
    def from_result(cls, result: ProductRetrievalResult) -> "ProductRetrievalTraceResponse":
        """把领域 trace 映射为不泄露内部 collection/key 的响应。"""

        trace = result.trace
        return cls(
            scopes=trace.scopes,
            dense_hits=trace.dense_hits,
            sparse_hits=trace.sparse_hits,
            fused_candidates=trace.fused_candidates,
            reranked_candidates=trace.reranked_candidates,
            parent_candidates=trace.parent_candidates,
            rrf_k=trace.rrf_k,
            reranker_used=trace.reranker_used,
        )


class ProductRagQueryResponse(ExcerptResponse):
    """产品问答响应，在 F2-G 摘录结果上附加安全检索摘要。"""

    retrieval_trace: ProductRetrievalTraceResponse


__all__ = [
    "ProductRagQueryRequest",
    "ProductRagQueryResponse",
    "ProductRetrievalTraceResponse",
    "ProductUploadResponse",
]
