"""显式回填当前本机演示身份有权读取的文档到新Embedding profile，不删除旧索引。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

from sqlalchemy import select

from enterprise_rag.db.models import DocumentVersionModel, IngestionJobModel
from enterprise_rag.domain.entities import JobStatus
from enterprise_rag.ingestion.service import IngestionService
from enterprise_rag.security.context import RequestContext
from scripts.local_p0_api import create_local_p0_app
from scripts.verify_local_runtime import write_json

OUTPUT = Path("artifacts/experiments/2026-10-03_embedding-reranker")


def local_context() -> RequestContext:
    """仅为显式development匿名演示模式构造同一固定身份，不赋予admin权限。"""
    now = int(time.time())
    return RequestContext(
        actor_id="local-demo-user", tenant_id="local-demo", roles=frozenset({"editor", "reader"}),
        issued_at=now, expires_at=now + 3600, token_version=1,
    )


async def prepare_document(app, context, document) -> dict:
    """找到当前发布版本的成功任务与原文元数据，验证hash，不输出私人标题或正文。"""
    async with app.state.database.session() as session:
        version = await session.get(DocumentVersionModel, (
            context.tenant_id, document.document_id, document.version_id,
        ))
        jobs = (await session.scalars(select(IngestionJobModel).where(
            IngestionJobModel.tenant_id == context.tenant_id,
            IngestionJobModel.document_id == document.document_id,
            IngestionJobModel.status == JobStatus.SUCCEEDED,
        ).order_by(IngestionJobModel.created_at.desc()))).all()
    storage = app.state.product_ingestion_service.storage
    for job in jobs:
        meta = json.loads(await storage.get(
            IngestionService._request_meta_key(context.tenant_id, job.id),
        ))
        if meta["version_id"] != document.version_id:
            continue
        assert (meta["tenant_id"], meta["document_id"]) == (
            context.tenant_id, document.document_id,
        )
        manifest = json.loads(await storage.get(meta["parsed_object_key"]))
        assert manifest["version_id"] == document.version_id
        return {
            "document": document, "job": job, "metadata": meta,
            "content_hash": version.content_hash, "children": len(manifest["chunks"]["children"]),
        }
    raise ValueError("当前发布版本缺少可复用的成功任务")


async def reindex(apply: bool) -> None:
    """默认只列脱敏回填计划，--apply时串行复用原文/任务建立并发布新profile。"""
    os.environ["P0_WORKBENCH_ENABLED"] = "false"
    app = create_local_p0_app()
    settings = app.state.settings
    if not settings.local_anonymous_enabled or not settings.dense_profile_namespace:
        raise ValueError("只允许显式启用新profile的本机演示环境")
    context = local_context()
    service = app.state.product_ingestion_service
    report = {
        "status": "planned", "embedding_model": settings.ollama_embedding_model,
        "dimension": settings.ollama_embedding_dimension, "profile": service.dense_profile.key,
        "reranker_model": settings.reranker_model, "applied": apply, "results": [],
    }
    try:
        async with app.router.lifespan_context(app):
            # 先校验新服务，失败时不写入任何generation。探测正文为公开常量。
            vectors = await app.state.embedding_provider.embed(["本地模型切换公开验证样本"])
            assert len(vectors[0]) == settings.ollama_embedding_dimension
            assert await app.state.product_retrieval_service.reranker.health_check()
            async with app.state.database.session() as session:
                documents = await app.state.authorization_service.list_authorized_documents(
                    session, context,
                )
            prepared = [await prepare_document(app, context, document) for document in documents]
            report["authorized_documents"] = len(prepared)
            report["expected_children"] = sum(item["children"] for item in prepared)
            if not apply:
                write_json(OUTPUT / "reindex-plan.json", report)
                print(json.dumps(report, ensure_ascii=False))
                return
            # 当前身份已经有READ ACL，重放相同任务/source/version，不生成重复文档。
            for item in prepared:
                started = time.monotonic()
                document, job, meta = item["document"], item["job"], item["metadata"]
                record = {
                    "document_id": document.document_id, "version_id": document.version_id,
                    "children": item["children"],
                }
                try:
                    async with app.state.database.session() as session:
                        await app.state.authorization_service.require_document_read(
                            session, context, document.document_id,
                        )
                    content = await service.storage.get(meta["raw_object_key"])
                    assert hashlib.sha256(content).hexdigest() == item["content_hash"]
                    result = await service.upload(
                        context, source_id=meta["source_id"], title=meta["title"],
                        filename=meta["filename"], content=content, content_type=meta["media_type"],
                        idempotency_key=job.idempotency_key, source_revision=meta.get("source_revision"),
                    )
                    assert result.document_id == document.document_id
                    assert result.version_id == document.version_id and result.published
                    record.update(status="passed", generation_id=result.generation_id)
                except Exception as exc:  # noqa: BLE001 -- 每文档失败隔离，禁止泄露原文或异常正文。
                    record.update(status="failed", error_type=type(exc).__name__)
                record["elapsed_seconds"] = round(time.monotonic() - started, 3)
                report["results"].append(record)
                write_json(OUTPUT / "reindex.json", report)
                print(json.dumps(record, ensure_ascii=False), flush=True)
            async with app.state.database.session() as session:
                scopes = await app.state.product_retrieval_service.scope_service.list_scopes(
                    session, context, index_profile=service.dense_profile,
                )
            report["new_profile_scopes"] = len(scopes)
            report["status"] = "passed" if (
                len(scopes) == len(prepared)
                and all(item["status"] == "passed" for item in report["results"])
            ) else "failed"
            write_json(OUTPUT / "reindex.json", report)
            print(json.dumps({
                "status": report["status"], "scopes": len(scopes), "documents": len(prepared),
            }, ensure_ascii=False))
            if report["status"] != "passed":
                raise ValueError("部分文档回填失败")
    finally:
        await app.state.database.dispose()


def main() -> None:
    """提供计划/apply入口，顶层只输出错误类型，不转储私有文件或凭据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(reindex(args.apply))
    except Exception as exc:  # noqa: BLE001 -- 命令行安全错误边界。
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
