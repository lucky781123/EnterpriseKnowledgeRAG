"""F2-G 原文摘录链路的确定性评测观察值。

本模块故意只做两件事：保存实际输出，并重新执行原文摘录的 identity 校验。
它不调用模型、不运行检索、不猜测答案是否符合事实，也不把调用方提交的
``evidence`` 当成已经通过 ACL 或来源认证的企业文档。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from collections.abc import Mapping, Sequence
from typing import Any

from enterprise_rag.excerpts.protocols import EvidenceInput, ExcerptCandidate
from enterprise_rag.excerpts.validation import (
    ExcerptValidationError,
    validate_evidence_bundle,
    validate_excerpt,
)


@dataclass(frozen=True, slots=True)
class EvaluationObservation:
    """一条可序列化的摘录评测结果。

    ``identity_passed`` 只表示所有已返回的 citation 是否逐字符属于本次
    调用方提交的 evidence；它不代表事实正确、语义支持、来源真实性或权限
    已通过。语义指标和检索指标在当前 F2-G 边界内没有运行，因此使用明确的
    ``not_run``/``not_applicable``，而不是伪造数值分数。
    """

    case_id: str
    question: str
    identity_passed: bool
    semantic_quality: str = "not_run"
    retrieval_metrics: str = "not_applicable"
    actual_output: str | None = None
    answer_status: str | None = None
    citation_validation: str | None = None
    expected_evidence: tuple[str, ...] = ()
    observed_evidence: tuple[str, ...] = ()
    identity_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """返回适合写入 JSON/JSONL 产物的普通字典。"""

        return asdict(self)


def _value(response: Mapping[str, Any] | Any, name: str, default: Any = None) -> Any:
    """同时支持 API 字典和应用服务 dataclass，避免评测层绑定单一调用方式。"""

    if isinstance(response, Mapping):
        return response.get(name, default)
    return getattr(response, name, default)


def _normalize_evidence(
    evidence: Sequence[EvidenceInput | Mapping[str, Any]],
) -> tuple[EvidenceInput, ...]:
    """把测试 fixture/API 字典归一化为领域类型，并保留原顺序。"""

    normalized: list[EvidenceInput] = []
    for item in evidence:
        if isinstance(item, EvidenceInput):
            normalized.append(item)
            continue
        normalized.append(
            EvidenceInput(
                evidence_id=str(item["evidence_id"]),
                text=str(item["text"]),
                source=item.get("source"),
            )
        )
    return tuple(normalized)


def evaluate_excerpt_response(
    *,
    case_id: str,
    question: str,
    evidence: Sequence[EvidenceInput | Mapping[str, Any]],
    response: Mapping[str, Any] | Any,
    expected_evidence: Sequence[str] = (),
) -> EvaluationObservation:
    """评估一次摘录响应的确定性结构与引用身份。

    这里不重新生成答案，也不把 ``expected_evidence`` 直接转化为通过/失败：
    该字段属于评测样例的黄金标注，真正的召回质量需要独立的检索评测。当前
    函数只记录它，避免把“返回了某个 ID”误报为完整检索正确性。
    """

    answer_status = _value(response, "answer_status")
    citation_validation = _value(response, "citation_validation")
    actual_output = _value(response, "message")
    if not isinstance(actual_output, str):
        actual_output = None

    raw_citations = _value(response, "citations", ())
    if raw_citations is None:
        raw_citations = ()

    observed_ids: list[str] = []
    identity_passed = True
    identity_reason: str | None = None

    try:
        normalized_evidence = _normalize_evidence(evidence)
        validated_evidence = validate_evidence_bundle(normalized_evidence)
        evidence_by_id = {item.evidence_id: item for item in validated_evidence}

        if not isinstance(raw_citations, Sequence) or isinstance(raw_citations, (str, bytes)):
            raise ExcerptValidationError("citations 必须是列表")

        for raw_citation in raw_citations:
            if not isinstance(raw_citation, Mapping):
                raise ExcerptValidationError("citation 必须是对象")
            candidate = ExcerptCandidate(
                evidence_id=str(raw_citation["evidence_id"]),
                quote=str(raw_citation["quote"]),
                start=raw_citation.get("start"),
                end=raw_citation.get("end"),
            )
            validate_excerpt(candidate, evidence_by_id)
            observed_ids.append(candidate.evidence_id)
    except (KeyError, TypeError, ValueError, ExcerptValidationError) as exc:
        # 评测输入异常和引用 identity 失败都应成为可观察的 false，而不是让
        # 单个坏样例中断整批评测；原因只记录稳定的本地分类，不回显正文。
        identity_passed = False
        identity_reason = str(exc)

    # “无答案”没有 citation 时，identity 检查在语义上不适用，但没有发现
    # 非法引用；将其视为结构通过，同时把语义正确性继续留给人工/独立指标。
    if answer_status in {"not_found", "abstained"} and not raw_citations and identity_passed:
        citation_validation = citation_validation or "not_applicable"
    elif answer_status in {"incomplete", "failed", "cancelled"}:
        # 生成未完整结束时，不能把尚未执行的引用检查写成通过。
        identity_passed = False
        citation_validation = citation_validation or "not_checked"
    elif answer_status == "answered" and not raw_citations:
        identity_passed = False
        citation_validation = citation_validation or "failed"

    return EvaluationObservation(
        case_id=case_id,
        question=question,
        identity_passed=identity_passed,
        actual_output=actual_output,
        answer_status=answer_status if isinstance(answer_status, str) else None,
        citation_validation=(
            citation_validation if isinstance(citation_validation, str) else None
        ),
        expected_evidence=tuple(map(str, expected_evidence)),
        observed_evidence=tuple(observed_ids),
        identity_reason=identity_reason,
    )

