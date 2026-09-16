"""真实摄取基础的最小编排服务。

本服务把对象存储和 PostgreSQL 事实源串成一条可重放流程，但不声称两个系统
之间存在跨系统原子事务。原文和一份不含正文的请求元数据先落盘，随后数据库
记录任务、版本和产物状态；解析失败时保留原文与 FAILED 状态，供显式 replay
重新执行。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.db.models import (
    ArtifactKind,
    ArtifactStatus,
    DocumentModel,
    DocumentStatus,
    DocumentVersionModel,
    IngestionJobModel,
    JobStatus,
    TenantModel,
    VersionArtifactModel,
    VersionStatus,
)
from enterprise_rag.db.session import Database
from enterprise_rag.db.state import transition_job_status, transition_version_status
from enterprise_rag.ingestion.chunking import STRATEGY_VERSION, chunk_blocks
from enterprise_rag.ingestion.contracts import (
    IngestionRequest,
    IngestionResult,
    ParsedBlock,
    ValidatedRequest,
)
from enterprise_rag.ingestion.parsers import parse_document
from enterprise_rag.ingestion.validation import validate_request
from enterprise_rag.storage.protocols import ObjectStorage


class IdempotencyConflictError(ValueError):
    """同一租户内幂等键对应了不同的请求内容。"""


class IngestionServiceError(RuntimeError):
    """摄取编排遇到无法安全归类的状态或对象存储问题。"""


Parser = Callable[[bytes, str], Sequence[ParsedBlock]]


async def _insert_if_absent(session: AsyncSession, model: type, values: dict) -> None:
    """以数据库原生 upsert 语义插入一行，并忽略并发产生的唯一键冲突。

    摄取请求可能来自多个进程，不能依赖 Python `asyncio.Lock` 解决租户、文档、
    version 或 job 的竞态。PostgreSQL 和 SQLite 都提供 `ON CONFLICT DO NOTHING`，
    这里按当前连接的 dialect 选择对应的 SQLAlchemy `insert` 构造器，让同一套服务
    在真实 PostgreSQL 和离线 SQLite 契约测试中都使用数据库级原子操作。
    """

    dialect_name = session.get_bind().dialect.name
    table = model.__table__
    if dialect_name == "postgresql":
        statement = postgresql_insert(table).values(**values).on_conflict_do_nothing()
    elif dialect_name == "sqlite":
        statement = sqlite_insert(table).values(**values).on_conflict_do_nothing()
    else:
        # 当前摄取基础只声明 PostgreSQL/SQLite 两种受支持方言；静默退回普通
        # INSERT 会重新引入“先查后插”的竞态，因此对未知方言明确拒绝。
        raise IngestionServiceError(f"不支持的摄取数据库方言: {dialect_name}")
    await session.execute(statement)


def _sha256_text(value: str) -> str:
    """使用固定 UTF-8 编码为文档身份生成稳定摘要。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _document_id(tenant_id: str, source_id: str) -> str:
    """文档身份只由租户和稳定 source identity 决定，不依赖文件名或自增顺序。"""

    identity = tenant_id + "\x00" + source_id
    return f"doc_{_sha256_text(identity)}"


def _version_id(document_id: str, content_hash: str) -> str:
    """同一文档同一原文内容始终映射到同一个不可变 version ID。"""

    identity = document_id + "\x00" + content_hash
    return f"version_{_sha256_text(identity)}"


