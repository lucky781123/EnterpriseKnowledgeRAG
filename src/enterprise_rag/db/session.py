"""异步数据库 engine、session 和显式事务边界。

应用代码显式选择只读 session 或自动提交/回滚的 transaction，避免隐藏式 commit。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


def normalize_database_url(url: str) -> str:
    """将常见 DSN 规范化为 SQLAlchemy async driver URL。"""

    if not url or not url.strip():
        raise ValueError("数据库 URL 不能为空")
    url = url.strip()
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    if url.startswith("sqlite://") and "+aiosqlite" not in url:
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


class Database:
    """封装单个数据库 engine，并集中管理连接与事务生命周期。"""

    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        pool_size: int = 3,
        max_overflow: int = 2,
        pool_timeout: int = 10,
        pool_recycle: int = 1800,
        statement_timeout_ms: int | None = 30_000,
        lock_timeout_ms: int | None = 5_000,
        **engine_options: Any,
    ) -> None:
        self.url = normalize_database_url(url)
        self._is_postgres = self.url.startswith("postgresql+asyncpg://")
        self.statement_timeout_ms = statement_timeout_ms
        self.lock_timeout_ms = lock_timeout_ms
        options: dict[str, Any] = {"echo": echo, "pool_pre_ping": True}
        if self._is_postgres:
            # 单机开发和 CI 使用小常驻池，只允许少量短时突发，避免连接数量随请求
            # 无限增长；pool_recycle 则定期淘汰长期存活连接，降低网络设备或服务端
            # 已关闭陈旧连接被重新取出的概率。
            options.update(
                {
                    "pool_size": pool_size,
                    "max_overflow": max_overflow,
                    "pool_timeout": pool_timeout,
                    "pool_recycle": pool_recycle,
                }
            )
        options.update(engine_options)
        if self.url.startswith("sqlite+aiosqlite://"):
            options.setdefault("connect_args", {"check_same_thread": False})
            if ":memory:" in self.url:
                options.setdefault("poolclass", StaticPool)
        self.engine: AsyncEngine = create_async_engine(self.url, **options)
        if self.url.startswith("sqlite+aiosqlite://"):

            @event.listens_for(self.engine.sync_engine, "connect")
            def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """提供不自动提交的 session。"""

        async with self.session_factory() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """提供单一数据库事务；异常时自动回滚。"""

        async with self.session_factory() as session:
            async with session.begin():
                if self._is_postgres:
                    # SET LOCAL 只在当前事务内生效。连接归还池后不会把本次请求的
                    # 超时设置泄漏给下一个请求，这是连接池场景下比 SET 更安全的边界。
                    if self.statement_timeout_ms is not None:
                        await session.execute(
                            text(f"SET LOCAL statement_timeout = {self.statement_timeout_ms}")
                        )
                    if self.lock_timeout_ms is not None:
                        await session.execute(
                            text(f"SET LOCAL lock_timeout = {self.lock_timeout_ms}")
                        )
                yield session

    async def dispose(self) -> None:
        """释放 engine 连接池。"""

        await self.engine.dispose()

    async def migrate(self) -> None:
        """执行仓库内 Python migration contract。"""

        from enterprise_rag.db.migrations import upgrade_database

        await upgrade_database(self.engine)

    async def schema_readiness(self):
        """读取 migration/schema 状态，绝不在 readiness 请求中执行 migration。"""

        from enterprise_rag.db.migrations import check_database_readiness

        return await check_database_readiness(self.engine)

    async def health_check(self) -> bool:
        """执行最小连通性查询，不替代 schema readiness。"""

        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return False
        return True


__all__ = ["Database", "normalize_database_url"]
