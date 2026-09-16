"""原文摘录身份校验的确定性契约测试。"""

import pytest

from enterprise_rag.excerpts.protocols import EvidenceInput, ExcerptCandidate
from enterprise_rag.excerpts.validation import (
    ExcerptValidationError,
    validate_evidence_bundle,
    validate_excerpt,
)


def test_evidence_bundle_rejects_duplicate_ids_and_empty_text() -> None:
    """证据 ID 是请求内引用的身份键，重复或空正文都不能进入后续 Prompt。"""

    with pytest.raises(ExcerptValidationError, match="evidence_id"):
        validate_evidence_bundle(
            [
                EvidenceInput("doc-1", "第一段"),
                EvidenceInput("doc-1", "第二段"),
            ]
        )

    with pytest.raises(ExcerptValidationError, match="text"):
        validate_evidence_bundle([EvidenceInput("doc-2", "   ")])


def test_excerpt_without_position_requires_unique_exact_match() -> None:
    """未给位置时只允许原文中唯一出现，避免重复句子被错误指认。"""

    evidence = validate_evidence_bundle(
        [EvidenceInput("doc-1", "alpha beta alpha")]
    )

    with pytest.raises(ExcerptValidationError, match="歧义"):
        validate_excerpt(ExcerptCandidate("doc-1", "alpha"), {"doc-1": evidence[0]})


def test_excerpt_with_position_requires_exact_python_slice() -> None:
    """start/end 使用 Python 字符索引，并且切片必须逐字符等于 quote。"""

    evidence = validate_evidence_bundle([EvidenceInput("doc-1", "标题\n正文")])
    result = validate_excerpt(
        ExcerptCandidate("doc-1", "正文", start=3, end=5),
        {"doc-1": evidence[0]},
    )

    assert result.start == 3
    assert result.end == 5
    assert result.quote == "正文"

    with pytest.raises(ExcerptValidationError, match="不一致"):
        validate_excerpt(
            ExcerptCandidate("doc-1", "正文", start=0, end=2),
            {"doc-1": evidence[0]},
        )


def test_excerpt_rejects_unknown_evidence_and_invalid_range() -> None:
    """未知证据或越界位置不能被服务端静默修正。"""

    evidence = validate_evidence_bundle([EvidenceInput("doc-1", "正文")])

    with pytest.raises(ExcerptValidationError, match="不存在"):
        validate_excerpt(
            ExcerptCandidate("missing", "正文", start=0, end=2),
            {"doc-1": evidence[0]},
        )

    with pytest.raises(ExcerptValidationError, match="范围"):
        validate_excerpt(
            ExcerptCandidate("doc-1", "正文", start=0, end=99),
            {"doc-1": evidence[0]},
        )
