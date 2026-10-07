"""F2-G HTTP 请求与响应模型。"""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


_EVIDENCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_EVIDENCE_TEXT_BYTES = 8 * 1024
_MAX_EVIDENCE_TOTAL_BYTES = 32 * 1024


class EvidenceRequest(BaseModel):
    """调用方提交的待核验证据；其中 source 只是展示文本。"""

    model_config = ConfigDict(extra="forbid")

    evidence_id: StrictStr = Field(min_length=1, max_length=64)
    text: StrictStr = Field(min_length=1, max_length=8192)
    source: StrictStr | None = Field(default=None, max_length=512)

    @field_validator("evidence_id")
    @classmethod
    def evidence_id_must_be_safe_identity(cls, value: str) -> str:
        """限制身份键字符集，避免把调用方文本误当作 Prompt 分隔符。"""

        if _EVIDENCE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("evidence_id 格式不合法")
        return value

    @field_validator("text")
    @classmethod
    def text_must_fit_utf8_budget(cls, value: str) -> str:
        """按实际 UTF-8 字节数限制正文，而不是只看 Unicode 字符数。"""

        if not value.strip():
            raise ValueError("evidence.text 不能为空")
        if len(value.encode("utf-8")) > _MAX_EVIDENCE_TEXT_BYTES:
            raise ValueError("单条 evidence.text 超过 8 KiB")
        return value

    @field_validator("source")
    @classmethod
    def source_must_be_non_blank(cls, value: str | None) -> str | None:
        """source 只用于展示，拒绝空声明但不把它当成抓取地址。"""

        if value is not None and not value.strip():
            raise ValueError("evidence.source 不能为空")
        return value


class ExcerptQueryRequest(BaseModel):
    """原文摘录问答请求，拒绝自带 endpoint、密钥或授权字段。"""

    model_config = ConfigDict(extra="forbid")

    question: StrictStr = Field(min_length=1, max_length=4096)
    evidence: list[EvidenceRequest] = Field(min_length=1, max_length=16)
    model_alias: Literal["local_answer"] = "local_answer"
    temperature: StrictFloat = Field(default=0.0, ge=0.0, le=1.0)
    # 产品路由复用此请求模型；上限必须兼容产品8192预算，旧入口默认512不变。
    max_tokens: StrictInt = Field(default=512, ge=1, le=8192)
    # timeout_ms 是调用级总预算，不允许客户端通过超大值绕过服务端保护。
    # 具体的排队、模型调用和流式发送都必须共同消耗这份预算。
    timeout_ms: StrictInt = Field(default=120_000, ge=1_000, le=300_000)

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question 不能为空")
        return value

    @field_validator("temperature")
    @classmethod
    def temperature_must_be_finite(cls, value: float) -> float:
        """拒绝 NaN/Infinity，避免它们进入上游 JSON 或绕过范围比较。"""

        if not math.isfinite(value):
            raise ValueError("temperature 必须是有限数值")
        return value

    @model_validator(mode="after")
    def evidence_total_must_fit_utf8_budget(self) -> "ExcerptQueryRequest":
        """限制所有 evidence 正文的总字节数，超限时拒绝整个请求。"""

        total_bytes = sum(len(item.text.encode("utf-8")) for item in self.evidence)
        if total_bytes > _MAX_EVIDENCE_TOTAL_BYTES:
            raise ValueError("evidence.text 总大小超过 32 KiB")
        return self


class RetrievalQueryRequest(BaseModel):
    """方案 B 的检索问答请求，只接受服务端检索所需的范围和生成参数。"""

    model_config = ConfigDict(extra="forbid")

    tenant_id: StrictStr = Field(min_length=1, max_length=128)
    version_id: StrictStr = Field(min_length=1, max_length=128)
    question: StrictStr = Field(min_length=1, max_length=4096)
    top_k: StrictInt = Field(default=5, ge=1, le=20)
    model_alias: Literal["local_answer"] = "local_answer"
    temperature: StrictFloat = Field(default=0.0, ge=0.0, le=1.0)
    # 与共用摘录生成保持相同上限，不改变旧PG检索入口的默认预算。
    max_tokens: StrictInt = Field(default=512, ge=1, le=8192)
    # 检索和生成共享同一调用预算；当前 P0 先由 F2-G 生成阶段执行该预算。
    timeout_ms: StrictInt = Field(default=120_000, ge=1_000, le=300_000)

    @field_validator("tenant_id", "version_id", "question")
    @classmethod
    def required_text_must_not_be_blank(cls, value: str) -> str:
        """拒绝只包含空白的检索范围或问题。"""

        if not value.strip():
            raise ValueError("检索范围和 question 不能为空")
        return value

    @field_validator("temperature")
    @classmethod
    def temperature_must_be_finite(cls, value: float) -> float:
        """拒绝 NaN/Infinity，避免非法数值进入内部 F2-G 请求。"""

        if not math.isfinite(value):
            raise ValueError("temperature 必须是有限数值")
        return value


class CitationResponse(BaseModel):
    evidence_id: str
    quote: str
    start: int
    end: int
    source: str | None = None


class UsageResponse(BaseModel):
    """对外暴露的可选 token 使用量；上游缺失时保持 ``null``。"""

    input_tokens: int | None = None
    output_tokens: int | None = None


class ProviderErrorResponse(BaseModel):
    """Provider 失败时的安全错误摘要；不允许携带上游原始 body。"""

    kind: str
    retryable: bool
    safe_message: str


class ProviderResultResponse(BaseModel):
    """遵守共享 ProviderResult 契约的调用摘要，不把它等同于答案质量。"""

    request_id: str
    provider: str
    model: str
    status: Literal["success", "partial", "failure", "cancelled"]
    latency_ms: float = Field(ge=0)
    content: None = None
    finish_reason: str | None = None
    usage: UsageResponse | None = None
    error: ProviderErrorResponse | None = None


class TimingsResponse(BaseModel):
    """各阶段耗时；没有到达的阶段使用 ``null`` 而不是伪造零值。"""

    queue_ms: float | None = Field(default=None, ge=0)
    ttft_ms: float | None = Field(default=None, ge=0)
    generation_ms: float | None = Field(default=None, ge=0)
    validation_ms: float | None = Field(default=None, ge=0)
    completion_ms: float | None = Field(default=None, ge=0)


class ExcerptResponse(BaseModel):
    request_id: str
    trace_id: str
    context_source: Literal["caller_supplied", "retrieved"]
    answer_status: str
    citation_validation: str
    message: str
    citations: list[CitationResponse]
    provider_result: ProviderResultResponse
    timings: TimingsResponse
    groundedness: str = "not_checked"
    groundedness_reason: str = ""
