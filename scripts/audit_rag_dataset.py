"""审计 RAG 评测集，并输出不含问题正文的覆盖报告。"""

from __future__ import annotations

import argparse
import json

from enterprise_rag.evaluation.dataset_audit import audit_dataset


def main() -> int:
    """解析评测集审计参数并返回结构校验退出码。"""

    parser = argparse.ArgumentParser(description="审计 RAG 评测集覆盖和冻结就绪条件")
    parser.add_argument("--dataset", required=True, help="评测集 JSONL")
    parser.add_argument("--dataset-id", required=True, help="脱敏数据集标识")
    parser.add_argument(
        "--dataset-kind",
        required=True,
        choices=("draft", "smoke", "development", "frozen", "security"),
    )
    parser.add_argument(
        "--require-answerability",
        action="append",
        required=True,
        help="要求存在的 answerability；可重复传入",
    )
    parser.add_argument(
        "--require-category",
        action="append",
        required=True,
        help="要求存在的 category；可重复传入",
    )
    parser.add_argument("--human-reviewed", action="store_true")
    parser.add_argument("--output", required=True, help="脱敏 JSON 输出路径")
    args = parser.parse_args()
    report = audit_dataset(
        args.dataset,
        dataset_id=args.dataset_id,
        dataset_kind=args.dataset_kind,
        required_answerability=args.require_answerability,
        required_categories=args.require_category,
        human_reviewed=args.human_reviewed,
    )
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["validation_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
