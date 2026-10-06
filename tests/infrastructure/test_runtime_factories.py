"""可选依赖工厂契约；替身不证明真实 SDK、TLS 或网络行为。"""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from enterprise_rag.infrastructure.celery_app import create_celery_app
from enterprise_rag.infrastructure.runtime import RedisRuntime
from enterprise_rag.infrastructure.settings import InfrastructureSettings
from enterprise_rag.storage.s3 import S3ObjectStorage


def enabled_settings(**kwargs) -> InfrastructureSettings:
    """构造不含真实凭据的显式开发配置。"""
    return InfrastructureSettings(
        enabled=True, redis_cache_url="rediss://cache/0",
        redis_coordination_url="rediss://queue/0", **kwargs,
    )


def inject_module(monkeypatch, name: str, **members) -> None:
    """仅替换本测试进程中的可选 SDK，不安装或修改外部环境。"""
    module = ModuleType(name)
    module.__dict__.update(members)
    monkeypatch.setitem(sys.modules, name, module)


def test_disabled_factories_fail_before_optional_imports(monkeypatch) -> None:
    """默认关闭不会导入可选依赖，更不会连接服务。"""
    for name in ("redis", "redis.asyncio", "celery", "boto3"):
        monkeypatch.setitem(sys.modules, name, None)
    with pytest.raises(ValueError, match="尚未启用"):
        RedisRuntime.create(InfrastructureSettings())
    with pytest.raises(ValueError, match="尚未启用"):
        create_celery_app(InfrastructureSettings())
    with pytest.raises(ValueError, match="尚未启用"):
        S3ObjectStorage.from_settings(InfrastructureSettings())


def test_redis_pools_are_separate_and_timeouts_are_bounded(monkeypatch) -> None:
    """分别构造两路 client，验证超时与连接数量由服务端固定。"""
    calls = []

    def from_url(url, **options):
        """记录配置，避免任何网络 I/O。"""
        client = object()
        calls.append((url, options, client))
        return client

    inject_module(monkeypatch, "redis", __path__=[])
    inject_module(monkeypatch, "redis.asyncio", Redis=SimpleNamespace(from_url=from_url))
    runtime = RedisRuntime.create(enabled_settings())
    assert runtime.cache is not runtime.coordination
    assert [call[0] for call in calls] == ["rediss://cache/0", "rediss://queue/0"]
    assert all(call[1]["socket_timeout"] == 2 for call in calls)
    assert all(call[1]["max_connections"] == 32 for call in calls)
    assert all(call[1]["ssl_cert_reqs"] == "required" for call in calls)


@pytest.mark.asyncio
async def test_close_attempts_both_clients_even_when_first_fails() -> None:
    """关闭第一路失败也必须释放第二路，保留错误交还调用者。"""
    closed = []

    class Client:
        """用于验证 finally 清理路径。"""

        def __init__(self, name):
            """标记连接池名称。"""
            self.name = name

        async def aclose(self):
            """记录调用并只在第一路注入故障。"""
            closed.append(self.name)
            if self.name == "cache":
                raise RuntimeError("close failure")

    with pytest.raises(RuntimeError):
        await RedisRuntime(Client("cache"), Client("coordination")).close()
    assert closed == ["cache", "coordination"]


def test_celery_factory_only_configures_application(monkeypatch) -> None:
    """工厂不注册伪入库任务、不发送消息，不调用启动 Worker。"""
    class App:
        """只提供允许调用的配置接口，其余调用将直接失败。"""

        def __init__(self, name):
            """接收 app 名称，不建立连接。"""
            self.name = name
            self.conf = {}

    inject_module(monkeypatch, "celery", Celery=App)
    app = create_celery_app(enabled_settings(
        celery_enabled=True, celery_result_url="rediss://queue/1",
    ))
    assert app.name == "enterprise_rag"
    assert app.conf["result_backend"] == "rediss://queue/1"
    assert app.conf["task_default_queue"] == "rag.ingestion"
    assert not app.conf["task_send_sent_event"]


def test_s3_factory_explicit_credentials_and_sdk_retry_bound(monkeypatch) -> None:
    """MinIO adapter 使用显式配置和 path-style，不依赖机器默认凭据链。"""
    calls = []

    def client(service, **kwargs):
        """记录 SDK 构造参数，不验证真实服务兼容性。"""
        calls.append((service, kwargs))
        return object()

    inject_module(monkeypatch, "boto3", client=client)
    inject_module(monkeypatch, "botocore", __path__=[])
    inject_module(monkeypatch, "botocore.config", Config=lambda **kwargs: kwargs)
    storage = S3ObjectStorage.from_settings(InfrastructureSettings(
        storage_provider="s3", s3_endpoint="https://minio.invalid",
        s3_access_key="fixture-access", s3_secret_key="fixture-secret",
        s3_bucket="rag", s3_prefix="tenant-data",
    ))
    assert storage.bucket == "rag" and storage.prefix == "tenant-data"
    service, options = calls[0]
    assert service == "s3"
    assert options["aws_access_key_id"] == "fixture-access"
    assert options["config"]["retries"]["total_max_attempts"] == 2
    assert options["config"]["s3"]["addressing_style"] == "path"
