"""生产 OIDC/JWT 身份适配。

开发环境继续使用 DevTokenCodec。生产实现使用 PyJWT 的 JWKS 校验；
本模块延迟导入可选依赖，避免离线单测和本地 P0 被迫访问网络或加载认证 SDK。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from fastapi import Header, HTTPException

from .context import RequestContext


class OIDCVerifier(Protocol):
    """OIDC verifier 的最小协议，便于测试注入 fake verifier。"""

    def verify(self, token: str) -> dict[str, Any]:
        """验证 JWT 签名和标准 claims，并返回可信 claims。"""


class TenantMembershipResolver(Protocol):
    """生产 membership 数据源协议。"""

    async def roles_for(self, *, actor_id: str, tenant_id: str) -> frozenset[str] | None:
        """返回 actor 在 tenant 内的服务端角色；不存在时返回 None。"""


class MappingTenantMembershipResolver:
    """使用进程内映射提供可替换的 membership resolver。

    该实现用于本地集成测试和单机演示，不声称具备生产目录的一致性、撤权
    推送或跨进程共享能力。生产部署应注入数据库、IdP group 或权限服务实现。
    """

    def __init__(
        self,
        memberships: Mapping[tuple[str, str], Iterable[str]],
    ) -> None:
        """复制并校验 actor/tenant 到角色集合的服务端映射。"""

        normalized: dict[tuple[str, str], frozenset[str]] = {}
        for identity, roles in memberships.items():
            if (
                not isinstance(identity, tuple)
                or len(identity) != 2
                or not all(isinstance(value, str) and value.strip() for value in identity)
            ):
                raise ValueError("membership key 必须是非空的 (actor_id, tenant_id)")
            role_set = frozenset(
                role.strip()
                for role in roles
                if isinstance(role, str) and role.strip()
            )
            if not role_set:
                raise ValueError("membership 角色集合不能为空")
            normalized[(identity[0].strip(), identity[1].strip())] = role_set
        self._memberships = normalized

    async def roles_for(self, *, actor_id: str, tenant_id: str) -> frozenset[str] | None:
        """按可信 actor/tenant identity 查询服务端角色，不回退到 token claims。"""

        return self._memberships.get((actor_id.strip(), tenant_id.strip()))


@dataclass(frozen=True, slots=True)
class OIDCSettings:
    """OIDC issuer、audience 和 JWKS 配置，不保存 access token。"""

    issuer: str
    audience: str
    jwks_url: str
    tenant_claim: str = "tenant_id"
    roles_claim: str = "roles"

    def validate(self) -> None:
        """拒绝非 HTTPS issuer/JWKS，避免把生产身份降级到明文 HTTP。"""

        for name, value in (("issuer", self.issuer), ("jwks_url", self.jwks_url)):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError(f"OIDC {name} 必须是 HTTPS URL")
        if not self.audience.strip():
            raise ValueError("OIDC audience 不能为空")


class PyJWTJWKSVerifier:
    """使用 PyJWT/JWKS 验证 RS256/ES256 OIDC access token。"""

    def __init__(self, settings: OIDCSettings, *, leeway_seconds: int = 30) -> None:
        """保存公开 OIDC 配置；构造时不发起网络请求。"""

        settings.validate()
        if leeway_seconds < 0:
            raise ValueError("leeway_seconds 不能小于 0")
        self.settings = settings
        self.leeway_seconds = leeway_seconds

    def verify(self, token: str) -> dict[str, Any]:
        """通过 issuer/audience/JWKS 验证 token，失败只抛固定认证错误。"""

        if not token.strip():
            raise ValueError("OIDC token 不能为空")
        try:
            import jwt
            from jwt import PyJWKClient

            signing_key = PyJWKClient(self.settings.jwks_url).get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                leeway=self.leeway_seconds,
                options={"require": ["sub", "exp", "iat"]},
            )
        except ImportError as exc:
            raise RuntimeError("生产 OIDC 需要安装 PyJWT[crypto]") from exc
        except Exception as exc:
            raise ValueError("OIDC token 无效") from exc
        if not isinstance(claims, dict):
            raise TypeError("OIDC claims 非法")
        return claims


def build_oidc_context_dependency(
    verifier: OIDCVerifier,
    membership_resolver: TenantMembershipResolver,
    *,
    settings: OIDCSettings,
):
    """构造基于可信 JWT 和服务端 membership 的 FastAPI dependency。"""

    settings.validate()

    async def resolve_request_context(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> RequestContext:
        """验证 Bearer JWT，并以 membership 角色覆盖 token 自报角色。"""

        if authorization is None:
            raise HTTPException(
                status_code=401,
                detail={"kind": "authentication_required", "message": "需要有效的 OIDC Bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token.strip():
            raise HTTPException(
                status_code=401,
                detail={"kind": "authentication_required", "message": "需要有效的 OIDC Bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            claims = verifier.verify(token)
            actor_id = claims.get("sub")
            tenant_id = claims.get(settings.tenant_claim)
            if not isinstance(actor_id, str) or not isinstance(tenant_id, str):
                raise TypeError("OIDC identity claims 缺失")
            roles = await membership_resolver.roles_for(
                actor_id=actor_id.strip(), tenant_id=tenant_id.strip()
            )
            if not roles:
                raise PermissionError("tenant membership 不存在")
            issued_at = claims.get("iat")
            expires_at = claims.get("exp")
            if not isinstance(issued_at, int) or not isinstance(expires_at, int):
                raise TypeError("OIDC 时间 claims 非法")
            return RequestContext(
                actor_id=actor_id.strip(),
                tenant_id=tenant_id.strip(),
                roles=frozenset(roles),
                issued_at=issued_at,
                expires_at=expires_at,
                token_version=1,
            )
        except PermissionError as exc:
            raise HTTPException(
                status_code=403,
                detail={"kind": "forbidden", "message": "当前身份未加入该租户"},
            ) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(
                status_code=401,
                detail={"kind": "authentication_required", "message": "OIDC 身份无效"},
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    return resolve_request_context


__all__ = [
    "MappingTenantMembershipResolver",
    "OIDCSettings",
    "OIDCVerifier",
    "PyJWTJWKSVerifier",
    "TenantMembershipResolver",
    "build_oidc_context_dependency",
]
