"""LM Studio OpenAI-compatible Chat Provider。

该适配器只负责 HTTP 字段转换、响应解析和错误归一化；Prompt、证据引用和业务状态由上层
ExcerptAnswerService 负责。这样可以把本地 LM Studio 与未来的在线兼容接口隔离开。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .errors import ProviderError, ProviderErrorKind
from .protocols import ChatRequest, ChatResponse, ChatStreamEvent, ChatUsage


# 这些是单次调用的工程保护值，不代表 LM Studio 或底层模型的上下文窗口。
# 解析器在达到任一上限时立即终止上游响应，避免恶意/异常服务让内存无界增长。
_MAX_SSE_FRAME_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_VISIBLE_BYTES = 64 * 1024


class LMStudioChatProvider:
    """调用 LM Studio `/v1/chat/completions` 的异步 Provider。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
        api_key: str | None = None,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key

    def _api_base_url(self) -> str:
        """把配置的服务根地址规范化为唯一的 OpenAI-compatible `/v1` 根。

        Settings 允许用户填写 `http://127.0.0.1:1234`，也允许直接填写
        `http://127.0.0.1:1234/v1`。统一在 adapter 内补齐一次，避免请求
        路径漏掉版本前缀或出现 `/v1/v1`；该地址来自服务端配置，不接受
        请求体覆盖，因此不会把此方法变成任意 URL 代理。
        """

        return self._base_url if self._base_url.endswith("/v1") else f"{self._base_url}/v1"

    def _url(self) -> str:
        """返回唯一的 chat completions endpoint。"""

        suffix = "/chat/completions"
        return f"{self._api_base_url()}{suffix}"

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def _payload(request: ChatRequest, *, stream: bool) -> dict[str, Any]:
        return {
            "model": request.model,
            "messages": [{"role": item.role, "content": item.content} for item in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": stream,
        }

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        mapping = {
            400: ProviderErrorKind.INVALID_REQUEST,
            401: ProviderErrorKind.AUTHENTICATION,
            403: ProviderErrorKind.AUTHENTICATION,
            408: ProviderErrorKind.TIMEOUT,
            422: ProviderErrorKind.INVALID_REQUEST,
            429: ProviderErrorKind.RATE_LIMITED,
            504: ProviderErrorKind.TIMEOUT,
        }
        kind = mapping.get(response.status_code, ProviderErrorKind.UNAVAILABLE)
        raise ProviderError(kind, f"LM Studio 请求失败: HTTP {response.status_code}", retryable=kind in {
            ProviderErrorKind.TIMEOUT,
            ProviderErrorKind.RATE_LIMITED,
            ProviderErrorKind.UNAVAILABLE,
        })

    @staticmethod
    def _choice(data: Any) -> dict[str, Any]:
        """校验 OpenAI-compatible 响应的最小 choices 结构。"""

        try:
            choices = data["choices"]
            choice = choices[0]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio 响应缺少必需字段") from exc
        if not isinstance(choices, list) or not isinstance(choice, dict):
            raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio 响应 choices 格式非法")
        return choice

    @staticmethod
    def _usage(data: Any) -> ChatUsage | None:
        """把 OpenAI-compatible usage 映射为严格的领域类型。"""

        usage_data = data.get("usage") if isinstance(data, dict) else None
        if usage_data is None:
            return None
        if not isinstance(usage_data, dict):
            raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio usage 格式非法")

        def token_value(name: str) -> int | None:
            value = usage_data.get(name)
            if value is None:
                return None
            # bool 是 int 的子类，但不是合法 token 计数；负数也没有
            # 合理含义，不能静默修正为零。
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio usage 计数格式非法")
            return value

        return ChatUsage(
            input_tokens=token_value("prompt_tokens"),
            output_tokens=token_value("completion_tokens"),
        )

    async def complete(self, request: ChatRequest) -> ChatResponse:
        """执行一次非流式请求，并保留上游可选的 usage/finish_reason。"""

        try:
            response = await self._client.post(
                self._url(),
                headers=self._headers(),
                json=self._payload(request, stream=False),
            )
            self._raise_for_status(response)
            data = response.json()
            choice = self._choice(data)
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str):
                raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio 响应 content 格式非法")
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None and not isinstance(finish_reason, str):
                raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio finish_reason 格式非法")
            return ChatResponse(
                content=content,
                model=str(data.get("model") or request.model),
                finish_reason=finish_reason,
                usage=self._usage(data),
            )
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError(ProviderErrorKind.TIMEOUT, "LM Studio 请求超时", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(ProviderErrorKind.UNAVAILABLE, "LM Studio 服务不可用", retryable=True) from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio 返回非法 JSON") from exc

    async def stream_events(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        """解析 SSE 并保留可见增量与终态元数据。

        HTTPX 的 ``aiter_lines`` 适合简单文本读取，但无法让本适配器明确
        控制跨网络 chunk 的单帧字节预算。因此这里按 bytes 自己组装 SSE
        data 事件：支持一个事件含多条 ``data:`` 行，也支持一行被拆到
        多个网络 chunk；只有看到 ``[DONE]`` 才允许正常结束。
        """

        done = False
        visible_bytes = 0
        last_finish_reason: str | None = None
        last_usage: ChatUsage | None = None
        try:
            async with self._client.stream(
                "POST",
                self._url(),
                headers=self._headers(),
                json=self._payload(request, stream=True),
            ) as response:
                self._raise_for_status(response)
                async for payload in self._iter_sse_payloads(response):
                    if payload.strip() == "[DONE]":
                        done = True
                        # 不单独暴露一个原始 DONE 字符串；使用一个只含
                        # 元数据的终态事件，让调用方能在不重复正文的前提下
                        # 获得 finish_reason/usage，同时保持 events[-1] 可解释。
                        yield ChatStreamEvent(
                            finish_reason=last_finish_reason,
                            usage=last_usage,
                            done=True,
                        )
                        break
                    if payload == "[DONE]":
                        continue
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio 流响应包含非法 JSON") from exc
                    if not isinstance(data, dict):
                        raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio 流响应顶层格式非法")
                    choices = data.get("choices")
                    if not isinstance(choices, list):
                        raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio 流响应缺少 choices")
                    choice: dict[str, Any] = {}
                    if choices:
                        if not isinstance(choices[0], dict):
                            raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio 流响应 choices 格式非法")
                        choice = choices[0]
                    elif data.get("usage") is None:
                        # 允许仅包含 usage 的 OpenAI 终态 chunk；没有
                        # choices 且也没有 usage 则无法判断该帧的语义。
                        raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio 流响应缺少 choices")

                    finish_reason = choice.get("finish_reason")
                    if finish_reason is not None and not isinstance(finish_reason, str):
                        raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio finish_reason 格式非法")
                    usage = self._usage(data)
                    if finish_reason is not None:
                        last_finish_reason = finish_reason
                    if usage is not None:
                        last_usage = usage

                    delta = choice.get("delta", {})
                    if delta is None:
                        delta = {}
                    if not isinstance(delta, dict):
                        raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio 流响应 delta 格式非法")
                    content = delta.get("content") if isinstance(delta, dict) else None
                    if content is not None and not isinstance(content, str):
                        raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio 流响应 content 格式非法")
                    if content:
                        visible_bytes += len(content.encode("utf-8"))
                        if visible_bytes > _MAX_VISIBLE_BYTES:
                            raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio 可见响应超过 64 KiB")
                    # reasoning/reasoning_content 等字段有意不读取；即使
                    # 上游同时返回 reasoning 和 content，也只把 content 交给
                    # 上层，避免内部推理进入 SSE token 或评测正文。
                    yield ChatStreamEvent(
                        text=content or "",
                        finish_reason=finish_reason,
                        usage=usage,
                    )
                if not done:
                    raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio 流响应未收到 [DONE]")
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError(ProviderErrorKind.TIMEOUT, "LM Studio 流请求超时", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(ProviderErrorKind.UNAVAILABLE, "LM Studio 流服务不可用", retryable=True) from exc

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        """兼容旧的字符串增量接口，只输出 stream_events 的可见正文。"""

        async for event in self.stream_events(request):
            if event.text:
                yield event.text

    async def _iter_sse_payloads(self, response: httpx.Response) -> AsyncIterator[str]:
        """按 SSE 空行边界组装 data payload，并执行字节预算。"""

        buffer = bytearray()
        data_lines: list[bytes] = []
        frame_bytes = 0
        response_bytes = 0

        def process_line(raw_line: bytes, *, has_newline: bool) -> str | None:
            """处理一行 SSE；返回空行结束的 data payload。"""

            nonlocal frame_bytes, data_lines
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            frame_bytes += len(raw_line) + (1 if has_newline else 0)
            if frame_bytes > _MAX_SSE_FRAME_BYTES:
                raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio SSE 单帧超过 64 KiB")
            if raw_line == b"":
                if not data_lines:
                    frame_bytes = 0
                    return None
                payload = b"\n".join(data_lines)
                data_lines = []
                frame_bytes = 0
                try:
                    return payload.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio SSE 不是合法 UTF-8") from exc
            if raw_line.startswith(b"data:"):
                value = raw_line[5:]
                if value.startswith(b" "):
                    value = value[1:]
                data_lines.append(value)
            # id/event/retry/comment 等字段对本 Provider 没有业务意义，
            # 只计入帧预算而不透传，避免把控制字段误当成模型正文。
            return None

        async for chunk in response.aiter_bytes():
            response_bytes += len(chunk)
            if response_bytes > _MAX_RESPONSE_BYTES:
                raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio 流响应超过 1 MiB")
            buffer.extend(chunk)
            if len(buffer) + frame_bytes > _MAX_SSE_FRAME_BYTES:
                raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio SSE 单帧超过 64 KiB")
            while True:
                try:
                    newline_index = buffer.index(10)
                except ValueError:
                    break
                raw_line = bytes(buffer[:newline_index])
                del buffer[: newline_index + 1]
                payload = process_line(raw_line, has_newline=True)
                if payload is not None:
                    yield payload

        if buffer:
            payload = process_line(bytes(buffer), has_newline=False)
            if payload is not None:
                yield payload
        if data_lines:
            # SSE 规范允许 EOF 前没有额外空行；把最后一帧交给上层，
            # 由它继续要求 [DONE]，因此不会把普通 EOF 误判成成功。
            payload = b"\n".join(data_lines)
            try:
                yield payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProviderError(ProviderErrorKind.PROTOCOL, "LM Studio SSE 不是合法 UTF-8") from exc

    async def health_check(self) -> bool:
        """只读探测模型列表，不触发生成。"""

        try:
            response = await self._client.get(
                f"{self._api_base_url()}/models",
                headers=self._headers(),
                timeout=2.0,
            )
            return response.status_code < 400
        except httpx.HTTPError:
            return False
