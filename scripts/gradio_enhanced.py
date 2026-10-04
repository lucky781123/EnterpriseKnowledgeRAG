"""兼容旧7860书签，转到一键启动的产品工作台，避免第二套UI配置漂移。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from enterprise_rag.workbench.app import build_product_workbench

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".local/runtime/service.json"


def resolve_api_base_url(state_path: Path = STATE) -> str:
    """优先使用显式地址，否则读取一键启动状态，禁止静默回退到旧8000服务。"""

    configured = os.getenv("P0_API_BASE_URL", "").strip()
    if not configured:
        if not state_path.exists():
            raise RuntimeError("未找到产品服务，请先执行 bash start.sh")
        try:
            configured = json.loads(state_path.read_text(encoding="utf-8"))["api"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("产品服务状态无效，请重新执行 bash start.sh") from exc
    # Token只能发给本地受控产品API，不能因状态文件或环境误配发送到远程地址。
    if not isinstance(configured, str):
        raise TypeError("产品API地址必须为字符串")
    parsed = urlsplit(configured)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeError("旧工作台兼容入口仅允许本地产品API")
    return configured.rstrip("/")


def create_interface(*, api_base_url: str | None = None):
    """复用产品UI的上传、状态、检索问答及显式Token输入，不自动代签管理员身份。"""

    return build_product_workbench(api_base_url=api_base_url or resolve_api_base_url())


def create_redirect_app(api: str) -> FastAPI:
    """构造仅跳转的兼容服务，使FastAPI能从模块命名空间解析Request类型。"""
    from enterprise_rag.api.dependencies import require_local_request

    application = FastAPI()

    @application.get("/{path:path}")
    async def redirect(request: Request, path: str):
        """核对本机访问后跳转，不接收上传正文或代签身份。"""
        require_local_request(request)
        return RedirectResponse(f"{api}/product-workbench/", status_code=307)

    return application


def main() -> None:
    """仅监听本机7860并跳转到受控UI，使免Token和文件预算始终保持一致。"""
    import uvicorn

    api = resolve_api_base_url()
    print(f"产品API：{api}；工作台：http://127.0.0.1:7860")
    application = create_redirect_app(api)
    uvicorn.run(application, host="127.0.0.1", port=7860, proxy_headers=False)


if __name__ == "__main__":
    main()
