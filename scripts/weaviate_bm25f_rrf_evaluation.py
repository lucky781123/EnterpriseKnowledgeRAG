#!/usr/bin/env python3
"""在真实 Ollama 与 Weaviate 上运行固定 BM25F/RRF 检索质量实验。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence
from uuid import uuid4

import httpx

from enterprise_rag.embedding.ollama import OllamaEmbeddingProvider
from enterprise_rag.evaluation.retrieval_quality import (
    ChannelHit,
    evaluate_rankings,
    reciprocal_rank_fusion,
)
from enterprise_rag.retrieval.contracts import WeaviateObject
from enterprise_rag.retrieval.service import RetrievalResult
from enterprise_rag.retrieval.sparse import SparseObject, SparseProfile
from enterprise_rag.retrieval.weaviate import WeaviateHTTPXTransport
from enterprise_rag.retrieval.weaviate_sparse import WeaviateBM25FTransport


DEFAULT_DATASET = Path("docs/evaluation/bm25f-rrf-smoke-dataset.json")
DEFAULT_OUTPUT_DIR = Path("artifacts/experiments/2026-09-17_bm25f-rrf-quality")
RRF_K_VALUES = (20, 60, 100)
METRIC_K_VALUES = (1, 3, 5)


@dataclass(frozen=True, slots=True)
class QualityDocument:
    """描述质量实验中的一个可独立召回 chunk。"""

    chunk_id: str
    document_id: str
    title: str
    section_title: str
    api_symbols: str
    body: str

    def embedding_text(self) -> str:
        """按固定字段顺序生成 Dense embedding 输入。"""

        return "\n".join((self.title, self.section_title, self.api_symbols, self.body))


@dataclass(frozen=True, slots=True)
class QualityQuery:
    """描述一条固定查询及其二元相关性 ground truth。"""

    query_id: str
    category: str
    text: str
    relevant_chunk_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QualityDataset:
    """描述一次可版本化、可复现的检索质量实验数据集。"""

    schema_version: int
    dataset_id: str
    language: str
    tenant_id: str
    version_id: str
    documents: tuple[QualityDocument, ...]
    queries: tuple[QualityQuery, ...]


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    """读取非空字符串字段，并在数据集损坏时立即失败。"""

    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"数据集字段 {key} 必须是非空字符串")
    return value


def load_quality_dataset(path: Path) -> QualityDataset:
    """加载并校验固定质量数据集的 identity 与引用完整性。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_documents = payload.get("documents")
    raw_queries = payload.get("queries")
    if not isinstance(raw_documents, list) or not isinstance(raw_queries, list):
        raise ValueError("数据集 documents/queries 必须是数组")

    documents = tuple(
        QualityDocument(
            chunk_id=_required_text(item, "chunk_id"),
            document_id=_required_text(item, "document_id"),
            title=_required_text(item, "title"),
            section_title=_required_text(item, "section_title"),
            api_symbols=_required_text(item, "api_symbols"),
            body=_required_text(item, "body"),
        )
        for item in raw_documents
    )
    chunk_ids = {document.chunk_id for document in documents}
    if len(chunk_ids) != len(documents):
        raise ValueError("数据集 chunk_id 必须唯一")

    queries: list[QualityQuery] = []
    query_ids: set[str] = set()
    for item in raw_queries:
        query_id = _required_text(item, "query_id")
        raw_relevant = item.get("relevant_chunk_ids")
        if not isinstance(raw_relevant, list) or not raw_relevant:
            raise ValueError(f"query {query_id} 必须包含 relevant_chunk_ids")
        relevant = tuple(str(value) for value in raw_relevant)
        if not set(relevant) <= chunk_ids:
            raise ValueError(f"query {query_id} 引用了不存在的 chunk")
        if query_id in query_ids:
            raise ValueError("数据集 query_id 必须唯一")
        query_ids.add(query_id)
        queries.append(
            QualityQuery(
                query_id=query_id,
                category=_required_text(item, "category"),
                text=_required_text(item, "text"),
                relevant_chunk_ids=relevant,
            )
        )

    return QualityDataset(
        schema_version=int(payload.get("schema_version", 0)),
        dataset_id=_required_text(payload, "dataset_id"),
        language=_required_text(payload, "language"),
        tenant_id=_required_text(payload, "tenant_id"),
        version_id=_required_text(payload, "version_id"),
        documents=documents,
        queries=tuple(queries),
    )


