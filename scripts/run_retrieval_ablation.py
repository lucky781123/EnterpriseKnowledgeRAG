"""运行 P1 检索五模式消融评测。"""

from __future__ import annotations

import argparse
import json

from enterprise_rag.evaluation.retrieval_ablation import run_retrieval_ablation


def main() -> int:
    """解析参数，运行消融并打印脱敏 JSON 报告。"""

    parser = argparse.ArgumentParser(description="运行 Dense/BM25F/RRF/Reranker 检索消融")
    parser.add_argument("--dataset", required=True, help="符合消融契约的 JSON 数据集")
    parser.add_argument("--output", required=True, help="脱敏 JSON 报告路径")
    parser.add_argument("--k", type=int, default=5, help="Recall/nDCG 截断值")
    parser.add_argument("--rrf-k", type=int, default=60, help="RRF 常数")
    args = parser.parse_args()
    report = run_retrieval_ablation(
        args.dataset,
        output_path=args.output,
        k=args.k,
        rrf_k=args.rrf_k,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
