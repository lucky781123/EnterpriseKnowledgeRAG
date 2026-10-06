"""Redis 缓存、token 锁及 token-bucket 限流；Redis 不保存长期事实。"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from collections.abc import Sequence
from dataclasses import dataclass

# 对比 token 与释放在一个 Lua 操作内完成，避免误删另一个持有者的租约。
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
# 全部计算使用 Redis TIME，同一 key 原子扣减；TTL 清理闲置 bucket。
_TOKEN_BUCKET = """
local nowparts = redis.call('TIME')
local now = tonumber(nowparts[1]) + tonumber(nowparts[2]) / 1000000
local cap = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local values = redis.call('HMGET', KEYS[1], 'tokens', 'updated')
local tokens = tonumber(values[1]) or cap
local updated = tonumber(values[2]) or now
tokens = math.min(cap, tokens + math.max(0, now - updated) * rate)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(cap / rate * 2000))
return allowed
"""


def _identity(*parts: str) -> str:
    """长度与分隔符通过 JSON 固定，避免组合 key 歧义且不暴露明文。"""
    if any(not isinstance(part, str) or not part for part in parts):
        raise ValueError("tenant/profile/resource identity 不能为空")
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class RedisLock:
    """随机 token 仅用于短期协调；发布还必须经过 PostgreSQL fencing。"""

    key: str
    token: str


class RedisCoordinator:
    """协调故障向上传播，调用方不能按成功准入或持锁处理。"""

    def __init__(self, client) -> None:
        """接受已配置 timeout/TLS 的异步 Redis client，便于依赖注入。"""
        self.client = client

    async def acquire_lock(self, tenant: str, resource: str, *, ttl_ms: int) -> RedisLock | None:
        """用 SET NX PX 获取有限租约，不在这里承诺 exactly-once。"""
        if type(ttl_ms) is not int or ttl_ms <= 0:
            raise ValueError("ttl_ms 必须为正整数")
        lease = RedisLock("rag:lock:v1:" + _identity(tenant, resource), secrets.token_hex(32))
        accepted = await self.client.set(lease.key, lease.token, nx=True, px=ttl_ms)
        return lease if accepted else None

    async def release_lock(self, lease: RedisLock) -> bool:
        """只释放仍属于该 token 的锁，不能释放过期后的新持有者。"""
        return bool(await self.client.eval(_RELEASE, 1, lease.key, lease.token))

    async def renew_lock(self, lease: RedisLock, *, ttl_ms: int) -> bool:
        """同 token 原子续期；失败后调用方必须停止受保护阶段。"""
        if type(ttl_ms) is not int or ttl_ms <= 0:
            raise ValueError("ttl_ms 必须为正整数")
        return bool(await self.client.eval(_RENEW, 1, lease.key, lease.token, ttl_ms))

    async def allow(
        self, tenant: str, operation: str, *, capacity: int, refill_per_second: float,
    ) -> bool:
        """执行单租户 token bucket；全局与租户策略由上层按顺序组合。"""
        if (
            type(capacity) is not int or capacity <= 0
            or isinstance(refill_per_second, bool)
            or not math.isfinite(refill_per_second) or refill_per_second <= 0
        ):
            raise ValueError("限流 capacity/rate 必须为有限正数")
        key = "rag:limit:v1:" + _identity(tenant, operation)
        return bool(await self.client.eval(_TOKEN_BUCKET, 1, key, capacity, refill_per_second))


class TenantEmbeddingCache:
    """仅缓存纯向量；不保存答案、ACL、原文或可见性结论。"""

    def __init__(self, client, *, ttl_seconds: int = 86400) -> None:
        """缓存写入始终带 TTL，防止无界保留。"""
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("缓存 TTL 必须为正整数")
        self.client = client
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def key(tenant: str, profile: str, text: str) -> str:
        """按 tenant、不可变 profile 和精确文本隔离，不规范化正文。"""
        return "rag:embedding:v1:" + _identity(tenant, profile, text)

    @staticmethod
    def _valid(vector, dimension: int) -> bool:
        """拒绝错误维度、bool、非数字及非有限值。"""
        if (
            type(dimension) is not int or dimension <= 0
            or not isinstance(vector, list) or len(vector) != dimension
        ):
            return False
        try:
            return all(
                type(value) in {int, float} and math.isfinite(float(value))
                for value in vector
            )
        except (OverflowError, TypeError, ValueError):
            return False

    async def get(
        self, tenant: str, profile: str, text: str, *, dimension: int,
    ) -> list[float] | None:
        """缓存损坏或服务不可用按 miss 回源；不吞掉 asyncio 取消。"""
        key = self.key(tenant, profile, text)
        try:
            raw = await self.client.get(key)
            if raw is None:
                return None
            payload = json.loads(raw)
            if (
                not isinstance(payload, dict) or payload.get("schema") != 1
                or not self._valid(payload.get("vector"), dimension)
            ):
                return None
            return [float(value) for value in payload["vector"]]
        except Exception:  # noqa: BLE001 - 缓存故障按 miss 回源，不是长期事实错误。
            # 缓存不是事实源；不回显异常中的地址、密码或正文。
            return None

    async def put(
        self, tenant: str, profile: str, text: str, vector: list[float], *, dimension: int,
    ) -> None:
        """先验证向量，再尽力缓存；写缓存失败不影响真实 embedding 结果。"""
        key = self.key(tenant, profile, text)
        if not self._valid(vector, dimension):
            raise ValueError("embedding 缓存向量非法")
        raw = json.dumps({"schema": 1, "vector": vector}, allow_nan=False)
        try:
            await self.client.set(key, raw, ex=self.ttl_seconds)
        except Exception:  # noqa: BLE001 - 缓存故障不能阻断真实 Embedding。
            return


class CachedTenantEmbeddingProvider:
    """显式绑定可信 tenant，不从问题正文或请求自报字段获取身份。"""

    def __init__(self, provider, cache: TenantEmbeddingCache, *, tenant: str, profile: str,
                 dimension: int) -> None:
        """只缓存固定 tenant/profile 的模型输出，不混用模型空间。"""
        _identity(tenant, profile)
        if type(dimension) is not int or dimension <= 0:
            raise ValueError("dimension 必须为正整数")
        self.provider, self.cache = provider, cache
        self.tenant, self.profile, self.dimension = tenant, profile, dimension

    async def embed(self, texts) -> list[list[float]]:
        """复用有效缓存并在本批去重；跨 Worker 复用由后续持久 checkpoint 保障。"""
        if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
            raise TypeError("texts 必须是非空文本列表")
        values = list(texts)
        if not values or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("texts 必须是非空文本列表")
        found = {}
        missing = []
        for text in dict.fromkeys(values):
            vector = await self.cache.get(
                self.tenant, self.profile, text, dimension=self.dimension,
            )
            if vector is None:
                missing.append(text)
            else:
                found[text] = vector
        if missing:
            vectors = await self.provider.embed(missing)
            if (
                not isinstance(vectors, list)
                or len(vectors) != len(missing)
                or any(
                    not self.cache._valid(vector, self.dimension) for vector in vectors
                )
            ):
                raise ValueError("embedding 返回数量或维度非法")
            for text, vector in zip(missing, vectors, strict=True):
                found[text] = list(vector)
                await self.cache.put(
                    self.tenant, self.profile, text, list(vector), dimension=self.dimension,
                )
        return [list(found[text]) for text in values]

    async def health_check(self) -> bool:
        """readiness 仍依赖真实模型；缓存不能让离线模型伪装健康。"""
        return await self.provider.health_check()
