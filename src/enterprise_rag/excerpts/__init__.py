"""F2-G 原文摘录领域模块。"""

from .protocols import EvidenceInput, ExcerptCandidate, ValidatedExcerpt
from .validation import ExcerptValidationError, validate_evidence_bundle, validate_excerpt

__all__ = [
    "EvidenceInput",
    "ExcerptCandidate",
    "ValidatedExcerpt",
    "ExcerptValidationError",
    "validate_evidence_bundle",
    "validate_excerpt",
]
