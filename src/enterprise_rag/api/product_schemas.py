"""本地 P0 产品 API 的安全请求和响应模型。"""

from __future__ import annotations

from datetime import datetime

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


class ProductArtifactStatusResponse(BaseModel):
    """对外返回单个产物的安全状态和数量，不返回对象存储 key。"""

    model_config = ConfigDict(extra="forbid")

    status: str
    item_count: int = Field(ge=0)


class ProductVersionStatusResponse(BaseModel):
    """对外返回版本身份和生命周期状态，不返回原文或 manifest。"""

    model_config = ConfigDict(extra="forbid")

    version_id: str
    status: str
    created_at: datetime | None = None
    published_at: datetime | None = None


class ProductJobStatusResponse(BaseModel):
    """对外返回最近摄取任务的安全摘要，不回显失败堆栈。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: str
    attempt: int = Field(ge=0)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProductGenerationStatusResponse(BaseModel):
    """对外返回 generation 生命周期和计数，不暴露 collection 或 worker lease。"""

    model_config = ConfigDict(extra="forbid")

    generation_id: str
    status: str
    expected_item_count: int = Field(ge=0)
    actual_item_count: int = Field(ge=0)
    checkpoint: int = Field(ge=0)


class ProductDocumentStatusResponse(BaseModel):
    """文档状态接口的安全响应模型。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    document_status: str
    current_version: ProductVersionStatusResponse | None = None
    latest_version: ProductVersionStatusResponse | None = None
    latest_job: ProductJobStatusResponse | None = None
    generation: ProductGenerationStatusResponse | None = None
    artifacts: dict[str, ProductArtifactStatusResponse]


class ProductACLGrantResponse(BaseModel):
    """ACL 授予接口的稳定响应。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    principal_type: str
    principal_id: str
    permission: str
    granted: bool


class ProductACLRevokeResponse(BaseModel):
    """ACL 撤销接口的稳定响应。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    principal_type: str
    principal_id: str
    permission: str
    revoked: bool


class ProductDependencyStatusResponse(BaseModel):
    """单项产品依赖只暴露状态枚举。"""

    model_config = ConfigDict(extra="forbid")

    status: str


class ProductReadinessResponse(BaseModel):
    """产品 readiness 的安全响应，不包含配置值和异常正文。"""

    model_config = ConfigDict(extra="forbid")

    status: str
    dependencies: dict[str, ProductDependencyStatusResponse]


class ProductRagQueryRequest(BaseModel):
    """产品问答请求；身份和 evidence 必须完全由服务端生成。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4096)
    top_k: int = Field(default=5, ge=1, le=20)
    candidate_k: int = Field(default=20, ge=1, le=100)
    model_alias: str = Field(default="local_answer", pattern="^local_answer$")
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_tokens: int = Field(default=2048, ge=1, le=2048)
    timeout_ms: int = Field(default=300_000, ge=1_000, le=300_000)

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
    mode: str = "normal"
    fallback_reasons: tuple[str, ...] = ()

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
            mode=trace.mode,
            fallback_reasons=trace.fallback_reasons,
        )


class ProductRagQueryResponse(ExcerptResponse):
    """产品问答响应，在 F2-G 摘录结果上附加安全检索摘要。"""

    retrieval_trace: ProductRetrievalTraceResponse


__all__ = [
    "ProductACLGrantResponse",
    "ProductACLRevokeResponse",
    "ProductArtifactStatusResponse",
    "ProductDependencyStatusResponse",
    "ProductDocumentStatusResponse",
    "ProductGenerationStatusResponse",
    "ProductRagQueryRequest",
    "ProductRagQueryResponse",
    "ProductReadinessResponse",
    "ProductRetrievalTraceResponse",
    "ProductUploadResponse",
    "ProductJobStatusResponse",
    "ProductVersionStatusResponse",
]
