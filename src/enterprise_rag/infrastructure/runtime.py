"""显式创建与关闭 Redis 连接池；默认入口不会自动装配本模块。"""

from __future__ import annotations

from urllib.parse import urlsplit

from enterprise_rag.infrastructure.settings import InfrastructureSettings


class RedisRuntime:
    """分别管理缓存与协调实例的异步 client。"""

    def __init__(self, cache, coordination) -> None:
        """保存仅由服务端配置构造的 client。"""
        self.cache = cache
        self.coordination = coordination

    @classmethod
    def create(cls, settings: InfrastructureSettings) -> RedisRuntime:
        """延迟导入可选依赖；禁用状态不连接、不回退到本机默认地址。"""
        settings.validate()
        if not settings.enabled:
            raise ValueError("RAG_INFRA_ENABLED 尚未启用")
        from redis.asyncio import Redis

        options = {
            "socket_connect_timeout": 2,
            "socket_timeout": 2,
            "health_check_interval": 30,
            "max_connections": 32,
            "decode_responses": False,
        }

        def create_client(url: str):
            """TLS 显式要求证书验证，不能由调用方传入宽松参数。"""
            tls = {"ssl_cert_reqs": "required"} if urlsplit(url).scheme == "rediss" else {}
            return Redis.from_url(url, **options, **tls)

        return cls(
            create_client(settings.redis_cache_url),
            create_client(settings.redis_coordination_url),
        )

    async def close(self) -> None:
        """关闭两路连接池，不删除任何缓存、锁或 broker key。"""
        try:
            await self.cache.aclose()
        finally:
            await self.coordination.aclose()
