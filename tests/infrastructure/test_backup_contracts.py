"""备份 CLI 安全门禁与完整性离线测试，不冒充恢复演练。"""

import hashlib
import json
from pathlib import Path

import pytest

from enterprise_rag.db.session import Database
from enterprise_rag.infrastructure.backup import (
    COMPONENTS,
    BackupError,
    RestoreRunner,
    project_path,
    verify,
)
from enterprise_rag.infrastructure.settings import InfrastructureSettings


def bundle(root: Path):
    """构造不含业务数据的备份替身清单。"""
    root.mkdir()
    files = {}
    for name in ("postgres.dump", "redis_cache.rdb", "redis_coordination.rdb"):
        data = b"contract-only"
        (root / name).write_bytes(data)
        files[name] = {"file": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    manifest = {
        "format": 1, "status": "complete", "components": sorted(COMPONENTS),
        "files": files, "objects": [], "backup_id": "test",
        "weaviate": {"status": "SUCCESS", "backup_id": "test", "backend": "s3"},
        "source": {
            "postgres": ["source-pg", 5432, "rag"],
            "minio": ["https://source-minio", "rag"],
            "weaviate": "https://source-vector",
            "redis": [["source-cache", 6379], ["source-coord", 6379]],
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_verify_rejects_missing_tampered_and_traversing_artifacts(tmp_path):
    """部分失败、hash 不一致和路径穿越都不能进入恢复阶段。"""
    manifest = bundle(tmp_path / "backup")
    assert verify(tmp_path / "backup")["status"] == "complete"
    (tmp_path / "backup/postgres.dump").write_bytes(b"tampered")
    with pytest.raises(BackupError):
        verify(tmp_path / "backup")
    manifest["files"]["postgres.dump"]["file"] = "../outside"
    (tmp_path / "backup/manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BackupError):
        verify(tmp_path / "backup")


def test_project_boundary_rejects_external_directory(tmp_path):
    """备份文件不能跳出当前项目目录。"""
    with pytest.raises(BackupError):
        project_path(tmp_path, tmp_path.parent / "other-project")


@pytest.mark.parametrize("component", ["postgres", "minio", "weaviate", "redis"])
async def test_restore_rejects_any_source_target_overlap(tmp_path, component):
    """不能只检查主数据库而漏掉 Redis 或向量库覆盖风险。"""
    manifest = bundle(tmp_path / "backup")
    source = manifest["source"]
    database = Database("postgresql+asyncpg://target:password@target-pg:5432/rag")
    infra = InfrastructureSettings(
        redis_cache_url="redis://target-cache:6379/0",
        redis_coordination_url="redis://target-coord:6379/0",
        s3_endpoint="https://target-minio", s3_bucket="target",
    )
    runner = RestoreRunner(tmp_path, database, infra, weaviate_url="https://target-vector")
    if component == "postgres":
        source["postgres"] = ["target-pg", 5432, "rag"]
    elif component == "minio":
        source["minio"] = ["https://target-minio", "target"]
    elif component == "weaviate":
        source["weaviate"] = "https://target-vector"
    else:
        source["redis"][0] = ["target-cache", 6379]
    try:
        with pytest.raises(BackupError):
            runner.validate_target(manifest)
    finally:
        await database.dispose()
