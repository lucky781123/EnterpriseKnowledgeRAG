"""真实运行验收：核对 smoke 文档的持久化事实并执行隔离的依赖故障注入。

正常路径使用已启动服务的网络 HTTP；故障路径使用真实 factory 的 ASGI HTTP，
仅将指定依赖替换为抛异常的函数，其他依赖仍连接真实服务。只允许公开合成 smoke
文档，拒绝把私人知识库正文、token、DSN 或内部 collection 写入验收产物。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
from sqlalchemy import select

from enterprise_rag.db.models import (
    DenseGenerationModel,
    DenseGenerationPointerModel,
    DocumentModel,
    DocumentVersionModel,
    VersionArtifactModel,
)
from enterprise_rag.ingestion.service import IngestionService
from enterprise_rag.providers.errors import ProviderError, ProviderErrorKind
from enterprise_rag.retrieval.weaviate import WeaviateTransportError
from scripts.local_p0_api import create_local_p0_app
from scripts.local_p0_smoke import _SMOKE_DOCUMENT, summarize_response


def write_json(path: Path, payload: dict) -> None:
    """仅保存经过调用方明确选取的公开验收信息，不转储环境或异常正文。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def inspect_persistence(
    app, identifiers: dict, *, expected_content: bytes | None = None,
    source_prefix: str = "smoke:local-p0:",
) -> tuple[dict, str]:
    """通过实际 PostgreSQL、对象存储和 Weaviate 交叉核对同一 generation。"""

    ingestion = app.state.product_ingestion_service
    database = app.state.database
    async with database.session() as session:
        generation = await session.get(DenseGenerationModel, identifiers["generation_id"])
        assert generation is not None, "generation 不存在"
        tenant = generation.tenant_id
        document = await session.get(DocumentModel, (tenant, generation.document_id))
        version = await session.get(
            DocumentVersionModel, (tenant, generation.document_id, generation.version_id),
        )
        pointer = await session.get(
            DenseGenerationPointerModel,
            (tenant, generation.document_id, generation.version_id, generation.index_profile_key),
        )
        artifacts = (await session.scalars(select(VersionArtifactModel).where(
            VersionArtifactModel.tenant_id == tenant,
            VersionArtifactModel.document_id == generation.document_id,
            VersionArtifactModel.version_id == generation.version_id,
        ))).all()
    assert document and document.source_id.startswith(source_prefix)
    assert document.id == identifiers["document_id"]
    assert version and version.id == identifiers["version_id"]
    assert document.current_version_id == version.id and version.status.value == "published"
    assert pointer and pointer.active_generation_id == generation.generation_id
    assert generation.status.value == "active"
    assert generation.index_profile_key == ingestion.dense_profile.key
    assert all(artifact.status.value == "ready" for artifact in artifacts)

    # 从可信业务 identity 派生对象 key，正文仅用于确认是脚本内的公开合成样本。
    storage = ingestion.storage
    metadata = json.loads(await storage.get(
        IngestionService._request_meta_key(tenant, identifiers["job_id"]),
    ))
    raw = await storage.get(metadata["raw_object_key"])
    assert raw == (
        _SMOKE_DOCUMENT.encode("utf-8") if expected_content is None else expected_content
    ), "仅允许公开合成验收文档"
    manifest_bytes = await storage.get(metadata["parsed_object_key"])
    manifest = json.loads(manifest_bytes)
    assert hashlib.sha256(raw).hexdigest() == version.content_hash
    assert hashlib.sha256(manifest_bytes).hexdigest() == generation.manifest_hash
    assert (manifest["tenant_id"], manifest["document_id"], manifest["version_id"]) == (
        tenant, document.id, version.id,
    )
    parents = manifest["chunks"]["parents"]
    children = manifest["chunks"]["children"]
    parent_ids = {parent["chunk_id"] for parent in parents}
    child_ids = {child["chunk_id"] for child in children}
    assert children and all(
        child["parent_id"] in parent_ids and child["version_id"] == version.id
        for child in children
    )

    # scoped 对象列表与 manifest 必须逐项同源，不以“HTTP 成功”代替内容校验。
    dense_objects = await ingestion.dense_transport._list_objects(
        generation.collection_name, generation_id=generation.generation_id,
        version_id=version.id,
    )
    sparse = await ingestion.sparse_transport.inspect_generation(
        generation.sparse_collection_name, tenant_id=tenant,
        generation_id=generation.generation_id, version_id=version.id,
        sparse_profile_key=generation.sparse_profile_key,
    )
    assert {item["properties"]["chunk_id"] for item in dense_objects} == child_ids
    assert sparse.chunk_ids == child_ids and sparse.item_count == len(children)
    dimensions = set()
    for item in dense_objects:
        properties = item["properties"]
        assert (properties["tenant_id"], properties["document_id"],
                properties["version_id"], properties["generation_id"]) == (
            tenant, document.id, version.id, generation.generation_id,
        )
        assert properties["manifest_hash"] == generation.manifest_hash
        # 新Embedding profile使用独立物理库；旧部署的映射仍返回原collection名。
        physical = ingestion.dense_transport.physical_collection_name(generation.collection_name)
        response = await ingestion.dense_transport._request(
            "GET", f"/v1/objects/{physical}/{item['id']}",
            params={"include": "vector"},
        )
        dimensions.add(len(response.json()["vector"]))
    assert dimensions == {generation.dimension}
    assert len(children) == len(dense_objects) == generation.expected_item_count
    assert generation.actual_item_count == generation.checkpoint == len(children)
    return {
        "status": "passed", "document_id": document.id, "version_id": version.id,
        "generation_id": generation.generation_id,
        "document_status": document.status.value, "version_status": version.status.value,
        "generation_status": generation.status.value, "active_pointer_matches": True,
        "raw_hash_matches": True, "manifest_hash_matches": True, "lineage_matches": True,
        "blocks": len(manifest["blocks"]), "parents": len(parents), "children": len(children),
        "dense_objects": len(dense_objects), "sparse_objects": sparse.item_count,
        "chunk_ids_match": True, "vector_dimensions": sorted(dimensions),
        "embedding_model": generation.model_name,
        "artifacts": {item.kind.value: {
            "status": item.status.value, "item_count": item.item_count,
        } for item in artifacts},
    }, tenant


