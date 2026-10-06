"""基础设施配置门禁：旧部署不受影响，启用时必须明确指定服务。"""

import pytest

from enterprise_rag.infrastructure.settings import InfrastructureSettings


def test_features_default_off_and_secrets_are_hidden() -> None:
    """默认不创建服务连接，敏感字段不进入 repr。"""
    settings = InfrastructureSettings()
    assert not settings.enabled
    assert not settings.celery_enabled
    assert settings.storage_provider == "local"
    assert "do-not-print" not in repr(
        InfrastructureSettings(redis_cache_url="rediss://:do-not-print@cache/0")
    )


def test_enabled_requires_separate_redis_instances() -> None:
    """不同 DB 编号不是缓存与协调系统的实例隔离。"""
    with pytest.raises(ValueError, match="独立"):
        InfrastructureSettings(
            enabled=True,
            redis_cache_url="rediss://cache/0",
            redis_coordination_url="rediss://cache/1",
        ).validate()


def test_production_requires_tls() -> None:
    """生产 Redis 明文连接拒绝启动，不回显连接串。"""
    with pytest.raises(ValueError, match="TLS"):
        InfrastructureSettings(
            enabled=True, environment="production",
            redis_cache_url="redis://cache/0",
            redis_coordination_url="redis://queue/0",
        ).validate()


def test_missing_minio_configuration_is_rejected() -> None:
    """MinIO 缺少地址、bucket 或密钥时不回退 Local。"""
    with pytest.raises(ValueError, match="S3"):
        InfrastructureSettings(storage_provider="s3").validate()


def test_celery_configuration_has_bounded_delivery_and_json_only() -> None:
    """Celery 的安全序列化和任务时限不依赖实时 broker。"""
    settings = InfrastructureSettings(
        enabled=True, celery_enabled=True,
        redis_cache_url="rediss://cache/0",
        redis_coordination_url="rediss://queue/0",
        celery_result_url="rediss://queue/1",
    )
    config = settings.celery_config()
    assert config["task_acks_late"]
    assert config["task_reject_on_worker_lost"]
    assert config["worker_prefetch_multiplier"] == 1
    assert config["accept_content"] == ["json"]
    assert config["task_time_limit"] < config["visibility_timeout"]
    assert config["broker_transport_options"]["visibility_timeout"] == config["visibility_timeout"]
    assert config["broker_use_ssl"]["ssl_cert_reqs"] == 2


def test_from_env_does_not_enable_unrelated_legacy_redis(monkeypatch) -> None:
    """旧 REDIS_URL 示例不能悄悄启用新运行路径。"""
    monkeypatch.setenv("REDIS_URL", "redis://unused/0")
    monkeypatch.delenv("RAG_INFRA_ENABLED", raising=False)
    assert not InfrastructureSettings.from_env().enabled


@pytest.mark.parametrize("url", [
    "rediss://user:secret@host:999999/0",
    "rediss://user:secret@host/0?ssl_cert_reqs=none",
    "rediss://user:secret@host/0/1",
    "rediss://user:secret@host/-1",
    "rediss://user:secret@host:0/0",
])
def test_bad_redis_url_rejects_without_echoing_credentials(url) -> None:
    """异常信息不包含原 URL，且不能接受非法端口或 DB。"""
    with pytest.raises(ValueError) as captured:
        InfrastructureSettings(
            enabled=True, redis_cache_url=url, redis_coordination_url="rediss://queue/0",
        ).validate()
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize("endpoint", [
    "https://minio.invalid:999999",
    "https://minio.invalid/path",
    "https://user:secret@minio.invalid",
])
def test_s3_endpoint_is_not_a_path_or_invalid_port(endpoint) -> None:
    """拒绝 SDK 可能忽略或歧义解释的 endpoint，不回显敏感内容。"""
    with pytest.raises(ValueError) as captured:
        InfrastructureSettings(
            storage_provider="s3", s3_endpoint=endpoint,
            s3_bucket="rag", s3_access_key="access", s3_secret_key="secret",
        ).validate()
    assert "secret" not in str(captured.value)


def test_celery_requires_room_for_soft_timeout() -> None:
    """hard time limit 不能小于等于 soft time limit。"""
    with pytest.raises(ValueError, match="time limit"):
        InfrastructureSettings(
            enabled=True, celery_enabled=True,
            redis_cache_url="rediss://cache/0",
            redis_coordination_url="rediss://queue/0",
            celery_result_url="rediss://queue/1",
            task_time_limit_seconds=1,
        ).validate()
