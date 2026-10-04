"""本地HTTP Reranker契约测试，不把MockTransport视作真实模型验收。"""

import asyncio
import json

import httpx
import pytest

from enterprise_rag.reranking.http import HTTPReranker
from enterprise_rag.reranking.protocols import RerankRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", ["http://localhost:8003", "http://localhost:8003/v1/"])
async def test_maps_requested_candidates_and_top_n(base_url):
    """根地址和/v1均请求固定端点；模型返回index映射回原候选，不接受新正文。"""
    def handler(request):
        """返回模型列表及用户示例形状的排序响应。"""
        if request.method == "GET":
            assert request.url.path == "/v1/models"
            return httpx.Response(200, json={"data": [{"id": "jina"}]})
        assert request.url.path == "/v1/rerank"
        assert json.loads(request.content) == {
            "model": "jina", "query": "茶", "documents": ["茶", "篮球", "健康"], "top_n": 2,
        }
        return httpx.Response(200, json={"results": [
            {"index": 2, "relevance_score": 0.8, "document": {"text": "健康"}},
            {"index": 0, "relevance_score": 0.7},
        ]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = HTTPReranker(client, base_url, "jina")
        assert await provider.health_check()
        rows = await provider.rerank(RerankRequest("茶", ["茶", "篮球", "健康"], 2))
    assert [(row.index, row.text, row.score) for row in rows] == [(2, "健康", 0.8), (0, "茶", 0.7)]


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    [{"index": -1, "relevance_score": 0.8}],
    [{"index": 2, "relevance_score": 0.8}],
    [{"index": True, "relevance_score": 0.8}],
    [{"index": 0, "relevance_score": "0.8"}],
    [{"index": 0, "relevance_score": True}],
    [{"index": 0, "relevance_score": 0.8, "document": {"text": "其他租户"}}],
    [],
])
async def test_rejects_corrupt_identity_or_score(rows):
    """非法identity/分数/正文/数量不能成为有效重排结果。"""
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"results": rows}),
    )) as client:
        with pytest.raises(ValueError):
            await HTTPReranker(client, "http://localhost", "jina").rerank(
                RerankRequest("茶", ["茶"], 1),
            )


@pytest.mark.asyncio
async def test_rejects_nonfinite_scores_and_duplicate_indexes():
    """拒绝非有限分数和重复index，防止同一候选被伪装为多条证据。"""
    for content in (
        '{"results":[{"index":0,"relevance_score":NaN}]}',
        '{"results":[{"index":0,"relevance_score":0.9},{"index":0,"relevance_score":0.8}]}',
    ):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _, content=content: httpx.Response(200, content=content),
        )) as client:
            with pytest.raises(ValueError):
                await HTTPReranker(client, "http://localhost", "jina").rerank(
                    RerankRequest("茶", ["茶", "篮球"], 2 if "0.9" in content else 1),
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 503])
async def test_http_failures_do_not_become_success(status):
    """上游HTTP错误交由检索层按既有政策处理，适配器不重试或伪造成功。"""
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(status),
    )) as client:
        provider = HTTPReranker(client, "http://localhost", "jina")
        assert not await provider.health_check()
        with pytest.raises(httpx.HTTPStatusError):
            await provider.rerank(RerankRequest("茶", ["茶"], 1))


@pytest.mark.asyncio
async def test_cancellation_reaches_http_transport():
    """取消等待会传到HTTP协程；不遗留进程内模型worker或自动重试。"""
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def handler(_):
        """模拟慢服务并记录取消传播。"""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(HTTPReranker(client, "http://localhost", "jina").rerank(
            RerankRequest("茶", ["茶"], 1),
        ))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()


@pytest.mark.asyncio
async def test_read_timeout_propagates_without_retry():
    """只发一次HTTP调用；读取超时交给检索层，不扩大预算重试。"""
    calls = []
    def handler(request):
        """验证显式局部预算，模拟读取超时。"""
        calls.append(request)
        assert request.extensions["timeout"]["read"] == 60
        raise httpx.ReadTimeout("timeout", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout):
            await HTTPReranker(client, "http://localhost", "jina").rerank(
                RerankRequest("茶", ["茶"], 1),
            )
    assert len(calls) == 1
