"""首个仓库内 Python migration contract。

当前项目只有初始 schema，因此使用一次受控 metadata.create_all 创建空库，并在
schema_migrations 记录成功版本。未来 schema 变化必须新增显式版本函数。
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import MetaData, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from enterprise_rag.db.models import Base, SchemaMigrationModel

INITIAL_VERSION = "0001_initial"
SUPPORTED_VERSIONS = frozenset({INITIAL_VERSION})
EXPECTED_TABLES = frozenset(
    {
        "document_acl",
        "document_versions",
        "documents",
        "ingestion_jobs",
        "schema_migrations",
        "tenants",
        "version_artifacts",
    }
)


class MigrationError(RuntimeError):
    """携带目标版本和原始异常，供部署程序明确判定 migration 失败。"""

    def __init__(self, target_version: str, cause: Exception) -> None:
        super().__init__(f"数据库迁移到 {target_version} 失败: {type(cause).__name__}")
        self.target_version = target_version
        self.cause = cause


@dataclass(frozen=True, slots=True)
class MigrationReadiness:
    """描述应用是否可以安全接收流量的 schema 版本状态。"""

    ready: bool
    status: str
    target_version: str
    applied_versions: tuple[str, ...] = ()
    missing_tables: tuple[str, ...] = ()
    unknown_versions: tuple[str, ...] = ()


def _read_applied_versions(connection: Connection) -> set[str]:
    """读取已应用版本；空数据库在版本表出现前返回空集合。"""

    if "schema_migrations" not in inspect(connection).get_table_names():
        return set()
    return set(connection.execute(select(SchemaMigrationModel.version)).scalars())


def _validate_applied_versions(applied_versions: Collection[str]) -> None:
    """拒绝当前代码无法理解的版本，防止旧应用误操作较新的数据库。"""

    unknown_versions = set(applied_versions) - SUPPORTED_VERSIONS
    if unknown_versions:
        versions = ", ".join(sorted(unknown_versions))
        raise RuntimeError(f"数据库包含当前代码不支持的 migration 版本: {versions}")


def _inspect_schema_readiness(
    connection: Connection, target_version: str = INITIAL_VERSION
) -> MigrationReadiness:
    """在已建立的连接上只读检查表集合和 migration 版本，不执行迁移。"""

    if target_version not in SUPPORTED_VERSIONS:
        return MigrationReadiness(
            ready=False,
            status="unsupported_target",
            target_version=target_version,
        )

    table_names = set(inspect(connection).get_table_names())
    if "schema_migrations" not in table_names:
        return MigrationReadiness(
            ready=False,
            status="migration_required",
            target_version=target_version,
        )

    applied_versions = tuple(
        sorted(connection.execute(select(SchemaMigrationModel.version)).scalars())
    )
    unknown_versions = tuple(sorted(set(applied_versions) - SUPPORTED_VERSIONS))
    if unknown_versions:
        return MigrationReadiness(
            ready=False,
            status="unsupported_version",
            target_version=target_version,
            applied_versions=applied_versions,
            unknown_versions=unknown_versions,
        )

    missing_tables = tuple(sorted(EXPECTED_TABLES - table_names))
    if missing_tables:
        return MigrationReadiness(
            ready=False,
            status="schema_incomplete",
            target_version=target_version,
            applied_versions=applied_versions,
            missing_tables=missing_tables,
        )

    if target_version not in applied_versions:
        return MigrationReadiness(
            ready=False,
            status="migration_required",
            target_version=target_version,
            applied_versions=applied_versions,
        )

    return MigrationReadiness(
        ready=True,
        status="ready",
        target_version=target_version,
        applied_versions=applied_versions,
    )


async def check_database_readiness(
    engine: AsyncEngine, target_version: str = INITIAL_VERSION
) -> MigrationReadiness:
    """检查数据库连接和 schema 版本；readiness 失败不会尝试修复数据库。"""

    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync_connection: _inspect_schema_readiness(
                    sync_connection, target_version
                )
            )
    except Exception:
        # readiness 只需要给编排层一个稳定的非就绪结果，不把 DSN、密码或驱动堆栈
        # 直接写入 HTTP 响应；详细异常仍由应用日志和部署任务负责记录。
        return MigrationReadiness(
            ready=False,
            status="database_unavailable",
            target_version=target_version,
        )


def _apply_initial_schema(connection: Connection) -> None:
    """创建初始 schema，并最后写入成功版本；失败不会写入版本行。"""

    applied_versions = _read_applied_versions(connection)
    _validate_applied_versions(applied_versions)
    if INITIAL_VERSION in applied_versions:
        return
    # 为每次 migration 构造一次性 metadata，避免同一进程先后使用 PostgreSQL、
    # SQLite 等不同 dialect 时，循环外键的建表排序状态互相污染。
    migration_metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(migration_metadata)
    migration_metadata.create_all(connection)
    connection.execute(SchemaMigrationModel.__table__.insert().values(version=INITIAL_VERSION))


async def upgrade_database(engine: AsyncEngine, target_version: str = INITIAL_VERSION) -> None:
    """把空库升级到受支持目标版本，并把任意失败包装为 MigrationError。"""

    try:
        if target_version != INITIAL_VERSION:
            raise ValueError(f"不支持的 migration 目标版本: {target_version}")
        async with engine.begin() as connection:
            await connection.run_sync(_apply_initial_schema)
    except Exception as exc:
        raise MigrationError(target_version, exc) from exc


__all__ = [
    "EXPECTED_TABLES",
    "INITIAL_VERSION",
    "MigrationError",
    "MigrationReadiness",
    "SUPPORTED_VERSIONS",
    "check_database_readiness",
    "upgrade_database",
]
