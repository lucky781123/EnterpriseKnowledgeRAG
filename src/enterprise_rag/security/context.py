"""跨 API、授权和检索层传递的可信请求上下文。"""

from __future__ import annotations

from dataclasses import dataclass


SUPPORTED_ROLES = frozenset({"reader", "editor", "admin"})


@dataclass(frozen=True, slots=True)
class RequestContext:
    """保存已验证 token 中的身份事实，禁止业务层重新信任客户端字段。"""

    actor_id: str
    tenant_id: str
    roles: frozenset[str]
    issued_at: int
    expires_at: int
    token_version: int

    def __post_init__(self) -> None:
        """校验上下文字段，阻止空身份、未知角色或非法时间范围向下游传播。"""

        if not self.actor_id.strip():
            raise ValueError("actor_id 不能为空")
        if not self.tenant_id.strip():
            raise ValueError("tenant_id 不能为空")
        if not self.roles:
            raise ValueError("roles 不能为空")
        unknown_roles = self.roles - SUPPORTED_ROLES
        if unknown_roles:
            raise ValueError("roles 包含未支持的角色")
        if self.issued_at < 0 or self.expires_at <= self.issued_at:
            raise ValueError("token 时间范围非法")
        if self.token_version != 1:
            raise ValueError("token version 不受支持")

    def has_role(self, role: str) -> bool:
        """判断当前身份是否具有指定角色，不执行任何隐式角色继承。"""

        return role in self.roles


__all__ = ["RequestContext", "SUPPORTED_ROLES"]
