#!/usr/bin/env python3
"""Weaviate BM25F schema/query 的最小真实 smoke 与脱敏报告入口。"""

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

from enterprise_rag.retrieval.sparse import SparseFieldProjector, SparseProfile
from enterprise_rag.retrieval.weaviate import WeaviateTransportError
from enterprise_rag.retrieval.weaviate_sparse import WeaviateBM25FTransport


_STAGE_NAMES = (
    "collection",
    "projection",
    "generation_write",
    "query_correct_scope",
    "query_wrong_tenant",
    "query_wrong_version",
    "cleanup",
)


def _utc_timestamp() -> str:
    """返回带 UTC 时区的实验时间戳。"""

    return datetime.now(UTC).isoformat()


def build_bm25f_report() -> dict[str, Any]:
    """创建固定字段的 BM25F smoke 脱敏报告骨架。"""

    return {
        "schema_version": 1,
        "experiment": "weaviate_bm25f_smoke",
        "started_at": _utc_timestamp(),
        "finished_at": None,
        "run_status": "pending",
        "sparse_backend": "weaviate_bm25f",
        "collection_strategy": "isolated_smoke_collection",
        "retrieval_quality_claim": False,
        "production_performance_claim": False,
        "stages": {name: {"status": "pending"} for name in _STAGE_NAMES},
        "observations": {
            "projected_object_count": None,
            "accepted_object_count": None,
            "correct_scope_result_count": None,
            "wrong_tenant_result_count": None,
            "wrong_version_result_count": None,
            "deleted_object_count": None,
            "field_weights": None,
        },
        "environment": {
            "weaviate_enabled": False,
            "endpoint_configured": False,
        },
    }


def _finalize_report(report: dict[str, Any], *, run_status: str) -> dict[str, Any]:
    """写入报告终态；报告只包含计数、状态和固定配置，不包含正文或凭据。"""

    report["run_status"] = run_status
    report["finished_at"] = _utc_timestamp()
    return report


def _profile() -> SparseProfile:
    """返回本次真实 Spike 使用的固定 BM25F profile。"""

    return SparseProfile(
        tokenizer_version="tokenizer-v1",
        preprocessing_version="preprocess-v1",
        schema_version="sparse-v1",
        field_weights={
            "title": 2.0,
            "section_title": 2.0,
            "api_symbols": 3.0,
            "body": 1.0,
        },
    )


def _manifest() -> dict[str, Any]:
    """构造只含固定技术词的最小 manifest，不使用真实企业文档。"""

    return {
        "tenant_id": "bm25f-smoke-tenant",
        "document_id": "bm25f-smoke-document",
        "version_id": "bm25f-smoke-version",
        "chunks": {
            "children": [
                {
                    "chunk_id": "bm25f-smoke-child",
                    "parent_id": "bm25f-smoke-parent",
                    "version_id": "bm25f-smoke-version",
                    "text": "CreateUser API returns ERR_401 for /v1/users.",
                    "heading_path": ["Accounts", "Create user"],
                    "source_locators": [{"line": 3}],
                }
            ],
            "parents": [
                {
                    "chunk_id": "bm25f-smoke-parent",
                    "version_id": "bm25f-smoke-version",
                    "text": "Create user API reference.",
                    "heading_path": ["Accounts", "Create user"],
                    "source_locators": [{"line": 1}],
                }
            ],
        },
    }


