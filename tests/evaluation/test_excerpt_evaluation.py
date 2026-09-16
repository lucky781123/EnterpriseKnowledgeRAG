"""F2-G 确定性评测观察值测试。"""

from enterprise_rag.evaluation.excerpt import evaluate_excerpt_response


def test_evaluator_reports_identity_and_unrun_semantic_metrics() -> None:
    """评测必须区分可确定性核验项与尚未运行的语义指标。"""

    observation = evaluate_excerpt_response(
        case_id="case-1",
        question="问题",
        evidence=[{"evidence_id": "doc-1", "text": "正文"}],
        response={
            "answer_status": "answered",
            "citation_validation": "passed",
            "message": "答案",
            "citations": [{"evidence_id": "doc-1", "quote": "正文", "start": 0, "end": 2}],
        },
        expected_evidence=("doc-1",),
    )

    assert observation.identity_passed is True
    assert observation.semantic_quality == "not_run"
    assert observation.retrieval_metrics == "not_applicable"
    assert observation.actual_output == "答案"
