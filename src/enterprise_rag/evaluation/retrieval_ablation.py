"""P1 检索消融评测 runner。

本模块只负责把已经产生的各路 ranking 统一计算成可归档报告。
它不调用 Weaviate、Embedding、Reranker 或 LLM，因此不会把离线 fixture
的结果冒充成真实在线质量或容量结论。真实检索实验可以先把各阶段排名和
观测延迟脱敏后写入同一输入契约，再复用这里的指标计算。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .retrieval_quality import ChannelHit, evaluate_rankings, reciprocal_rank_fusion

ABLATION_MODES = ("dense", "bm25f", "dense_bm25f", "rrf", "rrf_reranker")


def _require_non_empty_string(value: Any, field_name: str) -> str:
    """校验必填字符串，避免把数字或空字符串静默转换为 ID。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空字符串")
    return value


def _read_ranking(
    value: Any,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    """读取无重复的 chunk 排名，并拒绝隐式类型转换。

    真实检索允许某一路合法返回空结果，因此只有 gold 相关集合必须非空；
    调用方通过 ``allow_empty`` 明确表达该差异，避免把 miss 当成坏数据。
    """

    if not isinstance(value, list) or (not allow_empty and not value):
        expected = "字符串列表" if allow_empty else "非空字符串列表"
        raise ValueError(f"{field_name} 必须是{expected}")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field_name} 必须只包含非空字符串")
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} 不能包含重复 chunk_id")
    return value


def _read_latency(value: Any, query_id: str) -> dict[str, float]:
    """读取可选的外部观测延迟，禁止负数和布尔值。"""

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"query {query_id} 的 latency_ms 必须是对象")
    latency: dict[str, float] = {}
    for mode, raw_value in value.items():
        if mode not in ABLATION_MODES:
            raise ValueError(f"query {query_id} 的 latency_ms 包含未知模式: {mode}")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise TypeError(f"query {query_id} 的 {mode} 延迟必须是数字")
        if not math.isfinite(raw_value):
            raise ValueError(f"query {query_id} 的 {mode} 延迟必须是有限数字")
        if raw_value < 0:
            raise ValueError(f"query {query_id} 的 {mode} 延迟不能为负数")
        latency[mode] = float(raw_value)
    return latency


def load_ablation_fixture(path: str | Path) -> dict[str, Any]:
    """加载并校验检索消融输入，返回不含业务正文的规范化结构。"""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"消融数据不是有效 JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise TypeError("消融数据根对象必须是 JSON object")

    dataset_id = _require_non_empty_string(payload.get("dataset_id"), "dataset_id")
    dataset_kind = payload.get("dataset_kind", "offline-fixture")
    dataset_kind = _require_non_empty_string(dataset_kind, "dataset_kind")
    raw_queries = payload.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ValueError("queries 必须是非空列表")

    known_chunk_ids: set[str] = set()
    for raw_document in payload.get("documents", []):
        if isinstance(raw_document, dict) and isinstance(raw_document.get("chunk_id"), str):
            known_chunk_ids.add(raw_document["chunk_id"])
    raw_chunk_ids = payload.get("chunk_ids", [])
    if raw_chunk_ids:
        if not isinstance(raw_chunk_ids, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw_chunk_ids
        ):
            raise ValueError("chunk_ids 必须是非空字符串列表")
        known_chunk_ids.update(raw_chunk_ids)

    queries: list[dict[str, Any]] = []
    seen_query_ids: set[str] = set()
    for index, raw_query in enumerate(raw_queries, start=1):
        if not isinstance(raw_query, dict):
            raise TypeError(f"第 {index} 个 query 必须是 JSON object")
        query_id = _require_non_empty_string(raw_query.get("query_id"), "query_id")
        if query_id in seen_query_ids:
            raise ValueError(f"重复 query_id: {query_id}")
        seen_query_ids.add(query_id)

        relevant = _read_ranking(raw_query.get("relevant_chunk_ids"), f"{query_id}.relevant_chunk_ids")
        dense = _read_ranking(
            raw_query.get("dense"),
            f"{query_id}.dense",
            allow_empty=True,
        )
        bm25f = _read_ranking(
            raw_query.get("bm25f"),
            f"{query_id}.bm25f",
            allow_empty=True,
        )
        reranker = _read_ranking(
            raw_query.get("reranker"),
            f"{query_id}.reranker",
            allow_empty=True,
        )
        retrieval_candidates = set(dense) | set(bm25f)
        if not set(reranker) <= retrieval_candidates:
            outside = sorted(set(reranker) - retrieval_candidates)
            raise ValueError(
                f"{query_id}.reranker 包含检索候选集合外的 chunk_id: {outside}"
            )
        rankings = {
            "dense": dense,
            "bm25f": bm25f,
            "reranker": reranker,
        }
        if known_chunk_ids:
            for field_name, ranking in {"relevant_chunk_ids": relevant, **rankings}.items():
                unknown = set(ranking) - known_chunk_ids
                if unknown:
                    raise ValueError(
                        f"{query_id}.{field_name} 包含未知 chunk_id: {sorted(unknown)}"
                    )
        queries.append(
            {
                "query_id": query_id,
                "relevant_chunk_ids": relevant,
                **rankings,
                "latency_ms": _read_latency(raw_query.get("latency_ms"), query_id),
            }
        )

    return {
        "schema_version": payload.get("schema_version", 1),
        "dataset_id": dataset_id,
        "dataset_kind": dataset_kind,
        "queries": queries,
    }


