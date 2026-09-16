"""供部署 init container 和 CI 调用的一次性 migration 命令。"""

from __future__ import annotations

import asyncio
import sys

from enterprise_rag.config.settings import Settings
from enterprise_rag.db.migrations import LATEST_VERSION
from enterprise_rag.db.session import Database


async def _run_migration() -> None:
    """读取部署环境并执行 migration；成功后才允许 one-shot 容器退出 0。"""

    settings = Settings.from_env()
    if not settings.postgres_dsn:
        raise ValueError("POSTGRES_DSN 未配置，无法执行数据库 migration")

    database = Database(
        settings.postgres_dsn,
        pool_size=settings.postgres_pool_size,
        max_overflow=settings.postgres_max_overflow,
        pool_timeout=settings.postgres_pool_timeout_seconds,
        pool_recycle=settings.postgres_pool_recycle_seconds,
        statement_timeout_ms=settings.postgres_statement_timeout_ms,
        lock_timeout_ms=settings.postgres_lock_timeout_ms,
    )
    try:
        await database.migrate()
    finally:
        # one-shot 进程退出前主动释放 engine，确保连接不会在容器关闭阶段悬挂。
        await database.dispose()


def main() -> int:
    """返回 shell 可判定的退出码；不把 DSN 或密码写入标准错误。"""

    try:
        asyncio.run(_run_migration())
    except Exception as exc:
        print(f"数据库 migration 失败: {type(exc).__name__}", file=sys.stderr)
        return 1

    print(f"数据库 migration 已完成: {LATEST_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
