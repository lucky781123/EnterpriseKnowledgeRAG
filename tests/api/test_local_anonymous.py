"""显式本机免Token入口的固定身份与默认鉴权边界。"""

from dataclasses import replace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from enterprise_rag.api.app import create_app
from enterprise_rag.api.dependencies import build_local_anonymous_dependency
from enterprise_rag.config.settings import Settings


def _client(host="127.0.0.1") -> TestClient:
    """用固定socket peer构造测试应用，不能让请求头决定租户。"""

    app = FastAPI()
    dependency = build_local_anonymous_dependency()

    @app.get("/context")
    async def context(identity=Depends(dependency)):  # noqa: B008 -- FastAPI依赖注入。
        """只回显非敏感固定身份以断言请求不能控制scope。"""
        return {"actor": identity.actor_id, "tenant": identity.tenant_id, "roles": sorted(identity.roles)}

    return TestClient(app, client=(host, 1234))


def test_loopback_without_token_has_fixed_editor_identity() -> None:
    """无需Token也能上传，但伪造tenant/header不能扩大到其他租户。"""

    response = _client().get("/context", headers={"X-Tenant-ID": "other", "Authorization": "invalid"})
    assert response.status_code == 200
    assert response.json() == {
        "actor": "local-demo-user", "tenant": "local-demo", "roles": ["editor", "reader"],
    }


@pytest.mark.parametrize("host", ["192.168.1.4", "example.com"])
def test_anonymous_mode_rejects_non_loopback_peers(host) -> None:
    """免Token能力不能被LAN或外部客户端利用，不信任X-Forwarded-For。"""

    assert _client(host).get("/context", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403


def test_anonymous_mode_rejects_cross_site_origin() -> None:
    """恶意网页不能借浏览器请求本地匿名上传/检索服务。"""

    assert _client().get("/context", headers={"Origin": "https://evil.example"}).status_code == 403


def test_anonymous_configuration_is_development_only() -> None:
    """默认关闭免Token模式，production不能打开。"""

    settings = Settings.from_env()
    assert settings.local_anonymous_enabled is False
    with pytest.raises(ValueError, match="development"):
        replace(settings, app_env="production", local_anonymous_enabled=True).validate()


def test_anonymous_boundary_protects_gradio_queue_and_ignores_forwarded_headers() -> None:
    """UI代理和API使用同一本机边界，XFF不能让远程请求伪装成loopback。"""
    app = create_app(settings=replace(Settings.from_env(), local_anonymous_enabled=True))
    with TestClient(app, client=("127.0.0.1", 1234)) as local:
        assert local.get("/health/live").status_code == 200
        response = local.post(
            "/product-workbench/gradio_api/queue/join",
            headers={"Origin": "https://evil.example"}, json={},
        )
        assert response.status_code == 403
    with TestClient(app, client=("192.168.1.4", 1234)) as remote:
        assert remote.get(
            "/health/live", headers={"X-Forwarded-For": "127.0.0.1"},
        ).status_code == 403
