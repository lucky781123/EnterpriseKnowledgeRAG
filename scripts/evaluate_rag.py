"""运行脱敏 RAG 评测并输出可归档 JSON 报告。"""

from __future__ import annotations

import argparse
import json

from enterprise_rag.evaluation.quality_gate import (
    evaluate_quality_gate,
    load_quality_gate_thresholds,
)
from enterprise_rag.evaluation.rag import evaluate_jsonl


def main() -> int:
    """解析命令行参数，执行评测并返回稳定退出码。"""

    parser = argparse.ArgumentParser(description="评测检索、引用支持、拒答和权限结果")
    parser.add_argument("--dataset", required=True, help="评测集 JSONL")
    parser.add_argument("--predictions", required=True, help="脱敏预测 JSONL")
    parser.add_argument("--output", required=True, help="报告 JSON 输出路径")
    parser.add_argument(
        "--thresholds",
        help="可选质量门槛 JSON；仅检查显式配置的指标",
    )
    args = parser.parse_args()
    report = evaluate_jsonl(args.dataset, args.predictions, args.output)
    output = report.as_dict()
    failed = bool(report.failures)
    if args.thresholds:
        thresholds = load_quality_gate_thresholds(args.thresholds)
        gate = evaluate_quality_gate(report, thresholds)
        output["quality_gate"] = gate.as_dict()
        failed = not gate.passed
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(output, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
