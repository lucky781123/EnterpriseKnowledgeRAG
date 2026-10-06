"""本机异步后台生命周期：复用既有配置，只管理本项目的 API/Worker/dispatcher。"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

from enterprise_rag.config.settings import Settings
from scripts import local_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local/runtime/background.json"
MARKER = "enterprise-rag-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
COMPOSE = [
    "docker", "compose", "-f", str(ROOT / "compose.local-async.yaml"),
    "-p", "enterprise-rag-local-async",
]


def read_state():
    """只读取不含密钥的 PID/本机 Redis 端口记录，不读取任何凭据文件。"""
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def owns_process(entry):
    """PID 与项目专用命令标识必须同时匹配，防止误停其他项目或重用 PID。"""
    if not isinstance(entry.get("pid"), int):
        return False
    result = subprocess.run(
        ["ps", "-p", str(entry["pid"]), "-o", "command="],
        capture_output=True, text=True, check=False,
    )
    marker = entry.get("marker")
    return result.returncode == 0 and bool(marker) and marker in result.stdout


def stop_process(entry):
    """只向归属已核验的后台进程发送 TERM；退出前不重启重复消费者。"""
    if not owns_process(entry):
        return
    os.kill(entry["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if not owns_process(entry):
            return
        time.sleep(0.2)
    raise RuntimeError("本项目后台尚未退出")


def configure(state):
    """仅在 development 填补本机配置；不修改 .env、外发白名单或存储类型。"""
    settings = Settings.from_env()
    if settings.app_env != "development" or not settings.postgres_dsn:
        raise RuntimeError("本机异步启动需要已配置的 development PostgreSQL")
    pair = (os.getenv("REDIS_CACHE_URL"), os.getenv("REDIS_COORDINATION_URL"))
    if any(pair) and not all(pair):
        raise RuntimeError("双 Redis 必须完整配置，不能混用不同环境")
    if not all(pair):
        # 显式关闭 Compose 的隐式 .env 读取；仅 Redis 服务，不构建或拉取新镜像。
        environment = os.environ | {"COMPOSE_DISABLE_ENV_FILE": "true"}
        subprocess.run(
            COMPOSE + ["up", "-d", "--wait", "--pull", "never"],
            env=environment, cwd=ROOT, check=True,
        )
        for service, name in (
            ("redis-cache", "REDIS_CACHE_URL"),
            ("redis-coordination", "REDIS_COORDINATION_URL"),
        ):
            address = subprocess.check_output(
                COMPOSE + ["port", service, "6379"], env=environment, cwd=ROOT, text=True,
            ).strip()
            if not address.startswith("127.0.0.1:"):
                raise RuntimeError("本机 Redis 只能发布到 loopback")
            os.environ[name] = f"redis://{address}/0"
        state["managed_redis"] = True
    os.environ.update({
        "RAG_INFRA_ENABLED": "true", "RAG_CELERY_ENABLED": "true",
        "CELERY_RESULT_BACKEND": os.getenv("CELERY_RESULT_BACKEND")
        or os.environ["REDIS_COORDINATION_URL"].rsplit("/", 1)[0] + "/1",
        "INGESTION_OLLAMA_BASE_URL": os.getenv("INGESTION_OLLAMA_BASE_URL")
        or settings.ollama_base_url,
        "PYTHONUNBUFFERED": "1",
    })
    # 可选 SDK 已安装在项目专用目录时复用；不改 Conda，也不下载模型。
    paths = [str(ROOT), str(ROOT / "src"), str(ROOT / ".local/infrastructure-deps")]
    os.environ["PYTHONPATH"] = os.pathsep.join(paths + [os.getenv("PYTHONPATH", "")])
    # PYTHONPATH 仅对子进程生效；管理进程自己的 Celery 健康探测也要复用该 SDK。
    for path in reversed(paths):
        if path not in sys.path:
            sys.path.insert(0, path)
    from enterprise_rag.infrastructure.settings import InfrastructureSettings

    InfrastructureSettings.from_env()


def launch(name, command, marker):
    """脱离终端启动一个后台进程，日志仅写项目 runtime 目录且不包含环境配置。"""
    path = ROOT / f".local/runtime/{name}.log"
    with path.open("a") as log:
        process = subprocess.Popen(
            command, cwd=ROOT, env=os.environ.copy(), stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, start_new_session=True,
        )
    return {"pid": process.pid, "marker": marker}


def status():
    """同时展示 API/UI 和辅助进程存活，不把进程存活冒充任务已完成。"""
    state = read_state()
    runtime.show_status(runtime.read_state())
    print(json.dumps({
        "worker_running": owns_process(state.get("worker", {})),
        "dispatcher_running": owns_process(state.get("dispatcher", {})),
        "managed_redis": state.get("managed_redis", False),
        "background_log_directory": str(ROOT / ".local/runtime"),
    }, ensure_ascii=False, indent=2))


def start():
    """启动双 Redis、显式 0005、Worker/dispatcher 并重启自己的 API/UI 以加载新装配。"""
    ROOT.joinpath(".local/runtime").mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    state = read_state()
    configure(state)
    # 仅新增 0005 表；原同步数据、对象、generation 不迁移或删除。
    subprocess.run(
        [sys.executable, "-m", "scripts.migrate_async_ingestion"], cwd=ROOT, check=True,
    )
    commands = {
        "worker": (
            [sys.executable, "-m", "celery", "-A", "scripts.celery_worker:app", "worker",
             "--pool=solo" if sys.platform == "darwin" else "--pool=prefork",
             "--concurrency=1", "--loglevel=WARNING", "--hostname=" + MARKER + "@%h"],
            MARKER,
        ),
        "dispatcher": (
            [sys.executable, str(ROOT / "scripts/ingestion_runtime.py")],
            str(ROOT / "scripts/ingestion_runtime.py"),
        ),
    }
    for name, (command, marker) in commands.items():
        if not owns_process(state.get(name, {})):
            state[name] = launch(name, command, marker)
            STATE.write_text(json.dumps(state, indent=2) + "\n")
    previous = runtime.read_state()
    if runtime.owns_process(previous) and not previous.get("async_enabled"):
        # 新基础设施只在启动时装配，旧服务不能通过修改父进程环境热更新。
        stop_process({
            "pid": previous["pid"],
            "marker": f"--app-dir {ROOT} --host 127.0.0.1 --port {previous['port']}",
        })
        os.environ["P0_API_PORT"] = str(previous["port"])
    if runtime.start() != 0:
        raise RuntimeError("异步 API/UI 未就绪")
    # health 已证明 API 装配；进一步用 Celery ping 等待真实消费者接收控制消息。
    from enterprise_rag.infrastructure.celery_app import create_celery_app

    celery = create_celery_app()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any(
                any(name.startswith(MARKER + "@") for name in reply)
                for reply in celery.control.ping(timeout=1)
            ):
                break
            if not owns_process(state["worker"]) or not owns_process(state["dispatcher"]):
                raise RuntimeError("后台进程提前退出")
            time.sleep(1)
        else:
            raise RuntimeError("Celery 未回应健康探测")
    finally:
        celery.close()
    api = runtime.read_state()["api"]
    with httpx.Client(timeout=5, trust_env=False) as client:
        ready = client.get(api + "/health/product/ready")
        if ready.status_code != 200:
            raise RuntimeError("产品依赖未就绪")
    status()
    return 0


def stop():
    """停止本项目辅助进程与 API；数据库、模型和 Redis 数据卷保持不变。"""
    state = read_state()
    for name in ("dispatcher", "worker"):
        stop_process(state.get(name, {}))
    api = runtime.read_state()
    if runtime.owns_process(api):
        os.kill(api["pid"], signal.SIGTERM)
    status()
    return 0
