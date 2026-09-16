"""Provider alias 注册表。"""

from __future__ import annotations

from dataclasses import dataclass

from .protocols import ChatProvider


@dataclass(frozen=True, slots=True)
class RegisteredProvider:
    """服务端已注册的 Provider 与能力集合。"""

    provider: ChatProvider
    capabilities: frozenset[str]


class ProviderRegistry:
    """拒绝调用方自带 endpoint，只解析服务端预先注册的 alias。"""

    def __init__(self, providers: dict[str, RegisteredProvider] | None = None) -> None:
        self._providers = providers or {}

    def register(self, alias: str, provider: ChatProvider, *, capabilities: set[str]) -> None:
        self._providers[alias] = RegisteredProvider(provider, frozenset(capabilities))

    def items(self) -> tuple[tuple[str, RegisteredProvider], ...]:
        """返回只读快照，供 readiness 和生命周期管理遍历已注册 Provider。

        路由不能让调用方修改注册表，因此这里返回 tuple 快照而不是暴露内部
        dict。快照也避免 readiness 探测期间注册表被并发修改而产生不稳定遍历。
        """

        return tuple(self._providers.items())

    def resolve(self, alias: str, capability: str = "chat.complete") -> RegisteredProvider:
        try:
            registered = self._providers[alias]
        except KeyError as exc:
            raise ValueError("未注册的 model_alias") from exc
        if capability not in registered.capabilities:
            raise ValueError("Provider 不支持请求的 capability")
        return registered
