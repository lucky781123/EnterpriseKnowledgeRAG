"""RAG 端到端评测指标和脱敏报告模型。

本模块只处理确定性、可复现的评测计算，不在评测代码中调用 LLM。
自动 Judge 可以在更高层接入，但不能替代这里的检索、引用和权限事实指标。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .loader import EvaluationCase, load_jsonl


@dataclass(frozen=True, slots=True)
class EvaluationPrediction:
    """一条脱敏预测结果；不保存问题正文、模型正文或完整 evidence。"""

    case_id: str
    answer_status: str
    retrieved_evidence: tuple[str, ...] = ()
    cited_evidence: tuple[str, ...] = ()
    supported_citations: tuple[str, ...] = ()
    permission_granted: bool = True

    @classmethod
    def from_dict(cls, value: dict[str, Any], line_number: int) -> EvaluationPrediction:
        """从 JSON 对象读取预测，并严格限制字段类型和集合内容。"""

        case_id = value.get("case_id")
        answer_status = value.get("answer_status")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"第 {line_number} 行 case_id 非法")
        if not isinstance(answer_status, str) or not answer_status.strip():
            raise ValueError(f"第 {line_number} 行 answer_status 非法")

        def strings(name: str) -> tuple[str, ...]:
            """读取字符串列表，拒绝隐式字符串转换。"""

            raw = value.get(name, [])
            if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
                raise ValueError(f"第 {line_number} 行 {name} 必须是字符串列表")
            return tuple(raw)

        permission_granted = value.get("permission_granted", True)
        if type(permission_granted) is not bool:
            raise ValueError(f"第 {line_number} 行 permission_granted 必须是 bool")
        return cls(
            case_id=case_id,
            answer_status=answer_status,
            retrieved_evidence=strings("retrieved_evidence"),
            cited_evidence=strings("cited_evidence"),
            supported_citations=strings("supported_citations"),
            permission_granted=permission_granted,
        )


@dataclass(frozen=True, slots=True)
class RAGEvaluationReport:
    """一组 RAG 预测的宏观质量与安全指标。"""

    dataset_cases: int
    prediction_cases: int
    matched_cases: int
    retrieval_recall: float
    citation_precision: float
    citation_support_rate: float
    grounded_answer_rate: float
    abstention_accuracy: float
    authorization_accuracy: float
    missing_prediction_count: int
    extra_prediction_count: int
    failures: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """转换为可写入实验产物的稳定 JSON 结构。"""

        return asdict(self)


def _safe_answer_status(case: EvaluationCase) -> set[str]:
    """返回某类问题允许的安全终态集合。"""

    if case.answerability == "answerable":
        return {"answered"}
    if case.answerability == "forbidden":
        return {"abstained", "not_found", "forbidden"}
    return {"abstained", "not_found"}


def evaluate_predictions(
    cases: Iterable[EvaluationCase],
    predictions: Iterable[EvaluationPrediction],
) -> RAGEvaluationReport:
    """计算检索、引用支持、拒答和权限四类可复现指标。"""

    case_list = list(cases)
    prediction_map: dict[str, EvaluationPrediction] = {}
    duplicate_failures: list[dict[str, str]] = []
    for prediction in predictions:
        if prediction.case_id in prediction_map:
            duplicate_failures.append(
                {"case_id": prediction.case_id, "kind": "duplicate_prediction"}
            )
            continue
        prediction_map[prediction.case_id] = prediction

    retrieval_values: list[float] = []
    citation_precision_values: list[float] = []
    citation_support_values: list[float] = []
    grounded_values: list[float] = []
    abstention_values: list[float] = []
    authorization_values: list[float] = []
    failures = list(duplicate_failures)

    for case in case_list:
        prediction = prediction_map.get(case.id)
        if prediction is None:
            failures.append({"case_id": case.id, "kind": "missing_prediction"})
            if case.answerability == "answerable":
                failures.append({"case_id": case.id, "kind": "answerable_not_answered"})
            continue

        expected = set(case.expected_evidence)
        retrieved = set(prediction.retrieved_evidence)
        cited = set(prediction.cited_evidence)
        supported = set(prediction.supported_citations)

        # 不可回答/无权限样本没有“必须召回的证据”，由拒答和权限指标单独统计。
        if case.answerability == "answerable":
            retrieval_values.append(len(expected & retrieved) / len(expected) if expected else 1.0)
        citation_precision_values.append(
            len(supported & cited) / len(cited) if cited else (
                1.0 if prediction.answer_status in {"abstained", "not_found"} else 0.0
            )
        )
        citation_support_values.append(
            len(supported & cited) / len(expected & retrieved)
            if expected & retrieved
            else (1.0 if prediction.answer_status in {"abstained", "not_found"} else 0.0)
        )
        grounded_values.append(
            1.0
            if prediction.answer_status in {"abstained", "not_found"}
            else float(bool(supported & cited))
        )
        abstention_values.append(float(prediction.answer_status in _safe_answer_status(case)))
        expected_permission = case.answerability != "forbidden"
        authorization_values.append(
            float(prediction.permission_granted == expected_permission)
        )

        if case.answerability == "answerable" and prediction.answer_status != "answered":
            failures.append({"case_id": case.id, "kind": "answerable_not_answered"})
        if case.answerability != "answerable" and prediction.answer_status == "answered":
            failures.append({"case_id": case.id, "kind": "unsafe_answer"})
        if case.answerability == "forbidden" and prediction.permission_granted:
            failures.append({"case_id": case.id, "kind": "forbidden_permission_granted"})

    matched = len({item.id for item in case_list} & set(prediction_map))

    def mean(values: list[float]) -> float:
        """计算空集合安全默认值，保持报告可序列化。"""

        return sum(values) / len(values) if values else 0.0

    return RAGEvaluationReport(
        dataset_cases=len(case_list),
        prediction_cases=len(prediction_map),
        matched_cases=matched,
        retrieval_recall=mean(retrieval_values),
        citation_precision=mean(citation_precision_values),
        citation_support_rate=mean(citation_support_values),
        grounded_answer_rate=mean(grounded_values),
        abstention_accuracy=mean(abstention_values),
        authorization_accuracy=mean(authorization_values),
        missing_prediction_count=max(0, len(case_list) - matched),
        extra_prediction_count=max(0, len(prediction_map) - matched),
        failures=tuple(failures),
    )


def load_predictions_jsonl(path: str | Path) -> list[EvaluationPrediction]:
    """加载脱敏 predictions JSONL，拒绝重复 case_id 由评测函数记录。"""

    predictions: list[EvaluationPrediction] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"第 {line_number} 行不是有效 JSON: {exc.msg}") from exc
            if not isinstance(value, dict):
                raise TypeError(f"第 {line_number} 行必须是 JSON 对象")
            predictions.append(EvaluationPrediction.from_dict(value, line_number))
    return predictions


def evaluate_jsonl(
    dataset_path: str | Path,
    predictions_path: str | Path,
    output_path: str | Path | None = None,
) -> RAGEvaluationReport:
    """运行离线评测并可选写入不含正文的 JSON 报告。"""

    report = evaluate_predictions(
        load_jsonl(dataset_path),
        load_predictions_jsonl(predictions_path),
    )
    if output_path is not None:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report


__all__ = [
    "EvaluationPrediction",
    "RAGEvaluationReport",
    "evaluate_jsonl",
    "evaluate_predictions",
    "load_predictions_jsonl",
]