async def run_bm25f_smoke() -> dict[str, Any]:
    """执行 projector、真实 BM25F 查询、负 scope 查询和隔离 collection 清理。"""

    report = build_bm25f_report()
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
        return _finalize_report(report, run_status="skipped")
    if not base_url:
        report["stages"]["collection"] = {
            "status": "blocked",
            "error_kind": "configuration",
        }
        return _finalize_report(report, run_status="blocked")

    profile = _profile()
    manifest = _manifest()
    generation_id = f"bm25f-smoke-generation-{uuid4().hex}"
    collection_name = f"SparseSmoke_{uuid4().hex}"
    current_stage = "collection"
    run_status = "passed"
    collection_created = False
    report["observations"]["field_weights"] = dict(profile.field_weights)

    async with httpx.AsyncClient() as client:
        transport = WeaviateBM25FTransport(
            client,
            base_url,
            api_key=os.getenv("WEAVIATE_API_KEY"),
        )
        try:
            await transport.ensure_collection(collection_name, profile.schema_version)
            collection_created = True
            report["stages"]["collection"] = {"status": "passed"}

            current_stage = "projection"
            objects = SparseFieldProjector(profile).project_manifest(
                manifest,
                generation_id=generation_id,
                tenant_id="bm25f-smoke-tenant",
                document_id="bm25f-smoke-document",
                version_id="bm25f-smoke-version",
            )
            report["observations"]["projected_object_count"] = len(objects)
            report["stages"]["projection"] = {
                "status": "passed" if len(objects) == 1 else "failed",
                "object_count": len(objects),
            }

            current_stage = "generation_write"
            accepted = await transport.upsert_batch(collection_name, objects)
            report["observations"]["accepted_object_count"] = accepted
            report["stages"]["generation_write"] = {
                "status": "passed" if accepted == len(objects) else "failed",
                "accepted": accepted,
            }

            current_stage = "query_correct_scope"
            correct_rows = await transport.search(
                collection_name,
                profile=profile,
                tenant_id="bm25f-smoke-tenant",
                generation_id=generation_id,
                version_id="bm25f-smoke-version",
                query="CreateUser",
                top_k=3,
            )
            correct_scope_passed = (
                len(correct_rows) == 1
                and correct_rows[0].get("chunk_id") == "bm25f-smoke-child"
            )
            report["observations"]["correct_scope_result_count"] = len(correct_rows)
            report["stages"]["query_correct_scope"] = {
                "status": "passed" if correct_scope_passed else "failed",
                "result_count": len(correct_rows),
            }

            current_stage = "query_wrong_tenant"
            wrong_tenant_rows = await transport.search(
                collection_name,
                profile=profile,
                tenant_id="bm25f-wrong-tenant",
                generation_id=generation_id,
                version_id="bm25f-smoke-version",
                query="CreateUser",
                top_k=3,
            )
            report["observations"]["wrong_tenant_result_count"] = len(
                wrong_tenant_rows
            )
            report["stages"]["query_wrong_tenant"] = {
                "status": "passed" if not wrong_tenant_rows else "failed",
                "result_count": len(wrong_tenant_rows),
            }

            current_stage = "query_wrong_version"
            wrong_version_rows = await transport.search(
                collection_name,
                profile=profile,
                tenant_id="bm25f-smoke-tenant",
                generation_id=generation_id,
                version_id="bm25f-wrong-version",
                query="CreateUser",
                top_k=3,
            )
            report["observations"]["wrong_version_result_count"] = len(
                wrong_version_rows
            )
            report["stages"]["query_wrong_version"] = {
                "status": "passed" if not wrong_version_rows else "failed",
                "result_count": len(wrong_version_rows),
            }

            if not all(
                report["stages"][name]["status"] == "passed"
                for name in (
                    "projection",
                    "generation_write",
                    "query_correct_scope",
                    "query_wrong_tenant",
                    "query_wrong_version",
                )
            ):
                run_status = "failed"
        except WeaviateTransportError as exc:
            report["stages"][current_stage] = {
                "status": "blocked",
                "error_kind": exc.kind,
            }
            run_status = "blocked"
        except (httpx.HTTPError, ValueError):
            report["stages"][current_stage] = {
                "status": "blocked",
                "error_kind": "protocol",
            }
            run_status = "blocked"
        finally:
            if collection_created:
                try:
                    deleted = await transport.delete_generation(
                        collection_name,
                        generation_id=generation_id,
                        version_id="bm25f-smoke-version",
                    )
                    await transport.delete_collection(collection_name)
                    report["observations"]["deleted_object_count"] = deleted
                    report["stages"]["cleanup"] = {
                        "status": "passed",
                        "deleted": deleted,
                        "collection_deleted": True,
                    }
                except WeaviateTransportError as exc:
                    report["stages"]["cleanup"] = {
                        "status": "blocked",
                        "error_kind": exc.kind,
                    }
                    run_status = "blocked"

    return _finalize_report(report, run_status=run_status)


def _write_report(report: dict[str, Any], output_path: Path | None) -> None:
    """将脱敏报告写到标准输出或用户指定的项目内文件。"""

    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output_path is None:
        print(encoded, end="")
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(encoded, encoding="utf-8")


def main() -> int:
    """解析命令行参数，执行一次 BM25F smoke 并返回稳定退出码。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="将脱敏报告写入项目内文件")
    args = parser.parse_args()
    report = asyncio.run(run_bm25f_smoke())
    _write_report(report, args.output)
    return 0 if report["run_status"] in {"passed", "skipped"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
