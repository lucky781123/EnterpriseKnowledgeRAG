from fastapi.testclient import TestClient

from enterprise_rag.api.app import create_app


def test_live_health_endpoint(monkeypatch):
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")
    client = TestClient(create_app())

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_endpoint_exposes_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")
    client = TestClient(create_app())

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "environment": "test"}


def test_meta_endpoint_exposes_service_identity(monkeypatch):
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")
    client = TestClient(create_app())

    response = client.get("/api/v1/meta")

    assert response.status_code == 200
    assert response.json() == {"name": "EnterpriseKnowledgeRAG", "version": "0.1.0"}
