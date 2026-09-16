from fastapi.testclient import TestClient
import pytest

import enterprise_rag.api.app as app_module
from enterprise_rag.api.app import create_app
from enterprise_rag.db.migrations import MigrationReadiness
from enterprise_rag.providers.fake import FakeChatProvider
from enterprise_rag.providers.registry import ProviderRegistry


@pytest.mark.asyncio
async def test_ready_provider_probe_is_coalesced_and_cached(monkeypatch):
    """并发 readiness 请求只能触发一次只读 health probe，并复用短期缓存。"""

    import asyncio
    import httpx

    class CountingProvider(FakeChatProvider):
        def __init__(self):
            super().__init__()
            self.probe_count = 0

        async def health_check(self) -> bool:
            self.probe_count += 1
            await asyncio.sleep(0.01)
            return True

    provider = CountingProvider()
    registry = ProviderRegistry()
    registry.register("local_answer", provider, capabilities={"chat.complete", "chat.stream"})
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")
    app = create_app(provider_registry=registry)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first, second = await asyncio.gather(
                client.get("/health/ready"),
                client.get("/health/ready"),
            )
            third = await client.get("/health/ready")

    assert first.status_code == second.status_code == 503
    assert first.json()["provider"]["status"] == "ready"
    assert second.json()["provider"]["status"] == "ready"
    assert third.json()["provider"]["status"] == "ready"
    assert provider.probe_count == 1


def test_live_health_endpoint(monkeypatch):
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")
    client = TestClient(create_app())

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_default_provider_client_read_timeout_covers_api_budget(monkeypatch):
    """默认 HTTP client 的读取预算必须覆盖接口允许的最长请求时间。"""

    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")
    app = create_app()

    provider = app.state.provider_registry.resolve("local_answer", "chat.complete").provider
    assert provider._client.timeout.read >= 300.0
    assert provider._client.timeout.read == 330.0


def test_ready_endpoint_requires_database_configuration(monkeypatch):
    # 本测试专门验证数据库配置缺失时的 readiness 行为，因此必须显式清除
    # 调用测试进程时可能从项目 `.env` 或宿主 shell 继承的 POSTGRES_DSN，
    # 避免外部开发环境配置污染“未配置”这一断言。
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")
    client = TestClient(create_app())

    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["environment"] == "test"
    assert body["database"] == {"status": "not_configured"}
    # readiness 现在同时报告 Provider 摘要；数据库未配置仍然足以让
    # 整体保持 not_ready，但不能因此丢失模型端点的可观测状态。
    assert body["provider"]["status"] in {"ready", "unavailable"}


def test_ready_endpoint_reports_migration_readiness(monkeypatch):
    """readiness 只能检查版本，不能在请求过程中隐式执行 migration。"""

    class _FakeDatabase:
        async def schema_readiness(self):
            return MigrationReadiness(
                ready=True,
                status="ready",
                target_version="0001_initial",
                applied_versions=("0001_initial",),
            )

        async def dispose(self):
            return None

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://user:pass@localhost/rag")
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")
    monkeypatch.setattr(app_module, "Database", lambda *args, **kwargs: _FakeDatabase())

    client = TestClient(create_app())

    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] == "test"
    assert body["database"] == {"status": "ready", "migration": "0001_initial"}
    assert body["provider"]["status"] == "ready"


def test_ready_endpoint_returns_503_when_migration_is_missing(monkeypatch):
    class _FakeDatabase:
        async def schema_readiness(self):
            return MigrationReadiness(
                ready=False,
                status="migration_required",
                target_version="0001_initial",
            )

        async def dispose(self):
            return None

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://user:pass@localhost/rag")
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")
    monkeypatch.setattr(app_module, "Database", lambda *args, **kwargs: _FakeDatabase())

    client = TestClient(create_app())

    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["environment"] == "test"
    assert body["database"] == {"status": "migration_required"}
    assert body["provider"]["status"] == "ready"


def test_meta_endpoint_exposes_service_identity(monkeypatch):
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")
    client = TestClient(create_app())

    response = client.get("/api/v1/meta")

    assert response.status_code == 200
    assert response.json() == {"name": "EnterpriseKnowledgeRAG", "version": "0.1.0"}
