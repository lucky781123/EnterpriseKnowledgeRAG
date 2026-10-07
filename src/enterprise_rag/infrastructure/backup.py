"""维护窗口四系统备份与隔离恢复；原生工具失败即停止，不伪造快照。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from sqlalchemy.engine import make_url

from enterprise_rag.db.session import Database
from enterprise_rag.infrastructure.settings import InfrastructureSettings
from enterprise_rag.infrastructure.write_gate import WriteGate
from enterprise_rag.storage.s3 import S3ObjectStorage

COMPONENTS = {"postgres", "minio", "weaviate", "redis_cache", "redis_coordination"}


def build_backup_plan(action: str, directory: Path) -> dict:
    """生成不执行变更的备份/恢复计划，供 CLI、审批和面试演示复用。"""

    if action not in {"backup", "verify", "restore", "restore-verify"}:
        raise BackupError("备份操作不受支持")
    steps = {
        "backup": [
            "acquire-global-write-gate",
            "pg-custom-dump",
            "minio-current-object-export",
            "weaviate-native-backup",
            "redis-cache-rdb",
            "redis-coordination-rdb",
            "write-complete-manifest",
        ],
        "verify": ["validate-manifest", "verify-local-sha256", "verify-weaviate-backup-status"],
        "restore": [
            "validate-completed-manifest",
            "validate-empty-isolated-targets",
            "restore-postgres",
            "restore-minio",
            "restore-weaviate",
            "stage-offline-redis-rdb",
        ],
        "restore-verify": [
            "verify-schema-and-migration",
            "verify-object-sha256",
            "verify-generation-pointer",
            "verify-dense-sparse-lineage",
            "verify-redis-noeviction-aof",
            "run-acl-query-replay-smoke",
        ],
    }[action]
    return {
        "mode": "plan",
        "action": action,
        "directory": str(directory),
        "steps": steps,
        "mutated": False,
        "safety": {
            "requires_execute": action != "verify",
            "restore_requires_empty_targets": action in {"restore", "restore-verify"},
            "target_must_be_empty_and_isolated": action in {"restore", "restore-verify"},
            "never_deletes_source": True,
            "never_restores_to_source_identity": True,
        },
    }


class BackupError(RuntimeError):
    """命令/SDK/协议异常只能返回安全类别，不回显密钥和 endpoint。"""


def project_path(root: Path, value: str | Path) -> Path:
    """所有本地产物和恢复数据目录必须落在当前项目中。"""
    path = (root / value).resolve()
    if root.resolve() not in path.parents:
        raise BackupError("备份/恢复路径必须位于当前项目目录内")
    return path


def digest(path: Path) -> str:
    """以固定缓冲计算 SHA256，不把大备份读入内存。"""
    result = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def run_tool(argv: list[str], *, env=None, timeout=3600):
    """禁用 shell，隐藏工具 stderr；失败不执行后续恢复阶段。"""
    try:
        return subprocess.run(
            argv, env=env, check=True, capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        raise BackupError("原生备份/恢复命令失败，请检查工具与授权") from None


def postgres_command(dsn: str, tool: str, *arguments: str):
    """连接参数不携带密码；使用子进程私有环境传递 PGPASSWORD。"""
    url = make_url(dsn)
    if not url.drivername.startswith("postgresql") or not url.database:
        raise BackupError("需要显式 PostgreSQL 数据库")
    env = os.environ.copy()
    env.pop("PGOPTIONS", None)
    if url.password:
        env["PGPASSWORD"] = url.password
    clean = url.set(drivername="postgresql", password=None)
    return run_tool([tool, "--dbname", clean.render_as_string(hide_password=False), *arguments], env=env)


def redis_snapshot(url: str, destination: Path):
    """redis-cli --rdb 生成原生快照；认证不放在命令参数或 manifest 中。"""
    parsed = urlsplit(url)
    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
        raise BackupError("需要显式 Redis endpoint")
    env = os.environ.copy()
    if parsed.password:
        from urllib.parse import unquote

        env["REDISCLI_AUTH"] = unquote(parsed.password)
    netloc = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    clean = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    args = ["redis-cli", "-u", clean]
    if parsed.username:
        from urllib.parse import unquote

        args.extend(["--user", unquote(parsed.username)])
    args.extend(["--rdb", str(destination)])
    run_tool(args)
    run_tool(["redis-check-rdb", str(destination)])


def _s3_export(storage: S3ObjectStorage, destination: Path):
    """备份当前可见对象；不宣称覆盖 bucket 历史版本或服务商管理配置。"""
    destination.mkdir()
    records = []
    paginator = storage.client.get_paginator("list_objects_v2")
    prefix = storage.prefix + "/" if storage.prefix else ""
    for page in paginator.paginate(Bucket=storage.bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            # 本地名字用摘要，不用用户 key 拼文件路径；原 identity 存索引。
            name = hashlib.sha256(key.encode()).hexdigest() + ".object"
            path = destination / name
            storage.client.download_file(storage.bucket, key, str(path))
            records.append({
                "key": key, "file": "objects/" + name,
                "size": path.stat().st_size, "sha256": digest(path),
            })
    return records


def _s3_restore(storage: S3ObjectStorage, records: list, directory: Path):
    """仅向空隔离 bucket 恢复；现存任意对象都拒绝覆盖。"""
    response = storage.client.list_objects_v2(Bucket=storage.bucket, MaxKeys=1)
    if response.get("Contents"):
        raise BackupError("恢复 MinIO bucket 必须为空")
    for record in records:
        storage.client.upload_file(
            str(directory / record["file"]), storage.bucket, record["key"],
        )


class BackupRunner:
    """只有拿到全局 PG exclusive gate 才启动四系统快照。"""

    def __init__(self, root: Path, database: Database, infra: InfrastructureSettings,
                 *, weaviate_url: str, weaviate_key="", backend="s3", timeout=3600):
        """显式依赖托管快照 backend；不自动启用服务商 backup 模块或创建 bucket。"""
        if backend not in {"s3", "gcs", "azure", "filesystem"}:
            raise BackupError("Weaviate backup backend 非法")
        self.root, self.database, self.infra = root.resolve(), database, infra
        self.weaviate_url, self.weaviate_key = weaviate_url.rstrip("/"), weaviate_key
        self.backend, self.timeout = backend, timeout

    async def _vector(self, client, backup_id, *, restore=False):
        """使用原生 backup API 并等待终态；远程快照未 SUCCESS 就不能完成。"""
        endpoint = f"{self.weaviate_url}/v1/backups/{self.backend}/{backup_id}"
        create_endpoint = endpoint + "/restore" if restore else f"{self.weaviate_url}/v1/backups/{self.backend}"
        body = {} if restore else {"id": backup_id}
        response = await client.post(create_endpoint, json=body)
        response.raise_for_status()
        deadline = time.monotonic() + self.timeout
        status_endpoint = endpoint + "/restore" if restore else endpoint
        while time.monotonic() < deadline:
            status = await client.get(status_endpoint)
            status.raise_for_status()
            payload = status.json()
            if payload.get("status") == "SUCCESS":
                return {"backup_id": backup_id, "backend": self.backend, "status": "SUCCESS"}
            if payload.get("status") in {"FAILED", "CANCELED", "CANCELLED"}:
                raise BackupError("Weaviate 原生快照/恢复失败")
            await asyncio.sleep(2)
        raise BackupError("Weaviate 原生快照/恢复超时")

    async def run(self, directory: Path):
        """快照期间禁止 API/Worker/dispatcher 写入；异常留下无完成标记的目录。"""
        directory = project_path(self.root, directory)
        self.infra.validate()
        if not self.infra.enabled or self.infra.storage_provider != "s3":
            raise BackupError("四系统备份需要显式 Redis 与 MinIO 配置")
        directory.mkdir(parents=True, exist_ok=False)
        os.chmod(directory, 0o700)
        backup_id = "rag-" + uuid4().hex
        storage = S3ObjectStorage.from_settings(self.infra)
        gate = WriteGate(self.database)
        headers = {"Authorization": f"Bearer {self.weaviate_key}"} if self.weaviate_key else {}
        try:
            async with gate.maintenance():
                started = time.time()
                await asyncio.to_thread(
                    postgres_command, self.database.url, "pg_dump", "--format=custom",
                    "--no-owner", "--no-acl", "--file", str(directory / "postgres.dump"),
                )
                records = await asyncio.to_thread(_s3_export, storage, directory / "objects")
                for name, url in (
                    ("redis_cache", self.infra.redis_cache_url),
                    ("redis_coordination", self.infra.redis_coordination_url),
                ):
                    await asyncio.to_thread(redis_snapshot, url, directory / f"{name}.rdb")
                async with httpx.AsyncClient(timeout=30, headers=headers) as client:
                    vector = await self._vector(client, backup_id)
                files = {
                    name: {"file": name, "size": (directory / name).stat().st_size,
                           "sha256": await asyncio.to_thread(digest, directory / name)}
                    for name in ("postgres.dump", "redis_cache.rdb", "redis_coordination.rdb")
                }
                # 只有全部快照通过才写完成清单。远程 Weaviate 快照不是本地归档。
                manifest = {
                    "format": 1, "status": "complete", "components": sorted(COMPONENTS),
                    "backup_id": backup_id, "started_at": started, "completed_at": time.time(),
                    "files": files, "objects": records, "weaviate": vector,
                    "source": {
                        "postgres": _pg_identity(self.database.url),
                        "minio": [self.infra.s3_endpoint.rstrip("/"), self.infra.s3_bucket],
                        "weaviate": self.weaviate_url,
                        "redis": [_redis_identity(self.infra.redis_cache_url),
                                  _redis_identity(self.infra.redis_coordination_url)],
                    },
                    "limitations": [
                        "MinIO current objects only; bucket policies/version history managed separately",
                        "Weaviate native remote snapshot requires retained provider backup storage",
                        "No writers outside the shared gate are allowed during the window",
                    ],
                    "restore_contract": {
                        "target_must_be_empty_and_isolated": True,
                        "order": ["postgres", "minio", "weaviate", "redis"],
                        "business_ready_requires_restore_verify": True,
                    },
                }
                (directory / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
                )
                return manifest
        except BackupError:
            raise
        except Exception:  # noqa: BLE001 - 绝不回显原生工具、SDK 或远程响应异常。
            raise BackupError("四系统备份未完成，不能用于正式恢复") from None
        finally:
            await storage.close()
            await gate.close()


def _pg_identity(dsn):
    """manifest 中保存无凭据的恢复隔离 identity。"""
    value = make_url(dsn)
    return [value.host, value.port or 5432, value.database]


def _redis_identity(url):
    """Redis 恢复必须隔离整个实例，不能只换 logical DB。"""
    value = urlsplit(url)
    return [value.hostname, value.port or 6379]


def _s3_identity(endpoint: str, bucket: str) -> list[str]:
    """返回不含凭据的对象存储实例与 bucket identity。"""

    return [endpoint.rstrip("/"), bucket]


def verify(directory: Path) -> dict:
    """校验所有本地产物和 manifest，禁止路径穿越、缺件或 hash 不匹配。"""
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        if (
            manifest["format"] != 1 or manifest["status"] != "complete"
            or set(manifest["components"]) != COMPONENTS
            or set(manifest["files"]) != {"postgres.dump", "redis_cache.rdb", "redis_coordination.rdb"}
            or manifest["weaviate"]["status"] != "SUCCESS"
            or manifest["weaviate"]["backup_id"] != manifest["backup_id"]
        ):
            raise ValueError()
        for record in [*manifest["files"].values(), *manifest["objects"]]:
            path = project_path(directory.resolve(), record["file"])
            if path.stat().st_size != record["size"] or digest(path) != record["sha256"]:
                raise ValueError()
        return manifest
    except (OSError, KeyError, TypeError, ValueError):
        raise BackupError("备份清单缺件、非法或完整性失败") from None


class RestoreRunner(BackupRunner):
    """恢复只能到显式隔离目标；目标服务不得运行旧 Worker 或接受业务流量。"""

    def validate_target(self, manifest):
        """比较所有源目标 identity，任一相同都拒绝执行。"""
        source = manifest["source"]
        if (
            _pg_identity(self.database.url) == source["postgres"]
            or _s3_identity(self.infra.s3_endpoint, self.infra.s3_bucket) == source["minio"]
            or self.weaviate_url == source["weaviate"]
            or _redis_identity(self.infra.redis_cache_url) in source["redis"]
            or _redis_identity(self.infra.redis_coordination_url) in source["redis"]
        ):
            raise BackupError("恢复目标必须与所有生产源隔离")

    async def restore(self, directory: Path, *, cache_data: Path, coordination_data: Path):
        """阶段顺序 PG→MinIO→Weaviate→Redis；任何失败保持目标不对业务开放。"""
        directory = project_path(self.root, directory)
        manifest = await asyncio.to_thread(verify, directory)
        self.validate_target(manifest)
        self.infra.validate()
        if manifest["weaviate"]["backend"] != self.backend:
            raise BackupError("恢复 backend 必须与原生快照一致")
        destinations = [
            project_path(self.root, cache_data), project_path(self.root, coordination_data),
        ]
        if destinations[0] == destinations[1]:
            raise BackupError("Redis 恢复目录必须隔离")
        for path in destinations:
            if path.exists() and any(path.iterdir()):
                raise BackupError("Redis 恢复数据目录必须为空")
        # Redis 必须离线，在线实例不能通过覆盖 dump.rdb 获得一致恢复。
        from redis.asyncio import Redis
        from redis.exceptions import ConnectionError as RedisConnectionError

        for url in (self.infra.redis_cache_url, self.infra.redis_coordination_url):
            client = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
            try:
                try:
                    await client.ping()
                except (RedisConnectionError, OSError):
                    pass
                else:
                    raise BackupError("恢复 Redis 必须先停止隔离目标实例")
            finally:
                await client.aclose()
        async with self.database.engine.connect() as connection:
            from sqlalchemy import text

            count = await connection.scalar(text(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public'",
            ))
            if count:
                raise BackupError("恢复 PostgreSQL 必须是空隔离数据库")
        storage = S3ObjectStorage.from_settings(self.infra)
        headers = {"Authorization": f"Bearer {self.weaviate_key}"} if self.weaviate_key else {}
        try:
            # 所有远程目标在写入之前先核验为空，避免 PG 已恢复后才发现目标冲突。
            response = await asyncio.to_thread(
                storage.client.list_objects_v2, Bucket=storage.bucket, MaxKeys=1,
            )
            if response.get("Contents"):
                raise BackupError("恢复 MinIO bucket 必须为空")
            async with httpx.AsyncClient(timeout=30, headers=headers) as client:
                schema = await client.get(f"{self.weaviate_url}/v1/schema")
                schema.raise_for_status()
                if schema.json().get("classes"):
                    raise BackupError("恢复 Weaviate 必须为空隔离实例")
            # 不使用 --clean，不删除任何目标已有数据库事实。
            await asyncio.to_thread(
                postgres_command, self.database.url, "pg_restore", "--exit-on-error",
                "--single-transaction", "--no-owner", "--no-acl", str(directory / "postgres.dump"),
            )
            await asyncio.to_thread(_s3_restore, storage, manifest["objects"], directory)
            async with httpx.AsyncClient(timeout=30, headers=headers) as client:
                schema = await client.get(f"{self.weaviate_url}/v1/schema")
                schema.raise_for_status()
                if schema.json().get("classes"):
                    raise BackupError("恢复 Weaviate 必须为空隔离实例")
                await self._vector(client, manifest["backup_id"], restore=True)
            for name, target in zip(("redis_cache", "redis_coordination"), destinations, strict=True):
                target.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.copyfile, directory / f"{name}.rdb", target / "dump.rdb")
            return {"status": "restored_offline", "business_ready": False, "backup_id": manifest["backup_id"]}
        finally:
            await storage.close()

    async def verify_runtime(self, directory: Path):
        """隔离服务启动后核验对象、PG 发布事实和两路索引；不等于容量验收。"""
        from sqlalchemy import select

        from enterprise_rag.db.migrations import ASYNC_INGESTION_VERSION, check_database_readiness
        from enterprise_rag.db.models import (
            DenseGenerationModel,
            DenseGenerationPointerModel,
            DocumentModel,
        )
        from enterprise_rag.infrastructure.runtime import RedisRuntime
        from enterprise_rag.ingestion.service import IngestionService
        from enterprise_rag.retrieval.weaviate import WeaviateHTTPXTransport
        from enterprise_rag.retrieval.weaviate_sparse import WeaviateBM25FTransport

        manifest = await asyncio.to_thread(verify, project_path(self.root, directory))
        self.validate_target(manifest)
        readiness = await check_database_readiness(
            self.database.engine, target_version=ASYNC_INGESTION_VERSION,
        )
        if not readiness.ready:
            raise BackupError("恢复 PostgreSQL schema 未就绪")
        storage = S3ObjectStorage.from_settings(self.infra)
        redis = RedisRuntime.create(self.infra)
        try:
            if not await redis.cache.ping() or not await redis.coordination.ping():
                raise BackupError("恢复 Redis 未启动")
            policy = await redis.coordination.config_get("maxmemory-policy")
            persistence = await redis.coordination.info("persistence")
            if policy.get("maxmemory-policy") != "noeviction" or not persistence.get("aof_enabled"):
                raise BackupError("恢复协调 Redis 配置不符合要求")

            def verify_objects():
                """逐对象流式重算 hash，不把 ETag 当作内容摘要。"""
                for record in manifest["objects"]:
                    response = storage.client.get_object(Bucket=storage.bucket, Key=record["key"])
                    body = response["Body"]
                    actual, size = hashlib.sha256(), 0
                    try:
                        while chunk := body.read(1024 * 1024):
                            actual.update(chunk)
                            size += len(chunk)
                    finally:
                        body.close()
                    if actual.hexdigest() != record["sha256"] or size != record["size"]:
                        raise BackupError("恢复 MinIO 对象不一致")

            await asyncio.to_thread(verify_objects)
            async with self.database.session() as session:
                pointers = (await session.scalars(select(DenseGenerationPointerModel))).all()
                generations = {
                    row.generation_id: row for row in (
                        await session.scalars(select(DenseGenerationModel))
                    ).all()
                }
                documents = {
                    (row.tenant_id, row.id): row for row in (
                        await session.scalars(select(DocumentModel))
                    ).all()
                }
            async with httpx.AsyncClient(timeout=30) as client:
                sparse = WeaviateBM25FTransport(client, self.weaviate_url, api_key=self.weaviate_key)
                for pointer in pointers:
                    generation = generations.get(pointer.active_generation_id)
                    if generation is None or generation.status.value not in {"active", "retired"}:
                        raise BackupError("恢复 pointer 缺失 generation")
                    scope = (generation.tenant_id, generation.document_id, generation.version_id, generation.index_profile_key)
                    if scope != (
                        pointer.tenant_id, pointer.document_id, pointer.version_id, pointer.index_profile_key,
                    ):
                        raise BackupError("恢复 pointer scope 不一致")
                    document = documents[(generation.tenant_id, generation.document_id)]
                    # 历史已 superseded scope 可以保留，但当前文档 pointer 必须可读。
                    if document.current_version_id != generation.version_id:
                        continue
                    raw = await storage.get(IngestionService._parsed_key(
                        generation.tenant_id, generation.document_id, generation.version_id,
                    ))
                    if hashlib.sha256(raw).hexdigest() != generation.manifest_hash:
                        raise BackupError("恢复 canonical manifest 不一致")
                    payload = json.loads(raw)
                    expected = {child["chunk_id"] for child in payload["chunks"]["children"]}
                    # 新 profile 的物理 collection 有命名空间，不能把旧无后缀数据混用。
                    dense = WeaviateHTTPXTransport(
                        client, self.weaviate_url, api_key=self.weaviate_key,
                        index_profile_key=generation.index_profile_key,
                    )
                    arguments = {"generation_id": generation.generation_id, "version_id": generation.version_id}
                    ids = await dense.list_chunk_ids(generation.collection_name, **arguments)
                    count = await dense.count_generation(generation.collection_name, **arguments)
                    snapshot = await sparse.inspect_generation(
                        generation.sparse_collection_name,
                        tenant_id=generation.tenant_id, sparse_profile_key=generation.sparse_profile_key,
                        **arguments,
                    )
                    if ids != expected or count != len(expected) or snapshot.chunk_ids != frozenset(expected):
                        raise BackupError("恢复双路索引 lineage/count 不一致")
            return {
                "status": "runtime_integrity_verified", "backup_id": manifest["backup_id"],
                "business_ready": False, "remaining": ["ACL revocation/query smoke", "RPO/RTO", "capacity"],
            }
        finally:
            await storage.close()
            await redis.close()
