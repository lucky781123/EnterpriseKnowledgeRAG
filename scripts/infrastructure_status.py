"""私有运维聚合状态 CLI，不对外暴露租户任务详情或配置。"""

import asyncio
import json
import os

from sqlalchemy import func, select

from enterprise_rag.db.async_ingestion import AsyncOutboxModel, AsyncStageModel, AsyncTaskModel
from enterprise_rag.db.session import Database
from enterprise_rag.infrastructure.runtime import RedisRuntime
from enterprise_rag.infrastructure.settings import InfrastructureSettings


async def status():
    """输出任务/死信/outbox/租约聚合与协调 Redis 持久化、淘汰状态。"""
    database = Database(os.environ["POSTGRES_DSN"])
    runtime = RedisRuntime.create(InfrastructureSettings.from_env())
    try:
        async with database.session() as session:
            tasks = dict((await session.execute(select(
                AsyncTaskModel.status, func.count(),
            ).group_by(AsyncTaskModel.status))).all())
            pending = await session.scalar(select(func.count()).select_from(AsyncOutboxModel).where(
                AsyncOutboxModel.sent_at.is_(None),
            ))
            oldest = await session.scalar(select(func.min(AsyncOutboxModel.available_at)).where(
                AsyncOutboxModel.sent_at.is_(None),
            ))
            expired = await session.scalar(select(func.count()).select_from(AsyncStageModel).where(
                AsyncStageModel.status == "running", AsyncStageModel.lease_until <= func.now(),
            ))
        info = await runtime.coordination.info()
        config = await runtime.coordination.config_get("maxmemory-policy")
        print(json.dumps({
            "tasks": tasks, "outbox_pending": pending,
            "outbox_oldest_at": oldest.isoformat() if oldest else None,
            "expired_stage_leases": expired,
            "coordination": {
                "evicted_keys": info.get("evicted_keys"),
                "used_memory": info.get("used_memory"),
                "aof_enabled": info.get("aof_enabled"),
                "aof_last_write_status": info.get("aof_last_write_status"),
                "maxmemory_policy": config.get("maxmemory-policy"),
            },
        }))
    finally:
        await runtime.close()
        await database.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(status())
    except Exception:  # noqa: BLE001 - 运维结果不能回显凭据和连接错误。
        print('{"status": "unavailable"}')
        raise SystemExit(1) from None
