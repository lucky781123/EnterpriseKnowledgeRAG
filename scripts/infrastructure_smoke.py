"""隔离真实服务异步 smoke，公开合成样本，不读取 .env 或私人文档。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.engine import make_url

from enterprise_rag.db.migrations import ASYNC_INGESTION_VERSION, upgrade_database
from enterprise_rag.db.models import DenseGenerationModel, DocumentModel, TenantModel
from enterprise_rag.infrastructure.consumer import celery_publisher
from enterprise_rag.infrastructure.write_gate import MaintenanceBlocked
from enterprise_rag.ingestion.service import IngestionService
from enterprise_rag.ingestion.task_ledger import OutboxDispatcher
from enterprise_rag.providers.routed import PolicyRoutedProvider
from scripts.deepseek_smoke import TEST_TENANT
from scripts.ingestion_runtime import app as celery_factory
from scripts.local_p0_api import create_local_p0_app

ROOT = Path(__file__).resolve().parents[1]


async def run(*, online_public=False):
    """只允许专用 loopback infra_test 数据库，实际启动 Worker 并检查持久事实。"""
    url = make_url(os.environ["POSTGRES_DSN"])
    if (
        os.getenv("INFRA_SMOKE_ISOLATED") != "true"
        or url.host not in {"127.0.0.1", "localhost"}
        or not url.database.startswith("infra_test")
    ):
        raise RuntimeError("真实 smoke 必须使用显式专用隔离数据库")
    tenant = TEST_TENANT if online_public else "local-demo"
    actor = "public-smoke-user" if online_public else "local-demo-user"
    if online_public:
        from scripts.infrastructure_smoke_worker import public_identity

        application = create_local_p0_app(identity_resolver=public_identity)
        provider = application.state.provider_registry.resolve("local_answer").provider
        if not isinstance(provider, PolicyRoutedProvider) or not provider.settings.enabled:
            raise RuntimeError("真实在线演练需要启用 DeepSeek")
        # 只替换独立 ASGI 测试实例的配置，不影响 TOML 和运行中的业务进程。
        provider.settings = replace(provider.settings, allowed_tenants=(TEST_TENANT,))
    else:
        application = create_local_p0_app()
    database = application.state.database
    await upgrade_database(database.engine, target_version=ASYNC_INGESTION_VERSION)
    async with database.transaction() as session:
        if online_public and (await session.scalars(select(DocumentModel))).first() is not None:
            raise RuntimeError("在线演练只允许无已有文档的隔离数据库")
        for tenant_id in (tenant, "other-smoke"):
            if await session.get(TenantModel, tenant_id) is None:
                session.add(TenantModel(id=tenant_id, name=tenant_id))
    report = {
        "scope": "isolated-real-services-public-document", "online_deepseek": online_public,
        "business_allowlist_modified": False,
        "storage": application.state.infrastructure.storage_provider,
        "production_acceptance": False,
    }
    celery = celery_factory()
    runtime = ROOT / ".local" / (
        "infrastructure-online-smoke" if online_public else "infrastructure-smoke"
    )
    runtime.mkdir(parents=True, exist_ok=True)
    output = runtime / "result.json"
    process = None
    try:
        async with application.router.lifespan_context(application):
            worker = application.state.stage_worker
            coordinator = worker.coordinator
            nonce = str(time.time_ns())
            cache = application.state.redis_runtime.cache
            coordination = application.state.redis_runtime.coordination
            assert await cache.ping() and await coordination.ping()
            config = await coordination.config_get("maxmemory-policy")
            assert config["maxmemory-policy"] == "noeviction"
            info = await coordination.info("persistence")
            assert info["aof_enabled"] == 1
            lock = await coordinator.acquire_lock(tenant, "public-smoke", ttl_ms=5000)
            assert lock is not None
            assert await coordinator.acquire_lock(tenant, "public-smoke", ttl_ms=5000) is None
            assert await coordinator.release_lock(lock)
            assert await coordinator.allow(tenant, "smoke-limit-" + nonce, capacity=1, refill_per_second=0.01)
            assert not await coordinator.allow(tenant, "smoke-limit-" + nonce, capacity=1, refill_per_second=0.01)
            report["redis_lua_lock_limit_aof"] = True
            gate = worker.write_gate
            async with gate.writer():
                try:
                    async with gate.maintenance():
                        raise AssertionError("writer 活跃不能维护")
                except MaintenanceBlocked:
                    pass
            async with gate.maintenance():
                try:
                    async with gate.writer():
                        raise AssertionError("维护不能写入")
                except MaintenanceBlocked:
                    pass
            report["postgres_maintenance_gate"] = True
            log_path = runtime / "worker.log"
            with log_path.open("wb") as log:
                process = await asyncio.to_thread(
                    subprocess.Popen,
                    [sys.executable, "-m", "celery", "-A",
                     "scripts.infrastructure_smoke_worker:app" if online_public
                     else "scripts.celery_worker:app",
                     "worker", "--pool=" + ("solo" if sys.platform == "darwin" else "prefork"),
                     "--concurrency=1", "--loglevel=WARNING"],
                    cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT,
                )
                token = application.state.dev_token_codec.issue(
                    tenant_id=tenant, actor_id=actor, roles={"editor", "reader"},
                )
                headers = {"Authorization": f"Bearer {token}"}
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=application), base_url="http://localhost",
                ) as client:
                    raw = b"# ASYNC_PUBLIC_CHECK\n\nASYNC_PUBLIC_CHECK approval takes exactly three business days."
                    key = "public-smoke-" + str(time.time_ns())
                    response = await client.post(
                        "/api/v1/ingestion/tasks", headers=headers,
                        data={"source_id": key, "title": "Public async smoke", "idempotency_key": key},
                        files={"file": ("public.md", raw, "text/markdown")},
                    )
                    assert response.status_code == 202, response.status_code
                    task_id = response.json()["task_id"]
                    report["async_accept_202"] = True
                    report["worker_pool"] = "solo" if sys.platform == "darwin" else "prefork"
                    dispatcher = OutboxDispatcher(worker.ledger, celery_publisher(celery))
                    deadline = time.monotonic() + 300
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise RuntimeError("Celery Worker 提前退出")
                        async with gate.writer():
                            await worker.ledger.recover_expired(limit=10)
                            await worker.ledger.recover_deliveries(limit=10)
                            await dispatcher.dispatch_once(limit=10)
                        status = await client.get(
                            f"/api/v1/ingestion/tasks/{task_id}", headers=headers,
                        )
                        state = status.json()["status"]
                        if state == "dead_letter":
                            raise RuntimeError("异步 smoke 进入死信")
                        if state == "succeeded":
                            break
                        await asyncio.sleep(1)
                    else:
                        raise TimeoutError("异步 smoke 超时")
                    report["celery_six_stages"] = True
                    async with database.session() as session:
                        from enterprise_rag.db.async_ingestion import AsyncTaskModel

                        task = await session.get(AsyncTaskModel, (tenant, task_id))
                        meta = task.request_metadata
                        document = await session.get(DocumentModel, (tenant, meta["document_id"]))
                        assert document.current_version_id == meta["version_id"]
                        generation = (await session.scalars(select(DenseGenerationModel).where(
                            DenseGenerationModel.document_id == document.id,
                            DenseGenerationModel.tenant_id == tenant,
                            DenseGenerationModel.status == "active",
                        ))).one()
                    manifest = await worker.storage.get(IngestionService._parsed_key(
                        tenant, meta["document_id"], meta["version_id"],
                    ))
                    assert hashlib.sha256(manifest).hexdigest() == generation.manifest_hash
                    assert await worker.storage.get(task.raw_object_key) == raw
                    report["objects_manifest_pointer"] = True
                    foreign = application.state.dev_token_codec.issue(
                        tenant_id="other-smoke", actor_id=actor, roles={"editor"},
                    )
                    hidden = await client.get(
                        f"/api/v1/ingestion/tasks/{task_id}",
                        headers={"Authorization": f"Bearer {foreign}"},
                    )
                    assert hidden.status_code == 404
                    report["cross_tenant_hidden"] = True
                    query = await client.post(
                        "/api/v1/rag/query", headers=headers,
                        json={"question": "How long does ASYNC_PUBLIC_CHECK approval take?"},
                    )
                    payload = query.json()
                    # HTTP 200 不等于 answered；只保存脱敏的状态和引用结论。
                    report["query_http"] = query.status_code
                    report["query_answer_status"] = payload.get("answer_status")
                    report["query_citation_status"] = payload.get("citation_validation")
                    assert query.status_code == 200
                    assert payload.get("answer_status") == "answered"
                    assert payload.get("citation_validation") == "passed"
                    if online_public:
                        # 在线成功不能由本地兜底代替；检测本次实例所有生成调用。
                        assert provider.online_count == 1 and provider.fallback_count == 0
                        report["public_retrieval_rerank_deepseek"] = True
                        report["online_count"] = provider.online_count
                        report["fallback_count"] = provider.fallback_count
                        admin = application.state.dev_token_codec.issue(
                            tenant_id=tenant, actor_id="public-smoke-admin", roles={"admin"},
                        )
                        revoked = await client.delete(
                            f"/api/v1/documents/{meta['document_id']}/acl/user/{actor}",
                            headers={"Authorization": f"Bearer {admin}"},
                        )
                        assert revoked.status_code == 200
                        hidden_document = await client.get(
                            f"/api/v1/documents/{meta['document_id']}/status", headers=headers,
                        )
                        assert hidden_document.status_code == 404
                        denied = await client.post(
                            "/api/v1/rag/query", headers=headers,
                            json={"question": "How long does ASYNC_PUBLIC_CHECK approval take?"},
                        )
                        assert denied.status_code == 404
                        assert provider.online_count == 1
                        report["revoked_acl_hidden_no_online_call"] = True
                    else:
                        report["public_retrieval_rerank_local_llm"] = True
                    report["passed"] = True
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait)
        celery.close()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online-public", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(online_public=args.online_public)), ensure_ascii=False))
    except Exception:  # noqa: BLE001 - 不打印模型文本、凭据或 endpoint。
        print('{"passed": false, "kind": "infrastructure_smoke_failed"}')
        raise SystemExit(1) from None
