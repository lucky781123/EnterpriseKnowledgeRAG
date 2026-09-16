"""摄取流水线的稳定数据契约。

这些对象使用不可变 dataclass，便于在 parser、chunker、对象存储和测试之间传递。
`to_dict()` 明确控制 JSON 字段顺序和枚举/元组的转换，避免直接依赖 dataclass
内部表示导致 parsed artifact 在重放时发生无意义变化。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from enterprise_rag.domain.entities import JobStatus, VersionStatus


@dataclass(frozen=True, slots=True)
class IngestionRequest:
    """调用方提交的一次文档摄取请求。"""

    tenant_id: str
    source_id: str
    title: str
    filename: str
    content: bytes
    idempotency_key: str
    content_type: str | None = None
    source_revision: str | None = None


@dataclass(frozen=True, slots=True)
class ValidatedRequest:
    """通过入口校验后的请求，附带规范化文本和媒体信息。"""

    request: IngestionRequest
    extension: str
    media_type: str
    text: str | None
    content_hash: str


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    """解析后的结构化 block 及其原文 lineage。"""

    block_id: str
    block_type: str
    text: str
    heading_path: tuple[str, ...]
    source_locator: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 兼容字典；不暴露 Python tuple 或自定义对象。"""

        return {
            "block_id": self.block_id,
            "block_type": self.block_type,
            "text": self.text,
            "heading_path": list(self.heading_path),
            "source_locator": dict(self.source_locator),
        }


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """一个 parent 或 child chunk 的稳定身份和来源 lineage。"""

    chunk_id: str
    parent_id: str | None
    version_id: str
    position: int
    text: str
    text_hash: str
    heading_path: tuple[str, ...]
    source_block_ids: tuple[str, ...]
    source_locators: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        """转换为 parsed manifest 中的 JSON 结构。"""

        return {
            "chunk_id": self.chunk_id,
            "parent_id": self.parent_id,
            "version_id": self.version_id,
            "position": self.position,
            "text": self.text,
            "text_hash": self.text_hash,
            "heading_path": list(self.heading_path),
            "source_block_ids": list(self.source_block_ids),
            "source_locators": [dict(locator) for locator in self.source_locators],
        }


@dataclass(frozen=True, slots=True)
class ChunkManifest:
    """一次 chunking 的完整 parent/child 清单。"""

    strategy_version: str
    parent_size: int
    child_size: int
    parents: tuple[ChunkRecord, ...]
    children: tuple[ChunkRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        """按固定字段输出 manifest，保证同一输入可重放比较。"""

        return {
            "strategy_version": self.strategy_version,
            "parent_size": self.parent_size,
            "child_size": self.child_size,
            "parents": [parent.to_dict() for parent in self.parents],
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """摄取任务的安全结果摘要，不携带原文或凭据。"""

    job_id: str
    tenant_id: str
    document_id: str
    status: JobStatus
    version_id: str | None
    version_status: VersionStatus | None
    raw_object_key: str | None = None
    parsed_object_key: str | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """提供日志/接口可用的安全摘要；状态统一转为字符串。"""

        value = asdict(self)
        value["status"] = self.status.value
        value["version_status"] = self.version_status.value if self.version_status else None
        return value


__all__ = [
    "ChunkManifest",
    "ChunkRecord",
    "IngestionRequest",
    "IngestionResult",
    "ParsedBlock",
    "ValidatedRequest",
]

