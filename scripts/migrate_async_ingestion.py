"""显式 additive 0005 one-shot，默认 migration CLI 仍保持旧目标。"""

import asyncio
import os

from enterprise_rag.db.migrations import ASYNC_INGESTION_VERSION, upgrade_database
from enterprise_rag.db.session import Database


async def migrate():
    """仅连接进程显式配置的数据库，不读取 .env，不做 downgrade。"""
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        raise ValueError("需要 POSTGRES_DSN")
    database = Database(dsn)
    try:
        await upgrade_database(database.engine, target_version=ASYNC_INGESTION_VERSION)
    finally:
        await database.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(migrate())
    except Exception:  # noqa: BLE001 - one-shot 输出禁止暴露 DSN/凭据。
        print("async migration failed")
        raise SystemExit(1) from None
    print("0005_async_ingestion ready")
