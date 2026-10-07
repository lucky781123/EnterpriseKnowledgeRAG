"""OIDC 适配、tenant membership、配额和审计测试。"""

import pytest

from enterprise_rag.security.context import RequestContext
from enterprise_rag.security.governance import TenantGovernance, TenantQuotaExceeded
from enterprise_rag.security.oidc import (
    MappingTenantMembershipResolver,
    OIDCSettings,
    build_oidc_context_dependency,
)


class _Verifier:
    """返回固定 claims 的测试 verifier。"""

    def verify(self, token: str) -> dict:
        """拒绝错误 token，模拟 JWT 签名验证结果。"""

        if token != "valid":
            raise ValueError("invalid")
        return {"sub": "alice", "tenant_id": "tenant-a", "iat": 1, "exp": 9999999999}


class _Membership:
    """返回服务端 membership 角色。"""

    async def roles_for(self, *, actor_id: str, tenant_id: str):
        """只允许预置 tenant membership。"""

        return frozenset({"reader"}) if actor_id == "alice" and tenant_id == "tenant-a" else None


@pytest.mark.asyncio
async def test_mapping_membership_resolver_returns_server_roles_only():
    """映射 resolver 只返回服务端角色，并对未知租户返回 None。"""

    resolver = MappingTenantMembershipResolver(
        {
            ("alice", "tenant-a"): {"reader", "editor"},
        }
    )

    assert await resolver.roles_for(actor_id="alice", tenant_id="tenant-a") == {
        "reader",
        "editor",
    }
    assert await resolver.roles_for(actor_id="alice", tenant_id="tenant-b") is None


def test_oidc_settings_require_https():
    """生产 issuer 和 JWKS 不允许降级为 HTTP。"""

    with pytest.raises(ValueError):
        OIDCSettings("http://issuer", "rag", "https://jwks").validate()


@pytest.mark.asyncio
async def test_oidc_dependency_uses_server_membership_roles():
    """JWT 的身份与服务端 membership 共同决定 RequestContext。"""

    dependency = build_oidc_context_dependency(
        _Verifier(), _Membership(),
        settings=OIDCSettings("https://issuer", "rag", "https://jwks"),
    )
    context = await dependency("Bearer valid")
    assert context == RequestContext(
        actor_id="alice", tenant_id="tenant-a", roles=frozenset({"reader"}),
        issued_at=1, expires_at=9999999999, token_version=1,
    )


@pytest.mark.asyncio
async def test_tenant_quota_creates_audit_and_rejects_second_request():
    """超过租户配额时拒绝请求并留下脱敏审计事件。"""

    governance = TenantGovernance(requests_per_minute=1, uploads_per_minute=1)
    context = RequestContext("alice", "tenant-a", frozenset({"reader"}), 1, 99, 1)
    await governance.check(context, "rag.query")
    with pytest.raises(TenantQuotaExceeded):
        await governance.check(context, "rag.query")
    events = governance.audit_sink.snapshot()
    assert events[-1]["allowed"] is False
    assert events[-1]["tenant_id"] == "tenant-a"
