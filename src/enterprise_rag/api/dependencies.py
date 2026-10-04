"""FastAPI 产品路由共享的可信身份依赖。"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

from enterprise_rag.security.context import RequestContext
from enterprise_rag.security.dev_token import DevTokenCodec, DevTokenError

RequestContextDependency = Callable[
    [str | None],
    Coroutine[Any, Any, RequestContext],
]


def build_request_context_dependency(codec: DevTokenCodec) -> RequestContextDependency:
    """绑定服务端 token codec，生成只能从 Authorization Bearer 取身份的依赖。"""

    async def resolve_request_context(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> RequestContext:
        """验证 Bearer token，并将所有认证失败归一化为不泄露输入的 401。"""

        if authorization is None:
            raise _authentication_required()
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            raise _authentication_required()
        # token 中不允许额外空白；这既避免不同代理的解析歧义，也防止错误日志
        # 在后续层意外包含未经验证的原始 header。
        if token != token.strip() or any(character.isspace() for character in token):
            raise _authentication_required()
        try:
            return codec.verify(token)
        except DevTokenError as exc:
            raise _authentication_required() from exc

    return resolve_request_context


def _authentication_required() -> HTTPException:
    """构造固定认证错误，禁止把 token、签名错误细节或 secret 回显给客户端。"""

    return HTTPException(
        status_code=401,
        detail={
            "kind": "authentication_required",
            "message": "需要有效的开发身份 Bearer token",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_local_request(request: Request) -> None:
    """限定真实 socket peer 和浏览器 Origin；不信任转发头或客户端自报租户。"""

    try:
        local_peer = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        local_peer = False
    origin = request.headers.get("origin")
    # 无 Origin 的本机 CLI 可访问；浏览器请求只允许本机页面来源。
    parsed = urlsplit(origin) if origin else None
    local_origin = parsed is None or (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and parsed.username is None and parsed.password is None
    )
    if not local_peer or not local_origin:
        raise HTTPException(status_code=403, detail={"kind": "local_only", "message": "仅允许本机访问"})


def build_local_anonymous_dependency():
    """显式免 Token 模式使用服务端固定演示身份，保留下游所有 ACL 检查。"""

    async def resolve_local_context(request: Request) -> RequestContext:
        """先验证本机边界，再构造不可由请求头覆盖的 tenant/actor。"""

        require_local_request(request)
        now = int(time.time())
        return RequestContext(
            actor_id="local-demo-user", tenant_id="local-demo",
            roles=frozenset({"editor", "reader"}),
            issued_at=now, expires_at=now + 3600, token_version=1,
        )

    return resolve_local_context


__all__ = [
    "RequestContextDependency",
    "build_local_anonymous_dependency",
    "build_request_context_dependency",
    "require_local_request",
]
