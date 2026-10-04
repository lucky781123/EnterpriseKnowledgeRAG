"""用真实检索证据诊断本地模型生成；只归档状态与计数，不保存原文或reasoning。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import Counter
from pathlib import Path

import httpx

from enterprise_rag.db.models import DocumentModel
from enterprise_rag.excerpts.protocols import EvidenceInput
from enterprise_rag.excerpts.service import ExcerptAnswerService
from enterprise_rag.providers.errors import ProviderError, ProviderErrorKind
from enterprise_rag.providers.protocols import ChatResponse
from scripts.local_p0_api import create_local_p0_app
from scripts.verify_local_runtime import write_json

OUTPUT = Path("artifacts/experiments/2026-10-03_answer-model")
QUESTION = "平安银行公司股东数量及持股情况"


async def diagnose(sample: bool, native_off: bool) -> None:
    """拦截一次真实RAG模型调用，在内存中复用同一证据测试采样参数。"""
    os.environ["P0_WORKBENCH_ENABLED"] = "false"
    app = create_local_p0_app()
    captured = []

    async def capture(request):
        """记录领域请求但不触发模型；受控503仅用于本诊断实例。"""
        captured.append(request)
        raise ProviderError(ProviderErrorKind.UNAVAILABLE, "诊断拦截", retryable=False)

    try:
        async with app.router.lifespan_context(app):
            provider = app.state.provider_registry.resolve("local_answer").provider
            provider.complete = capture
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
            ) as client:
                response = await client.post("/api/v1/rag/query", json={"question": QUESTION})
            assert response.status_code == 503 and len(captured) == 1
            request = captured[0]
            body = json.loads(request.messages[1].content)
            evidence = [
                EvidenceInput(
                    evidence_id=item["evidence_id"], text=item["text"], source=item.get("source"),
                ) for item in body["evidence"]
            ]
            # 通过真实document lineage查标题与来源，不打印标题、路径或证据正文。
            sources = []
            async with app.state.database.session() as session:
                for item in evidence:
                    identity = (item.source or "").split("#", 1)[0]
                    document = await session.get(DocumentModel, ("local-demo", identity))
                    title = document.title if document else ""
                    sources.append({
                        "evidence_id": item.evidence_id, "document_id": identity,
                        "chars": len(item.text), "title_matches_requested_bank": "平安" in title,
                        "text_mentions_requested_bank": "平安银行" in item.text,
                        "text_mentions_other_bank": any(
                            name in item.text for name in ("招商银行", "工商银行", "建设银行", "农业银行")
                        ),
                    })
            report = {
                "status": "inspected", "model": request.model, "max_tokens": request.max_tokens,
                "temperature": request.temperature, "evidence_count": len(evidence),
                "distinct_documents": len({item["document_id"] for item in sources}),
                "evidence_sources": sources, "probe": None,
            }
            write_json(OUTPUT / "inspection.json", report)
            print(json.dumps(report, ensure_ascii=False))
            if not sample and not native_off:
                return
            # 仅本次请求改变采样；不添加未经LM Studio文档确认的reasoning开关，
            # 不修改运行API、模型加载状态、最大token或共享服务的全局配置。
            payload = {
                "model": request.model,
                "messages": [{"role": item.role, "content": item.content} for item in request.messages],
                "temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5,
                "max_tokens": request.max_tokens, "stream": False,
            }
            settings = app.state.settings
            base = settings.local_llm_base_url.rstrip("/")
            root = base.removesuffix("/v1")
            if not base.endswith("/v1"):
                base += "/v1"
            headers = {}
            if settings.local_llm_api_key:
                headers["authorization"] = f"Bearer {settings.local_llm_api_key}"
            started = time.monotonic()
            endpoint = f"{base}/chat/completions"
            if native_off:
                # 单独探测当前服务是否接受reasoning=off；HTTP错误不能证明开关生效，
                # 也不把其他运行时的chat_template_kwargs当作LM Studio兼容开关。
                endpoint = f"{root}/api/v1/chat"
                payload = {
                    "model": request.model, "system_prompt": request.messages[0].content,
                    "input": request.messages[1].content, "reasoning": "off", "store": False,
                    "temperature": 0.7, "top_p": 0.8, "top_k": 20,
                    "max_output_tokens": request.max_tokens, "stream": False,
                }
            async with httpx.AsyncClient(timeout=180) as client:
                result = await client.post(endpoint, headers=headers, json=payload)
            if result.status_code >= 400:
                report["probe"] = {"http_status": result.status_code, "mode": "native_off"}
                write_json(OUTPUT / "native-off-probe.json", report)
            result.raise_for_status()
            data = result.json()
            if native_off:
                content = "".join(
                    item["content"] for item in data.get("output", []) if item["type"] == "message"
                )
                usage = data.get("stats", {})
                # 当前解析未获得原生终止原因，保持未知，不从token计数推断stop/length。
                finish_reason = None
            else:
                choice = data["choices"][0]
                content = choice["message"].get("content") or ""
                usage = data.get("usage")
                finish_reason = choice.get("finish_reason")
            protocol_error = None
            answer = None
            try:
                answer = ExcerptAnswerService.interpret_response(ChatResponse(
                    content=content, model=request.model, finish_reason=finish_reason,
                ), evidence)
            except ProviderError as exc:
                # 协议失败也保存计数，不能因为解析失败丢失对照结果。
                protocol_error = exc.kind.value
            lines = Counter(line.strip() for line in content.splitlines() if len(line.strip()) > 30)
            report["probe"] = {
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "http_status": result.status_code, "finish_reason": finish_reason,
                "answer_status": answer.answer_status if answer else "protocol_failed",
                "citation_validation": answer.citation_validation if answer else "not_checked",
                "citations_count": len(answer.citations) if answer else 0,
                "message_chars": len(answer.message) if answer else 0,
                "usage": usage, "max_long_line_repeat": max(lines.values(), default=0),
                "protocol_error": protocol_error, "mode": "native_off" if native_off else "sampling",
                "sampler": {key: value for key, value in payload.items() if key not in {
                    "model", "messages", "system_prompt", "input",
                }},
                "business_passed": bool(answer and answer.answer_status == "answered"
                and answer.citation_validation == "passed"),
            }
            write_json(OUTPUT / ("native-off-probe.json" if native_off else "sampling-probe.json"), report)
            print(json.dumps(report["probe"], ensure_ascii=False))
    finally:
        # 诊断实例释放自己的数据库连接，不影响运行中的8001服务。
        await app.state.database.dispose()


def main() -> None:
    """仅显式--sample或--native-off调用模型；失败不输出敏感异常正文。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--native-off", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(diagnose(args.sample, args.native_off))
    except Exception as exc:  # noqa: BLE001 -- 顶层禁止打印敏感异常正文。
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
