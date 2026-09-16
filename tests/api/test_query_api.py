"""F2-G JSON API 契约测试。

除正常响应和错误映射外，本文件还覆盖 scheduler 排队与总 deadline。
这些测试只验证本服务的边界和清理，不把 Fake Provider 的耗时当成模型性能。
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from enterprise_rag.api.app import create_app
from enterprise_rag.api.schemas import ExcerptQueryRequest, ExcerptResponse
from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.errors import ProviderError, ProviderErrorKind
from enterprise_rag.providers.protocols import ChatRequest
from enterprise_rag.providers.protocols import ChatResponse, ChatUsage
from enterprise_rag.providers.registry import ProviderRegistry


def _client(response: str) -> TestClient:
    registry = ProviderRegistry()
    registry.register("local_answer", FakeChatProvider(lambda request: response), capabilities={"chat.complete"})
    return TestClient(create_app(provider_registry=registry))


def _query_endpoint(app):
    """取出真实 JSON endpoint，便于异步测试细粒度取消和排队行为。"""

    return next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/v1/query")


def _request(timeout_ms: int = 120_000) -> ExcerptQueryRequest:
    """构造边界测试共用的最小合法请求。"""

    return ExcerptQueryRequest(
        question="问题",
        evidence=[{"evidence_id": "doc-1", "text": "正文"}],
        timeout_ms=timeout_ms,
    )


def test_query_returns_validated_excerpt_and_caller_supplied_context() -> None:
    response = json.dumps(
        {
            "answer_status": "answered",
            "message": "由部署任务执行",
            "excerpts": [{"evidence_id": "doc-1", "quote": "部署任务", "start": 1, "end": 5}],
        },
        ensure_ascii=False,
    )

    with _client(response) as client:
        result = client.post(
            "/api/v1/query",
            json={
                "question": "谁执行？",
                "evidence": [{"evidence_id": "doc-1", "text": "由部署任务执行"}],
            },
        )

    assert result.status_code == 200
    body = result.json()
    assert body["context_source"] == "caller_supplied"
    assert body["citation_validation"] == "passed"
    assert body["citations"][0]["quote"] == "部署任务"


def test_query_rejects_client_provider_configuration_fields() -> None:
    with _client('{"answer_status":"not_found","message":"资料不足","excerpts":[]}') as client:
        result = client.post(
            "/api/v1/query",
            json={
                "question": "问题",
                "evidence": [{"evidence_id": "doc-1", "text": "正文"}],
                "base_url": "http://attacker.invalid",
                "api_key": "secret",
            },
        )

    assert result.status_code == 422


def test_query_maps_provider_error_to_safe_http_response() -> None:
    """Provider 错误在 HTTP 边界转换为稳定状态码，不能把异常文本直接抛给客户端。"""

    class FailingProvider(FakeChatProvider):
        async def complete(self, request: ChatRequest):
            raise ProviderError(ProviderErrorKind.AUTHENTICATION, "上游认证失败")

    registry = ProviderRegistry()
    registry.register("local_answer", FailingProvider(), capabilities={"chat.complete"})
    with TestClient(create_app(provider_registry=registry), raise_server_exceptions=False) as client:
        result = client.post(
            "/api/v1/query",
            json={"question": "问题", "evidence": [{"evidence_id": "doc-1", "text": "正文"}]},
        )

    assert result.status_code == 502
    assert result.json() == {"error": {"kind": "authentication", "message": "上游认证失败"}}


def test_query_rejects_invalid_timeout_and_accepts_timeout_field() -> None:
    """请求可声明受服务端上限约束的 timeout_ms，但不能传入非正数。"""

    with _client('{"answer_status":"not_found","message":"资料不足","excerpts":[]}') as client:
        valid = client.post(
            "/api/v1/query",
            json={
                "question": "问题",
                "evidence": [{"evidence_id": "doc-1", "text": "正文"}],
                "timeout_ms": 1000,
            },
        )
        invalid = client.post(
            "/api/v1/query",
            json={
                "question": "问题",
                "evidence": [{"evidence_id": "doc-1", "text": "正文"}],
                "timeout_ms": 0,
            },
        )

    assert valid.status_code == 200
    assert invalid.status_code == 422


def test_query_returns_trace_provider_result_and_timings_without_raw_model_output() -> None:
    """JSON 响应要能观测调用，但不能把模型原始 JSON 正文直接泄露给客户端。"""

    class MetadataProvider(FakeChatProvider):
        async def complete(self, request: ChatRequest) -> ChatResponse:
            return ChatResponse(
                content=json.dumps(
                    {
                        "answer_status": "answered",
                        "message": "答案",
                        "excerpts": [{"evidence_id": "doc-1", "quote": "正文"}],
                    },
                    ensure_ascii=False,
                ),
                model="qwen-observed",
                finish_reason="stop",
                usage=ChatUsage(input_tokens=8, output_tokens=2),
            )

    registry = ProviderRegistry()
    registry.register("local_answer", MetadataProvider(), capabilities={"chat.complete"})
    with TestClient(create_app(provider_registry=registry)) as client:
        result = client.post(
            "/api/v1/query",
            json={"question": "问题", "evidence": [{"evidence_id": "doc-1", "text": "正文"}]},
        )

    assert result.status_code == 200
    body = result.json()
    assert body["trace_id"]
    provider_result = body["provider_result"]
    assert provider_result["request_id"] == body["request_id"]
    assert provider_result["provider"] == "local_answer"
    assert provider_result["status"] == "success"
    assert provider_result["model"] == "qwen-observed"
    assert provider_result["finish_reason"] == "stop"
    assert provider_result["usage"] == {"input_tokens": 8, "output_tokens": 2}
    assert provider_result.get("content") is None
    assert body["timings"]["queue_ms"] >= 0
    assert body["timings"]["ttft_ms"] is None
    assert body["timings"]["generation_ms"] >= 0
    assert body["timings"]["validation_ms"] >= 0
    assert body["timings"]["completion_ms"] >= 0


@pytest.mark.asyncio
async def test_query_maps_scheduler_queue_timeout_to_rate_limited_and_cleans_waiter(monkeypatch) -> None:
    """排队请求超过自身预算时返回 429，且不会遗留等待队列条目。"""

    monkeypatch.setenv("LOCAL_LLM_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("LOCAL_LLM_MAX_QUEUE_SIZE", "1")
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")

    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(FakeChatProvider):
        async def complete(self, request: ChatRequest) -> ChatResponse:
            started.set()
            await release.wait()
            return ChatResponse(
                content='{"answer_status":"not_found","message":"资料不足","excerpts":[]}',
                model=request.model,
            )

    provider = BlockingProvider()
    registry = ProviderRegistry()
    registry.register("local_answer", provider, capabilities={"chat.complete"})
    app = create_app(provider_registry=registry)
    endpoint = _query_endpoint(app)

    async with app.router.lifespan_context(app):
        first_task = asyncio.create_task(endpoint(_request()))
        await started.wait()

        # model_copy 不重新校验字段，专门把已验证请求的总预算缩到 5ms，
        # 以便在不等待 1 秒的情况下复现真实 scheduler queue timeout。
        short_request = _request().model_copy(update={"timeout_ms": 5})
        second = await endpoint(short_request)
        assert isinstance(second, JSONResponse)
        assert second.status_code == 429
        assert app.state.schedulers["local_answer"].waiting_count == 0

        release.set()
        first = await first_task
        assert isinstance(first, ExcerptResponse)
        assert first.answer_status == "abstained"
        assert app.state.schedulers["local_answer"].active_count == 0


@pytest.mark.asyncio
async def test_query_total_deadline_cancels_provider_and_releases_lease(monkeypatch) -> None:
    """模型调用超过绝对 deadline 时返回 504，并把取消传到 Provider。"""

    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")
    cancelled = asyncio.Event()
    never = asyncio.Event()

    class SlowProvider(FakeChatProvider):
        async def complete(self, request: ChatRequest) -> ChatResponse:
            try:
                await never.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("永远不会到达正常返回")

    registry = ProviderRegistry()
    registry.register("local_answer", SlowProvider(), capabilities={"chat.complete"})
    app = create_app(provider_registry=registry)
    endpoint = _query_endpoint(app)

    async with app.router.lifespan_context(app):
        # 同样绕过模型层的最小 1000ms 校验，仅用于快速验证绝对 deadline。
        short_request = _request().model_copy(update={"timeout_ms": 5})
        result = await endpoint(short_request)

    assert isinstance(result, JSONResponse)
    assert result.status_code == 504
    assert cancelled.is_set()
    assert app.state.schedulers["local_answer"].active_count == 0
