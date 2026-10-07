"""回答 Groundedness、引用支持和拒答终态的确定性判定。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GroundednessAssessment:
    """保存不依赖 LLM Judge 的安全回答质量判定。"""

    status: str
    reason: str
    supported_citation_count: int


def assess_groundedness(
    *,
    answer_status: str,
    citation_validation: str,
    citation_count: int,
) -> GroundednessAssessment:
    """将引用校验结果映射为 grounded/abstained/ungrounded 状态。"""

    if answer_status in {"abstained", "not_found"}:
        if citation_count:
            return GroundednessAssessment("ungrounded", "abstention_has_citations", 0)
        return GroundednessAssessment("abstained", "no_supported_evidence", 0)
    if answer_status == "answered" and citation_validation == "passed" and citation_count > 0:
        return GroundednessAssessment("grounded", "validated_identity_citations", citation_count)
    return GroundednessAssessment("ungrounded", "citation_support_failed", 0)


__all__ = ["GroundednessAssessment", "assess_groundedness"]
