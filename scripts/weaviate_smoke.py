#!/usr/bin/env python3
"""Weaviate Dense transport smoke 与脱敏报告入口。

本脚本只在显式设置 ``WEAVIATE_ENABLED=true`` 时访问外部 Weaviate。没有真实服务
时输出 ``skipped`` 或 ``blocked``，不会用 Fake 结果冒充真实依赖证据。报告只记录
状态、数量、维度和错误分类，不保存 DSN、API key、原文或完整模型回答。
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

from enterprise_rag.retrieval.contracts import (
    WeaviateObject,
    collection_name_for_tenant,
)
from enterprise_rag.retrieval.weaviate import (
    WeaviateHTTPXTransport,
    WeaviateTransportError,
)


_SENSITIVE_REPORT_KEYS = {
    "api_key",
    "answer",
    "content",
    "dsn",
    "password",
    "prompt",
    "quote",
    "token",
}
_STAGE_NAMES = (
    "collection",
    "generation_write",
    "integrity",
    "publish",
    "query",
    "rollback",
    "cleanup",
)


def _utc_timestamp() -> str:
    """返回带 UTC 时区的时间戳，便于区分不同 smoke 运行。"""

    return datetime.now(UTC).isoformat()


def build_smoke_report() -> dict[str, Any]:
    """创建固定字段的 Weaviate Dense smoke 报告骨架。"""

    return {
        "schema_version": 1,
        "experiment": "weaviate_dense_smoke",
        "started_at": _utc_timestamp(),
        "finished_at": None,
        "run_status": "pending",
        "vector_backend": "weaviate_dense",
        "collection_strategy": "tenant_isolated",
        "generation_scope": [
            "tenant_id",
            "document_id",
            "version_id",
            "index_profile_key",
        ],
        "active_generation": None,
        "previous_generation": None,
        "production_performance_claim": False,
        "retrieval_quality_claim": False,
        "stages": {
            name: {"status": "pending"} for name in _STAGE_NAMES
        },
        "observations": {
            "object_count": None,
            "dimension": None,
            "model": None,
            "profile": None,
            "http_status_classes": [],
        },
        "environment": {
            "weaviate_enabled": False,
            "endpoint_configured": False,
        },
    }


def redact_smoke_report(value: Any) -> Any:
    """递归移除凭据、Prompt、正文和完整回答等敏感字段。"""

    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key.lower() in _SENSITIVE_REPORT_KEYS
            else redact_smoke_report(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_smoke_report(item) for item in value]
    if isinstance(value, tuple):
        return [redact_smoke_report(item) for item in value]
    return value


def finalize_smoke_report(report: dict[str, Any], *, run_status: str) -> dict[str, Any]:
    """写入终态并执行最后一轮递归脱敏。"""

    report["run_status"] = run_status
    report["finished_at"] = _utc_timestamp()
    return redact_smoke_report(report)


def _safe_error_kind(exc: WeaviateTransportError) -> str:
    """只提取固定错误分类，避免把上游正文带入报告。"""

    return exc.kind


def _smoke_objects(generation_id: str, profile: str) -> list[WeaviateObject]:
    """创建两条固定维度的最小 Dense 对象，不包含真实文档正文。"""

    return [
        WeaviateObject(
            object_id=f"smoke-{generation_id}-chunk-1",
            vector=(1.0, 0.0),
            properties={
                "tenant_id": "smoke-tenant",
                "document_id": "smoke-document",
                "version_id": "smoke-version",
                "generation_id": generation_id,
                "index_profile": profile,
                "chunk_id": "smoke-chunk-1",
                "parent_id": "smoke-parent-1",
                "text": "smoke fixture",
                "parent_text": "smoke fixture",
                "heading_path": ["smoke"],
                "source_locators": [{"line": 1}],
            },
        ),
        WeaviateObject(
            object_id=f"smoke-{generation_id}-chunk-2",
            vector=(0.0, 1.0),
            properties={
                "tenant_id": "smoke-tenant",
                "document_id": "smoke-document",
                "version_id": "smoke-version",
                "generation_id": generation_id,
                "index_profile": profile,
                "chunk_id": "smoke-chunk-2",
                "parent_id": "smoke-parent-1",
                "text": "smoke fixture",
                "parent_text": "smoke fixture",
                "heading_path": ["smoke"],
                "source_locators": [{"line": 2}],
            },
        ),
    ]


async def run_transport_smoke() -> dict[str, Any]:
    """执行不依赖 PostgreSQL 发布事实的 transport 层真实 smoke。"""

    report = build_smoke_report()
    enabled = os.getenv("WEAVIATE_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    base_url = os.getenv("WEAVIATE_BASE_URL", "").strip()
    report["environment"] = {
        "weaviate_enabled": enabled,
        "endpoint_configured": bool(base_url),
    }
    if not enabled:
        report["stages"]["collection"] = {
            "status": "skipped",
            "reason": "WEAVIATE_ENABLED=false",
        }
        return finalize_smoke_report(report, run_status="skipped")
    if not base_url:
        report["stages"]["collection"] = {
            "status": "blocked",
            "error_kind": "configuration",
        }
        return finalize_smoke_report(report, run_status="blocked")

    tenant_id = f"smoke-{uuid4().hex}"
    generation_id = f"smoke-generation-{uuid4().hex}"
    profile = "smoke-embedding:2:parent-child-v1:dense-v1"
    collection_name = collection_name_for_tenant(tenant_id)
    objects = _smoke_objects(generation_id, profile)
    report["active_generation"] = None
    report["observations"]["dimension"] = 2
    report["observations"]["profile"] = profile

    try:
        async with httpx.AsyncClient() as client:
            transport = WeaviateHTTPXTransport(
                client,
                base_url,
                api_key=os.getenv("WEAVIATE_API_KEY"),
            )
            await transport.ensure_collection(collection_name, "dense-v1")
            report["stages"]["collection"] = {"status": "passed"}
            result = await transport.upsert_batch(collection_name, objects)
            report["stages"]["generation_write"] = {
                "status": "passed",
                "accepted": result.accepted,
            }
            count = await transport.count_generation(
                collection_name,
                generation_id=generation_id,
                version_id="smoke-version",
            )
            chunk_ids = await transport.list_chunk_ids(
                collection_name,
                generation_id=generation_id,
                version_id="smoke-version",
            )
            report["observations"]["object_count"] = count
            report["stages"]["integrity"] = {
                "status": "passed" if count == 2 and len(chunk_ids) == 2 else "failed",
                "object_count": count,
                "chunk_id_count": len(chunk_ids),
            }
            rows = await transport.search(
                collection_name,
                generation_id=generation_id,
                version_id="smoke-version",
                query=(1.0, 0.0),
                top_k=1,
            )
            report["stages"]["query"] = {
                "status": "passed" if len(rows) == 1 else "failed",
                "result_count": len(rows),
            }
            report["stages"]["publish"] = {
                "status": "skipped",
                "reason": "PostgreSQL active pointer not exercised by transport-only smoke",
            }
            report["stages"]["rollback"] = {
                "status": "skipped",
                "reason": "PostgreSQL active pointer not exercised by transport-only smoke",
            }
            deleted = await transport.delete_generation(
                collection_name,
                generation_id=generation_id,
                version_id="smoke-version",
            )
            report["stages"]["cleanup"] = {"status": "passed", "deleted": deleted}
    except WeaviateTransportError as exc:
        for name in _STAGE_NAMES:
            if report["stages"][name]["status"] == "pending":
                report["stages"][name] = {
                    "status": "blocked",
                    "error_kind": _safe_error_kind(exc),
                }
                break
        return finalize_smoke_report(report, run_status="blocked")
    except (httpx.HTTPError, ValueError):
        for name in _STAGE_NAMES:
            if report["stages"][name]["status"] == "pending":
                report["stages"][name] = {
                    "status": "blocked",
                    "error_kind": "protocol",
                }
                break
        return finalize_smoke_report(report, run_status="blocked")

    return finalize_smoke_report(report, run_status="passed")


def _write_report(report: dict[str, Any], output_path: Path | None) -> None:
    """将脱敏报告输出到标准输出或用户显式指定的项目内路径。"""

    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output_path is None:
        print(encoded, end="")
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(encoded, encoding="utf-8")


def main() -> int:
    """解析命令行参数并执行一次 transport smoke。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="将脱敏报告写入项目内文件")
    args = parser.parse_args()
    report = asyncio.run(run_transport_smoke())
    _write_report(report, args.output)
    return 0 if report["run_status"] in {"passed", "skipped"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