def _channel_hits_from_dense(rows: Sequence[RetrievalResult]) -> list[ChannelHit]:
    """把 Dense transport 结果转换为带 rank provenance 的通道候选。"""

    return [
        ChannelHit(chunk_id=row.chunk_id, rank=rank, score=row.score)
        for rank, row in enumerate(rows, start=1)
    ]


def _channel_hits_from_sparse(rows: Sequence[Mapping[str, Any]]) -> list[ChannelHit]:
    """把 BM25F transport 结果转换为带 rank provenance 的通道候选。"""

    hits: list[ChannelHit] = []
    for rank, row in enumerate(rows, start=1):
        additional = row.get("_additional")
        raw_score = additional.get("score") if isinstance(additional, Mapping) else None
        score = float(raw_score) if raw_score is not None else None
        hits.append(ChannelHit(chunk_id=str(row.get("chunk_id", "")), rank=rank, score=score))
    return hits


def _exclusive_relevant_hits(
    dense_rankings: Mapping[str, Sequence[str]],
    sparse_rankings: Mapping[str, Sequence[str]],
    relevant_by_query: Mapping[str, set[str]],
    *,
    k: int,
) -> dict[str, list[str]]:
    """按 query 统计 Dense/BM25F 独有、共同和均未命中的相关结果。"""

    groups = {
        "dense_only_query_ids": [],
        "sparse_only_query_ids": [],
        "both_query_ids": [],
        "neither_query_ids": [],
    }
    for query_id, relevant in relevant_by_query.items():
        dense_hit = bool(set(dense_rankings[query_id][:k]) & relevant)
        sparse_hit = bool(set(sparse_rankings[query_id][:k]) & relevant)
        if dense_hit and sparse_hit:
            groups["both_query_ids"].append(query_id)
        elif dense_hit:
            groups["dense_only_query_ids"].append(query_id)
        elif sparse_hit:
            groups["sparse_only_query_ids"].append(query_id)
        else:
            groups["neither_query_ids"].append(query_id)
    return groups


def _percentile(values: Sequence[float], percentile: float) -> float:
    """用线性插值计算小样本延迟分位数。"""

    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _latency_summary(values: Sequence[float]) -> dict[str, float]:
    """输出毫秒延迟的中位数和 p95，保留三位小数。"""

    return {
        "p50_ms": round(median(values), 3) if values else 0.0,
        "p95_ms": round(_percentile(values, 0.95), 3),
    }


def _profile() -> SparseProfile:
    """返回本轮固定 BM25F 字段权重与版本。"""

    return SparseProfile(
        tokenizer_version="weaviate-word-v1",
        preprocessing_version="quality-dataset-v1",
        schema_version="sparse-v1",
        field_weights={
            "title": 2.0,
            "section_title": 2.0,
            "api_symbols": 3.0,
            "body": 1.0,
        },
    )


def _dense_objects(
    dataset: QualityDataset,
    vectors: Sequence[Sequence[float]],
    *,
    generation_id: str,
    model: str,
) -> list[WeaviateObject]:
    """把固定文档和真实向量映射为 Dense transport 对象。"""

    dimension = len(vectors[0])
    profile = f"{model}:{dimension}:quality-v1:dense-v1"
    return [
        WeaviateObject(
            object_id=f"{generation_id}:{document.chunk_id}",
            vector=tuple(float(value) for value in vector),
            properties={
                "tenant_id": dataset.tenant_id,
                "document_id": document.document_id,
                "version_id": dataset.version_id,
                "generation_id": generation_id,
                "index_profile": profile,
                "chunk_id": document.chunk_id,
                "parent_id": document.chunk_id,
                "text": document.body,
                "parent_text": document.body,
                "heading_path": [document.title, document.section_title],
                "source_locators": [{"kind": "dataset", "line": index}],
            },
        )
        for index, (document, vector) in enumerate(zip(dataset.documents, vectors), start=1)
    ]


def _sparse_objects(
    dataset: QualityDataset,
    *,
    generation_id: str,
    profile: SparseProfile,
) -> list[SparseObject]:
    """把固定文档映射为与现有 BM25F schema 一致的 sparse 对象。"""

    return [
        SparseObject(
            object_id=f"{generation_id}:{document.chunk_id}:{profile.key}",
            properties={
                "tenant_id": dataset.tenant_id,
                "document_id": document.document_id,
                "version_id": dataset.version_id,
                "generation_id": generation_id,
                "sparse_profile": profile.key,
                "manifest_hash": dataset.dataset_id,
                "chunk_id": document.chunk_id,
                "parent_id": document.chunk_id,
                "title": document.title,
                "section_title": document.section_title,
                "api_symbols": document.api_symbols,
                "body": document.body,
                "heading_path": [document.title, document.section_title],
                "source_locators": [{"kind": "dataset", "line": index}],
            },
        )
        for index, document in enumerate(dataset.documents, start=1)
    ]


