"""方案 B 真实依赖 smoke runner。

本脚本只验证当前开发机上的最小闭环：固定 Markdown 摄取、真实 Ollama
Embedding、PostgreSQL JSON 向量存储与应用层 cosine、FastAPI 检索入口以及
真实 LM Studio 摘录回答。它不会安装 ``pgvector``，也不会把一次 smoke 结果
外推为检索质量、生产性能、容量或高可用证据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

from enterprise_rag.api.app import create_app
from enterprise_rag.config.settings import Settings
from enterprise_rag.db.models import EmbeddingRecordModel
from enterprise_rag.db.session import Database, normalize_database_url
from enterprise_rag.embedding.ollama import OllamaEmbeddingProvider
from enterprise_rag.ingestion.contracts import IngestionRequest
from enterprise_rag.ingestion.service import IngestionService
from enterprise_rag.retrieval.indexer import DenseIndexer
from enterprise_rag.retrieval.repository import PostgresVectorRepository
from enterprise_rag.storage.local import LocalObjectStorage


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_REPORT_KEYS = {
    "answer",
    "api_key",
    "content",
    "dsn",
    "message",
    "password",
    "postgres_dsn",
    "prompt",
    "quote",
}
_DOCUMENT = """# Aurora 发布规则

项目代号 Aurora 的发布冻结窗口是每周三 20:00-22:00，时区为 Asia/Shanghai。
"""
_QUESTION = "Aurora 项目的发布冻结窗口是什么时间？"


def _utc_timestamp() -> str:
    """返回带 UTC 时区的 ISO 8601 时间戳，便于区分历史记录与本次观测。"""

    return datetime.now(timezone.utc).isoformat()


def _render_database_url(base_url: URL, database_name: str) -> str:
    """替换数据库名并保留真实密码，仅供进程内连接使用。

    ``str(URL)`` 默认会把密码渲染成 ``***``，该结果不能交给数据库驱动。
    返回值绝不允许写入报告或日志，最终报告还会执行递归脱敏作为第二道防线。
    """

    return base_url.set(database=database_name).render_as_string(hide_password=False)


def _normalize_vector_extension(
    available_version: str | None,
    installed_version: str | None,
) -> dict[str, Any]:
    """区分 ``vector`` 扩展可安装与已安装状态，不执行任何扩展变更。"""

    return {
        "available": available_version is not None,
        "installed": installed_version is not None,
        "available_version": available_version,
        "installed_version": installed_version,
    }


def _redact_report(value: Any) -> Any:
    """递归脱敏凭据、Prompt、正文、引用原文和完整模型回答。

    这里只按字段名处理，避免误删 ``safe_message`` 等已经过 API 固定映射的安全
    状态字段。runner 本身也不会主动把 DSN 或文档正文加入报告。
    """

    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in _SENSITIVE_REPORT_KEYS else _redact_report(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_report(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_report(item) for item in value]
    return value


def _base_report() -> dict[str, Any]:
    """创建固定的实验边界，禁止把 P0 smoke 冒充生产或质量结论。"""

    return {
        "schema_version": 1,
        "experiment": "retrieval_smoke",
        "started_at": _utc_timestamp(),
        "finished_at": None,
        "run_status": "pending",
        "stage": "initialization",
        "vector_backend": "postgres_json_application_cosine",
        "production_performance_claim": False,
        "retrieval_quality_claim": False,
        "scope": "single_machine_development_smoke",
        "dependencies": {
            "postgres": {"status": "pending", "vector_extension": None},
            "ollama": {"status": "pending", "model": None},
            "lmstudio": {"status": "pending", "model": None},
        },
        "ingestion": None,
        "indexing": None,
        "retrieval": None,
        "api": None,
        "cleanup": {
            "temporary_database_dropped": False,
            "temporary_storage_removed": False,
        },
    }


def _load_project_env() -> bool:
    """静默加载项目根目录 ``.env``，且不覆盖调用进程已有环境变量。

    解析器只支持空行、整行注释和 ``KEY=VALUE``；可选的单/双引号只会被移除，
    不执行变量替换、命令替换、反斜杠转义或 shell expansion。
    """

    env_path = _PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return False

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if _ENV_KEY_PATTERN.fullmatch(key) is None:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return True


async def _execute_admin_sql(database: Database, statement: str) -> None:
    """在 AUTOCOMMIT 连接中执行受控的临时数据库管理语句。"""

    async with database.engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(statement))


async def _inspect_vector_extension(database: Database) -> dict[str, Any]:
    """只读检查 ``vector`` 扩展的 available/installed 状态。"""

    async with database.engine.connect() as connection:
        available_version = await connection.scalar(
            text("SELECT default_version FROM pg_available_extensions WHERE name = 'vector'")
        )
        installed_version = await connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
    return _normalize_vector_extension(available_version, installed_version)


async def _inspect_index(database: Database) -> dict[str, Any]:
    """读取索引计数和维度，不把 child、Parent 或 embedding 正文写入报告。"""

    async with database.session() as session:
        record_count = await session.scalar(select(func.count()).select_from(EmbeddingRecordModel))
        dimensions = (
            await session.execute(select(EmbeddingRecordModel.dimension).distinct())
        ).scalars().all()
        parent_text_count = await session.scalar(
            select(func.count())
            .select_from(EmbeddingRecordModel)
            .where(EmbeddingRecordModel.parent_text.is_not(None))
        )
    return {
        "record_count": int(record_count or 0),
        "dimensions": sorted(int(value) for value in dimensions),
        "records_with_parent_text": int(parent_text_count or 0),
    }


def _safe_api_summary(response: httpx.Response) -> dict[str, Any]:
    """从 API 响应提取安全状态，主动丢弃回答、引用 quote 和上游正文。"""

    summary: dict[str, Any] = {"http_status": response.status_code}
    try:
        payload = response.json()
    except ValueError:
        summary["response_shape"] = "non_json"
        return summary

    if response.status_code != 200:
        error = payload.get("error") if isinstance(payload, dict) else None
        summary["error_kind"] = error.get("kind") if isinstance(error, dict) else "unknown"
        return summary

    provider_result = payload.get("provider_result") or {}
    citations = payload.get("citations") or []
    summary.update(
        {
            "context_source": payload.get("context_source"),
            "answer_status": payload.get("answer_status"),
            "citation_validation": payload.get("citation_validation"),
            "citation_count": len(citations) if isinstance(citations, list) else 0,
            "provider_result": {
                "status": provider_result.get("status"),
                "model": provider_result.get("model"),
                "finish_reason": provider_result.get("finish_reason"),
            },
            "timings": payload.get("timings"),
        }
    )
    return summary


def _api_completed(summary: dict[str, Any]) -> bool:
    """判断真实 API 是否完成“检索到证据并生成通过引用校验的回答”闭环。"""

    provider = summary.get("provider_result") or {}
    return (
        summary.get("http_status") == 200
        and summary.get("context_source") == "retrieved"
        and summary.get("answer_status") == "answered"
        and summary.get("citation_validation") == "passed"
        and summary.get("citation_count", 0) > 0
        and provider.get("status") == "success"
    )


async def _run_smoke() -> dict[str, Any]:
    """执行真实依赖 smoke，并保证临时数据库与对象目录最终被清理。"""

    report = _base_report()
    admin_database: Database | None = None
    smoke_database: Database | None = None
    embedding_client: httpx.AsyncClient | None = None
    database_name: str | None = None
    storage_root: Path | None = None

    try:
        report["stage"] = "configuration"
        _load_project_env()
        settings = Settings.from_env()
        if not settings.postgres_dsn:
            report.update(
                run_status="blocked",
                blocked_reason="postgres_dsn_not_configured",
            )
            return report

        normalized_dsn = normalize_database_url(settings.postgres_dsn)
        base_url = make_url(normalized_dsn)
        database_name = f"enterprise_rag_smoke_{uuid4().hex[:12]}"
        smoke_dsn = _render_database_url(base_url, database_name)

        report["stage"] = "temporary_database"
        admin_database = Database(normalized_dsn)
        await _execute_admin_sql(admin_database, f'CREATE DATABASE "{database_name}"')
        smoke_database = Database(smoke_dsn)
        await smoke_database.migrate()
        report["dependencies"]["postgres"] = {
            "status": "available",
            "vector_extension": await _inspect_vector_extension(smoke_database),
        }

        report["stage"] = "ingestion"
        storage_root = Path(tempfile.mkdtemp(prefix="enterprise-rag-retrieval-smoke-"))
        storage = LocalObjectStorage(storage_root)
        ingestion = IngestionService(smoke_database, storage)
        ingestion_result = await ingestion.ingest(
            IngestionRequest(
                tenant_id="smoke-tenant",
                source_id="smoke-aurora-release-policy",
                title="Aurora 发布规则",
                filename="aurora-release-policy.md",
                content=_DOCUMENT.encode("utf-8"),
                content_type="text/markdown",
                idempotency_key=f"retrieval-smoke-{uuid4().hex}",
                source_revision="smoke-v1",
            )
        )
        if not ingestion_result.version_id or not ingestion_result.parsed_object_key:
            raise ValueError("摄取未生成可索引的 version 或 parsed manifest")
        report["ingestion"] = {
            "status": ingestion_result.status.value,
            "version_status": ingestion_result.version_status.value
            if ingestion_result.version_status
            else None,
            "document_created": bool(ingestion_result.document_id),
            "version_created": True,
            "parsed_manifest_created": True,
        }

        report["stage"] = "ollama_indexing"
        embedding_client = httpx.AsyncClient()
        embedding_provider = OllamaEmbeddingProvider(
            embedding_client,
            base_url=settings.ollama_base_url,
            model=settings.ollama_embedding_model,
        )
        repository = PostgresVectorRepository()
        indexer = DenseIndexer(embedding_provider, repository, storage)
        async with smoke_database.transaction() as session:
            indexed_count = await indexer.index_manifest(
                session,
                tenant_id=ingestion_result.tenant_id,
                document_id=ingestion_result.document_id,
                version_id=ingestion_result.version_id,
                parsed_object_key=ingestion_result.parsed_object_key,
            )
        report["dependencies"]["ollama"] = {
            "status": "available",
            "model": settings.ollama_embedding_model,
        }
        report["indexing"] = {"indexed_count": indexed_count, **await _inspect_index(smoke_database)}
        if indexed_count <= 0:
            raise ValueError("固定文档没有生成 embedding 记录")

        report["stage"] = "retrieval"
        query_vectors = await embedding_provider.embed([_QUESTION])
        query_vector = query_vectors[0]
        async with smoke_database.session() as session:
            hits = await repository.search(
                session,
                ingestion_result.tenant_id,
                ingestion_result.version_id,
                query_vector,
                5,
            )
            wrong_tenant_hits = await repository.search(
                session,
                "wrong-tenant",
                ingestion_result.version_id,
                query_vector,
                5,
            )
            wrong_version_hits = await repository.search(
                session,
                ingestion_result.tenant_id,
                "wrong-version",
                query_vector,
                5,
            )
        report["retrieval"] = {
            "query_embedding_dimension": len(query_vector),
            "correct_scope_hit_count": len(hits),
            "wrong_tenant_hit_count": len(wrong_tenant_hits),
            "wrong_version_hit_count": len(wrong_version_hits),
            "top_hit_uses_parent_context": bool(hits and hits[0].parent_id),
            "top_hit_contains_expected_fact": bool(hits and "20:00-22:00" in hits[0].text),
        }
        if not hits or wrong_tenant_hits or wrong_version_hits:
            raise ValueError("tenant/version 检索隔离 smoke 未通过")

        report["stage"] = "retrieval_api"
        app_settings = replace(settings, postgres_dsn=smoke_dsn)
        app = create_app(settings=app_settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://smoke.local") as client:
                response = await client.post(
                    "/api/v1/retrieval/query",
                    json={
                        "tenant_id": ingestion_result.tenant_id,
                        "version_id": ingestion_result.version_id,
                        "question": _QUESTION,
                        "top_k": 5,
                        "max_tokens": 256,
                        "timeout_ms": 120_000,
                    },
                    timeout=180.0,
                )
        api_summary = _safe_api_summary(response)
        report["api"] = api_summary
        report["dependencies"]["lmstudio"] = {
            "status": "available" if response.status_code == 200 else "unavailable",
            "model": settings.local_llm_model,
        }
        if _api_completed(api_summary):
            report["run_status"] = "completed"
        elif response.status_code in {502, 503, 504}:
            report["run_status"] = "blocked"
            report["blocked_reason"] = "retrieval_or_model_dependency_unavailable"
        else:
            report["run_status"] = "failed"
            report["failure_reason"] = "retrieval_api_contract_not_completed"
        return report
    except (httpx.HTTPError, SQLAlchemyError, OSError) as exc:
        # 依赖连接、HTTP 或本地临时目录失败属于环境阻塞；只记录异常类型，避免
        # ``str(exc)`` 携带 DSN、请求正文或上游响应。
        report["run_status"] = "blocked"
        report["blocked_reason"] = "external_dependency_unavailable"
        report["error_type"] = type(exc).__name__
        return report
    except Exception as exc:
        # 契约或业务断言失败与环境不可用分开记录，同样不持久化异常正文。
        report["run_status"] = "failed"
        report["failure_reason"] = "smoke_contract_failed"
        report["error_type"] = type(exc).__name__
        return report
    finally:
        if embedding_client is not None:
            await embedding_client.aclose()
        if smoke_database is not None:
            await smoke_database.dispose()
        if database_name is not None and admin_database is not None:
            try:
                await _execute_admin_sql(
                    admin_database,
                    f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)',
                )
                report["cleanup"]["temporary_database_dropped"] = True
            except Exception as exc:
                # 清理失败必须可见，但不得用异常正文覆盖原始执行状态或泄露凭据。
                report["cleanup"]["database_cleanup_error_type"] = type(exc).__name__
                if report["run_status"] == "completed":
                    report["run_status"] = "failed"
                    report["failure_reason"] = "temporary_database_cleanup_failed"
        if admin_database is not None:
            await admin_database.dispose()
        if storage_root is not None:
            try:
                shutil.rmtree(storage_root)
                report["cleanup"]["temporary_storage_removed"] = True
            except OSError as exc:
                report["cleanup"]["storage_cleanup_error_type"] = type(exc).__name__
                if report["run_status"] == "completed":
                    report["run_status"] = "failed"
                    report["failure_reason"] = "temporary_storage_cleanup_failed"
        report["finished_at"] = _utc_timestamp()


def main() -> None:
    """运行 smoke，将递归脱敏后的 JSON 同时写入文件和标准输出。"""

    parser = argparse.ArgumentParser(description="方案 B 真实检索依赖 smoke")
    parser.add_argument("--output", type=Path, help="脱敏 JSON 输出路径")
    args = parser.parse_args()

    report = _redact_report(asyncio.run(_run_smoke()))
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")

    if report["run_status"] == "blocked":
        raise SystemExit(2)
    if report["run_status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
