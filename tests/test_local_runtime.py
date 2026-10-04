"""启动管理的端口避让、进程归属与重复启动回归测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "local_runtime", Path(__file__).resolve().parents[1] / "scripts/local_runtime.py",
)
runtime = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runtime)


def test_choose_port_skips_occupied_port(monkeypatch) -> None:
    """占用端口不能被夺取，下一空闲端口才可以作为启动地址。"""

    class Listener:
        """模拟端口绑定，避免离线测试依赖沙箱 socket 权限。"""

        def __enter__(self):
            """返回模拟监听器。"""
            return self

        def __exit__(self, *_args):
            """模拟释放监听资源。"""
            return False

        def bind(self, address):
            """将首选端口标记为占用。"""
            if address[1] == 8000:
                raise OSError("occupied")

    monkeypatch.setattr(runtime.socket, "socket", Listener)
    assert runtime.choose_port(8000) == 8001


def test_owns_process_requires_matching_factory_root_and_port(monkeypatch) -> None:
    """只确认本项目、本入口和本端口的 PID，禁止误停其他服务。"""

    command = f"python -m uvicorn scripts.local_p0_api:app --app-dir {runtime.ROOT} --port 8001"

    def run(*_args, **_kwargs):
        """返回受控的 ps 输出，不查询或停止真实用户进程。"""
        return SimpleNamespace(returncode=0, stdout=command)

    monkeypatch.setattr(runtime.subprocess, "run", run)
    assert runtime.owns_process({"pid": 123, "port": 8001})
    assert not runtime.owns_process({"pid": 123, "port": 8000})
    command = "python other_project.py --port 8001"
    assert not runtime.owns_process({"pid": 123, "port": 8001})


def test_start_reuses_owned_service(monkeypatch) -> None:
    """重复启动已归属的服务时不创建容器、迁移或新 API 进程。"""

    displayed = []
    monkeypatch.setattr(runtime, "read_state", lambda: {"pid": 123, "port": 8001})
    monkeypatch.setattr(runtime, "owns_process", lambda _state: True)
    monkeypatch.setattr(runtime, "show_status", displayed.append)
    assert runtime.start() == 0
    assert displayed == [{"pid": 123, "port": 8001}]
