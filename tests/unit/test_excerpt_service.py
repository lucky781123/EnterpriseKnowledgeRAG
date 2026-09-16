"""ExcerptAnswerService 的确定性应用契约测试。"""

import json

import pytest

from enterprise_rag.excerpts.service import (
    ExcerptAnswerService,
    VisibleStreamSanitizer,
    build_excerpt_prompt,
)
from enterprise_rag.providers.errors import ProviderError, ProviderErrorKind
from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.protocols import ChatMessage, ChatRequest, ChatResponse


def _request_payload() -> dict[str, object]:
    return {
        "question": "迁移由谁执行？",
        "evidence": [
            {
                "evidence_id": "doc-1",
                "text": "数据库迁移由部署阶段的 one-shot job 执行。",
                "source": "部署说明",
            }
        ],
    }


@pytest.mark.asyncio
async def test_service_returns_only_identity_validated_citations() -> None:
    """模型摘录必须逐字符命中提交文本，服务才允许返回 citation。"""

    provider = FakeChatProvider(
        lambda request: json.dumps(
            {
                "answer_status": "answered",
                "message": "由部署阶段任务执行。",
                "excerpts": [
                    {
                        "evidence_id": "doc-1",
                        "quote": "部署阶段的 one-shot job",
                        "start": 6,
                        "end": 24,
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    service = ExcerptAnswerService(provider)

    response = await service.answer(
        question=_request_payload()["question"],
        evidence=_request_payload()["evidence"],
    )

    assert response.context_source == "caller_supplied"
    assert response.answer_status == "answered"
    assert response.citation_validation == "passed"
    assert response.citations[0]["evidence_id"] == "doc-1"


@pytest.mark.asyncio
async def test_service_marks_invalid_model_quote_without_fabricating_citation() -> None:
    """引用失败时保留诊断状态，但不能替模型修正文案或伪造引用。"""

    provider = FakeChatProvider(
        lambda request: '{"answer_status":"answered","message":"答案","excerpts":[{"evidence_id":"doc-1","quote":"不存在的句子"}]}'
    )
    response = await ExcerptAnswerService(provider).answer(
        question="问题",
        evidence=[{"evidence_id": "doc-1", "text": "只有原文"}],
    )

    assert response.answer_status == "invalid_citations"
    assert response.citation_validation == "failed"
    assert response.citations == []


def test_prompt_uses_internal_labels_and_declares_untrusted_evidence() -> None:
    """Prompt 不把调用方 evidence_id 当作指令分隔符或授权声明。"""

    messages = build_excerpt_prompt(
        "问题",
        [{"evidence_id": "caller-id", "text": "正文", "source": "来源"}],
    )

    assert isinstance(messages[0], ChatMessage)
    assert "不可信" in messages[0].content
    assert '"label": "E1"' in messages[1].content
    assert '"evidence_id": "caller-id"' in messages[1].content


def test_visible_stream_sanitizer_drops_split_reasoning_wrapper() -> None:
    """跨多个网络增量拆开的 reasoning 标签也不能进入可见 token。"""

    sanitizer = VisibleStreamSanitizer()
    chunks = [
        "<th",
        "ink>内部 reasoning</think>",
        "\n\n```json\n{",
        '"answer_status":"answered"',
        "}\n```",
    ]

    visible = "".join(sanitizer.push(chunk) for chunk in chunks) + sanitizer.finish()

    assert "reasoning" not in visible
    assert visible.startswith("{")
    assert '"answer_status":"answered"' in visible


def test_visible_stream_sanitizer_rejects_unclosed_reasoning_wrapper() -> None:
    """流结束时 reasoning 未闭合必须是协议失败，而不是静默丢弃正文。"""

    sanitizer = VisibleStreamSanitizer()
    sanitizer.push("<think>未闭合")
    with pytest.raises(ProviderError) as raised:
        sanitizer.finish()

    assert raised.value.kind is ProviderErrorKind.PROTOCOL


@pytest.mark.asyncio
async def test_service_marks_length_finish_as_incomplete() -> None:
    """Provider 成功但因长度截断时，不能把结果标记为完整回答。"""

    class TruncatedProvider(FakeChatProvider):
        async def complete(self, request: ChatRequest) -> ChatResponse:
            return ChatResponse(
                content='{"answer_status":"answered","message":"部分","excerpts":[]}',
                model=request.model,
                finish_reason="length",
            )

    response = await ExcerptAnswerService(TruncatedProvider()).answer(
        question="问题", evidence=[{"evidence_id": "doc-1", "text": "正文"}]
    )

    assert response.answer_status == "incomplete"
    assert response.citation_validation == "not_checked"


@pytest.mark.asyncio
async def test_service_rejects_answer_without_citations() -> None:
    """正常回答必须至少有一条通过 identity 校验的 citation。"""

    provider = FakeChatProvider(lambda request: '{"answer_status":"answered","message":"答案","excerpts":[]}')
    response = await ExcerptAnswerService(provider).answer(
        question="问题", evidence=[{"evidence_id": "doc-1", "text": "正文"}]
    )

    assert response.answer_status == "invalid_citations"
    assert response.citation_validation == "failed"


@pytest.mark.asyncio
async def test_service_normalizes_malformed_model_json_as_protocol_error() -> None:
    """模型返回无法解析的 JSON 属于 Provider 协议错误，而不是调用方引用错误。"""

    provider = FakeChatProvider(lambda request: "not-json")
    with pytest.raises(ProviderError) as raised:
        await ExcerptAnswerService(provider).answer(
            question="问题", evidence=[{"evidence_id": "doc-1", "text": "正文"}]
        )

    assert raised.value.kind is ProviderErrorKind.PROTOCOL


@pytest.mark.asyncio
async def test_service_strips_reasoning_block_and_json_code_fence_before_validation() -> None:
    """模型的受控 reasoning/代码围栏不能进入答案，但其 JSON 正文仍可核验。"""

    content = """<think>这里是模型内部 reasoning，不应成为可见答案。</think>

```json
{
  "answer_status": "answered",
  "message": "答案",
  "excerpts": [{"evidence_id": "doc-1", "quote": "正文"}]
}
```"""
    response = await ExcerptAnswerService(FakeChatProvider(lambda request: content)).answer(
        question="问题", evidence=[{"evidence_id": "doc-1", "text": "正文"}]
    )

    assert response.answer_status == "answered"
    assert response.citation_validation == "passed"
    assert response.citations[0]["quote"] == "正文"
    # 原始响应仅保留在内部诊断对象中；API 层仍不得把它复制到
    # provider_result.content 或 SSE token。
    assert response.raw_model_output is not None
    assert "内部 reasoning" in response.raw_model_output


@pytest.mark.asyncio
async def test_service_rejects_unclosed_reasoning_wrapper_as_protocol_error() -> None:
    """未闭合的 reasoning 包装不能被宽松截断，以免隐藏半截模型输出。"""

    content = '<think>未闭合的内部内容\n{"answer_status":"answered"}'
    with pytest.raises(ProviderError) as raised:
        await ExcerptAnswerService(FakeChatProvider(lambda request: content)).answer(
            question="问题", evidence=[{"evidence_id": "doc-1", "text": "正文"}]
        )

    assert raised.value.kind is ProviderErrorKind.PROTOCOL


@pytest.mark.asyncio
async def test_service_retains_provider_response_metadata_for_api_observability() -> None:
    """服务层必须保留 Provider 的终态元数据，但 API 层可选择不暴露原始正文。"""

    class MetadataProvider(FakeChatProvider):
        async def complete(self, request: ChatRequest) -> ChatResponse:
            return ChatResponse(
                content='{"answer_status":"answered","message":"答案","excerpts":[{"evidence_id":"doc-1","quote":"正文"}]}',
                model="qwen-observed",
                finish_reason="stop",
                usage=type("Usage", (), {"input_tokens": 7, "output_tokens": 3})(),
            )

    response = await ExcerptAnswerService(MetadataProvider()).answer(
        question="问题", evidence=[{"evidence_id": "doc-1", "text": "正文"}]
    )

    assert response.provider_response is not None
    assert response.provider_response.model == "qwen-observed"
    assert response.provider_response.finish_reason == "stop"
    assert response.provider_response.usage is not None
    assert response.provider_response.usage.input_tokens == 7
