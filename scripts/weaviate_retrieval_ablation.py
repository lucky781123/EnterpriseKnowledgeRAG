"""采集真实本机 Dense/BM25F/Reranker 排名并复用 P1 消融契约。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from enterprise_rag.embedding.ollama import OllamaEmbeddingProvider
from enterprise_rag.evaluation.retrieval_ablation import run_retrieval_ablation
from enterprise_rag.reranking.http import HTTPReranker
from enterprise_rag.reranking.protocols import RerankRequest
from enterprise_rag.retrieval.weaviate import (
    WeaviateHTTPXTransport,
    WeaviateTransportError,
)
from enterprise_rag.retrieval.weaviate_sparse import WeaviateBM25FTransport

if __package__ in {None, ""}:
    # 直接执行 ``python scripts/...py`` 时，Python 只把 scripts 放入 sys.path；
    # 补入项目根目录后复用既有评测辅助模块，不复制第二份数据构造逻辑。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.weaviate_bm25f_rrf_evaluation import (
        QualityDataset,
        _channel_hits_from_dense,
        _channel_hits_from_sparse,
        _dense_objects,
        _profile,
        _sparse_objects,
        load_quality_dataset,
    )
else:
    from scripts.weaviate_bm25f_rrf_evaluation import (
        QualityDataset,
        _channel_hits_from_dense,
        _channel_hits_from_sparse,
        _dense_objects,
        _profile,
        _sparse_objects,
        load_quality_dataset,
    )


DEFAULT_DATASET = Path("docs/evaluation/bm25f-rrf-smoke-dataset.json")
DEFAULT_OUTPUT_DIR = Path("artifacts/experiments/2026-10-07_p1-retrieval-ablation-real")
DEFAULT_CONFIG_FILE = Path("config/local-llm.toml")


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """描述本次本机采集所需的非敏感服务配置。"""

    weaviate_base_url: str
    ollama_base_url: str
    embedding_model: str
    reranker_base_url: str
    reranker_model: str


def _required_text(value: Any, field_name: str) -> str:
    """校验配置字符串，避免把缺失地址静默转换为不可诊断的请求。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")
    return value.strip()


def _configured_value(
    environment_name: str,
    section: Mapping[str, Any],
    field_name: str,
    default: str,
) -> str:
    """按环境变量、TOML 分节和默认值的顺序读取非敏感配置。"""

    value = os.getenv(environment_name)
    if value is None:
        value = section.get(field_name, default)
    return _required_text(value, environment_name)


def load_collector_config(config_path: str | Path) -> CollectorConfig:
    """读取本机 Embedding/Reranker TOML，并从环境读取 Weaviate 地址。"""

    source = Path(config_path)
    payload: dict[str, Any] = {}
    if source.exists():
        with source.open("rb") as stream:
            loaded = tomllib.load(stream)
        if not isinstance(loaded, dict):
            raise ValueError("本地模型配置根对象必须是 TOML table")
        payload = loaded
    embedding = payload.get("embedding", {})
    reranker = payload.get("reranker", {})
    if not isinstance(embedding, dict) or not isinstance(reranker, dict):
        raise TypeError("本地模型配置的 embedding/reranker 必须是 TOML table")
    return CollectorConfig(
        weaviate_base_url=_configured_value(
            "WEAVIATE_BASE_URL",
            {},
            "base_url",
            "http://127.0.0.1:8080",
        ).rstrip("/"),
        ollama_base_url=_configured_value(
            "OLLAMA_BASE_URL",
            embedding,
            "base_url",
            "http://127.0.0.1:11434",
        ).rstrip("/"),
        embedding_model=_configured_value(
            "OLLAMA_EMBEDDING_MODEL",
            embedding,
            "model",
            "feadxus/qwen3-embedding:4b",
        ),
        reranker_base_url=_configured_value(
            "RERANKER_BASE_URL",
            reranker,
            "base_url",
            "http://127.0.0.1:8003",
        ).rstrip("/"),
        reranker_model=_configured_value(
            "RERANKER_MODEL",
            reranker,
            "model",
            "jina-reranker-v3-mlx",
        ),
    )


