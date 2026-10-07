"""P1 生成质量门禁测试。"""

import json

import pytest

from enterprise_rag.evaluation.quality_gate import (
    QualityGateThresholds,
    evaluate_quality_gate,
)
from enterprise_rag.evaluation.rag import RAGEvaluationReport
from scripts.evaluate_rag import main as evaluate_rag_main


def _report(
    *,
    citation_precision: float = 1.0,
    grounded_answer_rate: float = 1.0,
    failures: tuple[dict[str, str], ...] = (),
) -> RAGEvaluationReport:
    """构造最小脱敏报告，避免测试依赖真实问题正文。"""

    return RAGEvaluationReport(
        dataset_cases=3,
        prediction_cases=3,
        matched_cases=3,
        retrieval_recall=1.0,
        citation_precision=citation_precision,
        citation_support_rate=1.0,
        grounded_answer_rate=grounded_answer_rate,
        abstention_accuracy=1.0,
        authorization_accuracy=1.0,
        missing_prediction_count=0,
        extra_prediction_count=0,
        failures=failures,
    )


def test_quality_gate_passes_when_metrics_meet_thresholds() -> None:
    """全部指标达到门槛且无失败时，门禁必须通过。"""

    result = evaluate_quality_gate(
        _report(),
        QualityGateThresholds(
            min_citation_precision=0.95,
            min_grounded_answer_rate=0.95,
            min_authorization_accuracy=1.0,
        ),
    )

    assert result.passed is True
    assert result.failures == ()


def test_quality_gate_rejects_metric_regression() -> None:
    """引用或 groundedness 指标低于门槛时，门禁必须明确失败原因。"""

    result = evaluate_quality_gate(
        _report(citation_precision=0.8, grounded_answer_rate=0.7),
        QualityGateThresholds(
            min_citation_precision=0.95,
            min_grounded_answer_rate=0.9,
        ),
    )

    assert result.passed is False
    assert result.failures == (
        {
            "kind": "threshold_below",
            "metric": "citation_precision",
            "observed": "0.8",
            "required": "0.95",
        },
        {
            "kind": "threshold_below",
            "metric": "grounded_answer_rate",
            "observed": "0.7",
            "required": "0.9",
        },
    )


def test_quality_gate_keeps_safety_failures_hard_even_when_metric_thresholds_pass() -> None:
    """越权或不安全回答失败不能被平均指标掩盖。"""

    result = evaluate_quality_gate(
        _report(
            failures=(
                {"case_id": "case-forbidden", "kind": "forbidden_permission_granted"},
            )
        ),
        QualityGateThresholds(min_authorization_accuracy=1.0),
    )

    assert result.passed is False
    assert result.failures == (
        {
            "kind": "hard_failure",
            "failure_kind": "forbidden_permission_granted",
            "case_id": "case-forbidden",
        },
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("min_citation_precision", -0.1),
        ("min_grounded_answer_rate", 1.1),
        ("min_abstention_accuracy", float("nan")),
        ("min_retrieval_recall", float("inf")),
    ],
)
def test_quality_gate_rejects_invalid_thresholds(field: str, value: float) -> None:
    """门槛配置越界时必须失败在配置层，而不是静默修正。"""

    with pytest.raises(ValueError):
        thresholds = QualityGateThresholds(**{field: value})
        evaluate_quality_gate(_report(), thresholds)


def test_quality_gate_rejects_non_numeric_threshold() -> None:
    """布尔值不能因 Python 子类关系被误认为数值门槛。"""

    with pytest.raises(TypeError, match="必须是"):
        QualityGateThresholds(min_authorization_accuracy=True)


def test_quality_gate_rejects_unknown_configuration_field() -> None:
    """配置拼写错误必须显式失败，避免门槛被静默忽略。"""

    with pytest.raises(ValueError, match="未知质量门禁字段"):
        QualityGateThresholds.from_dict({"min_citation_precison": 0.9})


def test_evaluation_cli_returns_failure_and_persists_gate_result(
    tmp_path, monkeypatch
) -> None:
    """显式门槛未达标时 CLI 应返回失败码并保存完整门禁结果。"""

    dataset = tmp_path / "dataset.jsonl"
    predictions = tmp_path / "predictions.jsonl"
    thresholds = tmp_path / "thresholds.json"
    output = tmp_path / "report.json"
    dataset.write_text(
        '{"id":"case-1","category":"test","question":"q",'
        '"answerability":"answerable","expected_evidence":["doc-1"],'
        '"tenant":"tenant-test","user_roles":["reader"],"gold_notes":""}\n',
        encoding="utf-8",
    )
    predictions.write_text(
        '{"case_id":"case-1","answer_status":"answered",'
        '"retrieved_evidence":[],"cited_evidence":[],"supported_citations":[]}\n',
        encoding="utf-8",
    )
    thresholds.write_text('{"min_retrieval_recall":1.0}', encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate_rag",
            "--dataset", str(dataset),
            "--predictions", str(predictions),
            "--thresholds", str(thresholds),
            "--output", str(output),
        ],
    )

    exit_code = evaluate_rag_main()
    saved_report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 2
    assert saved_report["quality_gate"]["passed"] is False
    assert saved_report["quality_gate"]["failures"][0]["metric"] == "retrieval_recall"
