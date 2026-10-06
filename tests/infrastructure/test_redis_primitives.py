"""Redis 原语契约测试；替身不代表真实 Redis 或多进程验收。"""

import math

import pytest

from enterprise_rag.infrastructure.redis import (
    RedisCoordinator,
    TenantEmbeddingCache,
)


class MemoryRedis:
    """保留 TTL 与 Lua 参数供断言，不用它冒充 Redis 原子性实验。"""

    def __init__(self) -> None:
        """初始化独立测试 key 空间。"""
        self.values = {}
        self.expiries = {}
        self.evals = []

    async def get(self, key):
        """读取模拟值。"""
        return self.values.get(key)

    async def set(self, key, value, *, ex=None, px=None, nx=False):
        """记录带 TTL 和 NX 的写入。"""
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.expiries[key] = ex or px
        return True

    async def eval(self, script, numkeys, *args):
        """只模拟 token 对比释放，其他 Lua 保留给真实集成验证。"""
        self.evals.append((script, numkeys, args))
        if "DEL" in script and self.values.get(args[0]) == args[1]:
            del self.values[args[0]]
            return 1
        return 0


@pytest.mark.asyncio
async def test_embeddings_are_tenant_profile_scoped_and_ttl_bounded() -> None:
    """同文本不同 tenant/profile 不能复用缓存，key 不含正文。"""
    redis = MemoryRedis()
    cache = TenantEmbeddingCache(redis, ttl_seconds=60)
    await cache.put("a", "model:2:v1", "private text", [1.0, 2.0], dimension=2)
    assert await cache.get("a", "model:2:v1", "private text", dimension=2) == [1.0, 2.0]
    assert await cache.get("b", "model:2:v1", "private text", dimension=2) is None
    assert await cache.get("a", "model:2:v2", "private text", dimension=2) is None
    assert all("private text" not in key for key in redis.values)
    assert set(redis.expiries.values()) == {60}


@pytest.mark.asyncio
async def test_invalid_vectors_are_never_reused() -> None:
    """维度、NaN、无穷和类型错误不会成为命中。"""
    redis = MemoryRedis()
    cache = TenantEmbeddingCache(redis)
    for vector in ([math.nan, 1.0], [math.inf, 0.0], [True, 1.0], [1.0]):
        with pytest.raises(ValueError):
            await cache.put("a", "p", "text", vector, dimension=2)
    redis.values[cache.key("a", "p", "text")] = b'{"vector":[1,2],"schema":999}'
    assert await cache.get("a", "p", "text", dimension=2) is None


@pytest.mark.asyncio
async def test_expired_owner_cannot_release_new_lock() -> None:
    """仅比较自己的随机 token 后释放，禁止误删后来者的锁。"""
    redis = MemoryRedis()
    coordinator = RedisCoordinator(redis)
    first = await coordinator.acquire_lock("tenant", "document", ttl_ms=5000)
    assert first is not None
    assert await coordinator.acquire_lock("tenant", "document", ttl_ms=5000) is None
    redis.values[first.key] = "new-owner"
    assert not await coordinator.release_lock(first)
    assert redis.values[first.key] == "new-owner"


@pytest.mark.asyncio
async def test_limiter_uses_tenant_key_and_server_time_lua() -> None:
    """速率判定使用原子 Lua 与服务端时间，不信任客户端时钟。"""
    redis = MemoryRedis()
    coordinator = RedisCoordinator(redis)
    assert not await coordinator.allow("tenant", "query", capacity=10, refill_per_second=5)
    script, numkeys, args = redis.evals[-1]
    assert "TIME" in script and "PEXPIRE" in script
    assert numkeys == 1
    assert "tenant" not in args[0]


@pytest.mark.asyncio
async def test_cache_failure_is_miss_but_lock_failure_propagates() -> None:
    """缓存可回源；协调服务故障不能伪造锁准入成功。"""
    class Broken(MemoryRedis):
        """模拟连接故障而非权限错误。"""

        async def get(self, key):
            """注入读取故障。"""
            raise ConnectionError("secret endpoint")

        async def set(self, *args, **kwargs):
            """注入协调故障。"""
            raise ConnectionError("secret endpoint")

    redis = Broken()
    assert await TenantEmbeddingCache(redis).get("a", "p", "text", dimension=2) is None
    with pytest.raises(ConnectionError):
        await RedisCoordinator(redis).acquire_lock("a", "doc", ttl_ms=5000)
