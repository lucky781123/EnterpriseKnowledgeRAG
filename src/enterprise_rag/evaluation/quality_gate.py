"""对脱敏 RAG 评测报告执行显式质量门禁。"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .rag import RAGEvaluationReport


@dataclass(frozen=True, slots=True)
class QualityGateThresholds:
    """保存调用方提供的最低质量门槛；未校准的指标保持未配置。"""

    min_retrieval_recall: float | None = None
    min_citation_precision: float | None = None
    min_citation_support_rate: float | None = None
    min_grounded_answer_rate: float | None = None
    min_abstention_accuracy: float | None = None
    min_authorization_accuracy: float | None = None

    def __post_init__(self) -> None:
        """拒绝布尔值和 [0, 1] 范围外的指标门槛。"""

        for item in fields(self):
            value = getattr(self, item.name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{item.name} 必须是 [0, 1] 范围内的数字")
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{item.name} 必须是 [0, 1] 范围内的数字")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> QualityGateThresholds:
        """从 JSON 对象创建门槛，并拒绝未知字段或错误根类型。"""

        allowed = {item.name for item in fields(cls)}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"未知质量门禁字段: {sorted(unknown)}")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class QualityGateResult:
    """描述显式门槛检查的结果和脱敏失败原因。"""

    passed: bool
    thresholds: dict[str, float | None]
    observed_metrics: dict[str, float]
    failures: tuple[dict[str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        """返回可写入脱敏评测产物的稳定结构。"""

        return asdict(self)


_METRIC_FIELDS = {
    "min_retrieval_recall": "retrieval_recall",
    "min_citation_precision": "citation_precision",
    "min_citation_support_rate": "citation_support_rate",
    "min_grounded_answer_rate": "grounded_answer_rate",
    "min_abstention_accuracy": "abstention_accuracy",
    "min_authorization_accuracy": "authorization_accuracy",
}


def evaluate_quality_gate(
    report: RAGEvaluationReport,
    thresholds: QualityGateThresholds,
) -> QualityGateResult:
    """检查宏观指标门槛，并将全部样本级 evaluator failure 作为硬失败。"""

    threshold_values = asdict(thresholds)
    observed = {
        metric_name: float(getattr(report, metric_name))
        for metric_name in _METRIC_FIELDS.values()
    }
    failures: list[dict[str, str]] = []

    for threshold_name, metric_name in _METRIC_FIELDS.items():
        required = threshold_values[threshold_name]
        actual = observed[metric_name]
        if required is not None and actual < required:
            failures.append(
                {
                    "kind": "threshold_below",
                    "metric": metric_name,
                    "observed": str(actual),
                    "required": str(required),
                }
            )

    for item in report.failures:
        failures.append(
            {
                "kind": "hard_failure",
                "failure_kind": item.get("kind", "evaluation_failure"),
                "case_id": item.get("case_id", ""),
            }
        )

    return QualityGateResult(
        passed=not failures,
        thresholds=threshold_values,
        observed_metrics=observed,
        failures=tuple(failures),
    )


def load_quality_gate_thresholds(path: str | Path) -> QualityGateThresholds:
    """读取显式 JSON 门槛配置，不提供隐式默认阈值。"""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"质量门禁配置不是有效 JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise TypeError("质量门禁配置根节点必须是 JSON object")
    return QualityGateThresholds.from_dict(payload)


__all__ = [
    "QualityGateResult",
    "QualityGateThresholds",
    "evaluate_quality_gate",
    "load_quality_gate_thresholds",
]
