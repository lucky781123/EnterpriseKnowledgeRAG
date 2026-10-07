"""运行 P1 安全与可靠性回归门禁。"""

from __future__ import annotations

import argparse
import json

from enterprise_rag.evaluation.regression_gate import run_regression_gate


def main() -> int:
    """解析 JUnit 和场景清单路径，输出脱敏门禁报告。"""

    parser = argparse.ArgumentParser(description="检查 P1 安全与可靠性回归场景")
    parser.add_argument("--junitxml", required=True, help="pytest 生成的 JUnit XML")
    parser.add_argument("--manifest", required=True, help="P1 回归场景清单 JSON")
    parser.add_argument("--output", required=True, help="脱敏 JSON 报告路径")
    args = parser.parse_args()
    report = run_regression_gate(
        args.junitxml,
        args.manifest,
        output_path=args.output,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
