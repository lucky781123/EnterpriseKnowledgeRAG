#!/usr/bin/env python3
"""本地 P0 产品闭环的 HTTP smoke runner。

脚本只通过已经启动的 FastAPI 产品入口执行上传、状态、ACL、检索问答和撤权验证。
它不会读取项目 ``.env``，也不会把 token、文档正文、引用原文、DSN、密码或 API key
写入报告。真实依赖由 API 进程负责装配，调用前应在专用开发环境中显式注入配置。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from enterprise_rag.security.dev_token import DevTokenCodec

_DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
_DEFAULT_TIMEOUT_SECONDS = 150.0
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SENSITIVE_REPORT_KEYS = frozenset(
    {
        "answer",
        "api_key",
        "body",
        "content",
        "dsn",
        "password",
        "prompt",
        "quote",
        "secret",
        "token",
    }
)
_REQUIRED_STAGES = (
    "readiness",
    "upload",
    "status",
    "cross_tenant_deny",
    "cross_tenant_query_deny",
    "grant",
    "query",
    "revoke",
    "revoked_query_deny",
    "regrant",
    "regranted_query",
    "cleanup",
)
_SMOKE_DOCUMENT = """# 发布冻结规则

Aurora 项目的发布冻结窗口是每周三 20:00-22:00，时区为 Asia/Shanghai。
重复提交必须使用相同的幂等键。
"""


def _utc_timestamp() -> str:
    """返回带 UTC 时区的时间戳，便于区分不同 smoke 运行。"""

    return datetime.now(UTC).isoformat()


def build_smoke_report() -> dict[str, Any]:
    """创建固定结构的脱敏报告骨架，不预置任何凭据或文档正文。"""

    return {
        "schema_version": 1,
        "experiment": "local_p0_product_smoke",
        "started_at": _utc_timestamp(),
        "finished_at": None,
        "run_status": "pending",
        "production_capacity_claim": False,
        "retrieval_quality_claim": False,
        "environment": {
            "api_base_url_configured": False,
            "dev_auth_secret_configured": False,
        },
        "stages": {name: {"status": "pending"} for name in _REQUIRED_STAGES},
    }


def redact_report(value: Any) -> Any:
    """递归替换敏感字段，作为写入实验产物前的最后一道保护。"""

    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key.lower() in _SENSITIVE_REPORT_KEYS
            else redact_report(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_report(item) for item in value]
    if isinstance(value, tuple):
        return [redact_report(item) for item in value]
    return value


def _safe_error_kind(payload: Any) -> str | None:
    """只提取响应中的固定错误类别，不携带错误消息或原始响应正文。"""

    if not isinstance(payload, dict):
        return None
    error = payload.get("error", payload.get("detail"))
    if not isinstance(error, dict):
        return None
    kind = error.get("kind")
    if not isinstance(kind, str) or not kind or len(kind) > 64:
        return "unknown"
    if not all(character.islower() or character.isdigit() or character == "_" for character in kind):
        return "unknown"
    return kind


def summarize_response(status_code: int, payload: Any) -> dict[str, Any]:
    """把 HTTP 响应压缩成状态、计数和固定错误类别等安全摘要。"""

    if 200 <= status_code < 300:
        summary: dict[str, Any] = {"status_code": status_code, "status": "passed"}
        if isinstance(payload, dict):
            # 上传和状态接口只保留稳定的业务标识和生命周期状态，不保留
            # object key、collection、原文或任何底层异常信息。
            for field in (
                "document_id",
                "version_id",
                "job_id",
                "generation_id",
                "document_status",
                "job_status",
                "version_status",
                "generation_status",
            ):
                if isinstance(payload.get(field), str):
                    summary[field] = payload[field]
            for field in ("published", "granted", "revoked"):
                if isinstance(payload.get(field), bool):
                    summary[field] = payload[field]
            artifacts = payload.get("artifacts")
            if isinstance(artifacts, dict):
                safe_artifacts: dict[str, Any] = {}
                for name, artifact in artifacts.items():
                    if not isinstance(name, str) or len(name) > 64:
                        continue
                    if isinstance(artifact, str):
                        safe_artifacts[name] = artifact
                    elif isinstance(artifact, dict):
                        safe_artifact = {
                            field: artifact[field]
                            for field in ("status", "item_count")
                            if field in artifact
                            and isinstance(artifact[field], (str, int))
                        }
                        if safe_artifact:
                            safe_artifacts[name] = safe_artifact
                if safe_artifacts:
                    summary["artifacts"] = safe_artifacts
            generation = payload.get("generation")
            if isinstance(generation, dict):
                safe_generation = {
                    field: generation[field]
                    for field in (
                        "generation_id",
                        "status",
                        "expected_item_count",
                        "actual_item_count",
                        "checkpoint",
                    )
                    if field in generation
                    and isinstance(generation[field], (str, int))
                }
                if safe_generation:
                    summary["generation"] = safe_generation
            for field in ("answer_status", "citation_validation"):
                if isinstance(payload.get(field), str):
                    summary[field] = payload[field]
            citations = payload.get("citations")
            if isinstance(citations, list):
                summary["citation_count"] = len(citations)
            trace = payload.get("retrieval_trace")
            if isinstance(trace, dict):
                summary["retrieval_trace"] = {
                    name: trace[name]
                    for name in (
                        "scopes",
                        "dense_hits",
                        "sparse_hits",
                        "fused_candidates",
                        "reranked_candidates",
                        "parent_candidates",
                        "rrf_k",
                        "reranker_used",
                        "mode",
                        "fallback_reasons",
                    )
                    if name in trace
                }
            if "answer_status" in payload and (
                payload.get("answer_status") != "answered"
                or payload.get("citation_validation") != "passed"
                or not citations
            ):
                summary["status"] = "failed"
        return summary

    summary = {"status_code": status_code, "status": "blocked" if status_code == 404 else "failed"}
    error_kind = _safe_error_kind(payload)
    if error_kind is not None:
        summary["error_kind"] = error_kind
    return summary


def _read_json(response: httpx.Response) -> Any:
    """读取 JSON 响应；解析失败时只返回空对象，不回显响应正文。"""

    try:
        return response.json()
    except ValueError:
        return {}


def _issue_tokens(secret: str, *, tenant_suffix: str = "") -> dict[str, str]:
    """在进程内为四个固定 smoke 身份签发短期 token。"""

    codec = DevTokenCodec(secret, ttl_seconds=900)
    return {
        "uploader": codec.issue(
            actor_id="smoke-uploader",
            tenant_id=f"smoke-tenant-a{tenant_suffix}",
            roles=["editor"],
        ),
        "admin": codec.issue(
            actor_id="smoke-admin",
            tenant_id=f"smoke-tenant-a{tenant_suffix}",
            roles=["admin"],
        ),
        "reader": codec.issue(
            actor_id="smoke-reader",
            tenant_id=f"smoke-tenant-a{tenant_suffix}",
            roles=["reader"],
        ),
        "other_tenant": codec.issue(
            actor_id="smoke-other",
            tenant_id=f"smoke-tenant-b{tenant_suffix}",
            roles=["reader"],
        ),
    }


def _auth_headers(token: str) -> dict[str, str]:
    """构造产品 API 使用的 Bearer header，不记录 token 内容。"""

    return {"Authorization": f"Bearer {token}"}


def _set_stage(report: dict[str, Any], name: str, summary: dict[str, Any]) -> None:
    """写入单个阶段的安全摘要，避免调用方把原始响应放入报告。"""

    report["stages"][name] = redact_report(summary)


async def run_smoke(
    *,
    api_base_url: str,
    dev_auth_secret: str | None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """执行上传到撤权的产品 smoke，并只返回脱敏阶段结果。"""

    report = build_smoke_report()
    report["environment"]["api_base_url_configured"] = bool(api_base_url.strip())
    report["environment"]["dev_auth_secret_configured"] = bool(dev_auth_secret)
    if not api_base_url.strip() or not dev_auth_secret:
        report["run_status"] = "blocked"
        report["stages"]["readiness"] = {
            "status": "blocked",
            "error_kind": "configuration",
        }
        report["finished_at"] = _utc_timestamp()
        return redact_report(report)

    try:
        # 隔离每次运行的 tenant，历史测试 ACL 不应影响本轮撤权断言。
        tokens = _issue_tokens(dev_auth_secret, tenant_suffix=f"-{uuid4().hex[:12]}")
    except ValueError:
        report["run_status"] = "blocked"
        report["stages"]["readiness"] = {
            "status": "blocked",
            "error_kind": "configuration",
        }
        report["finished_at"] = _utc_timestamp()
        return redact_report(report)

    document_id: str | None = None
    try:
        async with httpx.AsyncClient(
            base_url=api_base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
        ) as client:
            readiness_response = await client.get("/health/product/ready")
            _set_stage(
                report,
                "readiness",
                summarize_response(readiness_response.status_code, _read_json(readiness_response)),
            )
            if readiness_response.status_code != 200:
                return _finish_report(report, "blocked")

            upload_response = await client.post(
                "/api/v1/documents",
                headers=_auth_headers(tokens["uploader"]),
                data={
                    "source_id": f"smoke:local-p0:{uuid4().hex}",
                    "title": "本地 P0 smoke 文档",
                    "idempotency_key": f"local-p0-{uuid4().hex}",
                },
                files={"file": ("local-p0.md", _SMOKE_DOCUMENT.encode("utf-8"), "text/markdown")},
            )
            upload_payload = _read_json(upload_response)
            _set_stage(
                report,
                "upload",
                summarize_response(upload_response.status_code, upload_payload),
            )
            if upload_response.status_code != 201 or not isinstance(upload_payload, dict):
                return _finish_report(report, "failed")
            if not (
                upload_payload.get("published") is True
                and upload_payload.get("version_status") == "published"
                and upload_payload.get("generation_status") == "active"
                and all(upload_payload.get("artifacts", {}).get(kind) == "ready"
                        for kind in ("raw", "parsed", "embedding", "sparse"))
            ):
                report["stages"]["upload"]["status"] = "failed"
                return _finish_report(report, "failed")
            document_id = upload_payload.get("document_id")
            if not isinstance(document_id, str) or not document_id:
                return _finish_report(report, "failed")

            status_response = await client.get(
                f"/api/v1/documents/{document_id}/status",
                headers=_auth_headers(tokens["uploader"]),
            )
            _set_stage(
                report,
                "status",
                summarize_response(status_response.status_code, _read_json(status_response)),
            )
            status_payload = _read_json(status_response)
            if status_response.status_code != 200 or not (
                status_payload.get("document_status") == "ready"
                and (status_payload.get("current_version") or {}).get("version_id")
                == upload_payload.get("version_id")
                and (status_payload.get("generation") or {}).get("status") == "active"
            ):
                report["stages"]["status"]["status"] = "failed"
                return _finish_report(report, "failed")

            cross_tenant_response = await client.get(
                f"/api/v1/documents/{document_id}/status",
                headers=_auth_headers(tokens["other_tenant"]),
            )
            _set_stage(
                report,
                "cross_tenant_deny",
                summarize_response(
                    cross_tenant_response.status_code,
                    _read_json(cross_tenant_response),
                ),
            )
            if cross_tenant_response.status_code != 404:
                return _finish_report(report, "failed")

            query_request = {
                "question": "Aurora 项目的发布冻结窗口是什么时间？",
                "top_k": 3,
                "candidate_k": 10,
                "max_tokens": 1024,
                "timeout_ms": 120_000,
            }
            cross_tenant_query_response = await client.post(
                "/api/v1/rag/query",
                headers=_auth_headers(tokens["other_tenant"]),
                json=query_request,
            )
            _set_stage(
                report,
                "cross_tenant_query_deny",
                summarize_response(
                    cross_tenant_query_response.status_code,
                    _read_json(cross_tenant_query_response),
                ),
            )
            if cross_tenant_query_response.status_code != 404:
                return _finish_report(report, "failed")

            grant_response = await client.put(
                f"/api/v1/documents/{document_id}/acl/user/smoke-reader",
                headers=_auth_headers(tokens["admin"]),
            )
            _set_stage(
                report,
                "grant",
                summarize_response(grant_response.status_code, _read_json(grant_response)),
            )
            if grant_response.status_code != 200:
                return _finish_report(report, "failed")

            query_response = await client.post(
                "/api/v1/rag/query",
                headers=_auth_headers(tokens["reader"]),
                json=query_request,
            )
            _set_stage(
                report,
                "query",
                summarize_response(query_response.status_code, _read_json(query_response)),
            )
            if report["stages"]["query"]["status"] != "passed":
                return _finish_report(report, "failed")
            trace = report["stages"]["query"].get("retrieval_trace", {})
            if not (
                trace.get("dense_hits", 0) > 0 and trace.get("sparse_hits", 0) > 0
                and trace.get("fused_candidates", 0) > 0
                and trace.get("rrf_k") == 60 and trace.get("reranker_used") is True
            ):
                return _finish_report(report, "failed")

            revoke_response = await client.delete(
                f"/api/v1/documents/{document_id}/acl/user/smoke-reader",
                headers=_auth_headers(tokens["admin"]),
            )
            _set_stage(
                report,
                "revoke",
                summarize_response(revoke_response.status_code, _read_json(revoke_response)),
            )
            if revoke_response.status_code != 200:
                return _finish_report(report, "failed")

            revoked_query_response = await client.post(
                "/api/v1/rag/query",
                headers=_auth_headers(tokens["reader"]),
                json=query_request,
            )
            _set_stage(
                report,
                "revoked_query_deny",
                summarize_response(
                    revoked_query_response.status_code,
                    _read_json(revoked_query_response),
                ),
            )
            if revoked_query_response.status_code != 404:
                return _finish_report(report, "failed")

            regrant_response = await client.put(
                f"/api/v1/documents/{document_id}/acl/user/smoke-reader",
                headers=_auth_headers(tokens["admin"]),
            )
            _set_stage(
                report,
                "regrant",
                summarize_response(regrant_response.status_code, _read_json(regrant_response)),
            )
            if regrant_response.status_code != 200:
                return _finish_report(report, "failed")

            regranted_query_response = await client.post(
                "/api/v1/rag/query",
                headers=_auth_headers(tokens["reader"]),
                json=query_request,
            )
            _set_stage(
                report,
                "regranted_query",
                summarize_response(
                    regranted_query_response.status_code,
                    _read_json(regranted_query_response),
                ),
            )
            if report["stages"]["regranted_query"]["status"] != "passed":
                return _finish_report(report, "failed")
    except httpx.TimeoutException:
        # 当前待执行阶段才是故障位置，不能覆盖已经成功的 readiness 证据。
        stage = next(name for name in _REQUIRED_STAGES
                     if report["stages"][name]["status"] == "pending")
        report["stages"][stage] = {
            "status": "blocked",
            "error_kind": "timeout",
        }
        return _finish_report(report, "blocked")
    except httpx.HTTPError:
        stage = next(name for name in _REQUIRED_STAGES
                     if report["stages"][name]["status"] == "pending")
        report["stages"][stage] = {
            "status": "blocked",
            "error_kind": "network_unavailable",
        }
        return _finish_report(report, "blocked")

    return _finish_report(report, "passed")


def _finish_report(report: dict[str, Any], run_status: str) -> dict[str, Any]:
    """写入 smoke 终态、清理标记和完成时间，并执行递归脱敏。"""

    report["run_status"] = run_status
    report["finished_at"] = _utc_timestamp()
    report["stages"]["cleanup"] = {
        "status": "not_applicable",
        "reason": "产品 API 当前没有删除文档接口；本 smoke 不直接删除数据库或索引对象",
    }
    return redact_report(report)


def _default_output_path() -> Path:
    """返回当前 UTC 日期对应的脱敏实验产物路径。"""

    date_text = datetime.now(UTC).date().isoformat()
    return _PROJECT_ROOT / "artifacts" / "experiments" / f"{date_text}_local-p0-smoke" / "metrics.json"


def _write_report(path: Path, report: dict[str, Any]) -> None:
    """创建报告目录并以 UTF-8 JSON 写入脱敏结果。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(redact_report(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    """读取显式进程配置、运行 smoke、写报告并返回稳定退出码。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("P0_API_BASE_URL", _DEFAULT_API_BASE_URL),
        help="已启动 FastAPI 的地址；默认读取 P0_API_BASE_URL 或 127.0.0.1:8000",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help="单次 HTTP 请求超时秒数",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_default_output_path(),
        help="脱敏 JSON 报告路径",
    )
    args = parser.parse_args()
    report = asyncio.run(
        run_smoke(
            api_base_url=args.api_base_url,
            dev_auth_secret=os.getenv("DEV_AUTH_SECRET"),
            timeout_seconds=args.timeout_seconds,
        )
    )
    _write_report(args.output, report)
    print(json.dumps({"run_status": report["run_status"], "report": str(args.output)}, ensure_ascii=False))
    return 0 if report["run_status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
