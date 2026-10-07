"""项目配置：读取环境变量及三类本地模型配置文件；密钥仅来自环境变量。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


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


def _env_optional_int(name: str, default: int | None) -> int | None:
    """读取可选正整数；空值或 unlimited 表示不设置应用层上限。"""
    value = _env(name)
    if value is None or value.strip().lower() in {"unlimited", "none", "null"}:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是正整数或 unlimited") from exc
    if parsed <= 0:
        raise ValueError(f"环境变量 {name} 必须大于 0")
    return parsed


def _local_llm_configuration() -> dict:
    """读取本地LLM、Embedding和Reranker配置；文件优先，不缓存或静默回退。"""
    configured = _env("LOCAL_LLM_CONFIG_FILE")
    if configured is None:
        # 未启用文件的旧部署保持兼容；本机启动在.env中显式指定TOML路径。
        return {
            "model": _env(
                "LOCAL_LLM_MODEL",
                "qwen3.6-35b-a3b-claude-4.7-opus-reasoning-distilled-vlm-mlx-oq6",
            ),
            "base_url": _env("LOCAL_LLM_BASE_URL", "http://127.0.0.1:1234"),
            "max_concurrency": _env_int("LOCAL_LLM_MAX_CONCURRENCY", 2),
            "max_queue_size": _env_int("LOCAL_LLM_MAX_QUEUE_SIZE", 8),
        }
    path = Path(configured)
    # 相对路径以源码项目根目录为基准，不受启动终端所在目录影响。
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[3] / path
    try:
        with path.open("rb") as source:
            values = tomllib.load(source)
    except (OSError, ValueError):
        # 不回显解析器异常或文件正文，避免非法配置包含敏感信息。
        raise ValueError("LOCAL_LLM_CONFIG_FILE 无法读取或不是有效TOML") from None
    allowed = {"model", "base_url", "max_concurrency", "max_queue_size", "embedding", "reranker"}
    if values.keys() - allowed:
        raise ValueError("LOCAL_LLM_CONFIG_FILE 包含不支持的字段；密钥请使用环境变量")
    # 模型和地址必须明确填写，避免错误文件落回旧模型或默认地址。
    for field in ("model", "base_url"):
        value = values.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"LOCAL_LLM_CONFIG_FILE 的 {field} 必须是非空字符串")
        values[field] = value.strip()
    # 只读取调度器已经支持的两个预算，不引入新的采样或重试行为。
    for field, default in (("max_concurrency", 2), ("max_queue_size", 8)):
        value = values.get(field, default)
        if type(value) is not int or value <= 0:
            raise ValueError(f"LOCAL_LLM_CONFIG_FILE 的 {field} 必须是正整数")
        values[field] = value
    # 同一份文件一次读取，嵌套配置可选，兼容此前仅配置回答LLM的TOML。
    for section, fields, integers in (
        ("embedding", {"base_url", "model", "dimension"}, ("dimension",)),
        ("reranker", {"base_url", "model"}, ()),
    ):
        if section not in values:
            continue
        options = values[section]
        if not isinstance(options, dict) or options.keys() != fields:
            raise ValueError(f"LOCAL_LLM_CONFIG_FILE 的 {section} 字段不完整或包含未知字段")
        for field in fields:
            value = options[field]
            if field in integers:
                if type(value) is not int or value <= 0:
                    raise ValueError(f"LOCAL_LLM_CONFIG_FILE 的 {section}.{field} 必须是正整数")
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"LOCAL_LLM_CONFIG_FILE 的 {section}.{field} 必须是非空字符串")
            else:
                options[field] = value.strip()
    return values


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
    weaviate_enabled: bool = False
    weaviate_base_url: str = "http://127.0.0.1:8080"
    weaviate_api_key: str | None = None
    weaviate_schema_version: str = "dense-v1"
    dev_auth_enabled: bool = False
    dev_auth_secret: str | None = None
    dev_auth_token_ttl_seconds: int = 3600
    local_anonymous_enabled: bool = False
    api_request_body_max_bytes: int = 128 * 1024
    product_upload_max_bytes: int | None = None
    ollama_embedding_dimension: int = 4096
    # 旧部署默认保留进程内BGE；显式TOML reranker表启用本地HTTP服务。
    reranker_provider: str = "bge"
    reranker_base_url: str = ""
    reranker_model: str | None = None
    reranker_device: str | None = None
    dense_profile_namespace: bool = False
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    tenant_requests_per_minute: int = 600
    tenant_uploads_per_minute: int = 120

    @classmethod
    def from_env(cls) -> Settings:
        """启动时加载配置；本地模型文件优先，其他服务和密钥仍使用环境变量。"""
        local_llm = _local_llm_configuration()
        embedding = local_llm.get("embedding")
        if embedding is None:
            embedding = {
                "base_url": _env("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
                "model": _env("OLLAMA_EMBEDDING_MODEL", "hardcore_aryabhata_598/qwen3-embedding:8b"),
                "dimension": _env_int("OLLAMA_EMBEDDING_DIMENSION", 4096),
            }
        reranker = local_llm.get("reranker")
        settings = cls(
            app_env=_env("APP_ENV", "development") or "development",
            log_level=_env("LOG_LEVEL", "INFO") or "INFO",
            local_llm_base_url=local_llm["base_url"] or "",
            local_llm_api_key=_env("LOCAL_LLM_API_KEY", "local") or "local",
            local_llm_model=local_llm["model"] or "",
            local_llm_max_concurrency=local_llm["max_concurrency"],
            local_llm_max_queue_size=local_llm["max_queue_size"],
            ollama_base_url=embedding["base_url"] or "",
            ollama_embedding_model=embedding["model"] or "",
            # 维度参与索引 profile，换模型必须显式匹配输出，不能混用历史索引。
            ollama_embedding_dimension=embedding["dimension"],
            ollama_reranker_model=_env("OLLAMA_RERANKER_MODEL"),
            reranker_provider="http" if reranker else "bge",
            reranker_base_url=reranker["base_url"] if reranker else "",
            reranker_model=reranker["model"] if reranker else _env("BGE_RERANKER_MODEL"),
            reranker_device=_env("BGE_RERANKER_DEVICE"),
            dense_profile_namespace="embedding" in local_llm,
            oidc_enabled=_env_bool("OIDC_ENABLED", False),
            oidc_issuer=_env("OIDC_ISSUER", "") or "",
            oidc_audience=_env("OIDC_AUDIENCE", "") or "",
            oidc_jwks_url=_env("OIDC_JWKS_URL", "") or "",
            tenant_requests_per_minute=_env_int("TENANT_REQUESTS_PER_MINUTE", 600),
            tenant_uploads_per_minute=_env_int("TENANT_UPLOADS_PER_MINUTE", 120),
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
            # Weaviate 默认关闭；只有显式启用且由部署注入 endpoint/key 时才创建适配器。
            weaviate_enabled=_env_bool("WEAVIATE_ENABLED", False),
            weaviate_base_url=_env("WEAVIATE_BASE_URL", "http://127.0.0.1:8080") or "",
            weaviate_api_key=_env("WEAVIATE_API_KEY"),
            weaviate_schema_version=_env("WEAVIATE_SCHEMA_VERSION", "dense-v1") or "",
            # 开发身份默认关闭，且只允许由未跟踪的环境变量注入 secret。
            dev_auth_enabled=_env_bool("DEV_AUTH_ENABLED", False),
            dev_auth_secret=_env("DEV_AUTH_SECRET"),
            dev_auth_token_ttl_seconds=_env_int("DEV_AUTH_TOKEN_TTL_SECONDS", 3600),
            local_anonymous_enabled=_env_bool("P0_ANONYMOUS_ENABLED", False),
            # 普通 JSON 保持较小预算；产品 multipart 上传默认不设置应用层大小上限。
            api_request_body_max_bytes=_env_int("API_REQUEST_BODY_MAX_BYTES", 128 * 1024),
            product_upload_max_bytes=_env_optional_int("PRODUCT_UPLOAD_MAX_BYTES", None),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.local_anonymous_enabled and self.app_env != "development":
            raise ValueError("本机免 Token 模式只允许在 development 环境启用")
        if self.ollama_embedding_dimension <= 0:
            raise ValueError("OLLAMA_EMBEDDING_DIMENSION 必须大于 0")
        if self.online_llm_enabled and not self.grok_api_key:
            raise ValueError("ONLINE_LLM_ENABLED=true 时必须设置 GROK_API_KEY")
        if not self.local_llm_model:
            raise ValueError("LOCAL_LLM_MODEL 不能为空")
        if not self.ollama_embedding_model:
            raise ValueError("OLLAMA_EMBEDDING_MODEL 不能为空")
        if self.reranker_provider == "http" and (
            not self.reranker_base_url or not self.reranker_model
        ):
            raise ValueError("Reranker 配置不能为空")
        if self.weaviate_enabled and not self.weaviate_base_url:
            raise ValueError("WEAVIATE_ENABLED=true 时 WEAVIATE_BASE_URL 不能为空")
        if not self.weaviate_schema_version:
            raise ValueError("WEAVIATE_SCHEMA_VERSION 不能为空")
        if self.dev_auth_enabled:
            if self.app_env != "development":
                raise ValueError("开发身份模式只允许在 development 环境启用")
            if self.dev_auth_secret is None or len(self.dev_auth_secret.encode("utf-8")) < 32:
                raise ValueError("DEV_AUTH_ENABLED=true 时 DEV_AUTH_SECRET 至少需要 32 字节")
        if (
            self.product_upload_max_bytes is not None
            and self.product_upload_max_bytes < self.api_request_body_max_bytes
        ):
            raise ValueError("PRODUCT_UPLOAD_MAX_BYTES 不能小于 API_REQUEST_BODY_MAX_BYTES")
        if self.oidc_enabled:
            from enterprise_rag.security.oidc import OIDCSettings

            OIDCSettings(
                issuer=self.oidc_issuer,
                audience=self.oidc_audience,
                jwks_url=self.oidc_jwks_url,
            ).validate()
