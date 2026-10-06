"""在线主用与单次本地兜底契约，不访问真实网关。"""

import time

import pytest

from enterprise_rag.providers.errors import ProviderError, ProviderErrorKind
from enterprise_rag.providers.protocols import ChatRequest, ChatStreamEvent
from enterprise_rag.providers.routed import DeepSeekSettings, PolicyRoutedProvider, model_scope


class Provider:
    """记录调用并可注入归一化失败。"""

    def __init__(self, error=None):
        """保存固定错误供故障矩阵使用。"""
        self.calls, self.error = [], error

    async def complete(self, request):
        """返回模型标识，不生成敏感数据。"""
        self.calls.append(request)
        if self.error:
            raise self.error
        return request.model


def route(error=None, tenants=("a",)):
    """构造开启但按租户限制外发的策略。"""
    local, online = Provider(), Provider(error)
    routed = PolicyRoutedProvider(
        local, online, DeepSeekSettings(enabled=True, api_key="test-key", allowed_tenants=tenants),
        "local-model",
    )
    return routed, local, online


async def test_no_scope_or_allowlist_never_calls_online():
    """匿名摘录入口与未允许 tenant 都不外发。"""
    routed, local, online = route(tenants=())
    await routed.complete(ChatRequest(model="alias", messages=()))
    with model_scope("a", time.monotonic() + 10):
        await routed.complete(ChatRequest(model="alias", messages=()))
    assert len(local.calls) == 2 and not online.calls


async def test_primary_and_one_way_fallback():
    """明确可用性故障只切换一次且使用固定模型。"""
    routed, local, online = route(ProviderError(ProviderErrorKind.UNAVAILABLE, "secret"))
    with model_scope("a", time.monotonic() + 10):
        assert await routed.complete(ChatRequest(model="alias", messages=())) == "local-model"
    assert len(online.calls) == len(local.calls) == 1
    assert online.calls[0].model == "deepseek-v4.1-flash"


async def test_authentication_failure_does_not_fallback():
    """配置/认证故障不被静默掩盖。"""
    routed, local, _ = route(ProviderError(ProviderErrorKind.AUTHENTICATION, "secret"))
    with model_scope("a", time.monotonic() + 10), pytest.raises(ProviderError):
        await routed.complete(ChatRequest(model="alias", messages=()))
    assert not local.calls


async def test_exhausted_deadline_never_resets():
    """总预算已耗尽时不启动备用模型。"""
    routed, local, _ = route()
    with model_scope("a", time.monotonic() - 1), pytest.raises(ProviderError) as exc:
        await routed.complete(ChatRequest(model="alias", messages=()))
    assert exc.value.kind is ProviderErrorKind.TIMEOUT and not local.calls


async def test_stream_after_first_event_never_switches():
    """流式已提交任何事件后只能失败，不能拼接备用模型的回答。"""
    routed, local, online = route()

    async def interrupted(request):
        """输出一片后模拟断流。"""
        online.calls.append(request)
        yield ChatStreamEvent(text="first")
        raise ProviderError(ProviderErrorKind.UNAVAILABLE, "secret")

    online.stream_events = interrupted
    received = []
    with model_scope("a", time.monotonic() + 10), pytest.raises(ProviderError):
        async for event in routed.stream_events(ChatRequest(model="alias", messages=())):
            received.append(event.text)
    assert received == ["first"] and not local.calls and routed.fallback_count == 0


async def test_stream_before_first_event_falls_back_once():
    """未输出事件的可用性错误允许一次本地兜底。"""
    routed, local, online = route()

    async def broken(request):
        """在第一片之前报稳定不可用错误。"""
        online.calls.append(request)
        raise ProviderError(ProviderErrorKind.UNAVAILABLE, "secret")
        yield  # pragma: no cover

    async def completed(request):
        """本地输出一片并结束。"""
        local.calls.append(request)
        yield ChatStreamEvent(text="local")

    online.stream_events, local.stream_events = broken, completed
    with model_scope("a", time.monotonic() + 10):
        events = [event async for event in routed.stream_events(ChatRequest(model="alias", messages=()))]
    assert events[0].text == "local" and routed.fallback_count == 1
