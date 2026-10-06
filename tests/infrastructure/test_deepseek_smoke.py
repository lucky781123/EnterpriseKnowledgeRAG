"""公开在线 smoke 的离线契约；不能把这些替身结果当作真实网关联调。"""

import importlib
import json
from unittest.mock import MagicMock

import httpx
import pytest

from enterprise_rag.providers.lmstudio import LMStudioChatProvider
from enterprise_rag.providers.routed import DeepSeekSettings
from scripts.deepseek_smoke import TEST_TENANT, public_request, verify_routes


@pytest.mark.parametrize("online_status", [200, 503])
async def test_public_smoke_separates_online_success_from_fallback(online_status):
    """在线失败即使本地回答有效也整体失败，业务 allowlist 和 key 不进入报告。"""
    seen = []
    content = json.dumps({
        "answer_status": "answered", "message": "审批期限为三个工作日。",
        "excerpts": [{"evidence_id": "public-smoke-1", "quote": "审批期限为三个工作日。"}],
    }, ensure_ascii=False)

    async def respond(request):
        """核验真实传输边界的固定模型、密钥 header 与唯一公开输入。"""
        body = json.loads(request.content)
        seen.append((request.url.host, body["stream"]))
        assert body["messages"] == [
            {"role": item.role, "content": item.content} for item in public_request()[0].messages
        ]
        if request.url.host == "online.test":
            assert body["model"] == "deepseek-v4.1-flash"
            assert request.headers["authorization"] == "Bearer offline-private-key"
            if online_status != 200:
                return httpx.Response(online_status)
        else:
            assert body["model"] == "local-test"
        if body["stream"]:
            data = {"choices": [{"delta": {"content": content}, "finish_reason": "stop"}]}
            return httpx.Response(
                200, text=f"data: {json.dumps(data)}\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json={
            "model": body["model"],
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        })

    async def unavailable(request):
        """仅提供故障注入，不计入在线网络调用。"""
        return httpx.Response(503)

    settings = DeepSeekSettings(
        enabled=True, api_key="offline-private-key", allowed_tenants=("existing-private-tenant",),
    )
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client,
        httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as failure_client,
    ):
        report = await verify_routes(
            settings,
            LMStudioChatProvider(client, "http://local.test", "local-test"),
            LMStudioChatProvider(client, "https://online.test/v1", settings.model, settings.api_key),
            LMStudioChatProvider(failure_client, "https://online.test/v1", settings.model),
            "local-test",
        )
    assert report["passed"] is (online_status == 200)
    assert settings.allowed_tenants == ("existing-private-tenant",)
    assert settings.allowed_tenants != (TEST_TENANT,)
    assert "offline-private-key" not in json.dumps(report)
    assert "审批期限" not in json.dumps(report, ensure_ascii=False)
    assert len([item for item in seen if item[0] == "online.test"]) == 2
    for name in ("disallowed_tenant", "no_tenant_context"):
        assert report["checks"][name]["passed"]
        assert report["checks"][name]["online_attempts"] == 0
    for name in ("unavailable_complete_fallback", "unavailable_stream_fallback"):
        assert report["checks"][name]["passed"]
        assert report["checks"][name]["fallback_count"] == 1
    if online_status != 200:
        assert not report["checks"]["online_complete"]["passed"]
        assert not report["checks"]["online_stream"]["passed"]


async def test_public_worker_identity_and_database_guard(monkeypatch):
    """公开专用Worker拒绝业务身份和非隔离数据库，不启动实际连接。"""
    from enterprise_rag.infrastructure import celery_app

    def offline_app():
        """只替换CLI导出的注册对象，不模拟真实Worker/消息消费成功。"""
        return MagicMock()

    monkeypatch.setattr(celery_app, "create_celery_app", offline_app)
    for name, value in {
        "RAG_INFRA_ENABLED": "true", "RAG_CELERY_ENABLED": "true",
        "REDIS_CACHE_URL": "redis://127.0.0.1:61001/0",
        "REDIS_COORDINATION_URL": "redis://127.0.0.1:61002/0",
        "CELERY_RESULT_BACKEND": "redis://127.0.0.1:61002/1",
    }.items():
        monkeypatch.setenv(name, value)
    worker = importlib.import_module("scripts.infrastructure_smoke_worker")
    from enterprise_rag.security.authorization import ResourceNotFound

    identity = await worker.public_identity(TEST_TENANT, "public-smoke-user")
    assert identity.tenant_id == TEST_TENANT
    assert identity.roles == frozenset({"reader", "editor"})
    with pytest.raises(ResourceNotFound):
        await worker.public_identity("local-demo", "public-smoke-user")
    with pytest.raises(ResourceNotFound):
        await worker.public_identity(TEST_TENANT, "local-demo-user")
    for isolated, dsn in (
        ("false", "postgresql+asyncpg://127.0.0.1/infra_test"),
        ("true", "postgresql+asyncpg://127.0.0.1/business_db"),
        ("true", "postgresql+asyncpg://remote.test/infra_test"),
    ):
        monkeypatch.setenv("INFRA_SMOKE_ISOLATED", isolated)
        monkeypatch.setenv("POSTGRES_DSN", dsn)
        with pytest.raises(RuntimeError):
            async with worker.public_runtime():
                pytest.fail("非隔离配置不应进入Worker")
    worker.app.close()
