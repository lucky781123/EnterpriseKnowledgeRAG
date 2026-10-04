"""本地 P0 产品 API 装配入口的测试。

这些测试只验证依赖图和 fail-closed 配置边界，不把测试替身当作真实依赖
smoke 证据。真实 PostgreSQL、Weaviate、Ollama、BGE 和 LM Studio 仍由独立
的 ``scripts/local_p0_smoke.py`` 在专用进程配置下验证。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from enterprise_rag.config.settings import Settings
from enterprise_rag.db.session import Database
from enterprise_rag.storage.local import LocalObjectStorage
from scripts.local_p0_api import create_local_p0_app

SECRET = "0123456789abcdef0123456789abcdef"


class _RerankerStub:
    """为装配测试提供最小 reranker 形状，不执行模型加载。"""

    async def health_check(self) -> bool:
        """返回固定健康状态，验证 readiness 能识别 reranker 依赖。"""

        return True


def _settings() -> Settings:
    """构造显式本地产品配置，避免测试读取项目外部或隐式凭据。"""

    return Settings(
        app_env="development",
        log_level="INFO",
        local_llm_base_url="http://127.0.0.1:1234",
        local_llm_api_key="local",
        local_llm_model="local-test-model",
        local_llm_max_concurrency=1,
        local_llm_max_queue_size=1,
        ollama_base_url="http://127.0.0.1:11434",
        ollama_embedding_model="hardcore_aryabhata_598/qwen3-embedding:8b",
        ollama_reranker_model=None,
        grok_api_key=None,
        grok_base_url="https://example.invalid/v1",
        grok_model="unused",
        online_llm_enabled=False,
        evaluation_dataset_path="docs/evaluation/dataset-draft.jsonl",
        postgres_dsn="postgresql+asyncpg://redacted/redacted",
        weaviate_enabled=True,
        weaviate_base_url="http://127.0.0.1:8080",
        weaviate_schema_version="dense-v1",
        dev_auth_enabled=True,
        dev_auth_secret=SECRET,
    )


def test_local_p0_app_requires_explicit_runtime_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少 PostgreSQL、Weaviate 或开发身份配置时必须拒绝启动。"""

    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.delenv("WEAVIATE_ENABLED", raising=False)
    monkeypatch.delenv("DEV_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("DEV_AUTH_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="POSTGRES_DSN"):
        create_local_p0_app()


@pytest.mark.parametrize(
    ("field_name", "error_fragment"),
    [
        ("local_llm_base_url", "LOCAL_LLM_BASE_URL"),
        ("local_llm_model", "LOCAL_LLM_MODEL"),
        ("ollama_base_url", "OLLAMA_BASE_URL"),
        ("ollama_embedding_model", "OLLAMA_EMBEDDING_MODEL"),
    ],
)
def test_local_p0_app_rejects_missing_model_service_configuration(
    field_name: str,
    error_fragment: str,
) -> None:
    """模型或 embedding endpoint 缺失时必须在装配前 fail closed。"""

    settings = replace(_settings(), **{field_name: ""})

    with pytest.raises(RuntimeError, match=error_fragment):
        create_local_p0_app(settings=settings)


def test_local_p0_app_requires_explicit_bge_model_when_not_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实入口未注入测试 reranker 时必须显式声明 BGE 模型。"""

    monkeypatch.delenv("BGE_RERANKER_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="BGE_RERANKER_MODEL"):
        create_local_p0_app(settings=_settings())


@pytest.mark.parametrize("dimension", [4096, 1024, 2560])
def test_local_p0_app_wires_complete_product_dependency_graph(tmp_path, dimension) -> None:
    """显式配置下应创建完整产品服务，并固定默认 Dense/Sparse profile。"""

    database = Database("sqlite+aiosqlite:///:memory:")
    client = httpx.AsyncClient()
    try:
        app = create_local_p0_app(
            settings=replace(_settings(), ollama_embedding_dimension=dimension),
            database=database,
            storage=LocalObjectStorage(tmp_path / "objects"),
            http_client=client,
            reranker=_RerankerStub(),
        )

        ingestion = app.state.product_ingestion_service
        retrieval = app.state.product_retrieval_service

        assert ingestion is not None
        assert retrieval is not None
        assert app.state.dev_token_codec is not None
        assert ingestion.dense_profile.dimension == dimension
        assert ingestion.dense_profile.chunking_version == "parent-child-v1"
        assert ingestion.dense_profile.schema_version == "dense-v1"
        assert ingestion.sparse_profile.tokenizer_version == "word"
        assert ingestion.sparse_profile.preprocessing_version == "identity"
        assert ingestion.sparse_profile.schema_version == "sparse-v1"
        assert ingestion.sparse_profile.field_weights == {
            "title": 2.0,
            "section_title": 2.0,
            "api_symbols": 3.0,
            "body": 1.0,
        }
        assert retrieval.index_profile == ingestion.dense_profile
        assert retrieval.sparse_profile == ingestion.sparse_profile
        assert app.state.product_readiness_manager.product_ingestion_service is ingestion
        assert app.state.product_readiness_manager.product_retrieval_service is retrieval
    finally:
        asyncio.run(client.aclose())
        asyncio.run(database.dispose())
