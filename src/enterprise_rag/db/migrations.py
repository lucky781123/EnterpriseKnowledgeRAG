"""仓库内 Python migration contract。

首期闭环将基础业务表、PostgreSQL-only Dense 向量表和 Weaviate generation 事实表
拆为三个显式版本，既支持空库一次升级，也支持已有数据库按顺序推进。
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import MetaData, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from enterprise_rag.db.models import Base, SchemaMigrationModel

INITIAL_VERSION = "0001_initial"
EMBEDDING_VERSION = "0002_embedding_records"
WEAVIATE_DENSE_VERSION = "0003_weaviate_dense_generations"
WEAVIATE_HYBRID_VERSION = "0004_weaviate_hybrid_generations"
LATEST_VERSION = WEAVIATE_HYBRID_VERSION
EXPECTED_TABLES = frozenset(
    {
        "document_acl",
        "document_versions",
        "documents",
        "ingestion_jobs",
        "schema_migrations",
        "tenants",
        "version_artifacts",
        "embedding_records",
        "tenant_dense_collections",
        "dense_generations",
        "dense_generation_pointers",
    }
)
WEAVIATE_DENSE_TABLES = frozenset(
    {"tenant_dense_collections", "dense_generations", "dense_generation_pointers"}
)
WEAVIATE_DENSE_TABLE_ORDER = (
    "tenant_dense_collections",
    "dense_generations",
    "dense_generation_pointers",
)
SUPPORTED_VERSIONS = frozenset(
    {
        INITIAL_VERSION,
        EMBEDDING_VERSION,
        WEAVIATE_DENSE_VERSION,
        WEAVIATE_HYBRID_VERSION,
    }
)
INITIAL_TABLES = EXPECTED_TABLES - {"embedding_records"} - WEAVIATE_DENSE_TABLES
EMBEDDING_TABLES = INITIAL_TABLES | {"embedding_records"}


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
    connection: Connection, target_version: str = LATEST_VERSION
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

    # 不同目标版本只检查该版本承诺的表集合，保证 ``0001`` 数据库不会因为尚未
    # 创建 ``embedding_records`` 而被错误判断为 schema 损坏。
    required_tables_by_version = {
        INITIAL_VERSION: INITIAL_TABLES,
        EMBEDDING_VERSION: EMBEDDING_TABLES,
        WEAVIATE_DENSE_VERSION: EXPECTED_TABLES,
        WEAVIATE_HYBRID_VERSION: EXPECTED_TABLES,
    }
    required_tables = required_tables_by_version[target_version]
    missing_tables = tuple(sorted(required_tables - table_names))
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
    engine: AsyncEngine, target_version: str = LATEST_VERSION
) -> MigrationReadiness:
    """检查数据库连接和 schema 版本；readiness 失败不会尝试修复数据库。"""

    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync_connection: _inspect_schema_readiness(
                    sync_connection, target_version
                )
            )
    except SQLAlchemyError:
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
        # ``embedding_records`` 属于 ``0002``，Dense generation 表属于 ``0003``，
        # 都不能被旧版本迁移提前创建。
        if table.name == "embedding_records" or table.name in WEAVIATE_DENSE_TABLES:
            continue
        table.to_metadata(migration_metadata)
    migration_metadata.create_all(connection)
    connection.execute(SchemaMigrationModel.__table__.insert().values(version=INITIAL_VERSION))


def _apply_embedding_schema(connection: Connection) -> None:
    """创建 ``0002`` 向量表，并在 DDL 成功后记录 migration 版本。"""

    applied_versions = _read_applied_versions(connection)
    _validate_applied_versions(applied_versions)
    if EMBEDDING_VERSION in applied_versions:
        return
    if INITIAL_VERSION not in applied_versions:
        raise RuntimeError("应用 0002 前必须先完成 0001")

    # 复制完整 metadata 以便 SQLAlchemy 解析复合外键依赖，但只创建本版本新增的
    # embedding_records 表，避免隐式修改任何既有基础表。
    migration_metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(migration_metadata)
    migration_metadata.tables["embedding_records"].create(connection, checkfirst=True)
    connection.execute(SchemaMigrationModel.__table__.insert().values(version=EMBEDDING_VERSION))


async def upgrade_database(engine: AsyncEngine, target_version: str = LATEST_VERSION) -> None:
    """按版本顺序升级数据库，并把任意失败包装为 ``MigrationError``。"""

    try:
        if target_version not in SUPPORTED_VERSIONS:
            raise ValueError(f"不支持的 migration 目标版本: {target_version}")
        async with engine.begin() as connection:
            await connection.run_sync(_apply_initial_schema)
            if target_version in {
                EMBEDDING_VERSION,
                WEAVIATE_DENSE_VERSION,
                WEAVIATE_HYBRID_VERSION,
            }:
                await connection.run_sync(_apply_embedding_schema)
            if target_version in {WEAVIATE_DENSE_VERSION, WEAVIATE_HYBRID_VERSION}:
                await connection.run_sync(_apply_weaviate_dense_schema)
            if target_version == WEAVIATE_HYBRID_VERSION:
                await connection.run_sync(_apply_weaviate_hybrid_schema)
    except Exception as exc:
        raise MigrationError(target_version, exc) from exc


def _apply_weaviate_dense_schema(connection: Connection) -> None:
    """创建 ``0003`` Dense generation 事实表，并在 DDL 成功后记录版本。"""

    applied_versions = _read_applied_versions(connection)
    _validate_applied_versions(applied_versions)
    if WEAVIATE_DENSE_VERSION in applied_versions:
        return
    if EMBEDDING_VERSION not in applied_versions:
        raise RuntimeError("应用 0003 前必须先完成 0002")

    # 复制完整 metadata 只为解析复合外键依赖，再按本版本的三张表逐一创建。
    migration_metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(migration_metadata)
    for table_name in WEAVIATE_DENSE_TABLE_ORDER:
        migration_metadata.tables[table_name].create(connection, checkfirst=True)
    connection.execute(
        SchemaMigrationModel.__table__.insert().values(version=WEAVIATE_DENSE_VERSION)
    )


def _apply_weaviate_hybrid_schema(connection: Connection) -> None:
    """为既有 generation 锚点补充 Sparse collection 与 profile 元数据。"""

    applied_versions = _read_applied_versions(connection)
    _validate_applied_versions(applied_versions)
    if WEAVIATE_HYBRID_VERSION in applied_versions:
        return
    if WEAVIATE_DENSE_VERSION not in applied_versions:
        raise RuntimeError("应用 0004 前必须先完成 0003")

    existing_columns = {
        item["name"] for item in inspect(connection).get_columns("dense_generations")
    }
    additions = {
        "sparse_collection_name": "VARCHAR(128)",
        "sparse_profile_key": "VARCHAR(1024)",
        "sparse_schema_version": "VARCHAR(128)",
    }
    for column_name, column_type in additions.items():
        if column_name in existing_columns:
            continue
        # 三列保持 nullable，兼容 0003 已存在的 Dense-only 历史 generation；
        # 产品发布路径会在服务层要求它们全部存在且与本次 profile 一致。
        connection.execute(
            text(
                f"ALTER TABLE dense_generations ADD COLUMN {column_name} {column_type}"
            )
        )
    connection.execute(
        SchemaMigrationModel.__table__.insert().values(version=WEAVIATE_HYBRID_VERSION)
    )


__all__ = [
    "EMBEDDING_VERSION",
    "EXPECTED_TABLES",
    "INITIAL_VERSION",
    "LATEST_VERSION",
    "SUPPORTED_VERSIONS",
    "WEAVIATE_DENSE_VERSION",
    "WEAVIATE_HYBRID_VERSION",
    "MigrationError",
    "MigrationReadiness",
    "check_database_readiness",
    "upgrade_database",
]
