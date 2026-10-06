"""缓存 Provider 的批次、隔离和故障契约，不访问真实模型或 Redis。"""

import asyncio

import pytest

from enterprise_rag.infrastructure.redis import (
    CachedTenantEmbeddingProvider,
    TenantEmbeddingCache,
)


class MemoryCache:
    """只模拟 get/set 以验证 Provider 的回源次数。"""

    def __init__(self) -> None:
        """为每个测试创建独立 key 空间。"""
        self.values = {}

    async def get(self, key):
        """读取原始 JSON 缓存。"""
        return self.values.get(key)

    async def set(self, key, value, *, ex):
        """记录带 TTL 的写入。"""
        assert ex > 0
        self.values[key] = value


class Embeddings:
    """记录真实回源输入，生成确定性二维向量。"""

    def __init__(self) -> None:
        """初始化调用记录和健康状态。"""
        self.calls = []
        self.healthy = True

    async def embed(self, texts):
        """保留原始文本，不在缓存层改变空白或 Unicode。"""
        self.calls.append(list(texts))
        return [[float(len(text)), 1.0] for text in texts]

    async def health_check(self):
        """返回上游自身状态，不以缓存命中代替 readiness。"""
        return self.healthy


def bind(provider, cache, *, tenant="a", profile="qwen:2:v1"):
    """绑定服务端提供的 tenant 和不可变模型 profile。"""
    return CachedTenantEmbeddingProvider(
        provider, cache, tenant=tenant, profile=profile, dimension=2,
    )


@pytest.mark.asyncio
async def test_deduplicates_batch_and_reuses_next_batch_without_normalizing() -> None:
    """批内去重、跨批复用，并保持输入顺序和精确文本 identity。"""
    provider = Embeddings()
    cached = bind(provider, TenantEmbeddingCache(MemoryCache()))
    output = await cached.embed(["text", "text", "text "])
    assert provider.calls == [["text", "text "]]
    assert output == [[4.0, 1.0], [4.0, 1.0], [5.0, 1.0]]
    # 返回列表互相独立，调用者修改一项不能污染同批结果或缓存。
    output[0][0] = 999
    assert output[1] == [4.0, 1.0]
    assert await cached.embed(["text", "new"]) == [[4.0, 1.0], [3.0, 1.0]]
    assert provider.calls == [["text", "text "], ["new"]]
    provider.healthy = False
    assert not await cached.health_check()


@pytest.mark.asyncio
async def test_provider_never_reuses_other_tenant_or_profile() -> None:
    """同文本跨租户或模型版本必须分别调用 Provider。"""
    provider = Embeddings()
    cache = TenantEmbeddingCache(MemoryCache())
    for tenant, profile in (("a", "v1"), ("b", "v1"), ("a", "v2")):
        await bind(provider, cache, tenant=tenant, profile=profile).embed(["text"])
    assert provider.calls == [["text"], ["text"], ["text"]]


@pytest.mark.asyncio
async def test_cache_outage_still_uses_real_provider() -> None:
    """缓存读写失败都不影响真实回源，不伪造向量。"""
    class Broken(MemoryCache):
        """只在缓存层注入故障。"""

        async def get(self, key):
            """模拟缓存读超时。"""
            raise TimeoutError()

        async def set(self, key, value, *, ex):
            """模拟缓存写超时。"""
            raise TimeoutError()

    provider = Embeddings()
    cached = bind(provider, TenantEmbeddingCache(Broken()))
    assert await cached.embed(["text"]) == [[4.0, 1.0]]
    assert provider.calls == [["text"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "bad", [[1.0]], [[True, 1]], [[10**1000, 1]]])
async def test_invalid_model_output_is_safe_and_never_cached(invalid) -> None:
    """非法输出统一拒绝，避免 OverflowError 或部分缓存污染。"""
    class Invalid(Embeddings):
        """提供一次非法模型响应。"""

        async def embed(self, texts):
            """返回需要被校验拦截的响应。"""
            return invalid

    client = MemoryCache()
    with pytest.raises(ValueError, match="embedding"):
        await bind(Invalid(), TenantEmbeddingCache(client)).embed(["text"])
    assert not client.values


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["text", b"text", [], [" "], [None]])
async def test_scalar_or_empty_input_is_rejected_before_provider(invalid) -> None:
    """单字符串不能被拆成字符批次，错误输入不调用模型。"""
    provider = Embeddings()
    with pytest.raises((ValueError, TypeError)):
        await bind(provider, TenantEmbeddingCache(MemoryCache())).embed(invalid)
    assert not provider.calls


@pytest.mark.asyncio
async def test_cache_cancellation_propagates_without_model_call() -> None:
    """请求取消不能被当成缓存 miss 后继续生成。"""
    class Cancelled(MemoryCache):
        """模拟连接读取时被请求取消。"""

        async def get(self, key):
            """将取消信号交还调用方。"""
            raise asyncio.CancelledError()

    provider = Embeddings()
    with pytest.raises(asyncio.CancelledError):
        await bind(provider, TenantEmbeddingCache(Cancelled())).embed(["text"])
    assert not provider.calls
