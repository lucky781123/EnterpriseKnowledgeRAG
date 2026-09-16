"""原文摘录的确定性 identity 校验。

校验目标是证明 quote 指向调用方本次提交的字符区间。它不验证事实支持、来源真实性、
文档版本、租户权限或 ACL，因此不能被当作完整 RAG 正确性证明。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .protocols import EvidenceInput, ExcerptCandidate, ValidatedExcerpt

_EVIDENCE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ExcerptValidationError(ValueError):
    """输入证据或摘录不满足 F2-G 契约。"""


def validate_evidence_bundle(
    evidence: Sequence[EvidenceInput],
) -> tuple[EvidenceInput, ...]:
    """验证证据包的数量、身份键和文本边界，并保留调用方顺序。"""

    if not 1 <= len(evidence) <= 16:
        raise ExcerptValidationError("evidence 数量必须在 1 到 16 条之间")

    seen: set[str] = set()
    total_bytes = 0
    validated: list[EvidenceInput] = []
    for item in evidence:
        if not _EVIDENCE_ID.fullmatch(item.evidence_id):
            raise ExcerptValidationError("evidence_id 格式不合法")
        if item.evidence_id in seen:
            raise ExcerptValidationError("evidence_id 不得重复")
        if not item.text or not item.text.strip():
            raise ExcerptValidationError("evidence.text 不能为空")
        if len(item.text.encode("utf-8")) > 8 * 1024:
            raise ExcerptValidationError("单条 evidence.text 超过 8 KiB")
        if item.source is not None and (not item.source.strip() or len(item.source) > 512):
            raise ExcerptValidationError("evidence.source 为空或超过 512 个字符")
        seen.add(item.evidence_id)
        total_bytes += len(item.text.encode("utf-8"))
        validated.append(item)

    if total_bytes > 32 * 1024:
        raise ExcerptValidationError("evidence.text 总大小超过 32 KiB")
    return tuple(validated)


def validate_excerpt(
    candidate: ExcerptCandidate,
    evidence: Mapping[str, EvidenceInput],
) -> ValidatedExcerpt:
    """核验摘录是否逐字符等于 evidence.text 的对应区间。"""

    item = evidence.get(candidate.evidence_id)
    if item is None:
        raise ExcerptValidationError("引用的 evidence_id 不存在")
    if not candidate.quote:
        raise ExcerptValidationError("quote 不能为空")

    if (candidate.start is None) != (candidate.end is None):
        raise ExcerptValidationError("start 和 end 必须同时提供")

    if candidate.start is None:
        first = item.text.find(candidate.quote)
        if first < 0:
            raise ExcerptValidationError("quote 与原文不一致")
        if item.text.find(candidate.quote, first + 1) >= 0:
            raise ExcerptValidationError("quote 在原文中出现多次，位置存在歧义")
        start, end = first, first + len(candidate.quote)
    else:
        start, end = candidate.start, candidate.end
        if start < 0 or end < start or end > len(item.text):
            raise ExcerptValidationError("摘录位置范围不合法")
        if item.text[start:end] != candidate.quote:
            raise ExcerptValidationError("quote 与原文位置切片不一致")

    return ValidatedExcerpt(
        evidence_id=item.evidence_id,
        quote=candidate.quote,
        start=start,
        end=end,
        source=item.source,
    )
