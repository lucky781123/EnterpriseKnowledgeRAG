from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from enterprise_rag.api.dependencies import build_request_context_dependency
from enterprise_rag.security.context import RequestContext
from enterprise_rag.security.dev_token import DevTokenCodec


SECRET = "0123456789abcdef0123456789abcdef"


def _auth_test_app(codec: DevTokenCodec) -> FastAPI:
    """创建只暴露受保护测试路由的应用，验证依赖本身而不扩展产品 API。"""

    app = FastAPI()
    dependency = build_request_context_dependency(codec)

    @app.get("/protected")
    async def protected(
        context: RequestContext = Depends(dependency),
    ) -> dict[str, object]:
        """仅回显非敏感身份字段，证明请求体或 header tenant 不能覆盖 token。"""

        return {
            "actor_id": context.actor_id,
            "tenant_id": context.tenant_id,
            "roles": sorted(context.roles),
        }

    return app


def test_bearer_dependency_builds_context_only_from_verified_token(monkeypatch) -> None:
    """可信上下文必须来自 Bearer token，伪造 tenant header 不得覆盖它。"""

    monkeypatch.setattr("enterprise_rag.security.dev_token.time.time", lambda: 1_789_600_100)
    codec = DevTokenCodec(SECRET, ttl_seconds=3600)
    token = codec.issue(
        actor_id="user-alice",
        tenant_id="tenant-a",
        roles=["reader"],
        now=1_789_600_000,
    )
    client = TestClient(_auth_test_app(codec))

    response = client.get(
        "/protected",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-ID": "tenant-b",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "actor_id": "user-alice",
        "tenant_id": "tenant-a",
        "roles": ["reader"],
    }


def test_bearer_dependency_rejects_missing_or_invalid_token(monkeypatch) -> None:
    """缺失、错误 scheme 或篡改 token 都应返回固定 401 且不回显输入。"""

    monkeypatch.setattr("enterprise_rag.security.dev_token.time.time", lambda: 1_789_600_100)
    codec = DevTokenCodec(SECRET, ttl_seconds=3600)
    token = codec.issue(
        actor_id="user-alice",
        tenant_id="tenant-a",
        roles=["reader"],
        now=1_789_600_000,
    )
    client = TestClient(_auth_test_app(codec))

    for authorization in (None, f"Basic {token}", f"Bearer {token[:-1]}x"):
        headers = {} if authorization is None else {"Authorization": authorization}
        response = client.get("/protected", headers=headers)
        assert response.status_code == 401
        body = response.json()
        assert body["detail"]["kind"] == "authentication_required"
        assert token not in response.text
        assert SECRET not in response.text
