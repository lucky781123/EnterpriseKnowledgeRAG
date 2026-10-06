"""只用固定公开合成证据联调在线模型，业务 allowlist 始终保持不变。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import tomllib
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from enterprise_rag.excerpts.protocols import EvidenceInput
from enterprise_rag.excerpts.service import ExcerptAnswerService
from enterprise_rag.providers.errors import ProviderError
from enterprise_rag.providers.lmstudio import LMStudioChatProvider
from enterprise_rag.providers.protocols import ChatResponse
from enterprise_rag.providers.routed import DeepSeekSettings, PolicyRoutedProvider, model_scope

ROOT = Path(__file__).resolve().parents[1]
TEST_TENANT = "deepseek-public-smoke"
OUTPUT = ROOT / ".local/deepseek-smoke/result.json"


def public_request():
    """只生成源码内公开样本，不接受路径、业务问题、租户或外部证据参数。"""
    evidence = (EvidenceInput(
        evidence_id="public-smoke-1",
        text="PUBLIC_SMOKE_CHECK 是公开合成测试。审批期限为三个工作日。",
        source="synthetic-public-fixture",
    ),)
    # 复用产品的结构化回答和逐字引用协议，不把连通性等同于回答有效。
    return ExcerptAnswerService(None).prepare_request(
        question="PUBLIC_SMOKE_CHECK 的审批期限是多久？",
        evidence=evidence, model="local_answer", temperature=0, max_tokens=512,
    )


async def verify_routes(settings, local, online, unavailable, local_model):
    """分开记录真实在线、注入故障兜底和拒绝外发，不输出模型正文或密钥。"""
    # 临时 allowlist 仅属于本测试实例；不修改 TOML 或正在运行的业务 Provider。
    settings = replace(settings, allowed_tenants=(TEST_TENANT,))
    request, evidence = public_request()
    cases = [
        ("online_complete", online, TEST_TENANT, False, 1, 0),
        ("online_stream", online, TEST_TENANT, True, 1, 0),
        ("unavailable_complete_fallback", unavailable, TEST_TENANT, False, 1, 1),
        ("unavailable_stream_fallback", unavailable, TEST_TENANT, True, 1, 1),
        ("disallowed_tenant", online, "public-smoke-not-authorized", False, 0, 0),
        ("no_tenant_context", online, None, False, 0, 0),
    ]
    results = {}
    for name, primary, tenant, streaming, expected_online, expected_fallback in cases:
        routed = PolicyRoutedProvider(local, primary, settings, local_model)
        started = time.monotonic()
        record = {"passed": False, "streaming": streaming}

        async def invoke(streaming, routed):
            """非流式直接验证响应；流式必须有 DONE 后才允许验证完整正文。"""
            if not streaming:
                return await routed.complete(request)
            events = [event async for event in routed.stream_events(request)]
            if not events or not events[-1].done:
                return None
            return ChatResponse(
                content="".join(event.text for event in events),
                model=settings.model if routed.fallback_count == 0 else local_model,
                finish_reason=events[-1].finish_reason, usage=events[-1].usage,
            )

        try:
            # 每个样本有独立的有界总预算；同一个样本的兜底继承该 deadline。
            async with asyncio.timeout(90):
                if tenant is None:
                    response = await invoke(streaming, routed)
                else:
                    with model_scope(tenant, started + 90):
                        response = await invoke(streaming, routed)
            if response is not None:
                answer = ExcerptAnswerService.interpret_response(response, evidence)
                record.update({
                    "answer_status": answer.answer_status,
                    "citation_validation": answer.citation_validation,
                    "citation_count": len(answer.citations),
                    "finish_reason": response.finish_reason,
                    "passed": (
                        answer.answer_status == "answered"
                        and answer.citation_validation == "passed"
                        and "三个工作日" in answer.message
                        and routed.online_count == expected_online
                        and routed.fallback_count == expected_fallback
                    ),
                })
        except ProviderError as exc:
            # 只保留领域分类，禁止保存网关错误 body、Authorization 或模型原文。
            record["error_kind"] = exc.kind.value
        except TimeoutError:
            record["error_kind"] = "timeout"
        record.update({
            "online_attempts": routed.online_count,
            "fallback_count": routed.fallback_count,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "primary_failure_injected": primary is unavailable,
        })
        results[name] = record
    return {
        "scope": "public_synthetic_provider_smoke",
        "test_tenant": TEST_TENANT,
        "business_allowlist_modified": False,
        "requested_model": settings.model,
        "passed": all(record["passed"] for record in results.values()),
        "checks": results,
        "not_verified": [
            "online_product_ingestion_retrieval_acl_pipeline",
            "private_document_egress_authorization",
            "hosted_backup_restore",
            "production_identity_capacity_rpo_rto",
        ],
    }


async def execute():
    """安全加载项目凭据并执行小规模测试，不读取 .env 或业务数据库。"""
    settings = DeepSeekSettings.load(ROOT)
    if not settings.enabled:
        raise ValueError("DeepSeek 尚未启用")
    with (ROOT / "config/local-llm.toml").open("rb") as source:
        local_config = tomllib.load(source)
    # 本地兜底必须真正在 loopback；此脚本不把本地配置当作任意外发地址。
    parsed = urlsplit(local_config["base_url"])
    if (
        parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username or parsed.password or parsed.query or parsed.fragment
    ):
        raise ValueError("公开 smoke 的本地兜底仅允许 loopback")

    async def inject_unavailable(request):
        """在 HTTP adapter 边界注入 503，不关闭真实服务，也不访问在线网关。"""
        return httpx.Response(503)

    # 真实 HTTP 连接池共享；注入故障使用另一 MockTransport，证据明确标记注入。
    async with (
        httpx.AsyncClient(timeout=90, trust_env=False) as client,
        httpx.AsyncClient(transport=httpx.MockTransport(inject_unavailable)) as failure_client,
    ):
        local = LMStudioChatProvider(
            client, local_config["base_url"], local_config["model"],
        )
        online = LMStudioChatProvider(client, settings.base_url, settings.model, settings.api_key)
        unavailable = LMStudioChatProvider(failure_client, settings.base_url, settings.model)
        return await verify_routes(settings, local, online, unavailable, local_config["model"])


def main():
    """默认只显示计划；显式 --execute 才读密钥、访问模型和写脱敏结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({
            "status": "planned", "test_tenant": TEST_TENANT,
            "business_allowlist_modified": False, "real_online_requests": 2,
            "real_local_requests": 4,
        }))
        return
    try:
        report = asyncio.run(execute())
    except Exception as exc:  # noqa: BLE001 -- 顶层只输出安全分类，避免凭据进入 traceback。
        report = {"passed": False, "error_type": type(exc).__name__}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
