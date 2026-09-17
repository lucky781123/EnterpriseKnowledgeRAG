"""FastAPI 产品路由共享的可信身份依赖。"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Header, HTTPException

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


__all__ = ["RequestContextDependency", "build_request_context_dependency"]