async def dependency_failure(*_args, **_kwargs):
    """注入可恢复的网络异常，绝不返回虚构候选或模型答案。"""

    raise httpx.ConnectError("验收故障注入")


async def authentication_failure(*_args, **_kwargs):
    """注入不可降级的认证异常，验证不会借另一通道掩盖认证错误。"""

    raise WeaviateTransportError("authentication", retryable=False, safe_message="验收故障注入")


async def model_failure(*_args, **_kwargs):
    """注入本地模型不可用，验证有证据也不会伪造回答成功。"""

    raise ProviderError(ProviderErrorKind.UNAVAILABLE, "验收故障注入", retryable=True)


async def verify(report_path: Path, api: str) -> None:
    """先核对真实产物，再分别执行网络 HTTP 和真实依赖 ASGI 故障矩阵。"""

    smoke = json.loads(report_path.read_text(encoding="utf-8"))
    assert smoke["run_status"] == "passed", "请先通过真实 HTTP smoke"
    # factory 不挂载第二个 Gradio，只隔离本次注入，运行中的 API 保持不变。
    os.environ["P0_WORKBENCH_ENABLED"] = "false"
    app = create_local_p0_app()
    output = report_path.parent
    try:
        async with app.router.lifespan_context(app):
            persistence, tenant = await inspect_persistence(app, smoke["stages"]["upload"])
            write_json(output / "persistence.json", persistence)
            codec = app.state.dev_token_codec
            token = codec.issue(actor_id="smoke-reader", tenant_id=tenant, roles={"reader"})
            # 交互验收使用已授权的上传者；只写本地私有目录，不归档 token。
            token_path = Path(".local/runtime/acceptance-token.txt")
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.touch(mode=0o600, exist_ok=True)
            token_path.chmod(0o600)
            token_path.write_text(codec.issue(
                actor_id="smoke-uploader", tenant_id=tenant, roles={"editor", "reader"},
            ), encoding="utf-8")
            headers = {"Authorization": f"Bearer {token}"}
            request = {
                "question": "Aurora 项目的发布冻结窗口是什么时间？",
                "top_k": 3, "candidate_k": 10, "max_tokens": 1024, "timeout_ms": 120_000,
            }
            async with httpx.AsyncClient(base_url=api, timeout=150) as client:
                response = await client.post("/api/v1/rag/query", headers=headers, json=request)
            payload = response.json()
            assert summarize_response(response.status_code, payload)["status"] == "passed"
            # 只在原文精确匹配公开样本后保存实际模型输出；不保存内部 reasoning。
            write_json(output / "answer.json", {
                "transport": "network_http", "status_code": response.status_code,
                **{key: payload[key] for key in (
                    "answer_status", "message", "citation_validation", "citations",
                    "retrieval_trace", "provider_result", "timings",
                )},
            })

            retrieval = app.state.product_retrieval_service
            provider = app.state.provider_registry.resolve("local_answer").provider
            complete = provider.complete
            calls = 0

            async def counted_complete(*args, **kwargs):
                """计数并转发真实 LM Studio 请求，不替换生成内容。"""

                nonlocal calls
                calls += 1
                return await complete(*args, **kwargs)

            provider.complete = counted_complete
            dependencies = {
                "embedding": (retrieval.embedding_provider, "embed"),
                "dense": (retrieval.dense_transport, "search"),
                "sparse": (retrieval.sparse_transport, "search"),
                "reranker": (retrieval.reranker, "rerank"),
                "model": (provider, "complete"),
            }
            cases = {
                "embedding": (["embedding"], 200, dependency_failure),
                "dense": (["dense"], 200, dependency_failure),
                "sparse": (["sparse"], 200, dependency_failure),
                "reranker": (["reranker"], 200, dependency_failure),
                "both_channels": (["dense", "sparse"], 503, dependency_failure),
                "authentication": (["dense"], 503, authentication_failure),
                "model": (["model"], 503, model_failure),
                "no_acl": ([], 404, dependency_failure),
            }
            results = {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://acceptance.local",
                timeout=150,
            ) as client:
                for name, (components, expected_code, failure) in cases.items():
                    originals = []
                    before = calls
                    started = time.monotonic()
                    try:
                        for component in components:
                            obj, method = dependencies[component]
                            originals.append((obj, method, getattr(obj, method)))
                            setattr(obj, method, failure)
                        case_headers = headers
                        if name == "no_acl":
                            denied = codec.issue(
                                actor_id="never-granted", tenant_id=tenant, roles={"reader"},
                            )
                            case_headers = {"Authorization": f"Bearer {denied}"}
                        response = await client.post(
                            "/api/v1/rag/query", headers=case_headers, json=request,
                        )
                        summary = summarize_response(response.status_code, response.json())
                        assert response.status_code == expected_code, name
                        if expected_code == 200:
                            assert summary["status"] == "passed", name
                            trace = summary["retrieval_trace"]
                            assert trace["mode"] == "degraded", name
                            assert f"{name}_unavailable" in trace["fallback_reasons"], name
                            assert calls - before == 1, name
                        else:
                            assert calls == before, name
                        results[name] = {
                            **summary, "expected_status_code": expected_code, "assertions": "passed",
                            "real_llm_calls": calls - before,
                            "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                        }
                        # 每个 case 完成即留证据，失败时也能看见之前真实通过的部分。
                        write_json(output / "fallback.json", {
                            "status": "in_progress", "transport": "asgi_http_fault_injection",
                            "remaining_dependencies": "real", "cases": results,
                        })
                        print(json.dumps({"case": name, "assertions": "passed"}, ensure_ascii=False))
                    finally:
                        for obj, method, original in originals:
                            setattr(obj, method, original)
            write_json(output / "fallback.json", {
                "status": "passed", "transport": "asgi_http_fault_injection",
                "remaining_dependencies": "real", "cases": results,
            })
    finally:
        # factory 显式注入 Database，连接池由本 runner 负责释放。
        await app.state.database.dispose()


def main() -> int:
    """读取显式参数，异常仅输出类型，避免故障日志回显配置或文档。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--api-base-url", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(verify(args.report, args.api_base_url))
    except Exception as exc:  # noqa: BLE001 -- 验收顶层不输出可能包含凭据的异常正文。
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1
    print('{"status": "passed"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
