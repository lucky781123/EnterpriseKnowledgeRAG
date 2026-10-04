"""一键启动的进程管理：只管理本脚本创建的 API，不停止外部模型或用户服务。"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".local" / "runtime"
STATE = RUNTIME / "service.json"
LOG = RUNTIME / "api.log"


def read_state() -> dict:
    """读取不含凭据的进程记录；无记录时返回空状态。"""
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def owns_process(state: dict) -> bool:
    """核对 PID 的实际命令，避免 PID 重用后误停其他服务。"""
    pid = state.get("pid")
    if not isinstance(pid, int):
        return False
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False,
    )
    return (
        result.returncode == 0
        and "scripts.local_p0_api:app" in result.stdout
        and f"--app-dir {ROOT}" in result.stdout
        and f"--port {state.get('port')}" in result.stdout
    )


def choose_port(start: int) -> int:
    """从期望端口起寻找空闲端口，不夺取现有监听进程。"""
    for port in range(start, min(start + 100, 65536)):
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("没有可用端口")


def show_status(state: dict) -> None:
    """打印前后端位置及日志路径，绝不打印环境变量或 token。"""
    print(json.dumps({
        "running": owns_process(state),
        "api": state.get("api"),
        "frontend": f"{state['api']}/product-workbench/" if state.get("api") else None,
        "log": str(LOG),
    }, ensure_ascii=False, indent=2))


def start() -> int:
    """预检真实依赖、迁移数据库，脱离终端启动并等待前后端实际就绪。"""
    state = read_state()
    if owns_process(state):
        show_status(state)
        return 0
    RUNTIME.mkdir(parents=True, exist_ok=True)
    # 已存在的本地开发 PostgreSQL 只执行 start；不创建未知凭据容器。
    if subprocess.run(
        ["docker", "inspect", "enterprise-rag-postgres"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0:
        subprocess.run(
            ["docker", "start", "enterprise-rag-postgres"],
            check=True, stdout=subprocess.DEVNULL,
        )
    subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "compose.yaml"), "up", "-d", "weaviate"],
        cwd=ROOT, check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "enterprise_rag.db.migration_cli"], cwd=ROOT, check=True,
    )
    port = choose_port(int(os.getenv("P0_API_PORT", "8000")))
    api = f"http://127.0.0.1:{port}"
    environment = os.environ | {
        "P0_API_BASE_URL": api, "P0_API_PORT": str(port), "P0_WORKBENCH_ENABLED": "true",
        "GRADIO_ANALYTICS_ENABLED": "false", "TOKENIZERS_PARALLELISM": "false",
    }
    # 不使用 reload，保证记录的 PID 就是实际服务进程；启动日志限制本地用户可读。
    with LOG.open("a") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "scripts.local_p0_api:app",
             "--factory", "--app-dir", str(ROOT), "--host", "127.0.0.1", "--port", str(port),
             "--no-proxy-headers"],
            cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, start_new_session=True,
        )
    state = {
        "pid": process.pid, "port": port, "api": api,
        "anonymous": os.getenv("P0_ANONYMOUS_ENABLED", "false").lower() in {"true", "1", "yes", "on"},
    }
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    deadline = time.monotonic() + 180
    with httpx.Client(timeout=10) as client:
        while time.monotonic() < deadline and process.poll() is None:
            try:
                ready = client.get(f"{api}/health/product/ready")
                frontend = client.get(f"{api}/product-workbench/")
                if ready.status_code == 200 and frontend.status_code == 200:
                    show_status(state)
                    return 0
            except httpx.HTTPError:
                pass
            time.sleep(2)
    # 失败时仅停止刚创建的进程，不触及用户的 8000 或共享容器。
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=15)
    print(f"启动失败，请检查本地日志：{LOG}", file=sys.stderr)
    return 1


def main() -> int:
    """提供 start/status/stop；错误只输出类型，避免 traceback 泄露配置。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "status", "stop"], nargs="?", default="start")
    args = parser.parse_args()
    try:
        if args.action == "start":
            return start()
        state = read_state()
        if args.action == "stop" and owns_process(state):
            os.kill(state["pid"], signal.SIGTERM)
            print("已向本脚本管理的 API 发送停止信号；数据库与模型服务保持运行。")
        else:
            show_status(state)
        return 0
    except Exception as exc:  # noqa: BLE001 -- 顶层安全边界禁止回显包含配置的异常正文。
        print(f"启动管理失败：{type(exc).__name__}（配置值未输出）", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