def _utc_timestamp() -> str:
    """生成带 UTC 时区的实验时间戳。"""

    return datetime.now(UTC).isoformat()


def _dataset_summary(dataset_path: Path, dataset: QualityDataset) -> dict[str, Any]:
    """生成不含问题正文、答案正文和租户原始标识的数据集摘要。"""

    return {
        "dataset_id": dataset.dataset_id,
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "document_count": len(dataset.documents),
        "query_count": len(dataset.queries),
        "language": dataset.language,
    }


def _config_summary(config: CollectorConfig) -> dict[str, str]:
    """只保留可公开记录的模型和服务地址，不读取或输出任何密钥。"""

    return {
        "weaviate_base_url": config.weaviate_base_url,
        "ollama_base_url": config.ollama_base_url,
        "embedding_model": config.embedding_model,
        "reranker_base_url": config.reranker_base_url,
        "reranker_model": config.reranker_model,
    }


def build_execution_plan(
    dataset_path: Path,
    config: CollectorConfig,
    output_dir: Path,
) -> dict[str, Any]:
    """构建默认的只读执行计划，保证未显式执行时不访问真实服务。"""

    dataset = load_quality_dataset(dataset_path)
    return {
        "schema_version": 1,
        "experiment": "weaviate_retrieval_ablation",
        "run_status": "planned",
        "execute_required": True,
        "started_at": _utc_timestamp(),
        "dataset": _dataset_summary(dataset_path, dataset),
        "configuration": _config_summary(config),
        "output_dir": str(output_dir),
        "collection_strategy": "two_isolated_ephemeral_collections",
        "stages": {
            "environment": {"status": "pending"},
            "embedding": {"status": "pending"},
            "collections": {"status": "pending"},
            "indexing": {"status": "pending"},
            "retrieval": {"status": "pending"},
            "reranker": {"status": "pending"},
            "cleanup": {"status": "pending"},
        },
        "claims": {
            "production_quality": False,
            "production_capacity": False,
            "frozen_golden": False,
        },
        "limitations": [
            "数据集是公开合成技术数据，不是人工冻结 golden 集。",
            "执行结果只代表当前本机服务和本次隔离 collection。",
            "本实验不推导 10 万文档、50 QPS、生产 SLO 或托管环境质量。",
        ],
    }


def _ordered_unique(values: Sequence[str]) -> list[str]:
    """按首次出现顺序去重，保留 Dense/BM25F 候选的可解释顺序。"""

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _candidate_documents(
    dataset: QualityDataset,
    dense_ids: Sequence[str],
    sparse_ids: Sequence[str],
) -> tuple[list[str], list[str]]:
    """把双路候选映射为 Reranker 输入，拒绝隔离 collection 外的 chunk。"""

    documents = {item.chunk_id: item for item in dataset.documents}
    candidate_ids = _ordered_unique([*dense_ids, *sparse_ids])
    unknown = sorted(set(candidate_ids) - set(documents))
    if unknown:
        raise ValueError(f"检索结果包含数据集外 chunk: {unknown}")
    return candidate_ids, [documents[item].embedding_text() for item in candidate_ids]


def _reranked_ids(
    candidate_ids: Sequence[str],
    rerank_rows: Sequence[Any],
) -> list[str]:
    """将 HTTP Reranker 的候选索引映射回稳定 chunk ID。"""

    result: list[str] = []
    for row in rerank_rows:
        if row.index < 0 or row.index >= len(candidate_ids):
            raise ValueError("Reranker index 超出本次候选集合")
        result.append(candidate_ids[row.index])
    return result


