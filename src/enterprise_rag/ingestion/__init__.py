"""真实摄取基础的公开入口。

本包只负责把受控文档转换为带 lineage 的解析块和确定性 chunk，并把任务状态
写入现有事实源。它不负责 Embedding、检索、授权或最终版本发布。
"""

from enterprise_rag.ingestion.chunking import chunk_blocks
from enterprise_rag.ingestion.contracts import (
    ChunkManifest,
    ChunkRecord,
    IngestionRequest,
    IngestionResult,
    ParsedBlock,
)
from enterprise_rag.ingestion.parsers import parse_document
from enterprise_rag.ingestion.service import IdempotencyConflictError, IngestionService
from enterprise_rag.ingestion.validation import validate_request

__all__ = [
    "ChunkManifest",
    "ChunkRecord",
    "IdempotencyConflictError",
    "IngestionRequest",
    "IngestionResult",
    "IngestionService",
    "ParsedBlock",
    "chunk_blocks",
    "parse_document",
    "validate_request",
]

