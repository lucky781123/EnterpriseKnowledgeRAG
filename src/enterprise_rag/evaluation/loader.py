"""评测集加载与结构校验。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ANSWERABILITY = {"answerable", "unanswerable", "conflict", "forbidden"}
REQUIRED_FIELDS = {
    "id",
    "category",
    "question",
    "answerability",
    "expected_evidence",
    "tenant",
    "user_roles",
    "gold_notes",
}


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    id: str
    category: str
    question: str
    answerability: str
    expected_evidence: tuple[str, ...]
    tenant: str
    user_roles: tuple[str, ...]
    gold_notes: str

    @classmethod
    def from_dict(cls, value: dict[str, Any], line_number: int) -> "EvaluationCase":
        missing = REQUIRED_FIELDS - value.keys()
        if missing:
            raise ValueError(f"第 {line_number} 行缺少字段: {sorted(missing)}")
        if value["answerability"] not in ANSWERABILITY:
            raise ValueError(f"第 {line_number} 行 answerability 无效")
        if not isinstance(value["expected_evidence"], list):
            raise ValueError(f"第 {line_number} 行 expected_evidence 必须是列表")
        if not isinstance(value["user_roles"], list):
            raise ValueError(f"第 {line_number} 行 user_roles 必须是列表")
        return cls(
            id=str(value["id"]),
            category=str(value["category"]),
            question=str(value["question"]),
            answerability=value["answerability"],
            expected_evidence=tuple(map(str, value["expected_evidence"])),
            tenant=str(value["tenant"]),
            user_roles=tuple(map(str, value["user_roles"])),
            gold_notes=str(value["gold_notes"]),
        )


def load_jsonl(path: str | Path) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"第 {line_number} 行不是有效 JSON: {exc.msg}") from exc
            case = EvaluationCase.from_dict(value, line_number)
            if case.id in seen_ids:
                raise ValueError(f"评测集存在重复 ID: {case.id}")
            seen_ids.add(case.id)
            cases.append(case)
    return cases