def build_ablation_input(
    dataset_path: Path,
    query_results: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """生成既有五模式 runner 使用的脱敏输入，不复制查询或文档正文。"""

    dataset = load_quality_dataset(dataset_path)
    selected_queries = []
    observed_chunk_ids: list[str] = []
    for query in dataset.queries:
        result = query_results.get(query.query_id)
        if result is None:
            continue
        rankings = {
            "dense": list(result.get("dense", [])),
            "bm25f": list(result.get("bm25f", [])),
            "reranker": list(result.get("reranker", [])),
        }
        for ranking in rankings.values():
            observed_chunk_ids.extend(ranking)
        observed_chunk_ids.extend(query.relevant_chunk_ids)
        selected_queries.append(
            {
                "query_id": query.query_id,
                "relevant_chunk_ids": list(query.relevant_chunk_ids),
                **rankings,
                "latency_ms": dict(result.get("latency_ms", {})),
            }
        )
    if not selected_queries:
        raise ValueError("真实采集没有产生任何 query 结果")
    return {
        "schema_version": 1,
        "dataset_id": dataset.dataset_id,
        "dataset_kind": "real-local-synthetic",
        "chunk_ids": _ordered_unique(observed_chunk_ids),
        "queries": selected_queries,
    }


def safe_error_kind(error: BaseException) -> str:
    """把异常归类为稳定标签，禁止把 URL、响应正文或凭据写入报告。"""

    if isinstance(error, WeaviateTransportError):
        return f"weaviate_{error.kind}"
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.HTTPStatusError):
        return "http_status_error"
    if isinstance(error, httpx.HTTPError):
        return "http_error"
    if isinstance(error, ValueError):
        return "validation_error"
    return "runtime_error"


def _elapsed_ms(started: float) -> float:
    """将单阶段 monotonic 计时转换为有限的毫秒浮点数。"""

    elapsed = (time.perf_counter() - started) * 1000
    if not math.isfinite(elapsed) or elapsed < 0:
        raise RuntimeError("实验阶段计时非法")
    return round(elapsed, 3)


