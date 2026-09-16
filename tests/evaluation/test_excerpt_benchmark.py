"""F2-G benchmark 的确定性统计契约测试。

这些测试不启动真实模型，只锁定样本分类、nearest-rank 分位数和 Fake
基线的产物结构；真实 LM Studio 的时延必须由独立命令产生，不能由这里的
确定性测试冒充。
"""

import asyncio
import json

import pytest
import httpx

from scripts.excerpt_benchmark import (
    BenchmarkSample,
    _benchmark_request,
    _run_sse_sample,
    run_fake,
    run_local,
    summarize_samples,
)
from enterprise_rag.excerpts.service import ExcerptAnswerService
from enterprise_rag.providers.errors import ProviderErrorKind
from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.protocols import ChatRequest, ChatStreamEvent
from enterprise_rag.providers.scheduler import LocalLLMScheduler


def test_summarize_uses_nearest_rank_and_keeps_cancelled_out_of_success_latency() -> None:
    """P50/P95 使用固定 nearest-rank，取消样本不能混入成功延迟。"""

    samples = [
        BenchmarkSample(
            mode="json",
            index=1,
            outcome="success",
            completion_ms=10.0,
            queue_ms=1.0,
            ttft_ms=None,
            generation_ms=8.0,
        ),
        BenchmarkSample(
            mode="json",
            index=2,
            outcome="success",
            completion_ms=20.0,
            queue_ms=2.0,
            ttft_ms=None,
            generation_ms=17.0,
        ),
        BenchmarkSample(
            mode="json",
            index=3,
            outcome="failure",
            completion_ms=30.0,
            queue_ms=None,
            ttft_ms=None,
            generation_ms=None,
            error_kind="timeout",
        ),
        BenchmarkSample(
            mode="json",
            index=4,
            outcome="cancelled",
            completion_ms=40.0,
            queue_ms=3.0,
            ttft_ms=None,
            generation_ms=None,
        ),
    ]

    summary = summarize_samples(samples)

    assert summary["sample_count"] == 4
    assert summary["success_count"] == 2
    assert summary["failure_count"] == 1
    assert summary["cancelled_count"] == 1
    assert summary["success_latency_ms"]["raw"] == [10.0, 20.0]
    assert summary["success_latency_ms"]["p50_nearest_rank"] == 10.0
    assert summary["success_latency_ms"]["p95_nearest_rank"] == 20.0
    assert summary["error_counts"] == {"timeout": 1}


def test_summarize_empty_success_set_keeps_percentiles_null() -> None:
    """没有成功请求时不能用零或异常值冒充 P50/P95。"""

    summary = summarize_samples(
        [
            BenchmarkSample(
                mode="sse",
                index=1,
                outcome="failure",
                completion_ms=12.0,
                queue_ms=None,
                ttft_ms=None,
                generation_ms=None,
                error_kind="protocol",
            ),
            BenchmarkSample(
                mode="sse",
                index=2,
                outcome="cancelled",
                completion_ms=13.0,
                queue_ms=1.0,
                ttft_ms=None,
                generation_ms=None,
            ),
        ]
    )

    assert summary["success_count"] == 0
    assert summary["failure_rate"] == 0.5
    assert summary["cancelled_rate"] == 0.5
    assert summary["success_latency_ms"]["raw"] == []
    assert summary["success_latency_ms"]["p50_nearest_rank"] is None
    assert summary["success_latency_ms"]["p95_nearest_rank"] is None


@pytest.mark.asyncio
async def test_run_fake_marks_retrieval_and_production_capacity_not_applicable() -> None:
    """Fake 基线必须明确它没有测量检索或生产容量。"""

    result = await run_fake(2)

    assert result["provider"] == "fake"
    assert result["iterations"] == 2
    assert result["success_count"] == 2
    assert result["failure_count"] == 0
    assert "retrieval" in result["not_applicable"]
    assert "production_capacity" in result["not_applicable"]


@pytest.mark.asyncio
async def test_run_local_separates_json_and_sse_groups_with_mock_transport() -> None:
    """离线 mock 只验证 runner 编排，不冒充真实 LM Studio 观测。"""

    target_model = "qwen-test"

    def handler(request: httpx.Request) -> httpx.Response:
        """返回最小 OpenAI-compatible models/complete/stream 响应。"""

        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": target_model}]},
                request=request,
            )
        if request.url.path != "/v1/chat/completions":
            return httpx.Response(404, request=request)
        payload = json.loads(request.content)
        answer = {
            "answer_status": "answered",
            "message": "答案",
            "excerpts": [{"evidence_id": "doc-1", "quote": "确定性证据"}],
        }
        if payload["stream"]:
            body = (
                "data: "
                + json.dumps({"choices": [{"delta": {"content": json.dumps(answer, ensure_ascii=False)}}]})
                + "\n\n"
                + "data: "
                + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
                + "\n\n"
                + "data: [DONE]\n\n"
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=body.encode("utf-8"),
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "model": target_model,
                "choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await run_local(
            base_url="http://mock",
            model=target_model,
            iterations=1,
            concurrency_levels=(1,),
            warmup=0,
            cancellation_runs=0,
            group_timeout_seconds=5,
            client=client,
        )
    finally:
        await client.aclose()

    assert result["status"] == "completed"
    assert [group["mode"] for group in result["groups"]] == ["json", "sse"]
    assert all(group["summary"]["success_count"] == 1 for group in result["groups"])
    assert result["groups"][1]["samples"][0]["ttft_ms"] is not None
    assert result["metadata"]["fixture_hash"]
    assert result["metadata"]["prompt_hash"]
    assert result["metadata"]["config_hash"]


