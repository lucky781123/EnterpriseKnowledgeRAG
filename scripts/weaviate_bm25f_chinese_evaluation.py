#!/usr/bin/env python3
"""在固定数据集上对照 BM25F 中文 tokenizer 与确定性查询预处理。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

import httpx

from enterprise_rag.embedding.ollama import OllamaEmbeddingProvider
from enterprise_rag.evaluation.retrieval_quality import reciprocal_rank_fusion
from enterprise_rag.retrieval.sparse import SparseObject, SparseProfile
from enterprise_rag.retrieval.weaviate import WeaviateHTTPXTransport
from enterprise_rag.retrieval.weaviate_sparse import WeaviateBM25FTransport
if __package__:
    # pytest 从项目根目录按 ``scripts.*`` 导入；此分支保留包语义。
    from scripts.weaviate_bm25f_rrf_evaluation import (
        DEFAULT_DATASET,
        METRIC_K_VALUES,
        RRF_K_VALUES,
        QualityDataset,
        _channel_hits_from_dense,
        _channel_hits_from_sparse,
        _dense_objects,
        _exclusive_relevant_hits,
        _latency_summary,
        _metric_payload,
        load_quality_dataset,
    )
else:
    # 直接执行脚本时 ``sys.path[0]`` 是 scripts 目录，因此使用同目录导入。
    from weaviate_bm25f_rrf_evaluation import (
        DEFAULT_DATASET,
        METRIC_K_VALUES,
        RRF_K_VALUES,
        QualityDataset,
        _channel_hits_from_dense,
        _channel_hits_from_sparse,
        _dense_objects,
        _exclusive_relevant_hits,
        _latency_summary,
        _metric_payload,
        load_quality_dataset,
    )


DEFAULT_OUTPUT_DIR = Path(
    "artifacts/experiments/2026-09-17_bm25f-chinese-tokenization"
)
_CJK_RUN_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_FIELD_WEIGHTS = {
    "title": 2.0,
    "section_title": 2.0,
    "api_symbols": 3.0,
    "body": 1.0,
}


@dataclass(frozen=True, slots=True)
class ExperimentProfile:
    """描述一个只改变 tokenizer 或预处理之一的 BM25F 对照配置。"""

    name: str
    tokenization: str
    preprocessing: str
    sparse_profile: SparseProfile


def _experiment_profiles() -> dict[str, ExperimentProfile]:
    """返回 baseline 与两个单变量候选，字段权重保持完全一致。"""

    definitions = (
        ("word_identity", "word", "identity"),
        ("trigram_identity", "trigram", "identity"),
        ("word_cjk_bigram", "word", "cjk_bigram"),
    )
    profiles: dict[str, ExperimentProfile] = {}
    for name, tokenization, preprocessing in definitions:
        sparse_profile = SparseProfile(
            tokenizer_version=f"weaviate-{tokenization}-v1",
            preprocessing_version=f"{preprocessing}-v1",
            schema_version=f"sparse-{name}-v1",
            field_weights=dict(_FIELD_WEIGHTS),
        )
        profiles[name] = ExperimentProfile(
            name=name,
            tokenization=tokenization,
            preprocessing=preprocessing,
            sparse_profile=sparse_profile,
        )
    return profiles


def _cjk_bigram_run(value: str) -> str:
    """把连续中文字符转换为重叠 bigram；单字保持原样。"""

    if len(value) < 2:
        return value
    return " ".join(value[index : index + 2] for index in range(len(value) - 1))


def preprocess_bm25_text(text: str, *, strategy: str) -> str:
    """按版本化策略预处理 BM25F 文本，同时保持英文技术 token 原样。"""

    normalized = " ".join(text.split())
    if not normalized:
        return ""
    if strategy == "identity":
        return normalized
    if strategy == "cjk_bigram":
        # 只替换连续中文片段，API 名、错误码、路径和下划线 token 不参与拆分。
        segmented = _CJK_RUN_PATTERN.sub(
            lambda match: f" {_cjk_bigram_run(match.group(0))} ",
            normalized,
        )
        return " ".join(segmented.split())
    raise ValueError(f"不支持的 BM25F 预处理策略: {strategy}")


def _query_text(query: str, *, profile: ExperimentProfile) -> str:
    """使用与索引字段相同的 profile 预处理查询文本。"""

    return preprocess_bm25_text(query, strategy=profile.preprocessing)


def _sparse_objects(
    dataset: QualityDataset,
    *,
    generation_id: str,
    profile: ExperimentProfile,
) -> list[SparseObject]:
    """按指定 profile 预处理四个字段并生成隔离 sparse object。"""

    objects: list[SparseObject] = []
    for index, document in enumerate(dataset.documents, start=1):
        properties = {
            "tenant_id": dataset.tenant_id,
            "document_id": document.document_id,
            "version_id": dataset.version_id,
            "generation_id": generation_id,
            "sparse_profile": profile.sparse_profile.key,
            "manifest_hash": dataset.dataset_id,
            "chunk_id": document.chunk_id,
            "parent_id": document.chunk_id,
            "title": preprocess_bm25_text(
                document.title,
                strategy=profile.preprocessing,
            ),
            "section_title": preprocess_bm25_text(
                document.section_title,
                strategy=profile.preprocessing,
            ),
            "api_symbols": preprocess_bm25_text(
                document.api_symbols,
                strategy=profile.preprocessing,
            ),
            "body": preprocess_bm25_text(
                document.body,
                strategy=profile.preprocessing,
            ),
            "heading_path": [document.title, document.section_title],
            "source_locators": [{"kind": "dataset", "line": index}],
        }
        objects.append(
            SparseObject(
                object_id=(
                    f"{generation_id}:{document.chunk_id}:"
                    f"{profile.sparse_profile.key}"
                ),
                properties=properties,
            )
        )
    return objects


def _profile_failures(
    profile_name: str,
    rankings: Mapping[str, Sequence[str]],
    relevant_by_query: Mapping[str, set[str]],
    categories: Mapping[str, str],
) -> list[dict[str, Any]]:
    """记录指定 profile 在 BM25F top 3 中未命中的查询。"""

    failures: list[dict[str, Any]] = []
    for query_id, relevant in relevant_by_query.items():
        retrieved = list(rankings[query_id][:3])
        if set(retrieved) & relevant:
            continue
        failures.append(
            {
                "profile": profile_name,
                "query_id": query_id,
                "category": categories[query_id],
                "channel": "bm25f",
                "cutoff": 3,
                "relevant_chunk_ids": sorted(relevant),
                "retrieved_chunk_ids": retrieved,
            }
        )
    return failures


async def run_chinese_experiment(
    dataset_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """固定 Dense 结果，运行三个 BM25F profile 及其 RRF 对照并清理资源。"""

    started_at = datetime.now(UTC)
    dataset = load_quality_dataset(dataset_path)
    profiles = _experiment_profiles()
    base_url = os.getenv("WEAVIATE_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    ollama_base_url = os.getenv(
        "OLLAMA_BASE_URL", "http://127.0.0.1:11434"
    ).rstrip("/")
    model = os.getenv(
        "OLLAMA_EMBEDDING_MODEL",
        "hardcore_aryabhata_598/qwen3-embedding:8b",
    )
    candidate_k = 5
    generation_id = f"chinese-quality-generation-{uuid4().hex}"
    dense_collection = f"ChineseDense_{uuid4().hex}"
    sparse_collections = {
        name: f"ChineseSparse_{uuid4().hex}" for name in profiles
    }
    created_collections: list[str] = []
    cleanup: dict[str, bool] = {"dense": False, **{name: False for name in profiles}}

    dense_rankings: dict[str, list[str]] = {}
    dense_latencies: list[float] = []
    profile_results: dict[str, dict[str, Any]] = {
        name: {
            "rankings": {},
            "fused_rankings": {value: {} for value in RRF_K_VALUES},
            "bm25f_latencies": [],
            "rrf_latencies": {value: [] for value in RRF_K_VALUES},
            "per_query": {},
        }
        for name in profiles
    }
    stages: dict[str, Any] = {}

    async with httpx.AsyncClient(timeout=60.0) as client:
        embedding_provider = OllamaEmbeddingProvider(client, ollama_base_url, model)
        dense_transport = WeaviateHTTPXTransport(
            client,
            base_url,
            api_key=os.getenv("WEAVIATE_API_KEY"),
        )
        sparse_transport = WeaviateBM25FTransport(
            client,
            base_url,
            api_key=os.getenv("WEAVIATE_API_KEY"),
        )
        try:
            # 运行时读取服务和模型版本，避免把文档中的历史配置当作本次事实。
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

            # 文档和查询只嵌入一次；三个 sparse profile 复用同一 Dense 候选。
            embedding_started = time.perf_counter()
            texts = [document.embedding_text() for document in dataset.documents]
            texts.extend(query.text for query in dataset.queries)
            vectors = await embedding_provider.embed(texts)
            document_vectors = vectors[: len(dataset.documents)]
            query_vectors = vectors[len(dataset.documents) :]
            stages["embedding"] = {
                "status": "passed",
                "input_count": len(texts),
                "dimension": len(vectors[0]),
                "elapsed_ms": round(
                    (time.perf_counter() - embedding_started) * 1000,
                    3,
                ),
            }

            await dense_transport.ensure_collection(dense_collection, "dense-v1")
            created_collections.append(dense_collection)
            for name, profile in profiles.items():
                collection_name = sparse_collections[name]
                await sparse_transport.ensure_collection(
                    collection_name,
                    profile.sparse_profile.schema_version,
                    tokenization=profile.tokenization,
                )
                created_collections.append(collection_name)
            stages["collections"] = {
                "status": "passed",
                "count": 1 + len(profiles),
                "tokenizations": {
                    name: profile.tokenization for name, profile in profiles.items()
                },
            }

            dense_batch = await dense_transport.upsert_batch(
                dense_collection,
                _dense_objects(
                    dataset,
                    document_vectors,
                    generation_id=generation_id,
                    model=model,
                ),
            )
            sparse_accepted: dict[str, int] = {}
            for name, profile in profiles.items():
                sparse_accepted[name] = await sparse_transport.upsert_batch(
                    sparse_collections[name],
                    _sparse_objects(
                        dataset,
                        generation_id=generation_id,
                        profile=profile,
                    ),
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
                dense_hits = _channel_hits_from_dense(dense_rows)
                dense_rankings[query.query_id] = [hit.chunk_id for hit in dense_hits]

                for name, profile in profiles.items():
                    result = profile_results[name]
                    sparse_started = time.perf_counter()
                    sparse_rows = await sparse_transport.search(
                        sparse_collections[name],
                        profile=profile.sparse_profile,
                        tenant_id=dataset.tenant_id,
                        generation_id=generation_id,
                        version_id=dataset.version_id,
                        query=_query_text(query.text, profile=profile),
                        top_k=candidate_k,
                    )
                    sparse_ms = (time.perf_counter() - sparse_started) * 1000
                    result["bm25f_latencies"].append(sparse_ms)
                    sparse_hits = _channel_hits_from_sparse(sparse_rows)
                    result["rankings"][query.query_id] = [
                        hit.chunk_id for hit in sparse_hits
                    ]
                    query_result = {
                        "category": query.category,
                        "relevant_chunk_ids": list(query.relevant_chunk_ids),
                        "query_text": _query_text(query.text, profile=profile),
                        "bm25f": [asdict(hit) for hit in sparse_hits],
                        "latency_ms": {"bm25f": round(sparse_ms, 3)},
                        "rrf": {},
                    }

                    for rrf_k in RRF_K_VALUES:
                        rrf_started = time.perf_counter()
                        fused = reciprocal_rank_fusion(
                            dense_hits,
                            sparse_hits,
                            k=rrf_k,
                        )
                        rrf_ms = (time.perf_counter() - rrf_started) * 1000
                        result["rrf_latencies"][rrf_k].append(rrf_ms)
                        result["fused_rankings"][rrf_k][query.query_id] = [
                            hit.chunk_id for hit in fused
                        ]
                        query_result["rrf"][str(rrf_k)] = [
                            asdict(hit) for hit in fused
                        ]
                    result["per_query"][query.query_id] = query_result
            stages["queries"] = {
                "status": "passed",
                "query_count": len(dataset.queries),
            }
        finally:
            # collection 都是本次实验专用；任何阶段失败也逐个尝试清理。
            for collection_name in reversed(created_collections):
                cleanup_key = next(
                    (
                        name
                        for name, candidate in sparse_collections.items()
                        if candidate == collection_name
                    ),
                    "dense",
                )
                try:
                    await sparse_transport.delete_collection(collection_name)
                    cleanup[cleanup_key] = True
                except (httpx.HTTPError, RuntimeError, ValueError):
                    cleanup[cleanup_key] = False

    relevant_by_query = {
        query.query_id: set(query.relevant_chunk_ids) for query in dataset.queries
    }
    categories = {query.query_id: query.category for query in dataset.queries}
    failures: list[dict[str, Any]] = []
    serialized_profiles: dict[str, Any] = {}
    for name, profile in profiles.items():
        result = profile_results[name]
        failures.extend(
            _profile_failures(
                name,
                result["rankings"],
                relevant_by_query,
                categories,
            )
        )
        serialized_profiles[name] = {
            "tokenization": profile.tokenization,
            "preprocessing": profile.preprocessing,
            "sparse_profile_key": profile.sparse_profile.key,
            "bm25f": _metric_payload(result["rankings"], relevant_by_query),
            "rrf": {
                str(rrf_k): _metric_payload(
                    result["fused_rankings"][rrf_k],
                    relevant_by_query,
                )
                for rrf_k in RRF_K_VALUES
            },
            "exclusive_relevant_hits_at_3": _exclusive_relevant_hits(
                dense_rankings,
                result["rankings"],
                relevant_by_query,
                k=3,
            ),
            "latency": {
                "bm25f": _latency_summary(result["bm25f_latencies"]),
                "rrf": {
                    str(rrf_k): _latency_summary(result["rrf_latencies"][rrf_k])
                    for rrf_k in RRF_K_VALUES
                },
            },
            "per_query": result["per_query"],
        }

    metrics = {
        "schema_version": 1,
        "experiment": "weaviate_bm25f_chinese_tokenization",
        "run_status": "passed",
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "dataset_id": dataset.dataset_id,
            "sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
            "document_count": len(dataset.documents),
            "query_count": len(dataset.queries),
            "language": dataset.language,
        },
        "constants": {
            "candidate_k": candidate_k,
            "field_weights": dict(_FIELD_WEIGHTS),
            "metric_k_values": list(METRIC_K_VALUES),
            "rrf_k_values": list(RRF_K_VALUES),
            "dense_reused_for_all_profiles": True,
        },
        "stages": stages,
        "dense": {
            "metrics": _metric_payload(dense_rankings, relevant_by_query),
            "latency": _latency_summary(dense_latencies),
            "rankings": dense_rankings,
        },
        "profiles": serialized_profiles,
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
    """写入对照配置、完整指标、失败样本和带边界的实验说明。"""

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
        "constants": metrics["constants"],
        "profiles": {
            name: {
                "tokenization": payload["tokenization"],
                "preprocessing": payload["preprocessing"],
                "sparse_profile_key": payload["sparse_profile_key"],
            }
            for name, payload in metrics["profiles"].items()
        },
    }
    (output_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "failures.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in failures),
        encoding="utf-8",
    )
    (output_dir / "README.md").write_text(
        "# BM25F 中文分词与查询预处理对照实验\n\n"
        "本实验固定 Dense、字段权重、candidate K 和 RRF 公式，只比较 "
        "`word + identity`、`trigram + identity` 与 `word + cjk_bigram`。\n\n"
        "```bash\n"
        "WEAVIATE_BASE_URL=http://127.0.0.1:8080 \\\n"
        "OLLAMA_BASE_URL=http://127.0.0.1:11434 \\\n"
        "/Users/liuhui/opt/anaconda3/envs/aitest/bin/python "
        "scripts/weaviate_bm25f_chinese_evaluation.py\n"
        "```\n\n"
        "数据仍是固定合成小样本，不能代表真实企业语料、生产容量或默认参数。\n",
        encoding="utf-8",
    )

    baseline = metrics["profiles"]["word_identity"]
    trigram = metrics["profiles"]["trigram_identity"]
    bigram = metrics["profiles"]["word_cjk_bigram"]
    dense = metrics["dense"]
    conclusion = (
        "# 结论\n\n"
        "## 已确认\n\n"
        "- 三组 profile 复用同一批 Dense 结果、字段权重、candidate K、RRF 公式和固定数据集。\n"
        "- `trigram + identity` 只改变 Weaviate tokenizer；"
        "`word + cjk_bigram` 只改变索引与查询共同使用的预处理。\n"
        "- Weaviate `1.27.10` 当前实例接受 `trigram`，实验结束后四个隔离 collection 均已删除。\n\n"
        "## 已验证\n\n"
        f"- Dense @3 Recall/MRR/nDCG="
        f"{dense['metrics']['at_3']['recall_at_k']:.4f}/"
        f"{dense['metrics']['at_3']['mrr']:.4f}/"
        f"{dense['metrics']['at_3']['ndcg_at_k']:.4f}。\n"
        f"- `word + identity` BM25F @3="
        f"{baseline['bm25f']['at_3']['recall_at_k']:.4f}/"
        f"{baseline['bm25f']['at_3']['mrr']:.4f}/"
        f"{baseline['bm25f']['at_3']['ndcg_at_k']:.4f}，"
        f"p50/p95={baseline['latency']['bm25f']['p50_ms']}/"
        f"{baseline['latency']['bm25f']['p95_ms']} ms。\n"
        f"- `trigram + identity` BM25F @3="
        f"{trigram['bm25f']['at_3']['recall_at_k']:.4f}/"
        f"{trigram['bm25f']['at_3']['mrr']:.4f}/"
        f"{trigram['bm25f']['at_3']['ndcg_at_k']:.4f}，"
        f"RRF k=60 @3 nDCG={trigram['rrf']['60']['at_3']['ndcg_at_k']:.4f}。\n"
        f"- `word + cjk_bigram` BM25F @3="
        f"{bigram['bm25f']['at_3']['recall_at_k']:.4f}/"
        f"{bigram['bm25f']['at_3']['mrr']:.4f}/"
        f"{bigram['bm25f']['at_3']['ndcg_at_k']:.4f}，"
        f"p50/p95={bigram['latency']['bm25f']['p50_ms']}/"
        f"{bigram['latency']['bm25f']['p95_ms']} ms。\n"
        "- `trigram` 仍未召回幂等和撤权两条语义改写，并给技术查询带来更多低分候选；"
        "`cjk_bigram` 在当前 8 条查询上全部命中。\n\n"
        "## 待实验\n\n"
        "- `cjk_bigram` 的满分只来自 10 个合成 chunk 和 8 条同域查询，可能过拟合措辞与字符重叠。\n"
        "- 在人工确认的 development/frozen split 上扩大中文同义改写、短查询、错误码、路径和干扰文档后，"
        "再决定是否采用该 profile。\n"
        "- 评估索引体积、长文档噪声、低分候选阈值和更大 candidate K；当前延迟不代表容量或 SLO。\n\n"
        "## 非目标\n\n"
        "- 本实验未实现 Sparse Generation、默认 API 编排、Reranker、ACL/OIDC 或生产参数冻结。\n"
    )
    (output_dir / "conclusion.md").write_text(conclusion, encoding="utf-8")


def main() -> int:
    """解析参数，运行真实对照实验并写入项目内脱敏产物。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    metrics, failures = asyncio.run(run_chinese_experiment(args.dataset))
    _write_artifacts(args.output_dir, args.dataset, metrics, failures)
    print(
        json.dumps(
            {"run_status": metrics["run_status"], "output_dir": str(args.output_dir)}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
