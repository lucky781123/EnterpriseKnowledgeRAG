"""LM Studio OpenAI-compatible adapter 的 mock HTTP 契约测试。"""

import httpx
import pytest

from enterprise_rag.providers.errors import ProviderError, ProviderErrorKind
from enterprise_rag.providers.lmstudio import LMStudioChatProvider
from enterprise_rag.providers.protocols import ChatMessage, ChatRequest


def _request() -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage("system", "只输出原文摘录"), ChatMessage("user", "问题")],
        model="qwen-test",
        temperature=0.0,
        max_tokens=128,
    )


@pytest.mark.asyncio
async def test_complete_calls_v1_once_and_maps_response() -> None:
    """base_url 已含 /v1 时，adapter 只能拼接一次 chat/completions。"""

    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "model": "qwen-test",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": '{"quote":"正文"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://testserver",
    ) as client:
        provider = LMStudioChatProvider(
            client,
            base_url="http://testserver/v1",
            model="qwen-test",
            api_key="lm-studio",
        )
        response = await provider.complete(_request())

    assert seen[0].url.path == "/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer lm-studio"
    assert response.content == '{"quote":"正文"}'
    assert response.finish_reason == "stop"
    assert response.usage is not None
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 4


@pytest.mark.asyncio
async def test_complete_normalizes_root_url_without_double_v1() -> None:
    """调用方可传根地址或 `/v1` 地址，但最终路径必须唯一。"""

    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "qwen-test",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = LMStudioChatProvider(client, "http://testserver", "qwen-test")
        await provider.complete(_request())

    assert seen[0].url.path == "/v1/chat/completions"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, ProviderErrorKind.INVALID_REQUEST),
        (401, ProviderErrorKind.AUTHENTICATION),
        (429, ProviderErrorKind.RATE_LIMITED),
        (503, ProviderErrorKind.UNAVAILABLE),
    ],
)
async def test_complete_maps_http_errors_without_leaking_body(status, kind) -> None:
    """HTTP 状态转换为稳定错误类别，响应正文中的敏感内容不得泄露。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="Authorization: very-secret-body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = LMStudioChatProvider(client, "http://testserver/v1", "qwen-test")
        with pytest.raises(ProviderError) as raised:
            await provider.complete(_request())

    assert raised.value.kind is kind
    assert "very-secret-body" not in str(raised.value)


@pytest.mark.asyncio
async def test_complete_allows_missing_optional_finish_reason() -> None:
    """兼容上游省略 finish_reason 的合法响应，不把缺失字段误判成协议失败。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"model": "qwen-test", "choices": [{"message": {"content": "ok"}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = LMStudioChatProvider(client, "http://testserver/v1", "qwen-test")
        response = await provider.complete(_request())

    assert response.finish_reason is None


@pytest.mark.asyncio
async def test_stream_requires_done_and_yields_content_deltas() -> None:
    """流式响应必须收到 [DONE] 才算正常结束，reasoning 字段不向外透传。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"choices":[{"delta":{"reasoning_content":"hidden"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"甲"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"乙"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://testserver",
    ) as client:
        provider = LMStudioChatProvider(client, "http://testserver/v1", "qwen-test")
        chunks = [chunk async for chunk in provider.stream(_request())]

    assert chunks == ["甲", "乙"]


@pytest.mark.asyncio
async def test_stream_events_preserve_finish_reason_and_usage_without_reasoning() -> None:
    """内部流事件保留终态元数据，但绝不把 reasoning 字段暴露为正文。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"choices":[{"delta":{"reasoning_content":"hidden","content":"甲"},"finish_reason":null}]}'
            "\n\n"
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1}}'
            "\n\n"
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://testserver",
    ) as client:
        provider = LMStudioChatProvider(client, "http://testserver/v1", "qwen-test")
        events = [event async for event in provider.stream_events(_request())]

    assert [event.text for event in events if event.text] == ["甲"]
    terminal = events[-1]
    assert terminal.finish_reason == "stop"
    assert terminal.usage is not None
    assert terminal.usage.input_tokens == 3
    assert terminal.usage.output_tokens == 1


@pytest.mark.asyncio
async def test_stream_eof_without_done_is_protocol_error() -> None:
    """上游提前 EOF 不能被 API 当成正常完成。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"甲"}}]}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = LMStudioChatProvider(client, "http://testserver/v1", "qwen-test")
        with pytest.raises(ProviderError) as raised:
            _ = [chunk async for chunk in provider.stream(_request())]

    assert raised.value.kind is ProviderErrorKind.PROTOCOL
