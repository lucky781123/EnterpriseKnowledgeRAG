"""Dense generation 回填、retire 与清理的 contract 测试。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from enterprise_rag.domain.entities import VersionStatus
from enterprise_rag.retrieval.contracts import IndexProfile
from enterprise_rag.retrieval.generation import backfill_versions


@dataclass
class _Version:
    """测试用版本快照。"""

    version_id: str
    status: VersionStatus


@pytest.mark.asyncio
async def test_backfill_only_builds_ready_versions_and_never_publishes() -> None:
    """历史回填只处理 READY version，构建完成后保持非 ACTIVE。"""

    versions = [
        _Version("version-ready", VersionStatus.READY),
        _Version("version-processing", VersionStatus.PROCESSING),
        _Version("version-failed", VersionStatus.FAILED),
    ]
    profile = IndexProfile("demo-model", 2, "parent-child-v1", "dense-v1")
    started: list[str] = []
    built: list[str] = []

    async def start(version: _Version, _profile: IndexProfile):
        """记录 generation 创建，不执行 pointer 发布。"""

        started.append(version.version_id)
        return {"version_id": version.version_id, "status": "building"}

    async def build(generation, _version: _Version):
        """记录构建完成并返回 READY 事实。"""

        built.append(generation["version_id"])
        generation["status"] = "ready"
        return generation

    results = await backfill_versions(
        object(), versions, profile=profile, start_generation=start, build_generation=build
    )

    assert started == ["version-ready"]
    assert built == ["version-ready"]
    assert results == [{"version_id": "version-ready", "status": "ready"}]
    assert all(item["status"] != "active" for item in results)
