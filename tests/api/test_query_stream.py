"""F2-G SSE 事件顺序与终态契约测试。

除了正常路径，本文件还通过可控的异步迭代器验证部分输出、总 deadline、
客户端取消以及清理异常时的资源释放。Fake Provider 只用于确定性契约，
不把这些结果当成真实 LM Studio 的性能或取消语义证明。
"""

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from enterprise_rag.api.app import create_app
from enterprise_rag.api.schemas import ExcerptQueryRequest
from enterprise_rag.providers.errors import ProviderError, ProviderErrorKind
from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.registry import ProviderRegistry
from enterprise_rag.providers.protocols import ChatRequest
from enterprise_rag.providers.protocols import ChatStreamEvent, ChatUsage


class _DisconnectProbe:
    """直接调用路由时使用的最小 Request 替身，可明确控制断开状态。"""

    def __init__(self, disconnected: bool = False) -> None:
        self.disconnected = disconnected

    async def is_disconnected(self) -> bool:
        """SSE 路由只依赖这一项能力，因此无需启动真实 socket。"""

        return self.disconnected


def _stream_endpoint(app):
    """从 FastAPI 路由表取出 SSE endpoint，便于测试真实生成器清理语义。"""

    return next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/v1/query/stream")


def _request(timeout_ms: int = 120_000) -> ExcerptQueryRequest:
    """构造所有边界测试共用的最小合法请求。"""

    return ExcerptQueryRequest(
        question="问题",
        evidence=[{"evidence_id": "doc-1", "text": "正文"}],
        timeout_ms=timeout_ms,
    )


def _decode_event(frame: str | bytes) -> dict[str, object]:
    """把 StreamingResponse 的单个 data-only 帧转换成业务事件。"""

    if isinstance(frame, bytes):
        frame = frame.decode("utf-8")
    return json.loads(frame.removeprefix("data: ").strip())


def test_query_stream_emits_monotonic_sequence_and_single_done() -> None:
    """正常流必须有单调 seq，且 done 是唯一终态。"""

    registry = ProviderRegistry()
    registry.register(
        "local_answer",
        FakeChatProvider(
            lambda request: json.dumps(
                {
                    "answer_status": "answered",
                    "message": "由部署任务执行",
                    "excerpts": [{"evidence_id": "doc-1", "quote": "部署任务", "start": 1, "end": 5}],
                },
                ensure_ascii=False,
            )
        ),
        capabilities={"chat.complete"},
    )
    with TestClient(create_app(provider_registry=registry)) as client:
        response = client.post(
            "/api/v1/query/stream",
            json={"question": "谁执行？", "evidence": [{"evidence_id": "doc-1", "text": "由部署任务执行"}]},
        )

    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    event_types = [event["type"] for event in events]
    assert event_types[:3] == ["stage", "stage", "stage"]
    assert event_types[-3:] == ["stage", "citation", "done"]
    # token 是可见正文增量，数量由 Provider chunk 边界决定，契约只要求
    # 它们位于 generating 与 validating_citations 两个 stage 之间。
    assert event_types[3:-3]
    assert all(event_type == "token" for event_type in event_types[3:-3])
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert sum(event["type"] == "done" for event in events) == 1


def test_query_stream_uses_provider_stream_and_emits_token() -> None:
    """SSE 不能退化为 complete；必须消费 Provider 的真实增量流。"""

    class StreamingProvider(FakeChatProvider):
        def __init__(self):
            super().__init__()
            self.stream_called = False

        async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
            self.stream_called = True
            yield '{"answer_status":"answered","message":"'
            yield '答案","excerpts":[{"evidence_id":"doc-1","quote":"正文"}]}'

    provider = StreamingProvider()
    registry = ProviderRegistry()
    registry.register("local_answer", provider, capabilities={"chat.stream"})
    with TestClient(create_app(provider_registry=registry)) as client:
        response = client.post(
            "/api/v1/query/stream",
            json={"question": "问题", "evidence": [{"evidence_id": "doc-1", "text": "正文"}]},
        )

    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert provider.stream_called is True
    assert any(event["type"] == "token" for event in events)


