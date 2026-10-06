"""兼容现有 alias 的有界路由；只有可信产品上下文可以选择在线模型。"""

from __future__ import annotations

import asyncio
import time
import tomllib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from enterprise_rag.providers.errors import ProviderError, ProviderErrorKind
from enterprise_rag.providers.protocols import ChatRequest

_scope: ContextVar[tuple[str, float] | None] = ContextVar("model_tenant_deadline", default=None)
_RETRYABLE = {ProviderErrorKind.TIMEOUT, ProviderErrorKind.RATE_LIMITED, ProviderErrorKind.UNAVAILABLE}


@contextmanager
def model_scope(tenant: str, deadline: float):
    """产品授权后注入可信 tenant/deadline，并在请求结束时清除。"""
    token = _scope.set((tenant, deadline))
    try:
        yield
    finally:
        _scope.reset(token)


@dataclass(frozen=True, slots=True)
class DeepSeekSettings:
    """在线路由配置；API key 不进入 repr，也不存入 TOML。"""

    enabled: bool = False
    base_url: str = "https://api.aiportx.com/v1"
    model: str = "deepseek-v4.1-flash"
    api_key: str = field(default="", repr=False)
    allowed_tenants: tuple[str, ...] = ()
    primary_seconds: int = 30

    @classmethod
    def load(cls, root: Path):
        """仅读项目指定配置和独立密钥文件，不读取旧 .env。"""
        path = root / "config/deepseek.toml"
        if not path.exists():
            return cls()
        with path.open("rb") as source:
            values = tomllib.load(source)
        if set(values) - {
            "enabled", "base_url", "model", "api_key_file", "allowed_tenants", "primary_seconds",
        }:
            raise ValueError("DeepSeek 配置包含未知字段")
        enabled = values.get("enabled", False)
        if type(enabled) is not bool:
            raise ValueError("DeepSeek enabled 必须为 bool")
        secret_path = (root / values.get(
            "api_key_file", ".local/secrets/deepseek-api-key.txt",
        )).resolve()
        if root.resolve() not in secret_path.parents:
            raise ValueError("DeepSeek 密钥文件必须在项目目录内")
        # 默认关闭不读取凭据；启用后空值和示例占位 key 明确拒绝。
        key = secret_path.read_text().strip() if enabled and secret_path.is_file() else ""
        tenants = values.get("allowed_tenants", [])
        if not isinstance(tenants, list) or any(not isinstance(value, str) for value in tenants):
            raise ValueError("DeepSeek allowed_tenants 必须是字符串列表")
        settings = cls(
            enabled=enabled, api_key=key, base_url=values.get("base_url", "https://api.aiportx.com/v1"),
            model=values.get("model", "deepseek-v4.1-flash"),
            allowed_tenants=tuple(tenants),
            primary_seconds=values.get("primary_seconds", 30),
        )
        settings.validate()
        return settings

    def validate(self):
        """禁止 HTTP、任意路径/凭据和非法 allowlist；空 allowlist 默认拒绝外发。"""
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path != "/v1" or parsed.query or parsed.fragment
        ):
            raise ValueError("DeepSeek base_url 必须为 HTTPS /v1 endpoint")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("DeepSeek model 不能为空")
        if type(self.primary_seconds) is not int or not 1 <= self.primary_seconds <= 120:
            raise ValueError("DeepSeek primary_seconds 必须为 1 到 120")
        if any(not isinstance(tenant, str) or not tenant.strip() for tenant in self.allowed_tenants):
            raise ValueError("DeepSeek allowed_tenants 非法")
        if self.enabled and (not self.api_key or self.api_key == "sk-xxxxxxxxxxx"):
            raise ValueError("请在 .local/secrets/deepseek-api-key.txt 填写真实 API key")


class PolicyRoutedProvider:
    """保留 local_answer alias；在线失败仅在未输出内容时单向兜底一次。"""

    def __init__(self, local, online, settings: DeepSeekSettings, local_model: str):
        """共享已有 HTTP client，策略对象不创建第二套传输/连接池。"""
        settings.validate()
        self.local, self.online, self.settings, self.local_model = local, online, settings, local_model
        self._client = getattr(local, "_client", None)
        self.fallback_count = 0
        self.online_count = 0

    def _route(self):
        """无可信上下文、未启用或租户未授权时直接本地，绝不先尝试外发。"""
        scope = _scope.get()
        online = bool(
            self.settings.enabled and scope is not None and scope[0] in self.settings.allowed_tenants
        )
        deadline = scope[1] if scope else time.monotonic() + 300
        return online, deadline

    def permits_online(self, tenant: str) -> bool:
        """供产品边界决定是否需要在线外发前的资源复核，不依赖调用方自报身份。"""
        return self.settings.enabled and tenant in self.settings.allowed_tenants

    @staticmethod
    def _remaining(deadline):
        """耗尽预算时在调用前拒绝，不能依赖 timeout(0) 的异步调度时机。"""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError(ProviderErrorKind.TIMEOUT, "模型总预算耗尽", retryable=True)
        return remaining

    async def complete(self, request: ChatRequest):
        """主用调用只使用局部预算，兜底继承原 deadline 而不重新计时。"""
        online, deadline = self._route()
        self._remaining(deadline)
        if online:
            self.online_count += 1
            try:
                async with asyncio.timeout(min(
                    self.settings.primary_seconds, self._remaining(deadline),
                )):
                    return await self.online.complete(replace(request, model=self.settings.model))
            except TimeoutError:
                pass
            except ProviderError as exc:
                if exc.kind not in _RETRYABLE:
                    raise
            self.fallback_count += 1
        try:
            async with asyncio.timeout(self._remaining(deadline)):
                return await self.local.complete(replace(request, model=self.local_model))
        except TimeoutError:
            raise ProviderError(ProviderErrorKind.TIMEOUT, "模型总预算耗尽", retryable=True) from None

    async def stream_events(self, request: ChatRequest):
        """首个上游事件后不透明切换，避免两模型输出被拼成一份回答。"""
        online, deadline = self._route()
        self._remaining(deadline)
        if online:
            started = False
            self.online_count += 1
            try:
                async with asyncio.timeout(min(
                    self.settings.primary_seconds, self._remaining(deadline),
                )):
                    async for event in self.online.stream_events(replace(request, model=self.settings.model)):
                        started = True
                        yield event
                return
            except TimeoutError:
                if started:
                    raise ProviderError(ProviderErrorKind.TIMEOUT, "在线流中断") from None
            except ProviderError as exc:
                if started or exc.kind not in _RETRYABLE:
                    raise
            self.fallback_count += 1
        try:
            async with asyncio.timeout(self._remaining(deadline)):
                async for event in self.local.stream_events(replace(request, model=self.local_model)):
                    yield event
        except TimeoutError:
            raise ProviderError(ProviderErrorKind.TIMEOUT, "模型总预算耗尽") from None

    async def stream(self, request: ChatRequest):
        """兼容原字符串流接口，不暴露 reasoning 或内部策略数据。"""
        async for event in self.stream_events(request):
            if event.text:
                yield event.text

    async def health_check(self):
        """本地兜底可用即可接收请求；在线探测失败由调用和监控体现。"""
        return await self.local.health_check()
