"""P1 RAG 评测集审计测试。"""

import json
from pathlib import Path

import pytest

from enterprise_rag.evaluation.dataset_audit import audit_dataset


def _write_dataset(path: Path, *, answerability: str = "answerable") -> None:
    """写入不包含正文输出的最小评测集。"""

    path.write_text(
        json.dumps(
            {
                "id": "case-1",
                "category": "安全",
                "question": "问题",
                "answerability": answerability,
                "expected_evidence": ["doc-1"],
                "tenant": "tenant-a",
                "user_roles": ["reader"],
                "gold_notes": "人工确认前的备注",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_dataset_audit_reports_coverage_without_claiming_frozen_readiness(tmp_path: Path) -> None:
    """草案数据集可以通过结构审计，但没有人工确认时不能标记 frozen ready。"""

    dataset_path = tmp_path / "dataset.jsonl"
    _write_dataset(dataset_path)

    report = audit_dataset(
        dataset_path,
        dataset_id="draft-v1",
        dataset_kind="draft",
        required_answerability=("answerable",),
        required_categories=("安全",),
        human_reviewed=False,
    )

    assert report["validation_passed"] is True
    assert report["ready_for_frozen"] is False
    assert report["case_count"] == 1
    assert report["answerability_counts"] == {"answerable": 1}
    assert report["category_counts"] == {"安全": 1}
    assert "human_review_required" in report["blocking_reasons"]


def test_dataset_audit_reports_missing_required_coverage(tmp_path: Path) -> None:
    """缺少要求的 answerability 或 category 时，审计必须失败。"""

    dataset_path = tmp_path / "dataset.jsonl"
    _write_dataset(dataset_path)

    report = audit_dataset(
        dataset_path,
        dataset_id="draft-v1",
        dataset_kind="development",
        required_answerability=("answerable", "forbidden"),
        required_categories=("安全", "权限撤销"),
        human_reviewed=False,
    )

    assert report["validation_passed"] is False
    assert report["ready_for_frozen"] is False
    assert report["issues"] == [
        {"kind": "missing_answerability", "value": "forbidden"},
        {"kind": "missing_category", "value": "权限撤销"},
    ]


def test_dataset_audit_rejects_invalid_kind_and_empty_required_values(tmp_path: Path) -> None:
    """审计配置必须显式合法，不允许空要求或未知数据集层级。"""

    dataset_path = tmp_path / "dataset.jsonl"
    _write_dataset(dataset_path)

    with pytest.raises(ValueError, match="dataset_kind"):
        audit_dataset(
            dataset_path,
            dataset_id="draft-v1",
            dataset_kind="production",
            required_answerability=("answerable",),
            required_categories=("安全",),
            human_reviewed=False,
        )

    with pytest.raises(ValueError, match="required_answerability"):
        audit_dataset(
            dataset_path,
            dataset_id="draft-v1",
            dataset_kind="draft",
            required_answerability=(),
            required_categories=("安全",),
            human_reviewed=False,
        )