def _stable_union(dense: list[str], bm25f: list[str]) -> list[str]:
    """按两路最早排名形成可解释的候选并集，作为非 RRF 对照模式。"""

    ranks: dict[str, tuple[int, int, str]] = {}
    for position, chunk_id in enumerate(dense, start=1):
        ranks[chunk_id] = (position, 0, chunk_id)
    for position, chunk_id in enumerate(bm25f, start=1):
        current = ranks.get(chunk_id)
        candidate = (position, 1, chunk_id)
        if current is None or candidate < current:
            ranks[chunk_id] = candidate
    return [item[2] for item in sorted(ranks.values())]


def _rrf_ranking(dense: list[str], bm25f: list[str], rrf_k: int) -> list[str]:
    """把两个字符串排名转换为保留通道 provenance 的 RRF 排名。"""

    fused = reciprocal_rank_fusion(
        [ChannelHit(chunk_id=item, rank=index) for index, item in enumerate(dense, start=1)],
        [ChannelHit(chunk_id=item, rank=index) for index, item in enumerate(bm25f, start=1)],
        k=rrf_k,
    )
    return [item.chunk_id for item in fused]


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    """用 nearest-rank 计算百分位；没有观测值时保持 None。"""

    if not values:
        return None
    ordered = sorted(values)
    position = max(1, int((len(ordered) * percentile) + 0.999999)) - 1
    return ordered[min(position, len(ordered) - 1)]


def _latency_summary(values: list[float]) -> dict[str, Any]:
    """生成不填充虚假零值的延迟摘要。"""

    return {
        "measured": bool(values),
        "sample_count": len(values),
        "p50_nearest_rank": _nearest_rank(values, 0.50),
        "p95_nearest_rank": _nearest_rank(values, 0.95),
    }


def _unique_hit_summary(queries: list[dict[str, Any]]) -> dict[str, int]:
    """统计 Dense/BM25F 对 gold 相关 chunk 的独有命中和共同命中。"""

    dense_only = 0
    bm25f_only = 0
    both = 0
    for query in queries:
        relevant = set(query["relevant_chunk_ids"])
        dense_hits = relevant & set(query["dense"])
        bm25f_hits = relevant & set(query["bm25f"])
        dense_only += len(dense_hits - bm25f_hits)
        bm25f_only += len(bm25f_hits - dense_hits)
        both += len(dense_hits & bm25f_hits)
    return {
        "dense_only_relevant_hits": dense_only,
        "bm25f_only_relevant_hits": bm25f_only,
        "both_channel_relevant_hits": both,
    }


def run_retrieval_ablation(
    dataset_path: str | Path,
    *,
    output_path: str | Path | None = None,
    k: int = 5,
    rrf_k: int = 60,
) -> dict[str, Any]:
    """运行五模式检索消融并可选写入脱敏 JSON 报告。"""

    if k <= 0:
        raise ValueError("指标 k 必须大于 0")
    if rrf_k <= 0:
        raise ValueError("RRF k 必须大于 0")

    fixture = load_ablation_fixture(dataset_path)
    queries = fixture["queries"]
    relevant_by_query = {
        query["query_id"]: set(query["relevant_chunk_ids"]) for query in queries
    }
    rankings_by_mode: dict[str, dict[str, list[str]]] = {
        mode: {} for mode in ABLATION_MODES
    }
    latency_by_mode: dict[str, list[float]] = {mode: [] for mode in ABLATION_MODES}

    for query in queries:
        query_id = query["query_id"]
        dense = query["dense"]
        bm25f = query["bm25f"]
        rankings_by_mode["dense"][query_id] = dense
        rankings_by_mode["bm25f"][query_id] = bm25f
        rankings_by_mode["dense_bm25f"][query_id] = _stable_union(dense, bm25f)
        rankings_by_mode["rrf"][query_id] = _rrf_ranking(dense, bm25f, rrf_k)
        rankings_by_mode["rrf_reranker"][query_id] = query["reranker"]
        for mode, value in query["latency_ms"].items():
            latency_by_mode[mode].append(value)

    modes: dict[str, Any] = {}
    for mode in ABLATION_MODES:
        metrics = evaluate_rankings(rankings_by_mode[mode], relevant_by_query, k=k)
        modes[mode] = {
            "metrics": {
                "recall_at_k": metrics.recall_at_k,
                "mrr": metrics.mrr,
                "ndcg_at_k": metrics.ndcg_at_k,
                "query_count": metrics.query_count,
                "hit_query_count": metrics.hit_query_count,
            },
            "latency_ms": _latency_summary(latency_by_mode[mode]),
        }

    report: dict[str, Any] = {
        "schema_version": 1,
        "dataset_id": fixture["dataset_id"],
        "dataset_kind": fixture["dataset_kind"],
        "k": k,
        "rrf_k": rrf_k,
        "modes": modes,
        "channel_unique_relevant_hits": _unique_hit_summary(queries),
        "limitations": [
            "本报告只反映输入 ranking 的离线结果，不证明真实 Weaviate、模型或生产容量。",
            "latency_ms 只有在输入提供外部观测值时才标记 measured=true。",
        ],
    }
    if output_path is not None:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report


__all__ = ["ABLATION_MODES", "load_ablation_fixture", "run_retrieval_ablation"]