def test_query_stream_prefers_structured_events_and_reports_terminal_metadata() -> None:
    """SSE 应优先使用结构化流事件，并在 done 中报告 finish_reason、usage 与 TTFT。"""

    class StructuredStreamingProvider(FakeChatProvider):
        async def stream_events(self, request: ChatRequest):
            yield ChatStreamEvent(text='{"answer_status":"answered","message":"')
            yield ChatStreamEvent(text='答案","excerpts":[{"evidence_id":"doc-1","quote":"正文"}]}')
            yield ChatStreamEvent(
                finish_reason="stop",
                usage=ChatUsage(input_tokens=5, output_tokens=2),
                done=True,
            )

        async def stream(self, request: ChatRequest):
            raise AssertionError("结构化流存在时不应退回旧字符串接口")

    provider = StructuredStreamingProvider()
    registry = ProviderRegistry()
    registry.register("local_answer", provider, capabilities={"chat.stream"})
    with TestClient(create_app(provider_registry=registry)) as client:
        response = client.post(
            "/api/v1/query/stream",
            json={"question": "问题", "evidence": [{"evidence_id": "doc-1", "text": "正文"}]},
        )

    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    done = [event for event in events if event["type"] == "done"]
    assert len(done) == 1
    payload = done[0]
    assert payload["trace_id"]
    assert payload["provider_result"]["status"] == "success"
    assert payload["provider_result"]["finish_reason"] == "stop"
    assert payload["provider_result"]["usage"] == {"input_tokens": 5, "output_tokens": 2}
    assert payload["timings"]["ttft_ms"] >= 0


@pytest.mark.asyncio
async def test_query_stream_maps_partial_provider_failure_and_releases_lease() -> None:
    """已经发出 token 后上游失败时，必须显式 partial/error/done 并释放槽位。"""

    class PartialProvider(FakeChatProvider):
        def __init__(self) -> None:
            super().__init__()
            self.iterator_closed = False

        async def stream_events(self, request: ChatRequest):
            """先产生可见片段，再模拟网络断连；finally 用于观测清理。"""

            try:
                yield ChatStreamEvent(text='{"answer_status":"answered","message":"部分')
                raise ProviderError(ProviderErrorKind.UNAVAILABLE, "upstream body contains secret")
            finally:
                self.iterator_closed = True

    provider = PartialProvider()
    registry = ProviderRegistry()
    registry.register("local_answer", provider, capabilities={"chat.stream"})
    app = create_app(provider_registry=registry)
    endpoint = _stream_endpoint(app)

    async with app.router.lifespan_context(app):
        response = await endpoint(_request(), _DisconnectProbe())
        assert isinstance(response, StreamingResponse)
        events = [_decode_event(frame) async for frame in response.body_iterator]

    error_events = [event for event in events if event["type"] == "error"]
    done_events = [event for event in events if event["type"] == "done"]
    assert error_events == [{"type": "error", "request_id": error_events[0]["request_id"], "seq": error_events[0]["seq"], "error_kind": "unavailable"}]
    assert len(done_events) == 1
    done = done_events[0]
    assert done["answer_status"] == "failed"
    assert done["provider_result"]["status"] == "partial"
    assert done["provider_result"]["error"]["kind"] == "unavailable"
    assert provider.iterator_closed is True
    assert app.state.schedulers["local_answer"].active_count == 0


@pytest.mark.asyncio
async def test_query_stream_total_deadline_closes_blocked_iterator_without_done() -> None:
    """总预算耗尽时直接关闭流，不为发送终态重新获得一份 deadline。"""

    class BlockingIterator:
        def __init__(self) -> None:
            self.closed = False
            self._never = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.closed:
                raise StopAsyncIteration
            await self._never.wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    class SlowProvider(FakeChatProvider):
        def __init__(self) -> None:
            super().__init__()
            self.iterator = BlockingIterator()

        def stream_events(self, request: ChatRequest):
            return self.iterator

    provider = SlowProvider()
    registry = ProviderRegistry()
    registry.register("local_answer", provider, capabilities={"chat.stream"})
    app = create_app(provider_registry=registry)
    endpoint = _stream_endpoint(app)

    async with app.router.lifespan_context(app):
        response = await endpoint(_request(timeout_ms=1_000), _DisconnectProbe())
        assert isinstance(response, StreamingResponse)
        events = [_decode_event(frame) async for frame in response.body_iterator]

    # deadline 到期时已经没有足够的发送预算，客户端应按“未完成流”处理，
    # 但上游迭代器和 scheduler 槽位必须确定性回收。
    assert not any(event["type"] == "done" for event in events)
    assert provider.iterator.closed is True
    assert app.state.schedulers["local_answer"].active_count == 0