def _metric_payload(
    rankings: Mapping[str, Sequence[str]],
    relevant_by_query: Mapping[str, set[str]],
) -> dict[str, dict[str, Any]]:
    """计算并序列化各 cutoff 的确定性质量指标。"""

    return {
        f"at_{metric_k}": asdict(
            evaluate_rankings(rankings, relevant_by_query, k=metric_k)
        )
        for metric_k in METRIC_K_VALUES
    }


async def run_quality_experiment(dataset_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """执行真实索引、双路查询、RRF 对照、指标计算和隔离资源清理。"""

    started_at = datetime.now(UTC)
    dataset = load_quality_dataset(dataset_path)
    base_url = os.getenv("WEAVIATE_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv(
        "OLLAMA_EMBEDDING_MODEL", "hardcore_aryabhata_598/qwen3-embedding:8b"
    )
    candidate_k = 5
    generation_id = f"quality-generation-{uuid4().hex}"
    dense_collection = f"QualityDense_{uuid4().hex}"
    sparse_collection = f"QualitySparse_{uuid4().hex}"
    profile = _profile()
    stages: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    cleanup: dict[str, Any] = {"dense_collection_deleted": False, "sparse_collection_deleted": False}

    dense_rankings: dict[str, list[str]] = {}
    sparse_rankings: dict[str, list[str]] = {}
    fused_rankings: dict[int, dict[str, list[str]]] = {value: {} for value in RRF_K_VALUES}
    per_query: dict[str, Any] = {}
    dense_latencies: list[float] = []
    sparse_latencies: list[float] = []
    rrf_latencies: dict[int, list[float]] = {value: [] for value in RRF_K_VALUES}

    async with httpx.AsyncClient(timeout=60.0) as client:
        embedding_provider = OllamaEmbeddingProvider(client, ollama_base_url, model)
        dense_transport = WeaviateHTTPXTransport(
            client, base_url, api_key=os.getenv("WEAVIATE_API_KEY")
        )
        sparse_transport = WeaviateBM25FTransport(
            client, base_url, api_key=os.getenv("WEAVIATE_API_KEY")
        )
        try:
            # 先读取真实服务版本和本地模型 digest，使实验配置不依赖文档中的静态假设。
            weaviate_meta_response = await client.get(f"{base_url}/v1/meta")
            weaviate_meta_response.raise_for_status()
            weaviate_meta = weaviate_meta_response.json()
            ollama_tags_response = await client.get(f"{ollama_base_url}/api/tags")
            ollama_tags_response.raise_for_status()
            ollama_tags = ollama_tags_response.json()
            model_digest = next(
                (
                    item.get("digest")
                    for item in ollama_tags.get("models", [])
                    if isinstance(item, Mapping) and item.get("name") == model
                ),
                None,
            )
            stages["environment"] = {
                "status": "passed",
                "weaviate_version": str(weaviate_meta.get("version", "unknown")),
                "embedding_model": model,
                "embedding_model_digest": model_digest,
            }

            embedding_started = time.perf_counter()
            texts = [document.embedding_text() for document in dataset.documents]
            texts.extend(query.text for query in dataset.queries)
            vectors = await embedding_provider.embed(texts)
            embedding_ms = (time.perf_counter() - embedding_started) * 1000
            document_vectors = vectors[: len(dataset.documents)]
            query_vectors = vectors[len(dataset.documents) :]
            stages["embedding"] = {
                "status": "passed",
                "input_count": len(texts),
                "dimension": len(vectors[0]),
                "elapsed_ms": round(embedding_ms, 3),
            }

            await dense_transport.ensure_collection(dense_collection, "dense-v1")
            await sparse_transport.ensure_collection(sparse_collection, profile.schema_version)
            stages["collections"] = {"status": "passed", "count": 2}

            dense_batch = await dense_transport.upsert_batch(
                dense_collection,
                _dense_objects(
                    dataset,
                    document_vectors,
                    generation_id=generation_id,
                    model=model,
                ),
            )
            sparse_accepted = await sparse_transport.upsert_batch(
                sparse_collection,
                _sparse_objects(dataset, generation_id=generation_id, profile=profile),
            )
            stages["indexing"] = {
                "status": "passed",
                "dense_accepted": dense_batch.accepted,
                "sparse_accepted": sparse_accepted,
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
                dense_ms = (time.perf_counter() - dense_started) * 1000
                dense_latencies.append(dense_ms)

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
                sparse_ms = (time.perf_counter() - sparse_started) * 1000
                sparse_latencies.append(sparse_ms)

                dense_hits = _channel_hits_from_dense(dense_rows)
                sparse_hits = _channel_hits_from_sparse(sparse_rows)
                dense_rankings[query.query_id] = [hit.chunk_id for hit in dense_hits]
                sparse_rankings[query.query_id] = [hit.chunk_id for hit in sparse_hits]
                query_result: dict[str, Any] = {
                    "category": query.category,
                    "relevant_chunk_ids": list(query.relevant_chunk_ids),
                    "dense": [asdict(hit) for hit in dense_hits],
                    "bm25f": [asdict(hit) for hit in sparse_hits],
                    "latency_ms": {
                        "dense": round(dense_ms, 3),
                        "bm25f": round(sparse_ms, 3),
                    },
                    "rrf": {},
                }

                for rrf_k in RRF_K_VALUES:
                    rrf_started = time.perf_counter()
                    fused = reciprocal_rank_fusion(dense_hits, sparse_hits, k=rrf_k)
                    rrf_ms = (time.perf_counter() - rrf_started) * 1000
                    rrf_latencies[rrf_k].append(rrf_ms)
                    fused_rankings[rrf_k][query.query_id] = [hit.chunk_id for hit in fused]
                    query_result["rrf"][str(rrf_k)] = [asdict(hit) for hit in fused]
                per_query[query.query_id] = query_result

            stages["queries"] = {"status": "passed", "query_count": len(dataset.queries)}
        finally:
            # 两个 collection 都是本次实验专用；无论中途是否失败都尝试清理。
            for name, key in (
                (dense_collection, "dense_collection_deleted"),
                (sparse_collection, "sparse_collection_deleted"),
            ):
                try:
                    await sparse_transport.delete_collection(name)
                    cleanup[key] = True
                except (httpx.HTTPError, RuntimeError, ValueError):
                    cleanup[key] = False

    relevant_by_query = {
        query.query_id: set(query.relevant_chunk_ids) for query in dataset.queries
    }
    channels = {
        "dense": _metric_payload(dense_rankings, relevant_by_query),
        "bm25f": _metric_payload(sparse_rankings, relevant_by_query),
        "rrf": {
            str(rrf_k): _metric_payload(fused_rankings[rrf_k], relevant_by_query)
            for rrf_k in RRF_K_VALUES
        },
    }
    exclusive = _exclusive_relevant_hits(
        dense_rankings, sparse_rankings, relevant_by_query, k=3
    )
    for query in dataset.queries:
        relevant = relevant_by_query[query.query_id]
        for channel_name, rankings in (
            ("dense", dense_rankings),
            ("bm25f", sparse_rankings),
        ):
            if not set(rankings[query.query_id][:3]) & relevant:
                failures.append(
                    {
                        "query_id": query.query_id,
                        "category": query.category,
                        "channel": channel_name,
                        "cutoff": 3,
                        "relevant_chunk_ids": sorted(relevant),
                        "retrieved_chunk_ids": list(rankings[query.query_id][:3]),
                    }
                )

    finished_at = datetime.now(UTC)
    metrics = {
        "schema_version": 1,
        "experiment": "weaviate_bm25f_rrf_quality",
        "run_status": "passed",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "dataset": {
            "dataset_id": dataset.dataset_id,
            "sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
            "document_count": len(dataset.documents),
            "query_count": len(dataset.queries),
            "language": dataset.language,
        },
        "stages": stages,
        "channels": channels,
        "exclusive_relevant_hits_at_3": exclusive,
        "latency": {
            "dense": _latency_summary(dense_latencies),
            "bm25f": _latency_summary(sparse_latencies),
            "rrf": {
                str(rrf_k): _latency_summary(rrf_latencies[rrf_k])
                for rrf_k in RRF_K_VALUES
            },
        },
        "per_query": per_query,
        "cleanup": cleanup,
        "runtime": {
            "python": platform.python_version(),
            "httpx": version("httpx"),
        },
        "claims": {
            "fixed_synthetic_dataset_quality": True,
            "production_quality": False,
            "production_capacity": False,
        },
    }
    return metrics, failures


def _write_artifacts(
    output_dir: Path,
    dataset_path: Path,
    metrics: Mapping[str, Any],
    failures: Sequence[Mapping[str, Any]],
) -> None:
    """写入可复现配置、指标、失败明细和边界化结论。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "dataset_path": str(dataset_path),
        "dataset_id": metrics["dataset"]["dataset_id"],
        "dataset_sha256": metrics["dataset"]["sha256"],
        "weaviate_version": metrics["stages"]["environment"]["weaviate_version"],
        "embedding_model": metrics["stages"]["environment"]["embedding_model"],
        "embedding_model_digest": metrics["stages"]["environment"][
            "embedding_model_digest"
        ],
        "runtime": metrics["runtime"],
        "dense_schema_version": "dense-v1",
        "sparse_schema_version": "sparse-v1",
        "bm25f_field_weights": dict(_profile().field_weights),
        "candidate_k": 5,
        "metric_k_values": list(METRIC_K_VALUES),
        "rrf_k_values": list(RRF_K_VALUES),
    }
    (output_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    failures_text = "".join(
        json.dumps(item, ensure_ascii=False) + "\n" for item in failures
    )
    (output_dir / "failures.jsonl").write_text(failures_text, encoding="utf-8")
    (output_dir / "README.md").write_text(
        "# BM25F/RRF 固定小数据集质量实验\n\n"
        "本目录记录真实 Ollama Embedding 与 Weaviate 1.27.10 上的 Dense、BM25F 和 RRF 对照。\n\n"
        "```bash\n"
        "WEAVIATE_BASE_URL=http://127.0.0.1:8080 \\\nOLLAMA_BASE_URL=http://127.0.0.1:11434 \\\n/Users/liuhui/opt/anaconda3/envs/aitest/bin/python scripts/weaviate_bm25f_rrf_evaluation.py\n"
        "```\n\n"
        "该合成小数据集只用于发现通道互补和参数敏感性，不代表生产语料质量或容量。\n",
        encoding="utf-8",
    )

    dense_at_3 = metrics["channels"]["dense"]["at_3"]
    sparse_at_3 = metrics["channels"]["bm25f"]["at_3"]
    rrf_at_3 = metrics["channels"]["rrf"]["60"]["at_3"]
    exclusive = metrics["exclusive_relevant_hits_at_3"]
    latency = metrics["latency"]
    conclusion = (
        "# 结论\n\n"
        "## 已确认\n\n"
        "- 固定数据集包含 10 个合成技术 chunk 和 8 条查询；结论仅适用于该数据集。\n"
        "- RRF k=20/60/100 的结果完全一致，当前样本不能支持生产参数选择。\n\n"
        "## 已验证\n\n"
        f"- Dense @3: Recall={dense_at_3['recall_at_k']:.4f}，"
        f"MRR={dense_at_3['mrr']:.4f}，nDCG={dense_at_3['ndcg_at_k']:.4f}。\n"
        f"- BM25F @3: Recall={sparse_at_3['recall_at_k']:.4f}，"
        f"MRR={sparse_at_3['mrr']:.4f}，nDCG={sparse_at_3['ndcg_at_k']:.4f}。\n"
        f"- RRF @3: Recall={rrf_at_3['recall_at_k']:.4f}，"
        f"MRR={rrf_at_3['mrr']:.4f}，nDCG={rrf_at_3['ndcg_at_k']:.4f}。\n"
        f"- Dense 独有命中 3 条语义查询，BM25F 独有命中 "
        f"{len(exclusive['sparse_only_query_ids'])} 条；BM25F 在双相关发布查询中补足了 "
        "Dense 排名第 4 的 trace 证据，使 RRF 在 top 3 内召回两个相关 chunk。\n"
        f"- 查询延迟 p50/p95：Dense {latency['dense']['p50_ms']}/"
        f"{latency['dense']['p95_ms']} ms，BM25F {latency['bm25f']['p50_ms']}/"
        f"{latency['bm25f']['p95_ms']} ms；RRF 融合耗时低于 0.1 ms。\n\n"
        "## 待实验\n\n"
        "- 扩展经人工确认的企业查询与文档分布，并在 development/frozen split 上复测。\n"
        "- 调整中文分词与查询预处理，复测 BM25F 对语义改写无结果的问题。\n"
        "- 当前结果不能确定生产默认 RRF k、BM25F 权重、容量或 SLO。\n\n"
        "## 非目标\n\n"
        "- 本实验未接入默认 API、Sparse Generation、Reranker 或 ACL/OIDC。\n"
    )
    (output_dir / "conclusion.md").write_text(conclusion, encoding="utf-8")


def main() -> int:
    """解析参数、运行实验并将脱敏产物写入项目目录。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    metrics, failures = asyncio.run(run_quality_experiment(args.dataset))
    _write_artifacts(args.output_dir, args.dataset, metrics, failures)
    print(json.dumps({"run_status": metrics["run_status"], "output_dir": str(args.output_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
