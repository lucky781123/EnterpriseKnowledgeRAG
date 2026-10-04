"""本机免Token TXT/大PDF验收：公开样本、持久化和真实本地模型降级证据。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)
from sqlalchemy import select

from enterprise_rag.db.models import IngestionJobModel
from scripts.local_p0_api import create_local_p0_app
from scripts.verify_local_runtime import dependency_failure, inspect_persistence, write_json

OUTPUT = Path("artifacts/experiments/2026-10-03_no-token-text-pdf")
TXT = (
    "Polaris 项目运维规则\n\n"
    "Polaris 项目的备份保留期为 37 天，每天凌晨 03:15 执行备份，时区为 Asia/Shanghai。\n\n"
    "恢复演练由平台运维组负责，每月第二个周二执行。\n"
)
PDF_TEXT = (
    "Vega maintenance policy. "
    "The Vega maintenance window is every Friday from 21:10 to 21:50 UTC. "
    "The rollback owner is the platform operations team."
)


def prepare() -> None:
    """用PyPDF2结构化API生成含真实图像和文字的大PDF，不添加无意义尾部填充。"""

    samples = OUTPUT / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    (samples / "polaris.txt").write_text(TXT, encoding="utf-8")
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    # PyPDF2会复制传入页面；必须修改writer实际持有的对象才能序列化资源和正文。
    page = writer.pages[0]
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    # 未压缩RGB图像是PDF实际页面资源；像素可见，大小与传输预算独立可核验。
    image = DecodedStreamObject()
    image.set_data(bytes(
        value for y in range(320) for x in range(320)
        for value in (x % 256, y % 256, (x + y) % 256)
    ))
    image.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(320), NameObject("/Height"): NumberObject(320),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        NameObject("/BitsPerComponent"): NumberObject(8),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): writer._add_object(image)}),
    })
    stream = DecodedStreamObject()
    lines = [
        "Vega maintenance policy.",
        "The Vega maintenance window is every Friday",
        "from 21:10 to 21:50 UTC.",
        "The rollback owner is the platform operations team.",
    ]
    commands = ["q 240 0 0 240 60 350 cm /Im1 Do Q", "BT /F1 12 Tf 60 740 Td"]
    for index, line in enumerate(lines):
        if index:
            commands.append("0 -20 Td")
        commands.append(f"({line}) Tj")
    stream.set_data(("\n".join(commands) + "\nET").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    pdf = samples / "vega.pdf"
    with pdf.open("wb") as target:
        writer.write(target)
    assert pdf.stat().st_size > 128 * 1024
    extracted = " ".join(PdfReader(pdf).pages[0].extract_text().split())
    assert extracted == PDF_TEXT
    write_json(OUTPUT / "samples.json", {
        "status": "passed", "pdf_has_extractable_text": True, "pdf_has_real_image": True,
        "files": {path.name: {
            "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        } for path in sorted(samples.iterdir())},
    })
    print(json.dumps({"samples": "passed", "pdf_bytes": pdf.stat().st_size}))


async def verify() -> None:
    """交叉核验真实浏览器上传产物，并在隔离ASGI应用中执行真实模型降级。"""

    os.environ["P0_WORKBENCH_ENABLED"] = "false"
    app = create_local_p0_app()
    assert app.state.settings.local_anonymous_enabled
    try:
        async with app.router.lifespan_context(app):
            for case, filename in (("txt", "polaris.txt"), ("pdf", "vega.pdf")):
                upload = json.loads((OUTPUT / f"{case}-upload.json").read_text())
                persistence, tenant = await inspect_persistence(
                    app, upload["data"], expected_content=(OUTPUT / "samples" / filename).read_bytes(),
                    source_prefix=f"upload:{filename}",
                )
                assert tenant == "local-demo"
                write_json(OUTPUT / f"{case}-persistence.json", persistence)
                print(json.dumps({"case": case, "persistence": "passed"}))
            retrieval = app.state.product_retrieval_service
            provider = app.state.provider_registry.resolve("local_answer").provider
            original_complete = provider.complete
            calls = 0

            async def counted(*args, **kwargs):
                """透传真实LM Studio调用，记录次数而不替换模型输出。"""
                nonlocal calls
                calls += 1
                return await original_complete(*args, **kwargs)

            provider.complete = counted
            dependencies = {
                "embedding": (retrieval.embedding_provider, "embed"),
                "dense": (retrieval.dense_transport, "search"),
                "sparse": (retrieval.sparse_transport, "search"),
                "reranker": (retrieval.reranker, "rerank"),
            }
            results = {}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
                timeout=150,
            ) as client:
                for name in ("embedding", "dense", "sparse", "reranker", "both_channels"):
                    before = calls
                    patched = []
                    try:
                        for key in (("dense", "sparse") if name == "both_channels" else (name,)):
                            obj, method = dependencies[key]
                            patched.append((obj, method, getattr(obj, method)))
                            setattr(obj, method, dependency_failure)
                        response = await client.post("/api/v1/rag/query", json={
                            "question": "Polaris 项目的备份保留期是多少天，执行时间是什么？",
                            "top_k": 3, "candidate_k": 10, "max_tokens": 1024,
                            "timeout_ms": 120_000,
                        })
                        payload = response.json()
                        if name == "both_channels":
                            assert response.status_code == 503 and calls == before
                            evidence = {"error_kind": payload["error"]["kind"]}
                        else:
                            assert response.status_code == 200 and calls - before == 1
                            assert payload["answer_status"] == "answered"
                            assert payload["citation_validation"] == "passed"
                            assert payload["retrieval_trace"]["mode"] == "degraded"
                            assert f"{name}_unavailable" in payload["retrieval_trace"]["fallback_reasons"]
                            evidence = {key: payload[key] for key in (
                                "message", "answer_status", "citation_validation", "citations",
                                "retrieval_trace", "provider_result",
                            )}
                        results[name] = {
                            "assertions": "passed", "status_code": response.status_code,
                            "authorization_header_sent": False, "real_local_llm_calls": calls - before,
                            **evidence,
                        }
                        write_json(OUTPUT / "fallback.json", {
                            "status": "in_progress", "transport": "asgi_http_fault_injection",
                            "remaining_dependencies": "real", "cases": results,
                        })
                        print(json.dumps({"fallback": name, "assertions": "passed"}))
                    finally:
                        for obj, method, original in patched:
                            setattr(obj, method, original)
            write_json(OUTPUT / "fallback.json", {
                "status": "passed", "transport": "asgi_http_fault_injection",
                "remaining_dependencies": "real", "cases": results,
            })
    finally:
        # factory显式注入数据库，HTTP客户端由lifespan释放，连接池由runner释放。
        await app.state.database.dispose()


async def replay_failed_samples() -> None:
    """仅恢复本任务公开样本的失败摄取，不改动其他用户任务或索引。"""

    os.environ["P0_WORKBENCH_ENABLED"] = "false"
    app = create_local_p0_app()
    try:
        async with app.router.lifespan_context(app):
            ingestion = app.state.product_ingestion_service.ingestion_service
            async with app.state.database.session() as session:
                jobs = (await session.scalars(select(IngestionJobModel).where(
                    IngestionJobModel.tenant_id == "local-demo",
                    IngestionJobModel.status == "failed",
                ))).all()
            for job in jobs:
                metadata = json.loads(await ingestion.storage.get(
                    ingestion._request_meta_key("local-demo", job.id),
                ))
                if metadata["filename"] not in {"polaris.txt", "vega.pdf"}:
                    continue
                sample = OUTPUT / "samples" / metadata["filename"]
                raw = await ingestion.storage.get(metadata["raw_object_key"])
                assert raw == sample.read_bytes()
                result = await ingestion.replay_failed("local-demo", job.id)
                assert result.status.value == "succeeded"
                write_json(OUTPUT / "replay.json", {
                    "status": "passed", "job_id": job.id, "reason": "TXT parser registration repaired",
                    "job_status": result.status.value,
                })
                print(json.dumps({"replay": "passed", "job_status": result.status.value}))
    finally:
        await app.state.database.dispose()


def main() -> None:
    """分别准备公开文件和核验浏览器结果，不读取Token文件。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "verify", "replay", "finalize"])
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            prepare()
        elif args.action == "verify":
            asyncio.run(verify())
        elif args.action == "finalize":
            finalize()
        else:
            asyncio.run(replay_failed_samples())
    except Exception as exc:  # noqa: BLE001 -- 顶层禁止异常正文泄露配置。
        # 顶层不输出可能包含配置或正文的异常值，仅提供稳定诊断类别。
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


