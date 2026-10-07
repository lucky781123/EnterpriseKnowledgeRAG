"""备份/隔离恢复 CLI；默认只输出计划，所有真实变更需要 --execute。"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from enterprise_rag.db.session import Database
from enterprise_rag.infrastructure.backup import (
    BackupError,
    BackupRunner,
    RestoreRunner,
    build_backup_plan,
    project_path,
    verify,
)
from enterprise_rag.infrastructure.settings import InfrastructureSettings

ROOT = Path(__file__).resolve().parents[1]


async def main_async(args):
    """恢复使用独立前缀配置，避免误用当前运行服务的连接信息。"""
    directory = project_path(ROOT, args.directory)
    if args.action == "plan":
        print(json.dumps(build_backup_plan(args.plan_action, directory), ensure_ascii=False, indent=2))
        return
    if args.action == "verify":
        await asyncio.to_thread(verify, directory)
        print('{"local_artifact_integrity": true, "runtime_acceptance": false}')
        return
    if not args.execute:
        print(json.dumps(build_backup_plan(args.action, directory), ensure_ascii=False, indent=2))
        return
    prefix = "RESTORE_" if args.action in {"restore", "restore-verify"} else ""
    dsn = os.getenv(prefix + "POSTGRES_DSN", "")
    if not dsn:
        raise BackupError("需要显式数据库配置")
    if prefix:
        # 仅在当前 CLI 子进程替换基础设施环境，不修改 .env 或父进程。
        for name in (
            "REDIS_CACHE_URL", "REDIS_COORDINATION_URL", "CELERY_RESULT_BACKEND",
            "S3_ENDPOINT", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_BUCKET",
            "S3_REGION", "S3_PREFIX",
        ):
            os.environ[name] = os.getenv("RESTORE_" + name, "")
        os.environ["RAG_INFRA_ENABLED"] = "true"
        os.environ["RAG_CELERY_ENABLED"] = "false"
        os.environ["OBJECT_STORAGE_PROVIDER"] = "s3"
    infra = InfrastructureSettings.from_env()
    database = Database(dsn)
    try:
        runner_type = RestoreRunner if prefix else BackupRunner
        runner = runner_type(
            ROOT, database, infra, weaviate_url=os.getenv(prefix + "WEAVIATE_BASE_URL", ""),
            weaviate_key=os.getenv(prefix + "WEAVIATE_API_KEY", ""),
            backend=os.getenv("WEAVIATE_BACKUP_BACKEND", "s3"),
        )
        if args.action == "restore-verify":
            result = await runner.verify_runtime(directory)
        elif prefix:
            if not args.cache_data or not args.coordination_data:
                raise BackupError("恢复需要两个显式离线 Redis 数据目录")
            result = await runner.restore(
                directory, cache_data=Path(args.cache_data), coordination_data=Path(args.coordination_data),
            )
        else:
            result = await runner.run(directory)
        print(json.dumps({"status": result["status"], "backup_id": result["backup_id"]}))
    finally:
        await database.dispose()


def main():
    """脱敏异常，CLI 非零退出不能被解释为完成。"""
    parser = argparse.ArgumentParser(description="维护备份与隔离恢复")
    parser.add_argument("action", choices=["plan", "backup", "verify", "restore", "restore-verify"])
    parser.add_argument("--directory", required=True)
    parser.add_argument(
        "--plan-action",
        choices=["backup", "verify", "restore", "restore-verify"],
        help="action=plan 时生成对应操作计划",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--cache-data")
    parser.add_argument("--coordination-data")
    args = parser.parse_args()
    if args.action == "plan" and args.plan_action is None:
        parser.error("action=plan 时必须提供 --plan-action")
    try:
        asyncio.run(main_async(args))
    except Exception:  # noqa: BLE001 - CLI 禁止回显 endpoint、凭据和原生工具异常。
        print('{"status": "failed", "kind": "backup_restore_failed"}')
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
