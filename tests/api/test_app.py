from fastapi.testclient import TestClient

import enterprise_rag.api.app as app_module
from enterprise_rag.api.app import create_app
from enterprise_rag.db.migrations import MigrationReadiness


def test_live_health_endpoint(monkeypatch):
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")
    client = TestClient(create_app())

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_endpoint_requires_database_configuration(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")
    client = TestClient(create_app())

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "environment": "test",
        "database": {"status": "not_configured"},
    }


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
    assert response.json() == {
        "status": "ok",
        "environment": "test",
        "database": {"status": "ready", "migration": "0001_initial"},
    }


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
    assert response.json() == {
        "status": "not_ready",
        "environment": "test",
        "database": {"status": "migration_required"},
    }


def test_meta_endpoint_exposes_service_identity(monkeypatch):
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")
    client = TestClient(create_app())

    response = client.get("/api/v1/meta")

    assert response.status_code == 200
    assert response.json() == {"name": "EnterpriseKnowledgeRAG", "version": "0.1.0"}
