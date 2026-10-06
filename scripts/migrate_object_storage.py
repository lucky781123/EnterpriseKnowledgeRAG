"""本地原文/manifest 渐进迁移 MinIO，默认计划、不删除源对象、不覆盖冲突。"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from enterprise_rag.infrastructure.backup import BackupError, digest, project_path
from enterprise_rag.infrastructure.settings import InfrastructureSettings
from enterprise_rag.storage.s3 import S3ObjectStorage

ROOT = Path(__file__).resolve().parents[1]


def migrate_files(source: Path, storage):
    """流式迁移所有版本产物，同 key 同 hash 才允许复用。"""
    records = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise BackupError("迁移不接受符号链接")
        if not path.is_file():
            continue
        key = path.relative_to(source).as_posix()
        before, size = digest(path), path.stat().st_size
        destination = storage._key(key)
        try:
            response = storage.client.get_object(Bucket=storage.bucket, Key=destination)
        except Exception as exc:  # noqa: BLE001 - 仅明确 404 才允许写入目标。
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code not in {"404", "NotFound", "NoSuchKey"}:
                raise BackupError("目标对象状态不可判定") from None
            storage.client.upload_file(str(path), storage.bucket, destination)
            response = storage.client.get_object(Bucket=storage.bucket, Key=destination)
        body = response["Body"]
        actual, received = hashlib.sha256(), 0
        try:
            while chunk := body.read(1024 * 1024):
                actual.update(chunk)
                received += len(chunk)
        finally:
            body.close()
        if actual.hexdigest() != before or received != size or digest(path) != before:
            raise BackupError("迁移 hash/size 冲突或源对象在维护期发生变化")
        records.append({"key": key, "sha256": before, "size": size})
    return records


async def run(args):
    """执行时持全局维护门禁；不能边上传边迁移再声称一致。"""
    import os

    from enterprise_rag.db.session import Database
    from enterprise_rag.infrastructure.write_gate import WriteGate

    source = project_path(ROOT, args.source)
    if not source.is_dir():
        raise BackupError("迁移源目录不存在")
    if not args.execute:
        count = sum(path.is_file() for path in source.rglob("*"))
        print(json.dumps({"mode": "plan", "files": count, "source_deleted": False}))
        return
    database = Database(os.environ["POSTGRES_DSN"])
    gate = WriteGate(database)
    storage = S3ObjectStorage.from_settings(InfrastructureSettings.from_env())
    try:
        async with gate.maintenance():
            records = await asyncio.to_thread(migrate_files, source, storage)
            report = project_path(ROOT, args.report)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(
                {"status": "copied_verified", "source_deleted": False, "objects": records}, indent=2,
            ))
            print(json.dumps({"status": "copied_verified", "objects": len(records), "source_deleted": False}))
    finally:
        await storage.close()
        await gate.close()
        await database.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--report", default=".local/object-migration/report.json")
    parser.add_argument("--execute", action="store_true")
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception:  # noqa: BLE001 - 不输出对象内容或凭据。
        print('{"status": "failed", "kind": "object_migration_failed"}')
        raise SystemExit(1) from None
