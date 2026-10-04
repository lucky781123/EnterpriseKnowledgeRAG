"""本地 P0 产品 API 的显式依赖装配入口。

默认 ``enterprise_rag.api.app:create_app`` 继续保持 fail-closed：没有显式注入
产品服务时不会偷偷切换到真实 Weaviate、BGE 或开发身份。本模块只为 Task 8
真实 smoke 提供一个可重复的本地装配入口，读取环境变量及显式三模型配置文件。

启动方式：

``uvicorn scripts.local_p0_api:app --factory --host 127.0.0.1 --port 8000``

真实运行前必须由启动进程显式提供 ``POSTGRES_DSN``、
``WEAVIATE_ENABLED=true``、``WEAVIATE_BASE_URL``、``DEV_AUTH_ENABLED=true`` 和
``DEV_AUTH_SECRET``。LLM/Embedding/Reranker由``LOCAL_LLM_CONFIG_FILE``指定的
TOML在Settings加载时读取；未启用嵌套模型表的旧部署继续使用环境变量。
本模块不会自动加载项目``.env``，模型密钥仍只来自环境变量。不把凭据写入报告或日志。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from enterprise_rag.api.app import create_app
from enterprise_rag.config.settings import Settings
from enterprise_rag.db.session import Database
from enterprise_rag.embedding.ollama import OllamaEmbeddingProvider
from enterprise_rag.ingestion.product_service import ProductIngestionService
from enterprise_rag.ingestion.service import IngestionService
from enterprise_rag.providers.lmstudio import LMStudioChatProvider
from enterprise_rag.providers.registry import ProviderRegistry
from enterprise_rag.reranking.adapters import SyncRerankerAdapter
from enterprise_rag.reranking.bge import BGEReranker
from enterprise_rag.reranking.http import HTTPReranker
from enterprise_rag.retrieval.contracts import IndexProfile
from enterprise_rag.retrieval.generation import GenerationService
from enterprise_rag.retrieval.indexer import WeaviateDenseIndexer
from enterprise_rag.retrieval.product import ProductHybridRetrievalService
from enterprise_rag.retrieval.scope import AuthorizationScopeService
from enterprise_rag.retrieval.sparse import SparseProfile
from enterprise_rag.retrieval.weaviate import WeaviateHTTPXTransport
from enterprise_rag.retrieval.weaviate_sparse import WeaviateBM25FTransport
from enterprise_rag.security.authorization import AuthorizationService
from enterprise_rag.security.dev_token import DevTokenCodec
from enterprise_rag.storage.local import LocalObjectStorage
from enterprise_rag.workbench.app import mount_product_workbench

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DENSE_CHUNKING_VERSION = "parent-child-v1"
_DENSE_SCHEMA_VERSION = "dense-v1"
_SPARSE_TOKENIZER_VERSION = "word"
_SPARSE_PREPROCESSING_VERSION = "identity"
_SPARSE_SCHEMA_VERSION = "sparse-v1"
def _require_explicit_runtime_configuration(settings: Settings) -> None:
    """拒绝缺少真实运行前置条件的启动，避免 smoke 落入离线替代路径。"""

    # 产品入口必须明确连接 PostgreSQL；不能因为环境变量缺失就回退到 SQLite
    # 或默认 ``create_app`` 的未配置状态，否则上传到引用 smoke 会失去证据意义。
    if not settings.postgres_dsn:
        raise RuntimeError("本地 P0 API 需要显式设置 POSTGRES_DSN")
    if not settings.local_llm_base_url.strip():
        raise RuntimeError("本地 P0 API 需要显式设置 LOCAL_LLM_BASE_URL")
    if not settings.local_llm_model.strip():
        raise RuntimeError("本地 P0 API 需要显式设置 LOCAL_LLM_MODEL")
    if not settings.ollama_base_url.strip():
        raise RuntimeError("本地 P0 API 需要显式设置 OLLAMA_BASE_URL")
    if not settings.ollama_embedding_model.strip():
        raise RuntimeError("本地 P0 API 需要显式设置 OLLAMA_EMBEDDING_MODEL")
    if not settings.weaviate_enabled:
        raise RuntimeError("本地 P0 API 需要显式设置 WEAVIATE_ENABLED=true")
    if not settings.weaviate_base_url.strip():
        raise RuntimeError("本地 P0 API 需要显式设置 WEAVIATE_BASE_URL")
    if not settings.dev_auth_enabled:
        raise RuntimeError("本地 P0 API 需要显式设置 DEV_AUTH_ENABLED=true")
    if not settings.dev_auth_secret:
        raise RuntimeError("本地 P0 API 需要显式设置 DEV_AUTH_SECRET")
    if settings.weaviate_schema_version != _DENSE_SCHEMA_VERSION:
        raise RuntimeError("本地 P0 API 只支持固定 Dense schema 版本 dense-v1")


def _storage_root_from_env() -> Path:
    """解析对象存储目录，并限制默认入口只能访问当前项目目录。"""

    configured = os.getenv(
        "PRODUCT_OBJECT_STORAGE_ROOT",
        str(_PROJECT_ROOT / ".local" / "p0-object-storage"),
    )
    candidate = Path(configured)
    if not candidate.is_absolute():
        candidate = _PROJECT_ROOT / candidate
    resolved = candidate.resolve()
    if resolved != _PROJECT_ROOT and _PROJECT_ROOT not in resolved.parents:
        raise RuntimeError("PRODUCT_OBJECT_STORAGE_ROOT 必须位于当前项目目录内")
    return resolved


def _shared_http_client() -> httpx.AsyncClient:
    """创建供 LM Studio、Ollama 和 Weaviate 复用的异步 HTTP client。"""

    # LM Studio 的生成可能持续数十秒；读取预算覆盖本地产品 API 的最长请求预算。
    # Weaviate adapter 自身仍会对单次索引/查询请求使用更短的局部 timeout。
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=5.0,
            read=330.0,
            write=30.0,
            pool=10.0,
        )
    )


def create_local_p0_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    storage: LocalObjectStorage | None = None,
    http_client: httpx.AsyncClient | None = None,
    reranker: Any | None = None,
) -> FastAPI:
    """装配同步上传、双路索引、ACL、混合检索和引用问答产品链。"""

    resolved_settings = settings or Settings.from_env()
    _require_explicit_runtime_configuration(resolved_settings)

    # 真实入口使用 PostgreSQL 事实源；测试可显式注入 Database，但不会改变运行
    # 时的配置守卫，也不会让默认 create_app 自动启用产品链。
    resolved_database = database or Database(
        resolved_settings.postgres_dsn,
        pool_size=resolved_settings.postgres_pool_size,
        max_overflow=resolved_settings.postgres_max_overflow,
        pool_timeout=resolved_settings.postgres_pool_timeout_seconds,
        pool_recycle=resolved_settings.postgres_pool_recycle_seconds,
        statement_timeout_ms=resolved_settings.postgres_statement_timeout_ms,
        lock_timeout_ms=resolved_settings.postgres_lock_timeout_ms,
    )
    resolved_storage = storage or LocalObjectStorage(_storage_root_from_env())
    shared_client = http_client or _shared_http_client()

    # 所有外部 HTTP 依赖共享连接池，但 endpoint 仍由服务端固定配置；请求体
    # 不会获得修改模型、Weaviate 或 Ollama 地址的能力。
    provider_registry = ProviderRegistry()
    provider_registry.register(
        "local_answer",
        LMStudioChatProvider(
            shared_client,
            base_url=resolved_settings.local_llm_base_url,
            model=resolved_settings.local_llm_model,
            api_key=resolved_settings.local_llm_api_key,
        ),
        capabilities={"chat.complete", "chat.stream"},
    )
    embedding_provider = OllamaEmbeddingProvider(
        shared_client,
        base_url=resolved_settings.ollama_base_url,
        model=resolved_settings.ollama_embedding_model,
    )
    # 模型与维度构成不可混用的profile；新配置使用独立物理collection保留旧向量。
    dense_profile = IndexProfile(
        model_name=resolved_settings.ollama_embedding_model,
        dimension=resolved_settings.ollama_embedding_dimension,
        chunking_version=_DENSE_CHUNKING_VERSION,
        schema_version=_DENSE_SCHEMA_VERSION,
    )
    dense_transport = WeaviateHTTPXTransport(
        shared_client,
        resolved_settings.weaviate_base_url,
        api_key=resolved_settings.weaviate_api_key,
        index_profile_key=dense_profile.key if resolved_settings.dense_profile_namespace else None,
    )
    sparse_transport = WeaviateBM25FTransport(
        shared_client,
        resolved_settings.weaviate_base_url,
        api_key=resolved_settings.weaviate_api_key,
    )

    # P0 profile 固定为已经完成本地实验的默认组合。Embedding 维度故意写进
    # profile key，模型输出维度不匹配时由 Dense indexer 直接拒绝发布。
    sparse_profile = SparseProfile(
        tokenizer_version=_SPARSE_TOKENIZER_VERSION,
        preprocessing_version=_SPARSE_PREPROCESSING_VERSION,
        schema_version=_SPARSE_SCHEMA_VERSION,
        field_weights={
            "title": 2.0,
            "section_title": 2.0,
            "api_symbols": 3.0,
            "body": 1.0,
        },
    )

    authorization_service = AuthorizationService()
    generation_service = GenerationService()
    ingestion_service = IngestionService(
        resolved_database,
        resolved_storage,
        max_bytes=resolved_settings.product_upload_max_bytes,
    )
    dense_indexer = WeaviateDenseIndexer(
        embedding_provider,
        dense_transport,
        resolved_storage,
        generation_service,
    )
    product_ingestion_service = ProductIngestionService(
        database=resolved_database,
        storage=resolved_storage,
        ingestion_service=ingestion_service,
        authorization_service=authorization_service,
        generation_service=generation_service,
        dense_indexer=dense_indexer,
        dense_transport=dense_transport,
        sparse_transport=sparse_transport,
        dense_profile=dense_profile,
        sparse_profile=sparse_profile,
    )

    # Reranker 通过配置文件指定的本地 HTTP 服务调用；测试仍可注入替身。
    if reranker is None:
        if resolved_settings.reranker_provider == "http":
            reranker = HTTPReranker(
                shared_client,
                base_url=resolved_settings.reranker_base_url,
                model=resolved_settings.reranker_model,
            )
        else:
            # 不启用新文件配置的旧部署保留原有BGE路径，避免把BGE模型发往HTTP服务。
            if not resolved_settings.reranker_model:
                raise RuntimeError("BGE_RERANKER_MODEL 不能为空")
            reranker = SyncRerankerAdapter(BGEReranker(
                model_name=resolved_settings.reranker_model,
                device=resolved_settings.reranker_device,
            ))
    product_retrieval_service = ProductHybridRetrievalService(
        scope_service=AuthorizationScopeService(authorization_service),
        embedding_provider=embedding_provider,
        dense_transport=dense_transport,
        sparse_transport=sparse_transport,
        reranker=reranker,
        authorization_service=authorization_service,
        index_profile=dense_profile,
        sparse_profile=sparse_profile,
    )
    dev_token_codec = DevTokenCodec(
        resolved_settings.dev_auth_secret,
        ttl_seconds=resolved_settings.dev_auth_token_ttl_seconds,
    )

    # 只显式注入产品服务；默认 API factory 的 fail-closed 逻辑和旧 PostgreSQL
    # retrieval 路径保持不变。create_app 会接管共享 Provider client 的关闭。
    application = create_app(
        settings=resolved_settings,
        provider_registry=provider_registry,
        database=resolved_database,
        embedding_provider=embedding_provider,
        product_ingestion_service=product_ingestion_service,
        product_retrieval_service=product_retrieval_service,
        authorization_service=authorization_service,
        dev_token_codec=dev_token_codec,
    )

    # Gradio 是可选依赖；只有显式打开时才挂载产品工作台，不影响没有安装
    # Gradio 的 API smoke 和基础测试启动路径。
    if os.getenv("P0_WORKBENCH_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        mount_product_workbench(
            application,
            api_base_url=os.getenv("P0_API_BASE_URL", "http://127.0.0.1:8000"),
            anonymous=resolved_settings.local_anonymous_enabled,
        )
    return application


def app() -> FastAPI:
    """提供给 Uvicorn ``--factory`` 使用的 FastAPI 工厂。"""

    return create_local_p0_app()


if __name__ == "__main__":
    import uvicorn

    # 直接执行脚本时仍复用同一个工厂，保持命令行和 Uvicorn 模式一致。
    uvicorn.run(
        "scripts.local_p0_api:app",
        factory=True,
        host=os.getenv("P0_API_HOST", "127.0.0.1"),
        port=int(os.getenv("P0_API_PORT", "8000")),
    )


__all__ = ["app", "create_local_p0_app"]
