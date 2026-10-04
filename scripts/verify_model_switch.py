"""使用公开样本验证三模型配置、网络上传、双路索引和HTTP重排，归档脱敏证据。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx

from enterprise_rag.reranking.http import HTTPReranker
from enterprise_rag.reranking.protocols import RerankRequest
from scripts.local_p0_api import create_local_p0_app
from scripts.verify_local_runtime import inspect_persistence, write_json

OUTPUT = Path("artifacts/experiments/2026-10-03_embedding-reranker")
CONTENT = "# MODEL_SWITCH_CHECK\n\nMODEL_SWITCH_CHECK 审批期限为三个工作日。\n".encode()


def answer_summary(response, settings) -> dict:
    """仅提取状态/计数，不保存用户文档答案、引用正文或内部reasoning。"""
    data = response.json()
    provider = data.get("provider_result") or {}
    return {
        "http_status": response.status_code, "answer_status": data.get("answer_status"),
        "citation_validation": data.get("citation_validation"),
        "citations_count": len(data.get("citations", [])), "model": provider.get("model"),
        "finish_reason": provider.get("finish_reason"), "usage": provider.get("usage"),
        "retrieval_trace": data.get("retrieval_trace"), "timings": data.get("timings"),
        "configured_llm_matches": provider.get("model") == settings.local_llm_model,
        "passed": response.status_code == 200 and data.get("answer_status") == "answered"
        and data.get("citation_validation") == "passed",
    }


async def verify() -> None:
    """调用运行中API上传公开TXT；独立factory只核验持久化，不替换依赖。"""
    state = json.loads(Path(".local/runtime/service.json").read_text())
    os.environ["P0_WORKBENCH_ENABLED"] = "false"
    app = create_local_p0_app()
    settings = app.state.settings
    report = {
        "llm_model": settings.local_llm_model,
        "embedding_model": settings.ollama_embedding_model,
        "dimension": settings.ollama_embedding_dimension,
        "reranker_model": settings.reranker_model,
        "reranker_provider": settings.reranker_provider,
    }
    try:
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            base_url=state["api"], timeout=310
        ) as client:
                ready = await client.get("/health/product/ready")
                assert ready.status_code == 200
                report["readiness"] = ready.json()
                report["frontend_http_status"] = (await client.get("/product-workbench/")).status_code
                # 公开例子通过正式HTTP适配器验证top_n与返回index映射。
                reranker = HTTPReranker(
                    client, settings.reranker_base_url, settings.reranker_model,
                )
                rows = await reranker.rerank(RerankRequest(
                    "绿茶对健康有什么好处?",
                    ["绿茶含有一种叫做儿茶素的抗氧化剂，可以帮助减轻炎症。",
                     "篮球是美国最受欢迎的运动之一。", "研究表明，经常喝绿茶可以改善大脑功能。"],
                    2,
                ))
                report["rerank_probe"] = {
                    "indexes": [item.index for item in rows],
                    "scores": [item.score for item in rows],
                    "passed": {item.index for item in rows} == {0, 2},
                }
                response = await client.post(
                    "/api/v1/documents",
                    data={"source_id": "model-switch:public-check", "title": "MODEL_SWITCH_CHECK",
                          "idempotency_key": "model-switch-public-20261003"},
                    files={"file": ("model-switch.txt", CONTENT, "text/plain")},
                )
                assert response.status_code == 201
                identifiers = response.json()
                write_json(OUTPUT / "upload.json", identifiers)
                persistence, tenant = await inspect_persistence(
                    app, identifiers, expected_content=CONTENT, source_prefix="model-switch:",
                )
                assert tenant == "local-demo" and persistence["vector_dimensions"] == [2560]
                report["persistence"] = persistence
                response = await client.post("/api/v1/rag/query", json={
                    "question": "MODEL_SWITCH_CHECK 审批期限是多少？", "top_k": 1,
                })
                report["public_answer"] = answer_summary(response, settings)
                data = response.json()
                if report["public_answer"]["passed"]:
                    # 只有精确指向公开样本的引用才允许把答案归档。
                    assert all(item["source"].split("#", 1)[0] == identifiers["document_id"]
                               for item in data["citations"])
                    assert "三个工作日" in data["message"]
                    write_json(OUTPUT / "public-answer.json", data)
                write_json(OUTPUT / "verification.json", report)
                trace = report["public_answer"]["retrieval_trace"] or {}
                assert report["public_answer"]["passed"]
                assert report["rerank_probe"]["passed"]
                assert trace["dense_hits"] and trace["sparse_hits"]
                assert trace["reranker_used"] and trace["mode"] == "normal"
                assert report["public_answer"]["configured_llm_matches"]
                # 原用户问题只归档状态，用于对比历史引用失败，不能把本次通过当完整质量验收。
                response = await client.post("/api/v1/rag/query", json={
                    "question": "平安银行公司股东数量及持股情况",
                })
                report["shareholder_answer"] = answer_summary(response, settings)
                # 总状态只指公开链路验收；原股东问题有独立passed=false，不掩盖质量失败。
                report["status"] = "passed"
                report["acceptance_scope"] = "model_configuration_and_public_pipeline"
                write_json(OUTPUT / "verification.json", report)
                print(json.dumps(report, ensure_ascii=False))
    finally:
        await app.state.database.dispose()


def main() -> None:
    """顶层失败只输出类型，保留已保存的阶段状态，不泄露私有错误正文。"""
    try:
        asyncio.run(verify())
    except Exception as exc:  # noqa: BLE001 -- 脱敏错误边界。
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
