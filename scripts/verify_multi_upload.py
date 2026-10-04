"""交叉核验公开批量样本的真实DB/对象/双路索引，保存脱敏验收证据。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from scripts.local_p0_api import create_local_p0_app
from scripts.verify_local_runtime import inspect_persistence, write_json

OUTPUT = Path("artifacts/experiments/2026-10-03_multi-upload")


async def verify() -> None:
    """只读取本实验的公开文件，用真实存储与HTTP状态验证独立发布。"""
    batch = json.loads((OUTPUT / "browser-upload.json").read_text())
    assert batch["status"] == "passed" and batch["count"] == 3
    os.environ["P0_WORKBENCH_ENABLED"] = "false"
    app = create_local_p0_app()
    assert app.state.settings.local_anonymous_enabled
    assert app.state.settings.product_upload_max_bytes is None
    persistence = {}
    try:
        async with app.router.lifespan_context(app):
            for item in batch["results"]:
                filename = item["filename"]
                assert filename in {"alpha.txt", "beta.md", "large-policy.pdf"}
                sample = OUTPUT / "samples" / filename
                result, tenant = await inspect_persistence(
                    app, item["result"]["data"], expected_content=sample.read_bytes(),
                    source_prefix=f"upload:{filename}",
                )
                assert tenant == "local-demo"
                persistence[filename] = {"file_bytes": sample.stat().st_size, **result}
                print(json.dumps({"file": filename, "persistence": "passed"}))
    finally:
        # 本脚本的factory连接池独立释放，运行中的API不受影响。
        await app.state.database.dispose()
    write_json(OUTPUT / "persistence.json", {"status": "passed", "files": persistence})
    state = json.loads(Path(".local/runtime/service.json").read_text())
    checks = {}
    async with httpx.AsyncClient(base_url=state["api"], timeout=15) as client:
        for endpoint in ("/health/product/ready", "/product-workbench/"):
            response = await client.get(endpoint)
            assert response.status_code == 200
            checks[endpoint] = response.status_code
        for item in batch["results"]:
            identity = item["result"]["data"]["document_id"]
            response = await client.get(f"/api/v1/documents/{identity}/status")
            assert "authorization" not in response.request.headers
            assert response.status_code == 200 and response.json()["document_status"] == "ready"
        ordinary = await client.post("/api/v1/rag/query", content=b"x" * (129 * 1024))
        cross_site = await client.post(
            "/product-workbench/gradio_api/queue/join",
            headers={"Origin": "https://evil.example"}, json={},
        )
        assert ordinary.status_code == 413 and cross_site.status_code == 403
        checks.update(ordinary_json_128kib=413, cross_site_queue=403)
    browser = json.loads((OUTPUT / "browser.json").read_text())
    answer = json.loads((OUTPUT / "browser-answer.json").read_text())
    assert browser["status"] == answer["status"] == "passed"
    data = answer["data"]
    assert data["answer_status"] == "answered" and data["citation_validation"] == "passed"
    document_ids = {item["result"]["data"]["document_id"] for item in batch["results"]}
    assert data["citations"] and all(
        item["source"].split("#", 1)[0] in document_ids for item in data["citations"]
    )
    assert "22:20" in data["message"] and "22:50" in data["message"]
    # 只归档固定格式访问日志，不复制异常正文、凭据或整份本机运行日志。
    pattern = re.compile(
        r'^INFO: +127\.0\.0\.1:\d+ - "(?:POST /api/v1/(?:documents|rag/query)|'
        r'GET /api/v1/documents/doc_[a-f0-9]+/status) HTTP/1\.1" (?:200 OK|201 Created)$'
    )
    selected = [
        line for line in Path(".local/runtime/api.log").read_text().splitlines()
        if pattern.fullmatch(line)
    ]
    (OUTPUT / "http-success.log").write_text("\n".join(selected[-10:]) + "\n")
    write_json(OUTPUT / "acceptance.json", {
        "status": "passed", "verified_at": datetime.now(UTC).isoformat(),
        "anonymous": True, "files": list(persistence), "independent_documents": len(document_ids),
        "largest_file_bytes": max(item["file_bytes"] for item in persistence.values()),
        "persistence": "passed", "answer_status": data["answer_status"],
        "citation_validation": data["citation_validation"], "retrieval_trace": data["retrieval_trace"],
        "finish_reason": data["provider_result"]["finish_reason"], "timings": data["timings"],
        "checks": checks,
    })
    print(json.dumps({"acceptance": "passed", "documents": len(document_ids)}))


def main() -> None:
    """失败仅输出类别，详细公开证据由各阶段显式落盘。"""
    try:
        asyncio.run(verify())
    except Exception as exc:  # noqa: BLE001 -- 禁止转储环境与异常正文。
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
