from enterprise_rag.config.settings import Settings


def test_settings_uses_confirmed_local_models(monkeypatch):
    monkeypatch.delenv("LOCAL_LLM_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_EMBEDDING_MODEL", raising=False)
    settings = Settings.from_env()
    assert settings.local_llm_model == "qwen3.6-35b-a3b-claude-4.7-opus-reasoning-distilled-vlm-mlx-oq6"
    assert settings.local_llm_base_url == "http://127.0.0.1:1234"
    assert settings.ollama_embedding_model == "hardcore_aryabhata_598/qwen3-embedding:8b"
    assert settings.online_llm_enabled is False


def test_online_provider_requires_key(monkeypatch):
    monkeypatch.setenv("ONLINE_LLM_ENABLED", "true")
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    try:
        Settings.from_env()
    except ValueError as exc:
        assert "GROK_API_KEY" in str(exc)
    else:
        raise AssertionError("应拒绝没有 API Key 的在线配置")
