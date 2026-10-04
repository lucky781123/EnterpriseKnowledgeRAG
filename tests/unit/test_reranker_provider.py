import asyncio
from threading import Event

import pytest

from enterprise_rag.reranking.adapters import SyncRerankerAdapter
from enterprise_rag.reranking.protocols import RerankRequest


class FakeSyncReranker:
    def rerank(self, query, documents, top_k=None):
        results = [
            {"index": index, "text": text, "score": float(len(text))}
            for index, text in enumerate(documents)
        ]
        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:top_k] if top_k else results


@pytest.mark.asyncio
async def test_adapter_normalizes_existing_sync_reranker():
    adapter = SyncRerankerAdapter(FakeSyncReranker())
    request = RerankRequest(query="企业知识库", documents=["短", "更长的证据"], top_k=1)
    results = await adapter.rerank(request)
    assert len(results) == 1
    assert results[0].index == 1
    assert results[0].text == "更长的证据"


@pytest.mark.asyncio
async def test_adapter_health_check_is_explicit():
    adapter = SyncRerankerAdapter(FakeSyncReranker())
    assert await adapter.health_check() is True


@pytest.mark.asyncio
async def test_cancel_keeps_slot_until_native_worker_finishes() -> None:
    """请求取消后仍保留模型占位，不能让新请求与未结束的native批次并行。"""
    started = Event()
    release = Event()
    calls = []
    cancellation = []

    class Controlled:
        """用线程事件模拟无法立即中断的native批次。"""

        def rerank_cancellable(self, query, documents, top_k, cancel):
            """记录取消信号，直到测试释放当前批次才返回。"""
            calls.append(query)
            cancellation.append(cancel)
            started.set()
            assert release.wait(3)
            return [{"index": 0, "text": documents[0], "score": 1.0}]

    adapter = SyncRerankerAdapter(Controlled())
    first = asyncio.create_task(adapter.rerank(RerankRequest(query="first", documents=["正文"])))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert cancellation[0].is_set()
        second = asyncio.create_task(adapter.rerank(RerankRequest(query="second", documents=["正文"])))
        await asyncio.sleep(0.03)
        assert calls == ["first"]
        release.set()
        assert len(await asyncio.wait_for(second, 2)) == 1
        assert calls == ["first", "second"]
    finally:
        release.set()


@pytest.mark.asyncio
async def test_worker_error_releases_slot_for_next_request() -> None:
    """线程异常也必须释放准入锁，避免后续请求永久等待。"""
    class FailingOnce:
        """第一次推理失败，第二次返回正常结果。"""

        def rerank(self, query, documents, top_k):
            """通过查询标识模拟异常，不依赖真实权重。"""
            if query == "fail":
                raise RuntimeError("测试故障")
            return [{"index": 0, "text": documents[0], "score": 1.0}]

    adapter = SyncRerankerAdapter(FailingOnce())
    with pytest.raises(RuntimeError, match="测试故障"):
        await adapter.rerank(RerankRequest(query="fail", documents=["正文"]))
    results = await asyncio.wait_for(
        adapter.rerank(RerankRequest(query="ok", documents=["正文"])), 1,
    )
    assert len(results) == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_never_starts_native_work() -> None:
    """排队者取消后不提交工作线程，避免无用户等待的推理浪费资源。"""
    started, release = Event(), Event()
    calls = []

    class Controlled:
        """阻塞第一个重排，观察第二个被取消的排队者。"""

        def rerank(self, query, documents, top_k):
            """当前native batch仅由测试事件结束。"""
            calls.append(query)
            started.set()
            assert release.wait(3)
            return [{"index": 0, "text": documents[0], "score": 1.0}]

    adapter = SyncRerankerAdapter(Controlled())
    first = asyncio.create_task(adapter.rerank(RerankRequest(query="first", documents=["正文"])))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        second = asyncio.create_task(adapter.rerank(RerankRequest(query="second", documents=["正文"])))
        await asyncio.sleep(0.03)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        release.set()
        await first
        assert calls == ["first"]
    finally:
        release.set()