def _json_bytes(value: dict) -> bytes:
    """以固定排序序列化 JSON，保证对象产物可比较、可重放。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


class IngestionService:
    """编排校验、原文存储、解析、chunk 和 PostgreSQL 状态。"""

    def __init__(
        self,
        database: Database,
        storage: ObjectStorage,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        parent_size: int = 800,
        child_size: int = 320,
    ) -> None:
        self.database = database
        self.storage = storage
        self.max_bytes = max_bytes
        self.parent_size = parent_size
        self.child_size = child_size
        # 使用二参数 wrapper 统一 parser 协议，测试或未来适配器可以直接替换某个
        # 扩展名的函数，而不需要了解 parse_document 的关键字参数细节。
        self.parsers: dict[str, Parser] = {
            ".md": lambda data, filename: parse_document(data, filename=filename),
            ".markdown": lambda data, filename: parse_document(data, filename=filename),
            ".html": lambda data, filename: parse_document(data, filename=filename),
            ".htm": lambda data, filename: parse_document(data, filename=filename),
            ".pdf": lambda data, filename: parse_document(data, filename=filename),
        }

    @staticmethod
    def make_block(
        block_type: str, text: str, source_locator: dict, heading_path: tuple[str, ...] = ()
    ) -> ParsedBlock:
        """为测试 double 和小型适配器创建一个确定性的 ParsedBlock。"""

        normalized = text.strip()
        identity = json.dumps(
            {
                "block_type": block_type,
                "text": normalized,
                "heading_path": list(heading_path),
                "source_locator": source_locator,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return ParsedBlock(
            block_id=f"block_{_sha256_text(identity)}",
            block_type=block_type,
            text=normalized,
            heading_path=heading_path,
            source_locator=dict(source_locator),
        )

    @staticmethod
    def _raw_key(tenant_id: str, document_id: str, job_id: str, filename: str) -> str:
        """生成不受用户路径影响的原文 key；文件名只保留安全后缀。"""

        suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower() or ".bin"
        tenant_part = _sha256_text(tenant_id)[:24]
        return f"raw/{tenant_part}/{document_id}/{job_id}{suffix}"

    @staticmethod
    def _request_meta_key(tenant_id: str, job_id: str) -> str:
        """请求元数据 key 与 job 一一对应，供幂等检查和 replay 使用。"""

        return f"raw-meta/{_sha256_text(tenant_id)[:24]}/{job_id}.json"

    @staticmethod
    def _parsed_key(tenant_id: str, document_id: str, version_id: str) -> str:
        """parsed manifest 按不可变 version 定位，重放不会产生第二份清单。"""

        return f"parsed/{_sha256_text(tenant_id)[:24]}/{document_id}/{version_id}.json"

    async def ingest(self, request: IngestionRequest) -> IngestionResult:
        """提交并执行一次摄取；同幂等键的重复请求只返回已有任务结果。"""

        validated = validate_request(request, max_bytes=self.max_bytes)
        document_id = _document_id(request.tenant_id, request.source_id)

        # 先查看事实源中的幂等键。若已有任务，读取其对象元数据比较内容指纹；
        # 不根据调用方第二次提交的 bytes 覆盖原文，从而保持 replay 语义明确。
        async with self.database.session() as session:
            existing = await session.scalar(
                select(IngestionJobModel).where(
                    IngestionJobModel.tenant_id == request.tenant_id,
                    IngestionJobModel.idempotency_key == request.idempotency_key,
                )
            )
        if existing is not None:
            return await self._handle_existing_job(existing, validated)

        job_id = f"job_{uuid4().hex}"
        version_id = _version_id(document_id, validated.content_hash)
        raw_key = self._raw_key(request.tenant_id, document_id, job_id, request.filename)
        meta_key = self._request_meta_key(request.tenant_id, job_id)

        # 对象存储和数据库不共享事务；这里先落原文与可验证元数据，数据库失败时
        # 仍留下可由运维清理的对象，而不会留下“数据库声称可 replay 但没有原文”的
        # 更难诊断状态。
        await self.storage.put(raw_key, request.content, validated.media_type)
        await self.storage.put(
            meta_key,
            _json_bytes(
                {
                    "content_hash": validated.content_hash,
                    "document_id": document_id,
                    "filename": request.filename,
                    "media_type": validated.media_type,
                    "parsed_object_key": self._parsed_key(
                        request.tenant_id, document_id, version_id
                    ),
                    "raw_object_key": raw_key,
                    "source_id": request.source_id,
                    "source_revision": request.source_revision,
                    "tenant_id": request.tenant_id,
                    "title": request.title,
                    "version_id": version_id,
                }
            ),
            "application/json",
        )

        try:
            async with self.database.transaction() as session:
                # 先用数据库级 upsert 创建租户，再锁住该租户行。这样两个进程即使
                # 同时看见“没有 job”，也会在同一个可信边界内串行完成 document、
                # version 和 job 的创建；不能用进程内锁替代这一步。
                await self._ensure_tenant_locked(session, request.tenant_id)

                existing_job = await session.scalar(
                    select(IngestionJobModel).where(
                        IngestionJobModel.tenant_id == request.tenant_id,
                        IngestionJobModel.idempotency_key == request.idempotency_key,
                    )
                )
                if existing_job is not None:
                    # 事务提交后再读取赢家的 metadata。当前候选请求此前写入的
                    # 临时对象可以由独立清理任务回收，绝不能覆盖赢家原文。
                    winner_job_id = existing_job.id
                else:
                    await _insert_if_absent(
                        session,
                        DocumentModel,
                        {
                            "tenant_id": request.tenant_id,
                            "id": document_id,
                            "source_id": request.source_id,
                            "title": request.title,
                            "status": DocumentStatus.DRAFT,
                        },
                    )
                    document = await session.get(DocumentModel, (request.tenant_id, document_id))
                    if document is None:
                        raise IngestionServiceError("无法创建或读取文档身份")

                    await _insert_if_absent(
                        session,
                        DocumentVersionModel,
                        {
                            "tenant_id": request.tenant_id,
                            "document_id": document_id,
                            "id": version_id,
                            "content_hash": validated.content_hash,
                            "source_revision": request.source_revision,
                            "status": VersionStatus.STAGED,
                        },
                    )
                    version = await session.get(
                        DocumentVersionModel, (request.tenant_id, document_id, version_id)
                    )
                    if version is None:
                        raise IngestionServiceError("无法创建或读取文档 version")
                    if version.status is VersionStatus.FAILED:
                        # 同一内容但新的幂等键代表一次新的执行尝试。FAILED version
                        # 不能直接走到 PROCESSING；先显式回到 STAGED，重置解析产物，
                        # 随后由 run_job 按正常状态图推进。
                        transition_version_status(version.status, VersionStatus.STAGED)
                        version.status = VersionStatus.STAGED
                        artifacts = (
                            await session.scalars(
                                select(VersionArtifactModel).where(
                                    VersionArtifactModel.tenant_id == request.tenant_id,
                                    VersionArtifactModel.document_id == document_id,
                                    VersionArtifactModel.version_id == version_id,
                                )
                            )
                        ).all()
                        for artifact in artifacts:
                            if artifact.kind is ArtifactKind.PARSED:
                                artifact.status = ArtifactStatus.PENDING
                                artifact.item_count = 0
                                artifact.failure_reason = None
                    else:
                        # 版本首次出现时按类别补齐 artifact 行；使用同样的 upsert
                        # 保护历史数据库中已存在部分 artifact 的恢复场景。
                        for kind in ArtifactKind:
                            await _insert_if_absent(
                                session,
                                VersionArtifactModel,
                                {
                                    "tenant_id": request.tenant_id,
                                    "document_id": document_id,
                                    "version_id": version_id,
                                    "kind": kind,
                                    "status": ArtifactStatus.PENDING,
                                },
                            )

                    await _insert_if_absent(
                        session,
                        IngestionJobModel,
                        {
                            "tenant_id": request.tenant_id,
                            "id": job_id,
                            "document_id": document_id,
                            "idempotency_key": request.idempotency_key,
                            "status": JobStatus.PENDING,
                        },
                    )
                    job = await session.scalar(
                        select(IngestionJobModel).where(
                            IngestionJobModel.tenant_id == request.tenant_id,
                            IngestionJobModel.idempotency_key == request.idempotency_key,
                        )
                    )
                    if job is None:
                        raise IngestionServiceError("无法创建或读取摄取 job")
                    winner_job_id = job.id
        except Exception:
            # 数据库失败时不删除原文；跨系统清理必须由单独的安全清理任务负责，
            # 这里避免把一次失败扩大成不可恢复的数据丢失。
            raise

        if winner_job_id != job_id:
            # 另一个并发请求已经赢得了幂等键；让该请求读取赢家 metadata，并在
            # 赢家执行期间短暂等待终态，保证并发调用最终返回同一份事实结果。
            async with self.database.session() as session:
                winner = await session.get(IngestionJobModel, (request.tenant_id, winner_job_id))
            if winner is None:
                raise IngestionServiceError("幂等赢家 job 在提交后消失")
            return await self._handle_existing_job(winner, validated, wait_for_terminal=True)

        return await self.run_job(request.tenant_id, winner_job_id)

    async def _ensure_tenant_locked(self, session: AsyncSession, tenant_id: str) -> TenantModel:
        """创建租户并在当前事务内取得行锁，作为摄取幂等串行化边界。

        PostgreSQL 的 `FOR UPDATE` 能让跨进程请求按租户排队；SQLite 不支持行级锁，
        但其 `ON CONFLICT DO NOTHING` 写入会取得数据库写锁，仍能把离线并发测试中的
        “同时插入同一 tenant”收敛为一个赢家。锁只覆盖短暂的事实源创建事务，不跨越
        解析和对象存储 I/O，避免把慢 parser 放进长事务。
        """

        await _insert_if_absent(
            session,
            TenantModel,
            {"id": tenant_id, "name": tenant_id},
        )
        tenant = await session.scalar(
            select(TenantModel).where(TenantModel.id == tenant_id).with_for_update()
        )
        if tenant is None:
            raise IngestionServiceError("无法创建或锁定租户")
        return tenant

    async def _handle_existing_job(
        self,
        job: IngestionJobModel,
        validated: ValidatedRequest,
        *,
        wait_for_terminal: bool = False,
    ) -> IngestionResult:
        """处理重复幂等键，区分同 payload 重放与内容冲突。

        普通重复请求保持“读取当前事实”的轻量语义；只有明确知道自己是并发
        竞态中的落败者时，调用方才传入 ``wait_for_terminal=True``，等待赢家把
        同一个 job 推进到终态，避免两个并发调用返回一个 PENDING 中间状态而让
        上层误以为任务已经完成。
        """

        meta_key = self._request_meta_key(job.tenant_id, job.id)
        try:
            metadata = json.loads(await self.storage.get(meta_key))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise IngestionServiceError("幂等任务元数据缺失，拒绝覆盖原文") from exc
        if metadata.get("content_hash") != validated.content_hash:
            raise IdempotencyConflictError("同一租户的幂等键不能复用不同内容")

        # 已完成、失败或正在运行的任务都返回事实源当前状态；FAILED 必须由
        # replay_failed 显式恢复，普通重复请求不能隐式触发昂贵解析。
        if not wait_for_terminal:
            return await self._result_from_job(job, metadata)
        return await self._wait_for_job_terminal(job.tenant_id, job.id, metadata)

    async def _wait_for_job_terminal(
        self, tenant_id: str, job_id: str, metadata: dict, *, timeout: float = 30.0
    ) -> IngestionResult:
        """有界等待并发赢家 job 的终态，避免无限等待掩盖执行器故障。

        这里采用短轮询而不是持有数据库连接等待：每次查询结束后立即归还连接，
        不会因为一个慢解析任务占满连接池。超时只报告安全的服务层错误，不把
        DSN、对象 key 细节或原文内容写入异常消息。
        """

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            async with self.database.session() as session:
                job = await session.get(IngestionJobModel, (tenant_id, job_id))
            if job is None:
                raise IngestionServiceError("幂等赢家 job 在等待期间消失")
            if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
                return await self._result_from_job(job, metadata)
            if asyncio.get_running_loop().time() >= deadline:
                raise IngestionServiceError("幂等赢家 job 在限定时间内未完成")
            await asyncio.sleep(0.01)

    async def run_job(self, tenant_id: str, job_id: str) -> IngestionResult:
        """执行一个 PENDING/RUNNING 摄取任务，并持久化可识别的终态。"""

        # 共享 version 的协调需要一个小循环：如果另一个 job 已经占用 version，
        # 当前 job 在事务外等待其终态，然后重新获取行锁并复用 READY/FAILED 事实。
        # 这样不会把数据库连接占用在解析或对象存储 I/O 上，也不会让多个 worker
        # 同时把同一个不可变 version 从 PROCESSING 写成互相覆盖的终态。
        while True:
            wait_for_version = False
            async with self.database.transaction() as session:
                job = await session.scalar(
                    select(IngestionJobModel)
                    .where(
                        IngestionJobModel.tenant_id == tenant_id,
                        IngestionJobModel.id == job_id,
                    )
                    .with_for_update()
                )
                if job is None:
                    raise IngestionServiceError("摄取任务不存在")
                metadata = json.loads(
                    (await self.storage.get(self._request_meta_key(tenant_id, job_id))).decode(
                        "utf-8"
                    )
                )
                version = await session.scalar(
                    select(DocumentVersionModel)
                    .where(
                        DocumentVersionModel.tenant_id == tenant_id,
                        DocumentVersionModel.document_id == metadata["document_id"],
                        DocumentVersionModel.id == metadata["version_id"],
                    )
                    .with_for_update()
                )
                if version is None:
                    raise IngestionServiceError("摄取任务对应的 version 不存在")
                if job.status is JobStatus.SUCCEEDED:
                    return self._result_from_models(job, version, metadata)
                if job.status not in {JobStatus.PENDING, JobStatus.RUNNING}:
                    return self._result_from_models(job, version, metadata)

                # READY 且 parsed manifest 存在时，说明同一内容已经被另一个 job
                # 完整处理。当前 job 仍需经过 RUNNING 才能合法进入 SUCCEEDED，不能
                # 直接违反状态机从 PENDING 跳到终态。
                if version.status is VersionStatus.READY and await self.storage.exists(
                    metadata["parsed_object_key"]
                ):
                    self._mark_job_running(job)
                    job.status = transition_job_status(job.status, JobStatus.SUCCEEDED)
                    job.last_error = None
                    return self._result_from_models(job, version, metadata)

                if version.status is VersionStatus.PROCESSING:
                    # 另一个 job 已经在事务中抢到该 version；当前事务不改变任何
                    # 状态，提交后在连接池外轮询，避免重复执行 parser。
                    wait_for_version = True
                elif version.status is VersionStatus.FAILED:
                    # 该 job 是在共享 version 进入 PROCESSING 后登记的并发尝试。
                    # 它不能隐式 replay，也不能再次把共享 version 写成 FAILED；只
                    # 把自身从 PENDING/RUNNING 结束为 FAILED，并保留赢家的失败原因。
                    self._mark_job_running(job)
                    reason = await self._shared_version_failure_reason(
                        session,
                        tenant_id,
                        metadata["document_id"],
                        metadata["version_id"],
                    )
                    job.status = transition_job_status(job.status, JobStatus.FAILED)
                    job.last_error = reason
                    return self._result_from_models(
                        job, version, metadata, failure_reason=reason
                    )
                elif version.status is VersionStatus.STAGED:
                    # 仅依赖 SELECT ... FOR UPDATE 在 SQLite 上是不够的，因为该方言
                    # 会忽略 FOR UPDATE，两个事务可能同时读到 STAGED。用带状态条件
                    # 的原子 UPDATE 做 compare-and-set：只有 rowcount=1 的事务取得
                    # 解析权；PostgreSQL 和 SQLite 都会在写入冲突时等待并重新判断。
                    claim = await session.execute(
                        update(DocumentVersionModel)
                        .where(
                            DocumentVersionModel.tenant_id == tenant_id,
                            DocumentVersionModel.document_id == metadata["document_id"],
                            DocumentVersionModel.id == metadata["version_id"],
                            DocumentVersionModel.status == VersionStatus.STAGED,
                        )
                        .values(status=VersionStatus.PROCESSING)
                    )
                    if claim.rowcount == 1:
                        self._mark_job_running(job)
                        version.status = VersionStatus.PROCESSING
                        document = await session.get(
                            DocumentModel, (tenant_id, metadata["document_id"])
                        )
                        if document is not None:
                            document.status = DocumentStatus.PROCESSING
                    else:
                        # 另一事务已经完成状态抢占；刷新 ORM 快照后按最新事实进入
                        # 等待或失败分支，绝不能在本地旧快照上继续解析。
                        await session.refresh(version)
                        if version.status is VersionStatus.PROCESSING:
                            wait_for_version = True
                        elif version.status is VersionStatus.READY:
                            if await self.storage.exists(metadata["parsed_object_key"]):
                                self._mark_job_running(job)
                                job.status = transition_job_status(
                                    job.status, JobStatus.SUCCEEDED
                                )
                                return self._result_from_models(job, version, metadata)
                            raise IngestionServiceError("READY version 的 parsed manifest 缺失")
                        elif version.status is VersionStatus.FAILED:
                            self._mark_job_running(job)
                            reason = await self._shared_version_failure_reason(
                                session,
                                tenant_id,
                                metadata["document_id"],
                                metadata["version_id"],
                            )
                            job.status = transition_job_status(job.status, JobStatus.FAILED)
                            job.last_error = reason
                            return self._result_from_models(
                                job, version, metadata, failure_reason=reason
                            )
                        else:
                            raise IngestionServiceError(
                                f"version 抢占后状态异常: {version.status.value}"
                            )
                else:
                    # READY 但 parsed object 缺失、或已发布/撤销的 version 都属于
                    # 跨系统状态不一致/不可再处理边界；保留事实源，不静默回退状态。
                    raise IngestionServiceError(
                        f"version 状态不允许继续摄取: {version.status.value}"
                    )

            if wait_for_version:
                await self._wait_for_version_terminal(
                    tenant_id,
                    metadata["document_id"],
                    metadata["version_id"],
                )
                # 终态可能是 READY、FAILED，或运维显式 replay 后重新回到 STAGED；
                # 统一回到同一段带行锁的判定逻辑，避免在轮询中自行猜测状态。
                continue
            break

        raw_key = metadata["raw_object_key"]
        try:
            raw = await self.storage.get(raw_key)
            parser = self.parsers[PurePosixPath(metadata["filename"]).suffix.lower()]
            blocks = list(parser(raw, metadata["filename"]))
            manifest = chunk_blocks(
                blocks,
                version_id=metadata["version_id"],
                parent_size=self.parent_size,
                child_size=self.child_size,
                strategy_version=STRATEGY_VERSION,
            )
            parsed_payload = {
                "blocks": [block.to_dict() for block in blocks],
                "chunks": manifest.to_dict(),
                "content_hash": metadata["content_hash"],
                "document_id": metadata["document_id"],
                "parser_version": "structured-block-v1",
                "tenant_id": tenant_id,
                "version_id": metadata["version_id"],
            }
            parsed_key = metadata["parsed_object_key"]
            await self.storage.put(parsed_key, _json_bytes(parsed_payload), "application/json")
        except Exception as exc:
            return await self._mark_failed(tenant_id, job_id, metadata, exc)

        async with self.database.transaction() as session:
            job = await session.scalar(
                select(IngestionJobModel)
                .where(
                    IngestionJobModel.tenant_id == tenant_id,
                    IngestionJobModel.id == job_id,
                )
                .with_for_update()
            )
            version = await session.scalar(
                select(DocumentVersionModel)
                .where(
                    DocumentVersionModel.tenant_id == tenant_id,
                    DocumentVersionModel.document_id == metadata["document_id"],
                    DocumentVersionModel.id == metadata["version_id"],
                )
                .with_for_update()
            )
            if job is None or version is None:
                raise IngestionServiceError("摄取状态在完成提交前消失")
            if job.status is JobStatus.SUCCEEDED:
                return self._result_from_models(job, version, metadata)
            if job.status is not JobStatus.RUNNING:
                return self._result_from_models(job, version, metadata, failure_reason=job.last_error)
            job.status = transition_job_status(job.status, JobStatus.SUCCEEDED)
            if version.status is VersionStatus.PROCESSING:
                version.status = transition_version_status(version.status, VersionStatus.READY)
            elif version.status is not VersionStatus.READY:
                raise IngestionServiceError(
                    f"解析完成时 version 状态异常: {version.status.value}"
                )
            job.last_error = None
            document = await session.get(
                DocumentModel, (tenant_id, metadata["document_id"])
            )
            if document is not None:
                # 没有自动发布；没有 current_version 时回到 draft，已有可见版本
                # 则继续保持 ready，避免 staged version 改变查询可见性。
                document.status = (
                    DocumentStatus.READY
                    if document.current_version_id is not None
                    else DocumentStatus.DRAFT
                )
            artifacts = (
                await session.scalars(
                    select(VersionArtifactModel).where(
                        VersionArtifactModel.tenant_id == tenant_id,
                        VersionArtifactModel.document_id == metadata["document_id"],
                        VersionArtifactModel.version_id == metadata["version_id"],
                    )
                )
            ).all()
            for artifact in artifacts:
                if artifact.kind is ArtifactKind.RAW:
                    artifact.status = ArtifactStatus.READY
                    artifact.item_count = 1
                    artifact.failure_reason = None
                elif artifact.kind is ArtifactKind.PARSED:
                    artifact.status = ArtifactStatus.READY
                    artifact.item_count = len(blocks)
                    artifact.failure_reason = None
                # EMBEDDING/SPARSE 在本切片没有生成，必须继续保持 PENDING，
                # 防止 parsed artifact 被误认为可检索或可发布的完整索引。
            return self._result_from_models(job, version, metadata)

    async def _mark_failed(
        self, tenant_id: str, job_id: str, metadata: dict, error: Exception
    ) -> IngestionResult:
        """把解析/对象读取失败写成安全 FAILED 状态，不把原文塞进错误字段。"""

        reason = f"{type(error).__name__}: 摄取阶段失败"
        async with self.database.transaction() as session:
            job = await session.scalar(
                select(IngestionJobModel)
                .where(
                    IngestionJobModel.tenant_id == tenant_id,
                    IngestionJobModel.id == job_id,
                )
                .with_for_update()
            )
            version = await session.scalar(
                select(DocumentVersionModel)
                .where(
                    DocumentVersionModel.tenant_id == tenant_id,
                    DocumentVersionModel.document_id == metadata["document_id"],
                    DocumentVersionModel.id == metadata["version_id"],
                )
                .with_for_update()
            )
            if job is None or version is None:
                raise IngestionServiceError("无法记录摄取失败状态") from error
            if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
                return self._result_from_models(job, version, metadata, failure_reason=job.last_error)
            self._mark_job_running(job)
            job.status = transition_job_status(job.status, JobStatus.FAILED)
            job.last_error = reason
            # 只有仍处于 PROCESSING 的 version 才由当前失败 owner 推进到 FAILED。
            # 若另一条合法路径已经完成 READY，失败 job 不能把共享不可变 version
            # 回写成 FAILED；这正是并发不同幂等键场景需要保护的状态边界。
            if version.status is VersionStatus.PROCESSING:
                version.status = transition_version_status(version.status, VersionStatus.FAILED)
            document = await session.get(
                DocumentModel, (tenant_id, metadata["document_id"])
            )
            if document is not None and version.status is VersionStatus.FAILED:
                document.status = DocumentStatus.FAILED
            artifacts = (
                await session.scalars(
                    select(VersionArtifactModel).where(
                        VersionArtifactModel.tenant_id == tenant_id,
                        VersionArtifactModel.document_id == metadata["document_id"],
                        VersionArtifactModel.version_id == metadata["version_id"],
                    )
                )
            ).all()
            for artifact in artifacts:
                if artifact.kind is ArtifactKind.RAW:
                    artifact.status = ArtifactStatus.READY
                    artifact.item_count = 1
                elif artifact.kind is ArtifactKind.PARSED and version.status is VersionStatus.FAILED:
                    artifact.status = ArtifactStatus.FAILED
                    artifact.failure_reason = reason
            return self._result_from_models(job, version, metadata, failure_reason=reason)

    @staticmethod
    def _mark_job_running(job: IngestionJobModel) -> None:
        """把待执行 job 推进到 RUNNING，并统一维护 attempt 计数。"""

        if job.status is JobStatus.PENDING:
            job.status = transition_job_status(job.status, JobStatus.RUNNING)
            job.attempt += 1
        elif job.status is not JobStatus.RUNNING:
            raise IngestionServiceError(f"job 状态不允许执行: {job.status.value}")
        job.last_error = None

    @staticmethod
    async def _shared_version_failure_reason(
        session: AsyncSession,
        tenant_id: str,
        document_id: str,
        version_id: str,
    ) -> str:
        """读取共享 version 的安全失败原因，不暴露原文或底层对象 key。"""

        artifact = await session.scalar(
            select(VersionArtifactModel).where(
                VersionArtifactModel.tenant_id == tenant_id,
                VersionArtifactModel.document_id == document_id,
                VersionArtifactModel.version_id == version_id,
                VersionArtifactModel.kind == ArtifactKind.PARSED,
            )
        )
        return artifact.failure_reason or "共享 version 已在其他 job 中失败"

    async def _wait_for_version_terminal(
        self,
        tenant_id: str,
        document_id: str,
        version_id: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        """在连接池外有界等待共享 version 离开 PROCESSING。"""

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            async with self.database.session() as session:
                version = await session.get(
                    DocumentVersionModel, (tenant_id, document_id, version_id)
                )
            if version is None:
                raise IngestionServiceError("共享 version 在等待期间消失")
            if version.status is not VersionStatus.PROCESSING:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise IngestionServiceError("共享 version 在限定时间内未完成")
            await asyncio.sleep(0.01)

    async def replay_failed(self, tenant_id: str, job_id: str) -> IngestionResult:
        """显式重放 FAILED job；原文必须来自该 job 已保存的 raw object。"""

        meta_key = self._request_meta_key(tenant_id, job_id)
        # 先查 PostgreSQL 事实源，避免不存在的 job 把对象存储的 FileNotFoundError
        # 直接暴露给调用方；只有确认 job 存在后才读取其受控 raw-meta 对象。
        async with self.database.session() as session:
            existing_job = await session.get(IngestionJobModel, (tenant_id, job_id))
        if existing_job is None:
            raise IngestionServiceError("摄取任务不存在")
        try:
            metadata = json.loads((await self.storage.get(meta_key)).decode("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise IngestionServiceError("摄取任务元数据缺失，无法安全 replay") from exc
        async with self.database.transaction() as session:
            job = await session.get(IngestionJobModel, (tenant_id, job_id))
            version = await session.get(
                DocumentVersionModel,
                (tenant_id, metadata["document_id"], metadata["version_id"]),
            )
            if job is None or version is None:
                raise IngestionServiceError("摄取任务或 version 不存在")
            if job.status is not JobStatus.FAILED:
                raise ValueError("只有 FAILED job 才能显式 replay")
            transition_job_status(job.status, JobStatus.PENDING)
            transition_version_status(version.status, VersionStatus.STAGED)
            job.status = JobStatus.PENDING
            job.last_error = None
            version.status = VersionStatus.STAGED
            artifacts = (
                await session.scalars(
                    select(VersionArtifactModel).where(
                        VersionArtifactModel.tenant_id == tenant_id,
                        VersionArtifactModel.document_id == metadata["document_id"],
                        VersionArtifactModel.version_id == metadata["version_id"],
                    )
                )
            ).all()
            for artifact in artifacts:
                if artifact.kind is ArtifactKind.PARSED:
                    artifact.status = ArtifactStatus.PENDING
                    artifact.failure_reason = None
                    artifact.item_count = 0
        return await self.run_job(tenant_id, job_id)

    async def _result_from_job(self, job: IngestionJobModel, metadata: dict) -> IngestionResult:
        """从事实源读取 version 后构造安全结果。"""

        async with self.database.session() as session:
            version = await session.get(
                DocumentVersionModel,
                (job.tenant_id, metadata["document_id"], metadata["version_id"]),
            )
        if version is None:
            raise IngestionServiceError("幂等任务对应的 version 不存在")
        return self._result_from_models(job, version, metadata, failure_reason=job.last_error)

    @staticmethod
    def _result_from_models(
        job: IngestionJobModel,
        version: DocumentVersionModel,
        metadata: dict,
        *,
        failure_reason: str | None = None,
    ) -> IngestionResult:
        """集中映射 ORM 状态，避免 API/测试看到不一致的结果字段。"""

        return IngestionResult(
            job_id=job.id,
            tenant_id=job.tenant_id,
            document_id=job.document_id,
            status=job.status,
            version_id=version.id,
            version_status=version.status,
            raw_object_key=metadata.get("raw_object_key"),
            parsed_object_key=metadata.get("parsed_object_key"),
            failure_reason=failure_reason,
        )


__all__ = ["IdempotencyConflictError", "IngestionService", "IngestionServiceError"]
