"""独立配置基础，不改变既有 Settings 的默认运行行为。"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class InfrastructureSettings:
    """保存自建/托管服务 endpoint；敏感值不进入 repr，功能需显式启用。"""

    enabled: bool = False
    environment: str = "development"
    redis_cache_url: str = field(default="", repr=False)
    redis_coordination_url: str = field(default="", repr=False)
    celery_enabled: bool = False
    celery_result_url: str = field(default="", repr=False)
    storage_provider: str = "local"
    s3_endpoint: str = field(default="", repr=False)
    s3_access_key: str = field(default="", repr=False)
    s3_secret_key: str = field(default="", repr=False)
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_prefix: str = "rag"
    embedding_cache_ttl_seconds: int = 86400
    task_time_limit_seconds: int = 900
    visibility_timeout_seconds: int = 3600
    result_ttl_seconds: int = 86400
    ingestion_embedding_base_url: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> InfrastructureSettings:
        """仅读启动进程环境；不打开 .env，不推断服务供应商或密码。"""
        def flag(name: str) -> bool:
            """读取显式开关，拼写错误拒绝而非静默关闭。"""
            value = os.getenv(name, "false").strip().lower()
            if value not in {"true", "false", "1", "0"}:
                raise ValueError(f"{name} 必须是 true/false")
            return value in {"true", "1"}

        def number(name: str, default: int) -> int:
            """读取正整数，不在异常中回显原始配置。"""
            try:
                value = int(os.getenv(name, str(default)))
                if value <= 0:
                    raise ValueError()
                return value
            except ValueError:
                raise ValueError(f"{name} 必须为正整数") from None

        settings = cls(
            enabled=flag("RAG_INFRA_ENABLED"),
            environment=os.getenv("APP_ENV", "development"),
            redis_cache_url=os.getenv("REDIS_CACHE_URL", ""),
            redis_coordination_url=os.getenv("REDIS_COORDINATION_URL", ""),
            celery_enabled=flag("RAG_CELERY_ENABLED"),
            celery_result_url=os.getenv("CELERY_RESULT_BACKEND", ""),
            storage_provider=os.getenv("OBJECT_STORAGE_PROVIDER", "local"),
            s3_endpoint=os.getenv("S3_ENDPOINT", ""),
            s3_access_key=os.getenv("S3_ACCESS_KEY_ID", ""),
            s3_secret_key=os.getenv("S3_SECRET_ACCESS_KEY", ""),
            s3_bucket=os.getenv("S3_BUCKET", ""),
            s3_region=os.getenv("S3_REGION", "us-east-1"),
            s3_prefix=os.getenv("S3_PREFIX", "rag"),
            ingestion_embedding_base_url=os.getenv("INGESTION_OLLAMA_BASE_URL", ""),
            embedding_cache_ttl_seconds=number("EMBEDDING_CACHE_TTL_SECONDS", 86400),
            task_time_limit_seconds=number("CELERY_TASK_TIME_LIMIT_SECONDS", 900),
            visibility_timeout_seconds=number("CELERY_VISIBILITY_TIMEOUT_SECONDS", 3600),
            result_ttl_seconds=number("CELERY_RESULT_TTL_SECONDS", 86400),
        )
        settings.validate()
        return settings

    def _redis_endpoint(self, value: str) -> tuple[str, int]:
        """检查 Redis URL 并只返回无凭据实例 identity。"""
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
                raise ValueError()
            if parsed.query or parsed.fragment:
                raise ValueError()
            if self.environment == "production" and parsed.scheme != "rediss":
                raise ValueError("生产 Redis 需要 TLS")
            port = 6379 if parsed.port is None else parsed.port
            if not 1 <= port <= 65535:
                raise ValueError()
            database = "0" if parsed.path in {"", "/"} else parsed.path[1:]
            if not database.isdigit():
                raise ValueError()
            return parsed.hostname.lower(), port
        except ValueError:
            message = "生产 Redis 需要 TLS 且 URL 必须有效" if (
                self.environment == "production"
            ) else "Redis URL 必须有效"
            raise ValueError(message) from None

    def validate(self) -> None:
        """只校验本地配置结构；不声称托管服务权限和容量已通过验证。"""
        if self.environment not in {"development", "test", "production"}:
            raise ValueError("APP_ENV 必须为 development/test/production")
        if any(type(value) is not int or value <= 0 for value in (
            self.embedding_cache_ttl_seconds, self.task_time_limit_seconds,
            self.visibility_timeout_seconds, self.result_ttl_seconds,
        )):
            raise ValueError("基础设施 TTL/预算必须为正整数")
        if self.task_time_limit_seconds <= 30:
            raise ValueError("Celery task time limit 必须大于 30 秒")
        if self.enabled:
            cache = self._redis_endpoint(self.redis_cache_url)
            coordination = self._redis_endpoint(self.redis_coordination_url)
            if cache == coordination:
                raise ValueError("缓存和协调 Redis 必须使用独立实例")
        if self.celery_enabled:
            if not self.enabled:
                raise ValueError("Celery 需要先启用 RAG_INFRA_ENABLED")
            # broker 和 backend 必须在同一独立协调实例，不能占用缓存实例。
            backend = self._redis_endpoint(self.celery_result_url)
            if backend != self._redis_endpoint(self.redis_coordination_url):
                raise ValueError("Celery backend 必须使用协调 Redis 实例")
            if self.visibility_timeout_seconds <= self.task_time_limit_seconds:
                raise ValueError("visibility_timeout 必须大于单阶段 hard time limit")
        if self.storage_provider not in {"local", "s3"}:
            raise ValueError("OBJECT_STORAGE_PROVIDER 只支持 local/s3")
        if self.storage_provider == "s3":
            try:
                parsed = urlsplit(self.s3_endpoint)
                valid_url = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                default_port = 443 if parsed.scheme == "https" else 80
                port = default_port if parsed.port is None else parsed.port
                if (
                    not valid_url or parsed.username or parsed.password
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                    or not 1 <= port <= 65535
                ):
                    raise ValueError()
                if self.environment == "production" and parsed.scheme != "https":
                    raise ValueError()
                if not all((self.s3_access_key, self.s3_secret_key, self.s3_bucket)):
                    raise ValueError()
            except ValueError:
                raise ValueError("S3 需要有效 endpoint、bucket、密钥；生产须 HTTPS") from None

    def celery_config(self) -> dict:
        """生成配置但不连接 broker；长期状态和死信仍由 PostgreSQL 实现。"""
        self.validate()
        if not self.celery_enabled:
            raise ValueError("RAG_CELERY_ENABLED 尚未启用")
        config = {
            "broker_url": self.redis_coordination_url,
            "result_backend": self.celery_result_url,
            "task_serializer": "json",
            "result_serializer": "json",
            "accept_content": ["json"],
            "task_acks_late": True,
            "task_reject_on_worker_lost": True,
            "worker_prefetch_multiplier": 1,
            "worker_concurrency": 1,
            "task_time_limit": self.task_time_limit_seconds,
            "task_soft_time_limit": max(1, self.task_time_limit_seconds - 30),
            "visibility_timeout": self.visibility_timeout_seconds,
            "broker_transport_options": {
                "visibility_timeout": self.visibility_timeout_seconds,
                "global_keyprefix": "rag:broker:v1:",
            },
            "result_backend_transport_options": {
                "visibility_timeout": self.visibility_timeout_seconds,
                "global_keyprefix": "rag:result:v1:",
            },
            "result_expires": self.result_ttl_seconds,
            "broker_connection_timeout": 5,
            "broker_connection_retry_on_startup": True,
            "timezone": "UTC",
            "enable_utc": True,
            "task_default_queue": "rag.ingestion",
            # 不在 events/results 写参数或正文；任务事件需在 Worker 完成后显式开启。
            "task_send_sent_event": False,
            "task_track_started": False,
        }
        for url, name in (
            (self.redis_coordination_url, "broker_use_ssl"),
            (self.celery_result_url, "redis_backend_use_ssl"),
        ):
            if urlsplit(url).scheme == "rediss":
                config[name] = {"ssl_cert_reqs": ssl.CERT_REQUIRED}
        return config
