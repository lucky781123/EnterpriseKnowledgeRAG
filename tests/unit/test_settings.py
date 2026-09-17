import pytest

from enterprise_rag.config.settings import Settings


def test_settings_uses_confirmed_local_models(monkeypatch):
    monkeypatch.delenv("LOCAL_LLM_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_EMBEDDING_MODEL", raising=False)
    settings = Settings.from_env()
    assert settings.local_llm_model == "qwen3.6-35b-a3b-claude-4.7-opus-reasoning-distilled-vlm-mlx-oq6"
    assert settings.local_llm_base_url == "http://127.0.0.1:1234"
    assert settings.ollama_embedding_model == "hardcore_aryabhata_598/qwen3-embedding:8b"
    assert settings.online_llm_enabled is False
    assert settings.postgres_pool_size == 3
    assert settings.postgres_max_overflow == 2
    assert settings.postgres_pool_timeout_seconds == 10
    assert settings.postgres_pool_recycle_seconds == 1800
    assert settings.postgres_statement_timeout_ms == 30_000
    assert settings.postgres_lock_timeout_ms == 5_000
    assert settings.dev_auth_enabled is False
    assert settings.dev_auth_secret is None
    assert settings.dev_auth_token_ttl_seconds == 3600


def test_online_provider_requires_key(monkeypatch):
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "true")
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    try:
        Settings.from_env()
    except ValueError as exc:
        assert "GROK_API_KEY" in str(exc)
    else:
        raise AssertionError("应拒绝没有 API Key 的在线配置")


def test_settings_reads_optional_postgres_dsn(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://user:pass@localhost/rag")
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "false")

    settings = Settings.from_env()

    assert settings.postgres_dsn == "postgresql://user:pass@localhost/rag"


def test_development_auth_requires_secret_when_enabled(monkeypatch):
    """显式启用开发认证时必须配置至少 32 字节 secret。"""

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.delenv("DEV_AUTH_SECRET", raising=False)

    with pytest.raises(ValueError, match="DEV_AUTH_SECRET"):
        Settings.from_env()


def test_development_auth_is_rejected_outside_development(monkeypatch):
    """开发 token 模式不能在非 development 环境中被意外开启。"""

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEV_AUTH_ENABLED", "true")
    monkeypatch.setenv("DEV_AUTH_SECRET", "0123456789abcdef0123456789abcdef")

    with pytest.raises(ValueError, match="development"):
        Settings.from_env()