@pytest.mark.asyncio
async def test_stream_length_signal_is_reported_as_truncated_not_protocol_error() -> None:
    """上游明确 finish_reason=length 时，未闭合 reasoning 不能遮蔽截断分类。"""

    class TruncatedProvider(FakeChatProvider):
        async def stream_events(self, request: ChatRequest):
            yield ChatStreamEvent(text="<think>尚未结束的 reasoning")
            yield ChatStreamEvent(finish_reason="length", done=True)

    provider = TruncatedProvider()
    service = ExcerptAnswerService(provider)
    request, evidence = _benchmark_request(service, model="qwen-test", max_tokens=128)
    scheduler = LocalLLMScheduler(provider, max_concurrency=1, max_queue_size=1)

    sample = await _run_sse_sample(
        index=1,
        service=service,
        provider=provider,
        scheduler=scheduler,
        request=request,
        evidence=evidence,
        deadline=__import__("time").perf_counter() + 5,
    )

    assert sample.outcome == "failure"
    assert sample.error_kind == "truncated"
    assert sample.finish_reason == "length"
    assert sample.error_kind != ProviderErrorKind.PROTOCOL.value


@pytest.mark.asyncio
async def test_cancel_sample_cancels_blocked_stream_and_records_cleanup() -> None:
    """主动取消必须结束阻塞流，并记录非空的清理耗时。"""

    class BlockingProvider(FakeChatProvider):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False
            self.never = asyncio.Event()

        async def stream_events(self, request: ChatRequest):
            try:
                yield ChatStreamEvent(text='{"answer_status":"answered"')
                await self.never.wait()
            finally:
                self.closed = True

    provider = BlockingProvider()
    service = ExcerptAnswerService(provider)
    request, evidence = _benchmark_request(service, model="qwen-test", max_tokens=128)
    scheduler = LocalLLMScheduler(provider, max_concurrency=1, max_queue_size=1)

    from scripts.excerpt_benchmark import _run_cancel_sample

    sample = await _run_cancel_sample(
        index=1,
        service=service,
        provider=provider,
        scheduler=scheduler,
        request=request,
        evidence=evidence,
        deadline=__import__("time").perf_counter() + 5,
    )

    assert sample.outcome == "cancelled"
    assert sample.cancellation_requested is True
    assert sample.cancellation_observed is True
    assert sample.cleanup_ms is not None
    assert provider.closed is True
    assert scheduler.active_count == 0


@pytest.mark.asyncio
async def test_cancel_sample_cleans_first_visible_waiter_when_no_visible_token_arrives() -> None:
    """取消前没有可见 token 时，也必须回收等待首 token 的辅助 task。

    真实模型可能先输出被过滤的 reasoning，因此 ``first_visible`` 在主动取消
    前仍未触发。回归测试直接检查事件等待 task 是否残留，避免 CLI 退出时出现
    ``Task was destroyed but it is pending``，并确保修复的是 benchmark 的清理
    生命周期，而不是把取消结果伪装成模型成功。
    """

    class HiddenBlockingProvider(FakeChatProvider):
        """只输出不可见 reasoning，然后阻塞到调用方取消。"""

        def __init__(self) -> None:
            super().__init__()
            self.never = asyncio.Event()

        async def stream_events(self, request: ChatRequest):
            """让 sanitizer 始终无法设置首个可见 token 事件。"""

            try:
                await self.never.wait()
                if False:
                    # 保持这是一个 async generator，同时避免产生任何可见内容。
                    yield ChatStreamEvent(text="")
            finally:
                # 生成器的 finally 只用于确认上游被关闭；这里不需要额外状态。
                return

    provider = HiddenBlockingProvider()
    service = ExcerptAnswerService(provider)
    request, evidence = _benchmark_request(service, model="qwen-test", max_tokens=128)
    scheduler = LocalLLMScheduler(provider, max_concurrency=1, max_queue_size=1)

    from scripts.excerpt_benchmark import _run_cancel_sample

    leaked_waiters: list[asyncio.Task[object]] = []
    try:
        sample = await _run_cancel_sample(
            index=1,
            service=service,
            provider=provider,
            scheduler=scheduler,
            request=request,
            evidence=evidence,
            deadline=__import__("time").perf_counter() + 5,
        )
        await asyncio.sleep(0)
        leaked_waiters = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and getattr(task.get_coro(), "__qualname__", "") == "Event.wait"
        ]

        assert sample.outcome == "cancelled"
        assert leaked_waiters == []
    finally:
        # 断言失败时也要清理测试自身发现的残留 task，避免红灯阶段污染后续测试。
        for task in leaked_waiters:
            task.cancel()
        if leaked_waiters:
            await asyncio.gather(*leaked_waiters, return_exceptions=True)
