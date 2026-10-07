"""对脱敏 RAG 评测集执行结构、覆盖和冻结就绪审计。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .loader import ANSWERABILITY, load_jsonl

_DATASET_KINDS = {"draft", "smoke", "development", "frozen", "security"}


def _require_non_empty_values(values: Iterable[str], field_name: str) -> tuple[str, ...]:
    """校验要求列表，并拒绝空字符串和重复值。"""

    normalized = tuple(values)
    if not normalized or any(not isinstance(value, str) or not value.strip() for value in normalized):
        raise ValueError(f"{field_name} 必须是非空字符串列表")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} 不能包含重复值")
    return normalized


def audit_dataset(
    path: str | Path,
    *,
    dataset_id: str,
    dataset_kind: str,
    required_answerability: Iterable[str],
    required_categories: Iterable[str],
    human_reviewed: bool,
) -> dict[str, Any]:
    """审计评测集覆盖，并显式返回 frozen 就绪条件。"""

    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise ValueError("dataset_id 必须是非空字符串")
    if dataset_kind not in _DATASET_KINDS:
        raise ValueError(f"dataset_kind 无效: {dataset_kind}")
    if type(human_reviewed) is not bool:
        raise TypeError("human_reviewed 必须是 bool")

    required_answerability_values = _require_non_empty_values(
        required_answerability, "required_answerability"
    )
    invalid_answerability = set(required_answerability_values) - ANSWERABILITY
    if invalid_answerability:
        raise ValueError(f"required_answerability 包含无效值: {sorted(invalid_answerability)}")
    required_category_values = _require_non_empty_values(
        required_categories, "required_categories"
    )

    cases = load_jsonl(path)
    answerability_counts = Counter(case.answerability for case in cases)
    category_counts = Counter(case.category for case in cases)
    issues: list[dict[str, str]] = []
    for value in required_answerability_values:
        if not answerability_counts.get(value):
            issues.append({"kind": "missing_answerability", "value": value})
    for value in required_category_values:
        if not category_counts.get(value):
            issues.append({"kind": "missing_category", "value": value})

    if not cases:
        issues.append({"kind": "empty_dataset", "value": str(path)})
    for case in cases:
        if case.answerability == "answerable" and not case.expected_evidence:
            issues.append({"kind": "answerable_without_evidence", "value": case.id})

    blocking_reasons = [item["kind"] for item in issues]
    if dataset_kind != "frozen":
        blocking_reasons.append("dataset_kind_not_frozen")
    if not human_reviewed:
        blocking_reasons.append("human_review_required")

    validation_passed = not issues
    return {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "dataset_kind": dataset_kind,
        "case_count": len(cases),
        "category_counts": dict(sorted(category_counts.items())),
        "answerability_counts": dict(sorted(answerability_counts.items())),
        "validation_passed": validation_passed,
        "ready_for_frozen": validation_passed
        and dataset_kind == "frozen"
        and human_reviewed,
        "issues": issues,
        "blocking_reasons": blocking_reasons,
    }


__all__ = ["audit_dataset"]
