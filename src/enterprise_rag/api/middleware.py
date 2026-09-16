"""API 通用 ASGI middleware。

本模块只处理与业务无关、但必须在 Pydantic 解析之前成立的请求体预算。
实际读取并累计每个 ``http.request`` 消息的字节数，因此不会把客户端提供的
``Content-Length`` 当作可信安全边界。缓存上限只有 128 KiB，随后把已读取的
正文以一次 ``receive`` 消息重放给 FastAPI，避免重复读取或让路由看到空正文。
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any


ASGIMessage = dict[str, Any]
Receive = Callable[[], Awaitable[ASGIMessage]]
Send = Callable[[ASGIMessage], Awaitable[None]]


class RequestBodyLimitMiddleware:
    """按实际读取字节数拒绝超过上限的 HTTP 请求。"""

    def __init__(self, app: Callable[..., Awaitable[None]], max_bytes: int = 128 * 1024) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes 必须大于 0")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        """读取并重放请求体；超限时直接发送稳定 413 响应。"""

        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # 记录尽可能靠近入口的 monotonic 起点，后续路由可把它用于总预算。
        scope.setdefault("state", {})["request_started_at"] = time.monotonic()
        chunks: list[bytes] = []
        total_bytes = 0

        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                # 客户端在正文完成前断开时交回下游，由框架按取消处理。
                await self.app(scope, receive, send)
                return
            if message.get("type") != "http.request":
                await self.app(scope, receive, send)
                return

            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes):
                # ASGI server 应始终提供 bytes；异常消息不做隐式转换，避免
                # 统计结果与实际网络流不一致。
                await self.app(scope, receive, send)
                return
            total_bytes += len(chunk)
            if total_bytes > self.max_bytes:
                await self._send_too_large(send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        replayed = False

        async def replay_receive() -> ASGIMessage:
            """把已计数正文只重放一次，后续交给原始 receive。"""

            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _send_too_large(self, send: Send) -> None:
        payload = json.dumps(
            {"error": {"kind": "invalid_request", "message": "请求体超过 128 KiB"}},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
