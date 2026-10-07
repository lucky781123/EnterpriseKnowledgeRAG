"""RAG 离线评测指标测试。"""

from enterprise_rag.evaluation.loader import EvaluationCase
from enterprise_rag.evaluation.rag import EvaluationPrediction, evaluate_predictions


def _case(case_id: str, answerability: str = "answerable") -> EvaluationCase:
    """构造最小评测样本。"""

    return EvaluationCase(
        id=case_id,
        category="test",
        question="问题",
        answerability=answerability,
        expected_evidence=("doc-1",),
        tenant="tenant-a",
        user_roles=("reader",),
        gold_notes="边界",
    )


def test_evaluation_reports_retrieval_citation_groundedness_and_abstention():
    """评测应同时覆盖检索、引用支持、groundedness 和拒答。"""

    report = evaluate_predictions(
        [_case("a"), _case("b", "unanswerable"), _case("c", "forbidden")],
        [
            EvaluationPrediction(
                "a", "answered", ("doc-1",), ("doc-1",), ("doc-1",), True,
            ),
            EvaluationPrediction("b", "abstained", (), (), (), True),
            EvaluationPrediction("c", "not_found", (), (), (), False),
        ],
    )
    assert report.retrieval_recall == 1.0
    assert report.citation_precision == 1.0
    assert report.grounded_answer_rate == 1.0
    assert report.abstention_accuracy == 1.0
    assert report.authorization_accuracy == 1.0
    assert not report.failures


def test_evaluation_rejects_unsafe_answer_and_tracks_missing_prediction():
    """不安全回答和缺失预测必须出现在失败清单中。"""

    report = evaluate_predictions(
        [_case("a"), _case("missing")],
        [EvaluationPrediction("a", "answered", (), ("unknown",), (), True)],
    )
    assert report.missing_prediction_count == 1
    assert {item["kind"] for item in report.failures} >= {
        "answerable_not_answered",
        "missing_prediction",
    }