async def run_real_collection(
    dataset_path: Path,
    config: CollectorConfig,
    *,
    candidate_k: int = 5,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """执行隔离 collection 采集，并在成功后返回执行摘要与消融输入。"""

    if candidate_k <= 0:
        raise ValueError("candidate_k 必须大于 0")
    dataset = load_quality_dataset(dataset_path)
    started_at = _utc_timestamp()
    generation_id = f"ablation-generation-{uuid4().hex}"
    dense_collection = f"AblationDense_{uuid4().hex}"
    sparse_collection = f"AblationSparse_{uuid4().hex}"
    profile = _profile()
    cleanup = {
        "dense_collection_deleted": False,
        "sparse_collection_deleted": False,
    }
    stages: dict[str, Any] = {}
    query_results: dict[str, dict[str, Any]] = {}
    error_kind: str | None = None

    async with httpx.AsyncClient(timeout=60.0) as client:
        embedding_provider = OllamaEmbeddingProvider(
            client,
            config.ollama_base_url,
            config.embedding_model,
        )
        dense_transport = WeaviateHTTPXTransport(
            client,
            config.weaviate_base_url,
            api_key=os.getenv("WEAVIATE_API_KEY"),
        )
        sparse_transport = WeaviateBM25FTransport(
            client,
            config.weaviate_base_url,
            api_key=os.getenv("WEAVIATE_API_KEY"),
        )
        reranker = HTTPReranker(
            client,
            config.reranker_base_url,
            config.reranker_model,
        )
        dense_created = False
        sparse_created = False
        try:
            # 先读取环境元数据，失败时只记录安全分类，不读取上游错误正文。
            meta_started = time.perf_counter()
            meta_response = await client.get(
                f"{config.weaviate_base_url}/v1/meta",
                timeout=5.0,
            )
            meta_response.raise_for_status()
            meta = meta_response.json()
            tags_response = await client.get(
                f"{config.ollama_base_url}/api/tags",
                timeout=5.0,
            )
            tags_response.raise_for_status()
            tags = tags_response.json()
            model_digest = next(
                (
                    item.get("digest")
                    for item in tags.get("models", [])
                    if isinstance(item, Mapping)
                    and item.get("name") == config.embedding_model
                ),
                None,
            )
            stages["environment"] = {
                "status": "passed",
                "elapsed_ms": _elapsed_ms(meta_started),
                "weaviate_version": str(meta.get("version", "unknown")),
                "embedding_model": config.embedding_model,
                "embedding_model_digest": model_digest,
            }

            # 一次批量生成公开合成文档和查询向量，避免把正文写入结果产物。
            embedding_started = time.perf_counter()
            embedding_texts = [
                item.embedding_text() for item in dataset.documents
            ] + [item.text for item in dataset.queries]
            vectors = await embedding_provider.embed(embedding_texts)
            document_vectors = vectors[: len(dataset.documents)]
            query_vectors = vectors[len(dataset.documents) :]
            stages["embedding"] = {
                "status": "passed",
                "elapsed_ms": _elapsed_ms(embedding_started),
                "input_count": len(embedding_texts),
                "dimension": len(vectors[0]),
            }

            await dense_transport.ensure_collection(dense_collection, "dense-v1")
            dense_created = True
            await sparse_transport.ensure_collection(
                sparse_collection,
                profile.schema_version,
            )
            sparse_created = True
            stages["collections"] = {
                "status": "passed",
                "isolated": True,
                "count": 2,
            }

            dense_result = await dense_transport.upsert_batch(
                dense_collection,
                _dense_objects(
                    dataset,
                    document_vectors,
                    generation_id=generation_id,
                    model=config.embedding_model,
                ),
            )
            sparse_result = await sparse_transport.upsert_batch(
                sparse_collection,
                _sparse_objects(
                    dataset,
                    generation_id=generation_id,
                    profile=profile,
                ),
            )
            stages["indexing"] = {
                "status": "passed",
                "dense_accepted": dense_result.accepted,
                "sparse_accepted": sparse_result,
            }

            if not await reranker.health_check():
                raise RuntimeError("reranker health check failed")
            stages["reranker"] = {
                "status": "passed",
                "model": config.reranker_model,
            }

            for query, query_vector in zip(dataset.queries, query_vectors):
                dense_started = time.perf_counter()
                dense_rows = await dense_transport.search(
                    dense_collection,
                    generation_id=generation_id,
                    version_id=dataset.version_id,
                    query=query_vector,
                    top_k=candidate_k,
                )
                dense_ms = _elapsed_ms(dense_started)

                sparse_started = time.perf_counter()
                sparse_rows = await sparse_transport.search(
                    sparse_collection,
                    profile=profile,
                    tenant_id=dataset.tenant_id,
                    generation_id=generation_id,
                    version_id=dataset.version_id,
                    query=query.text,
                    top_k=candidate_k,
                )
                sparse_ms = _elapsed_ms(sparse_started)

                dense_hits = _channel_hits_from_dense(dense_rows)
                sparse_hits = _channel_hits_from_sparse(sparse_rows)
                dense_ids = [item.chunk_id for item in dense_hits]
                sparse_ids = [item.chunk_id for item in sparse_hits]
                candidate_ids, candidate_documents = _candidate_documents(
                    dataset,
                    dense_ids,
                    sparse_ids,
                )

                rerank_started = time.perf_counter()
                rerank_rows = await reranker.rerank(
                    RerankRequest(
                        query=query.text,
                        documents=candidate_documents,
                        top_k=candidate_k,
                    )
                )
                rerank_ms = _elapsed_ms(rerank_started)

                union_started = time.perf_counter()
                union_ms = _elapsed_ms(union_started)
                query_results[query.query_id] = {
                    "dense": dense_ids,
                    "bm25f": sparse_ids,
                    "reranker": _reranked_ids(candidate_ids, rerank_rows),
                    "latency_ms": {
                        "dense": dense_ms,
                        "bm25f": sparse_ms,
                        "dense_bm25f": union_ms,
                        "rrf_reranker": rerank_ms,
                    },
                }
            stages["retrieval"] = {
                "status": "passed",
                "query_count": len(query_results),
                "candidate_k": candidate_k,
            }
        except Exception as error:  # noqa: BLE001 -- 采集失败必须写安全摘要并清理资源。
            error_kind = safe_error_kind(error)
            failed_stage = "retrieval"
            if "retrieval" not in stages:
                failed_stage = "indexing" if "indexing" not in stages else "reranker"
            stages[failed_stage] = {"status": "failed", "error_kind": error_kind}
        finally:
            # 两个 collection 只属于本次实验；无论业务阶段是否失败都尽力清理。
            for created, collection_name, key, transport in (
                (
                    dense_created,
                    dense_collection,
                    "dense_collection_deleted",
                    dense_transport,
                ),
                (
                    sparse_created,
                    sparse_collection,
                    "sparse_collection_deleted",
                    sparse_transport,
                ),
            ):
                if not created:
                    continue
                try:
                    await transport.delete_collection(collection_name)
                    cleanup[key] = True
                except Exception:  # noqa: BLE001 -- 清理失败不能覆盖主实验结果。
                    cleanup[key] = False
            stages["cleanup"] = {
                "status": "passed" if all(cleanup.values()) else "failed",
                **cleanup,
            }

    execution = {
        "schema_version": 1,
        "experiment": "weaviate_retrieval_ablation",
        "run_status": "failed" if error_kind else "passed",
        "started_at": started_at,
        "finished_at": _utc_timestamp(),
        "dataset": _dataset_summary(dataset_path, dataset),
        "configuration": _config_summary(config),
        "collection_strategy": "two_isolated_ephemeral_collections",
        "stages": stages,
        "cleanup": cleanup,
        "runtime": {
            "python": platform.python_version(),
            "httpx": version("httpx"),
        },
        "claims": {
            "production_quality": False,
            "production_capacity": False,
            "frozen_golden": False,
        },
        "limitations": [
            "输入是公开合成数据，不能替代人工冻结 golden 集。",
            "结果只描述本机真实服务和本次隔离 collection。",
        ],
    }
    if error_kind:
        execution["error_kind"] = error_kind
        return execution, None
    return execution, build_ablation_input(dataset_path, query_results)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """以固定 UTF-8 格式写入实验 JSON，便于人工审阅和后续归档。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_plan_artifacts(output_dir: Path, plan: Mapping[str, Any]) -> None:
    """写入默认计划和边界说明，不产生任何远程副作用。"""

    _write_json(output_dir / "plan.json", plan)
    (output_dir / "README.md").write_text(
        "# 真实检索消融采集计划\n\n"
        "默认命令只生成本计划，不访问 Ollama、Weaviate 或 Jina。\n\n"
        "显式执行需要追加 `--execute`；执行只使用一次性 Dense/Sparse collection，"
        "结束后尝试清理，结果不代表生产质量或容量。\n",
        encoding="utf-8",
    )


def _write_execution_artifacts(
    output_dir: Path,
    execution: Mapping[str, Any],
    ablation_input: Mapping[str, Any] | None,
) -> int:
    """写入真实采集脱敏产物，并在成功时运行统一五模式消融。"""

    _write_json(output_dir / "execution.json", execution)
    if ablation_input is None:
        return 2
    _write_json(output_dir / "ablation-input.json", ablation_input)
    report = run_retrieval_ablation(
        output_dir / "ablation-input.json",
        output_path=output_dir / "metrics.json",
        k=5,
        rrf_k=60,
    )
    _write_json(
        output_dir / "verification.json",
        {
            "run_status": "passed",
            "execution_run_status": execution["run_status"],
            "ablation_modes": list(report["modes"]),
            "claims": execution["claims"],
            "limitations": execution["limitations"],
        },
    )
    return 0


def main() -> int:
    """解析参数，默认仅输出计划，显式执行后归档脱敏结果。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--config-file",
        type=Path,
        default=Path(os.getenv("LOCAL_LLM_CONFIG_FILE", DEFAULT_CONFIG_FILE)),
    )
    parser.add_argument("--candidate-k", type=int, default=5)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="显式访问本机服务并创建一次性隔离 collection",
    )
    args = parser.parse_args()
    config = load_collector_config(args.config_file)
    if not args.execute:
        _write_plan_artifacts(
            args.output_dir,
            build_execution_plan(args.dataset, config, args.output_dir),
        )
        print(json.dumps({"run_status": "planned", "output_dir": str(args.output_dir)}))
        return 0

    execution, ablation_input = __import__("asyncio").run(
        run_real_collection(
            args.dataset,
            config,
            candidate_k=args.candidate_k,
        )
    )
    return _write_execution_artifacts(args.output_dir, execution, ablation_input)


if __name__ == "__main__":
    raise SystemExit(main())
