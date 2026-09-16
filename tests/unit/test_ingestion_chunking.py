"""确定性 parent/child chunk 策略的单元测试。"""

from __future__ import annotations

from enterprise_rag.ingestion.chunking import chunk_blocks
from enterprise_rag.ingestion.contracts import ParsedBlock


def _blocks() -> list[ParsedBlock]:
    """提供带标题和 locator 的固定输入，验证 lineage 不会在切块时丢失。"""

    return [
        ParsedBlock(
            block_id="b-1",
            block_type="heading",
            text="部署",
            heading_path=("部署",),
            source_locator={"kind": "line", "start": 1, "end": 1},
        ),
        ParsedBlock(
            block_id="b-2",
            block_type="paragraph",
            text="这是第一段说明。",
            heading_path=("部署",),
            source_locator={"kind": "line", "start": 3, "end": 3},
        ),
        ParsedBlock(
            block_id="b-3",
            block_type="paragraph",
            text="这是第二段说明，包含足够长的内容用于触发 child 切分。" * 4,
            heading_path=("部署",),
            source_locator={"kind": "line", "start": 5, "end": 5},
        ),
    ]


def test_chunk_ids_are_stable_and_child_keeps_parent_lineage():
    """相同 version、策略和内容必须产生完全相同的 ID 与顺序。"""

    first = chunk_blocks(_blocks(), version_id="version-1", parent_size=120, child_size=40)
    second = chunk_blocks(_blocks(), version_id="version-1", parent_size=120, child_size=40)

    assert first.to_dict() == second.to_dict()
    assert first.parents
    assert first.children
    assert all(child.parent_id in {parent.chunk_id for parent in first.parents} for child in first.children)
    assert all(child.source_block_ids for child in first.children)
    assert all(len(child.text) <= 40 for child in first.children)


def test_strategy_or_version_change_changes_chunk_identity():
    """版本或策略变化必须生成新身份，避免不同产物混在一起。"""

    baseline = chunk_blocks(_blocks(), version_id="version-1", parent_size=120, child_size=40)
    other_version = chunk_blocks(_blocks(), version_id="version-2", parent_size=120, child_size=40)
    other_strategy = chunk_blocks(
        _blocks(), version_id="version-1", parent_size=120, child_size=40, strategy_version="v2"
    )

    assert baseline.parents[0].chunk_id != other_version.parents[0].chunk_id
    assert baseline.parents[0].chunk_id != other_strategy.parents[0].chunk_id