@pytest.mark.asyncio
async def test_query_stream_active_cancellation_closes_iterator_and_releases_lease() -> None:
    """取消正在读取的流时，不发送 done，且必须传播取消并释放槽位。"""

    class CancellableProvider(FakeChatProvider):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.closed = False
            self._never = asyncio.Event()

        async def stream_events(self, request: ChatRequest):
            try:
                yield ChatStreamEvent(text="partial")
                self.started.set()
                await self._never.wait()
            finally:
                self.closed = True

    provider = CancellableProvider()
    registry = ProviderRegistry()
    registry.register("local_answer", provider, capabilities={"chat.stream"})
    app = create_app(provider_registry=registry)
    endpoint = _stream_endpoint(app)

    async with app.router.lifespan_context(app):
        response = await endpoint(_request(), _DisconnectProbe())
        assert isinstance(response, StreamingResponse)
        iterator = response.body_iterator
        first_events = []
        for _ in range(4):
            first_events.append(_decode_event(await iterator.__anext__()))
        assert first_events[-1]["type"] == "token"

        drain_task = asyncio.create_task(iterator.__anext__())
        await provider.started.wait()
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert provider.closed is True
        assert app.state.schedulers["local_answer"].active_count == 0


@pytest.mark.asyncio
async def test_query_stream_release_survives_cancelled_iterator_cleanup() -> None:
    """即使上游 aclose 自身抛出 CancelledError，也不能遗留 active lease。"""

    class CancelOnCloseIterator:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.closed:
                raise StopAsyncIteration
            self.closed = True
            return ChatStreamEvent(text="partial")

        async def aclose(self) -> None:
            self.closed = True
            raise asyncio.CancelledError

    class Provider(FakeChatProvider):
        def __init__(self) -> None:
            super().__init__()
            self.iterator = CancelOnCloseIterator()

        def stream_events(self, request: ChatRequest):
            return self.iterator

    provider = Provider()
    registry = ProviderRegistry()
    registry.register("local_answer", provider, capabilities={"chat.stream"})
    app = create_app(provider_registry=registry)
    endpoint = _stream_endpoint(app)

    async with app.router.lifespan_context(app):
        response = await endpoint(_request(), _DisconnectProbe())
        assert isinstance(response, StreamingResponse)
        iterator = response.body_iterator
        for _ in range(4):
            event = _decode_event(await iterator.__anext__())
        assert event["type"] == "token"

        with pytest.raises(asyncio.CancelledError):
            await iterator.aclose()

        # 这是当前实现的关键回归断言：清理异常不能跳过 lease.release。
        assert app.state.schedulers["local_answer"].active_count == 0


def test_query_stream_filters_reasoning_tokens_before_sending_sse() -> None:
    """模型把 reasoning 放进正文时，客户端只能收到可解析的可见 JSON。"""

    class ReasoningProvider(FakeChatProvider):
        async def stream_events(self, request: ChatRequest):
            # 故意把标签拆在不同事件中，模拟真实 HTTP chunk 边界。
            yield ChatStreamEvent(text="<th")
            yield ChatStreamEvent(text="ink>secret reasoning</think>\n\n```json\n{")
            yield ChatStreamEvent(
                text='"answer_status":"answered","message":"答案",'
                '"excerpts":[{"evidence_id":"doc-1","quote":"正文"}]}\n```'
            )
            yield ChatStreamEvent(finish_reason="stop", done=True)

    provider = ReasoningProvider()
    registry = ProviderRegistry()
    registry.register("local_answer", provider, capabilities={"chat.stream"})
    with TestClient(create_app(provider_registry=registry)) as client:
        response = client.post(
            "/api/v1/query/stream",
            json={"question": "问题", "evidence": [{"evidence_id": "doc-1", "text": "正文"}]},
        )

    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    tokens = "".join(event.get("text", "") for event in events if event["type"] == "token")
    done = [event for event in events if event["type"] == "done"]
    assert response.status_code == 200
    assert "secret reasoning" not in tokens
    assert "<think>" not in tokens
    assert len(done) == 1
    assert done[0]["answer_status"] == "answered"
