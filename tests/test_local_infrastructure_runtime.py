"""本机后台生命周期离线测试，不启动进程、迁移业务数据库或停止容器。"""

from types import SimpleNamespace

import pytest

from scripts import local_infrastructure_runtime as runtime


def test_background_pid_requires_project_marker(monkeypatch):
    """不同项目的同名Celery也不能被当成本项目后台。"""
    monkeypatch.setattr(runtime.subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=0, stdout="python -m celery --hostname=another-project",
    ))
    assert not runtime.owns_process({"pid": 123, "marker": runtime.MARKER})
    assert not runtime.owns_process({"pid": 123})


def test_stop_never_signals_unowned_process(monkeypatch):
    """归属不明确时禁止发信号，不依赖PID是否存在。"""
    monkeypatch.setattr(runtime, "owns_process", lambda _entry: False)

    def signal_is_forbidden(*_args):
        """外部进程不能收到本项目停止命令。"""
        pytest.fail("不应停止其他进程")

    monkeypatch.setattr(runtime.os, "kill", signal_is_forbidden)
    runtime.stop_process({"pid": 123, "marker": "other"})


def test_configuration_rejects_partial_redis_pair(monkeypatch):
    """不能自动补另一实例而混用既有外部Redis，失败时没有Docker或迁移调用。"""
    monkeypatch.setattr(runtime.Settings, "from_env", lambda: SimpleNamespace(
        app_env="development", postgres_dsn="configured",
    ))
    monkeypatch.setenv("REDIS_CACHE_URL", "redis://cache/0")
    monkeypatch.delenv("REDIS_COORDINATION_URL", raising=False)
    with pytest.raises(RuntimeError, match="完整配置"):
        runtime.configure({})
