"""F2-G 原文摘录的领域数据类型。

这些类型只描述调用方提交的文本和模型候选摘录，不代表文档来源、租户身份或授权结果。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EvidenceInput:
    """调用方提交的一条待核验证据。"""

    evidence_id: str
    text: str
    source: str | None = None


@dataclass(frozen=True, slots=True)
class ExcerptCandidate:
    """模型返回的原文摘录候选，位置使用 Python 字符索引。"""

    evidence_id: str
    quote: str
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True, slots=True)
class ValidatedExcerpt:
    """通过 identity 校验后的摘录；不表示语义正确或来源真实。"""

    evidence_id: str
    quote: str
    start: int
    end: int
    source: str | None
