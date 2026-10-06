"""全写入门禁：PG session advisory lock 跨进程覆盖外部 I/O，不持长事务。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

_LOCK_ID = 718294650301


class MaintenanceBlocked(RuntimeError):
    """维护窗口或门禁不可用时，写入必须明确拒绝。"""


class WriteGate:
    """API/Worker 与备份工具必须共用同一个 PostgreSQL 锁 namespace。"""

    def __init__(self, database):
        """门禁只支持 PG；SQLite 的替身只能用于离线测试，不能生产装配。"""
        self.database = database
        # 长时门禁使用独立连接，不占用业务短事务池，避免并发上传耗尽后自锁。
        self.engine = (
            create_async_engine(database.url, poolclass=NullPool, pool_pre_ping=True)
            if database.url.startswith("postgresql+asyncpg://") else None
        )

    async def close(self):
        """释放独立门禁 engine，不删除任何数据。"""
        if self.engine is not None:
            await self.engine.dispose()

    @asynccontextmanager
    async def _lock(self, *, exclusive: bool):
        """独立连接持 session lock，事务提交不会释放；连接故障停止受保护操作。"""
        if not self.database.url.startswith("postgresql+asyncpg://"):
            raise MaintenanceBlocked("写入门禁需要 PostgreSQL")
        connection = await self.engine.connect()
        suffix = "" if exclusive else "_shared"
        acquired = False
        heartbeat = None
        lost = False
        owner = asyncio.current_task()
        try:
            acquired = await connection.scalar(text(
                f"SELECT pg_try_advisory_lock{suffix}(:identity)",
            ), {"identity": _LOCK_ID})
            await connection.commit()
            if not acquired:
                raise MaintenanceBlocked("维护窗口中，暂不接受写入")

            async def keepalive():
                """锁连接失效即取消受保护操作，不能在失去维护锁后宣布一致快照。"""
                nonlocal lost
                try:
                    while True:
                        await asyncio.sleep(2)
                        async with asyncio.timeout(5):
                            await connection.execute(text("SELECT 1"))
                            await connection.commit()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - 连接异常固定映射，禁止输出 DSN。
                    lost = True
                    owner.cancel()

            heartbeat = asyncio.create_task(keepalive())
            try:
                yield
            except asyncio.CancelledError:
                if lost:
                    raise MaintenanceBlocked("写入门禁连接失效") from None
                raise
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                if acquired and not lost:
                    await connection.execute(text(
                        f"SELECT pg_advisory_unlock{suffix}(:identity)",
                    ), {"identity": _LOCK_ID})
                    await connection.commit()
            except BaseException:
                # 取消/异常导致无法确认解锁时丢弃连接，绝不带 session lock 归还池。
                await asyncio.shield(connection.invalidate())
                raise
            finally:
                await asyncio.shield(connection.close())

    def writer(self):
        """业务变更持 shared lock，备份持 exclusive lock 时失败关闭。"""
        return self._lock(exclusive=False)

    def maintenance(self):
        """所有参与写入结束后才取得 exclusive；取得失败不能拍不一致快照。"""
        return self._lock(exclusive=True)
