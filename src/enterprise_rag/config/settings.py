"""项目配置：只读取环境变量，不在代码中保存密钥。"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数") from exc
    if parsed <= 0:
        raise ValueError(f"环境变量 {name} 必须大于 0")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str
    log_level: str
    local_llm_base_url: str
    local_llm_api_key: str
    local_llm_model: str
    local_llm_max_concurrency: int
    # 等待队列是有界的；超过该数量的请求应立即返回 rate_limited，
    # 不允许请求在进程内无限堆积并把本地模型拖入不可控状态。
    local_llm_max_queue_size: int
    ollama_base_url: str
    ollama_embedding_model: str
    ollama_reranker_model: str | None
    grok_api_key: str | None
    grok_base_url: str
    grok_model: str
    online_llm_enabled: bool
    evaluation_dataset_path: str
    postgres_dsn: str | None = None
    postgres_pool_size: int = 3
    postgres_max_overflow: int = 2
    postgres_pool_timeout_seconds: int = 10
    postgres_pool_recycle_seconds: int = 1800
    postgres_statement_timeout_ms: int = 30_000
    postgres_lock_timeout_ms: int = 5_000

    @classmethod
    def from_env(cls) -> "Settings":
        local_model = _env("LOCAL_LLM_MODEL", "qwen3.6-35b-a3b-claude-4.7-opus-reasoning-distilled-vlm-mlx-oq6")
        embedding_model = _env("OLLAMA_EMBEDDING_MODEL", "hardcore_aryabhata_598/qwen3-embedding:8b")
        settings = cls(
            app_env=_env("APP_ENV", "development") or "development",
            log_level=_env("LOG_LEVEL", "INFO") or "INFO",
            local_llm_base_url=_env("LOCAL_LLM_BASE_URL", "http://127.0.0.1:1234") or "",
            local_llm_api_key=_env("LOCAL_LLM_API_KEY", "local") or "local",
            local_llm_model=local_model or "",
            local_llm_max_concurrency=_env_int("LOCAL_LLM_MAX_CONCURRENCY", 2),
            local_llm_max_queue_size=_env_int("LOCAL_LLM_MAX_QUEUE_SIZE", 8),
            ollama_base_url=_env("OLLAMA_BASE_URL", "http://127.0.0.1:11434") or "",
            ollama_embedding_model=embedding_model or "",
            ollama_reranker_model=_env("OLLAMA_RERANKER_MODEL"),
            grok_api_key=_env("GROK_API_KEY"),
            grok_base_url=_env("GROK_BASE_URL", "https://api.deepspacecode.chat/v1") or "",
            grok_model=_env("GROK_MODEL", "grok-4.6") or "",
            online_llm_enabled=_env_bool("ONLINE_LLM_ENABLED", False),
            evaluation_dataset_path=_env("RAG_DATASET_PATH", "docs/evaluation/dataset-draft.jsonl") or "",
            postgres_dsn=_env("POSTGRES_DSN"),
            # 连接池和事务超时是保守的单机开发/CI 默认值；生产环境应在部署配置中
            # 根据数据库连接配额、请求 deadline 和锁竞争实验重新评估。
            postgres_pool_size=_env_int("POSTGRES_POOL_SIZE", 3),
            postgres_max_overflow=_env_int("POSTGRES_MAX_OVERFLOW", 2),
            postgres_pool_timeout_seconds=_env_int("POSTGRES_POOL_TIMEOUT_SECONDS", 10),
            postgres_pool_recycle_seconds=_env_int("POSTGRES_POOL_RECYCLE_SECONDS", 1800),
            postgres_statement_timeout_ms=_env_int("POSTGRES_STATEMENT_TIMEOUT_MS", 30_000),
            postgres_lock_timeout_ms=_env_int("POSTGRES_LOCK_TIMEOUT_MS", 5_000),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.online_llm_enabled and not self.grok_api_key:
            raise ValueError("ONLINE_LLM_ENABLED=true 时必须设置 GROK_API_KEY")
        if not self.local_llm_model:
            raise ValueError("LOCAL_LLM_MODEL 不能为空")
        if not self.ollama_embedding_model:
            raise ValueError("OLLAMA_EMBEDDING_MODEL 不能为空")
