"""确定性 parent/child chunk 生成器。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from enterprise_rag.ingestion.contracts import ChunkManifest, ChunkRecord, ParsedBlock


STRATEGY_VERSION = "parent-child-v1"


def _text_hash(text: str) -> str:
    """所有 chunk 使用 UTF-8 SHA-256，避免平台默认编码造成漂移。"""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identity(prefix: str, values: Iterable[object]) -> str:
    """按固定 JSON 序列化计算稳定 chunk ID。"""

    payload = json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _split_text(text: str, limit: int) -> list[str]:
    """优先在空白处断开，单个超长 token 才退化为字符边界。"""

    if limit <= 0:
        raise ValueError("chunk size 必须大于 0")
    remaining = text.strip()
    pieces: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            pieces.append(remaining)
            break
        cut = remaining.rfind(" ", 0, limit + 1)
        newline_cut = remaining.rfind("\n", 0, limit + 1)
        cut = max(cut, newline_cut)
        if cut <= 0:
            cut = limit
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return [piece for piece in pieces if piece]


def _block_segments(block: ParsedBlock, limit: int) -> list[tuple[str, tuple[str, ...]]]:
    """把超长 block 分成文本片段，同时保留原 block lineage。"""

    return [(piece, (block.block_id,)) for piece in _split_text(block.text, limit)]


def chunk_blocks(
    blocks: Iterable[ParsedBlock],
    *,
    version_id: str,
    parent_size: int = 800,
    child_size: int = 320,
    strategy_version: str = STRATEGY_VERSION,
) -> ChunkManifest:
    """生成不重叠 parent/child chunks，并保留来源 block 和 locator。"""

    if not version_id.strip():
        raise ValueError("version_id 不能为空")
    if parent_size <= 0 or child_size <= 0:
        raise ValueError("chunk size 必须大于 0")
    source_blocks = list(blocks)
    if not source_blocks:
        raise ValueError("至少需要一个 ParsedBlock")

    parents: list[ChunkRecord] = []
    current_text: list[str] = []
    current_ids: list[str] = []
    current_locators: list[dict] = []
    current_heading: tuple[str, ...] = ()

    def flush_parent() -> None:
        """把累计文本封装成一个 parent，并重置缓冲区。"""

        nonlocal current_text, current_ids, current_locators, current_heading
        if not current_text:
            return
        text = "\n\n".join(current_text).strip()
        position = len(parents)
        parent_hash = _text_hash(text)
        parent_id = _identity(
            "parent", [version_id, strategy_version, position, parent_hash]
        )
        parents.append(
            ChunkRecord(
                chunk_id=parent_id,
                parent_id=None,
                version_id=version_id,
                position=position,
                text=text,
                text_hash=parent_hash,
                heading_path=current_heading,
                source_block_ids=tuple(current_ids),
                source_locators=tuple(dict(item) for item in current_locators),
            )
        )
        current_text = []
        current_ids = []
        current_locators = []
        current_heading = ()

    for block in source_blocks:
        for segment, segment_ids in _block_segments(block, parent_size):
            proposed = "\n\n".join((*current_text, segment)).strip()
            if current_text and len(proposed) > parent_size:
                flush_parent()
            current_text.append(segment)
            current_ids.extend(segment_ids)
            current_locators.append(dict(block.source_locator))
            if not current_heading:
                current_heading = block.heading_path
    flush_parent()

    children: list[ChunkRecord] = []
    for parent in parents:
        for local_position, child_text in enumerate(_split_text(parent.text, child_size)):
            child_hash = _text_hash(child_text)
            child_id = _identity(
                "child",
                [parent.chunk_id, version_id, strategy_version, local_position, child_hash],
            )
            children.append(
                ChunkRecord(
                    chunk_id=child_id,
                    parent_id=parent.chunk_id,
                    version_id=version_id,
                    position=len(children),
                    text=child_text,
                    text_hash=child_hash,
                    heading_path=parent.heading_path,
                    source_block_ids=parent.source_block_ids,
                    source_locators=parent.source_locators,
                )
            )

    return ChunkManifest(
        strategy_version=strategy_version,
        parent_size=parent_size,
        child_size=child_size,
        parents=tuple(parents),
        children=tuple(children),
    )


__all__ = ["STRATEGY_VERSION", "chunk_blocks"]