def finalize() -> None:
    """汇总已真实完成的断言并保存精选访问日志；不得仅凭HTTP200宣布验收。"""

    state = json.loads(Path(".local/runtime/service.json").read_text())
    assert state["anonymous"] is True
    documents = {}
    checks = {}
    with httpx.Client(base_url=state["api"], timeout=15) as client:
        ready = client.get("/health/product/ready")
        frontend = client.get("/product-workbench/")
        assert ready.status_code == frontend.status_code == 200
        checks["readiness"] = ready.json()
        checks["frontend_status"] = frontend.status_code
        for case in ("txt", "pdf"):
            browser = json.loads((OUTPUT / f"{case}.json").read_text())
            upload = json.loads((OUTPUT / f"{case}-upload.json").read_text())
            answer = json.loads((OUTPUT / f"{case}-answer.json").read_text())
            persistence = json.loads((OUTPUT / f"{case}-persistence.json").read_text())
            assert browser["status"] == "passed" and browser["anonymous"]
            assert upload["status_code"] == 201 and upload["data"]["published"]
            assert answer["data"]["answer_status"] == "answered"
            assert answer["data"]["citation_validation"] == "passed"
            trace = answer["data"]["retrieval_trace"]
            assert trace["dense_hits"] and trace["sparse_hits"] and trace["reranker_used"]
            assert persistence["status"] == "passed" and persistence["vector_dimensions"] == [1024]
            status = client.get(f"/api/v1/documents/{upload['data']['document_id']}/status")
            assert "authorization" not in status.request.headers and status.status_code == 200
            assert status.json()["document_status"] == "ready"
            documents[case] = {
                "document_id": upload["data"]["document_id"],
                "generation_id": upload["data"]["generation_id"],
                "upload_status": 201, "status_without_authorization": 200,
                "message": answer["data"]["message"], "trace": trace,
                "persistence": "passed", "citations": "passed",
            }
        cross_site = client.post(
            "/product-workbench/gradio_api/queue/join",
            headers={"Origin": "https://evil.example"}, json={},
        )
        ordinary = client.post("/api/v1/rag/query", content=b"x" * (129 * 1024))
        assert cross_site.status_code == 403 and ordinary.status_code == 413
        checks["cross_site_queue"] = 403
        checks["ordinary_json_128kib_limit"] = 413
    fallback = json.loads((OUTPUT / "fallback.json").read_text())
    assert fallback["status"] == "passed" and len(fallback["cases"]) == 5
    assert all(case["assertions"] == "passed" for case in fallback["cases"].values())
    assert "303 passed" in (OUTPUT / "pytest-offline.txt").read_text()
    assert "16 passed" in (OUTPUT / "pytest-postgres.txt").read_text()
    # 仅摘录固定HTTP访问行；不归档traceback、正文、endpoint配置或整个本地日志。
    logs = Path(".local/runtime/api.log").read_text()
    pattern = re.compile(
        r'^INFO: +127\.0\.0\.1:\d+ - "(?:POST /api/v1/(?:documents|rag/query)|'
        r'POST /product-workbench/gradio_api/upload\?upload_id=[a-z0-9]+|'
        r'GET /api/v1/documents/doc_[a-f0-9]+/status) HTTP/1\.1" (?:200 OK|201 Created)$'
    )
    selected = [line for line in logs.splitlines() if pattern.fullmatch(line)]
    (OUTPUT / "http-success.log").write_text("\n".join(selected[-12:]) + "\n", encoding="utf-8")
    write_json(OUTPUT / "acceptance.json", {
        "status": "passed", "verified_at": datetime.now(UTC).isoformat(),
        "anonymous": True, "tenant": "local-demo", "documents": documents,
        "checks": checks, "fallback_cases": 5, "offline_tests": "303 passed, 12 skipped",
        "postgres_tests": "16 passed", "api": state["api"],
    })
    print(json.dumps({"final_acceptance": "passed", "documents": 2, "fallback_cases": 5}))


if __name__ == "__main__":
    main()
