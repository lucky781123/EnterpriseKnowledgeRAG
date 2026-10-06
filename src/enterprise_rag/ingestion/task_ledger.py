"""短事务任务账本与 outbox；不把生命周期原语冒充真实摄取 Worker。"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.db.async_ingestion import AsyncOutboxModel, AsyncStageModel, AsyncTaskModel
from enterprise_rag.db.models import TenantModel
from enterprise_rag.db.session import Database
from enterprise_rag.security.authorization import (
    AuthorizationService,
    ResourceNotFound,
)
from enterprise_rag.security.context import RequestContext
from enterprise_rag.storage.s3 import _safe_key

STAGES = ("parse", "embedding", "dense", "sparse", "verify", "publish")
ERROR_CODES = frozenset({
    "dependency_unavailable", "timeout", "worker_lost", "invalid_input",
    "permission_denied", "profile_mismatch", "integrity_failed", "internal_error",
})


class TaskConflict(ValueError):
    """同幂等键内容冲突或当前状态不允许 replay；不回显对象元数据。"""


class StaleStageLease(RuntimeError):
    """旧 Worker 已失去本阶段的租约，禁止继续提交 checkpoint 或发布事实。"""


@dataclass(frozen=True, slots=True)
class TaskStatus:
    """状态查询摘要不含原文、对象 key、身份快照或上游错误内容。"""

    task_id: str
    execution: int
    status: str
    stage: str
    error_code: str | None


@dataclass(frozen=True, slots=True)
class StageLease:
    """Worker 内部租约，所有提交仍需到 PG 核对，不信任调用者自行生成的值。"""

    tenant_id: str
    task_id: str
    execution: int
    stage: str
    attempt: int
    fence: int
    token: str
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class StageFailure:
    """只接受稳定错误类别，原始异常与凭据不能写入任务事实。"""

    code: str
    retryable: bool

    def __post_init__(self) -> None:
        """永久错误不允许自动重试，未知错误不能以自由文本保存。"""
        if self.code not in ERROR_CODES or type(self.retryable) is not bool:
            raise ValueError("阶段错误类别非法")
        if self.retryable and self.code not in {
            "dependency_unavailable", "timeout", "worker_lost",
        }:
            raise ValueError("该阶段错误不可重试")


@dataclass(frozen=True, slots=True)
class OutboxLease:
    """消息投递租约只保存 opaque ID 和随机 token。"""

    event_id: str
    token: str


def _utc(value: datetime) -> datetime:
    """SQLite 丢失 tzinfo 时按 UTC 解释；生产使用 PG 带时区字段。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _hash(value: str) -> None:
    """校验摘要格式，不把非法值回显到异常。"""
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError("SHA256 摘要非法")


class TaskLedger:
    """PG 负责长期任务、重试和 fencing；不连接对象存储或 broker。"""

    def __init__(self, database: Database, *, lease_seconds: int = 900) -> None:
        """租约时长有界，由服务端配置，不能从消息或 HTTP 请求指定。"""
        if type(lease_seconds) is not int or not 30 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds 必须在 30 到 3600 秒之间")
        self.database = database
        self.lease_seconds = lease_seconds
        self.authorization = AuthorizationService()

    @staticmethod
    async def _now(session: AsyncSession) -> datetime:
        """用数据库时间判断租约，避免不同 Worker 的系统时钟偏差。"""
        clock = (
            func.clock_timestamp() if session.get_bind().dialect.name == "postgresql"
            else func.current_timestamp()
        )
        return _utc(await session.scalar(select(clock)))

    @staticmethod
    def _summary(task: AsyncTaskModel) -> TaskStatus:
        """显式挑选安全字段，不序列化 ORM 全对象。"""
        return TaskStatus(task.id, task.execution, task.status, task.stage, task.error_code)

    @staticmethod
    async def _task(session: AsyncSession, task_id: str, *, tenant: str | None = None):
        """统一先锁任务再锁阶段，避免 replay 与 Worker 更新交错。"""
        statement = select(AsyncTaskModel).where(AsyncTaskModel.id == task_id)
        if tenant is not None:
            statement = statement.where(AsyncTaskModel.tenant_id == tenant)
        task = await session.scalar(statement.with_for_update())
        if task is None:
            raise ResourceNotFound("资源不存在")
        return task

    @staticmethod
    async def _active_tenant(session: AsyncSession, tenant: str) -> None:
        """任务/恢复/消费都检查租户状态，不在 tenant 不可用时继续写入。"""
        active = await session.scalar(select(TenantModel.is_active).where(TenantModel.id == tenant))
        if not active:
            raise ResourceNotFound("资源不存在")

    @staticmethod
    def _owner(context: RequestContext, task: AsyncTaskModel) -> None:
        """非同 tenant 所有者或 admin 不暴露任务是否存在。"""
        if task.tenant_id != context.tenant_id or (
            task.actor_id != context.actor_id and not context.has_role("admin")
        ):
            raise ResourceNotFound("资源不存在")

    @staticmethod
    def _stages(session: AsyncSession, task: AsyncTaskModel, now: datetime) -> None:
        """每次执行创建独立阶段，replay 不删除旧终态或旧 checkpoint。"""
        session.add_all([
            AsyncStageModel(
                tenant_id=task.tenant_id, task_id=task.id, execution=task.execution,
                name=name, available_at=now, replay_actor_id=task.replay_actor_id,
            )
            for name in STAGES
        ])

    @staticmethod
    async def _enqueue(
        session: AsyncSession, task: AsyncTaskModel, *, stage: str, attempt: int,
        available_at: datetime,
    ) -> None:
        """在阶段状态事务内创建下一次投递，事务提交前不发送外部消息。"""
        await session.flush()
        session.add(AsyncOutboxModel(
            id=f"event_{uuid4().hex}", tenant_id=task.tenant_id, task_id=task.id,
            execution=task.execution, stage=stage, attempt=attempt, available_at=available_at,
        ))

    async def submit(
        self, context: RequestContext, *, idempotency_key: str, fingerprint: str,
        raw_object_key: str, metadata: dict,
    ) -> TaskStatus:
        """接受已落原文的可信 locator；实际原文校验/流式受理由后续业务服务负责。"""
        self.authorization.require_any_role(context, {"editor", "admin"})
        _hash(fingerprint)
        _safe_key(raw_object_key)
        if not idempotency_key.strip() or len(idempotency_key) > 255:
            raise ValueError("幂等键非法")
        allowed = {
            "filename", "profile", "source_id", "title", "source_revision", "media_type",
            "content_hash", "document_id", "version_id", "parsed_object_key",
            "policy_version", "roles",
        }
        if not isinstance(metadata, dict) or not set(metadata) <= allowed:
            raise ValueError("受理 metadata 字段非法")
        # 元数据只允许最小字符串事实，禁止混入 token、模型地址或任意正文。
        if any(value is not None and not isinstance(value, str) for value in metadata.values()):
            raise ValueError("受理 metadata 值非法")
        if len(json.dumps(metadata, ensure_ascii=False).encode()) > 8192:
            raise ValueError("受理 metadata 过大")
        async with self.database.transaction() as session:
            tenant = await session.scalar(select(TenantModel).where(
                TenantModel.id == context.tenant_id,
            ).with_for_update())
            if tenant is None or not tenant.is_active:
                raise ResourceNotFound("资源不存在")
            existing = await session.scalar(select(AsyncTaskModel).where(
                AsyncTaskModel.tenant_id == context.tenant_id,
                AsyncTaskModel.idempotency_key == idempotency_key,
            ))
            if existing is not None:
                self._owner(context, existing)
                if existing.fingerprint != fingerprint:
                    raise TaskConflict("幂等键内容冲突")
                return self._summary(existing)
            now = await self._now(session)
            task = AsyncTaskModel(
                tenant_id=context.tenant_id, id=f"task_{uuid4().hex}",
                actor_id=context.actor_id, idempotency_key=idempotency_key,
                fingerprint=fingerprint, raw_object_key=raw_object_key,
                request_metadata=dict(metadata), execution=1, status="queued", stage=STAGES[0],
            )
            session.add(task)
            await session.flush()
            self._stages(session, task, now)
            await self._enqueue(session, task, stage=STAGES[0], attempt=1, available_at=now)
            return self._summary(task)

    async def get(self, context: RequestContext, task_id: str) -> TaskStatus:
        """从长期事实查询状态，backend TTL/缓存命中不能改变可见性。"""
        async with self.database.transaction() as session:
            task = await self._task(session, task_id, tenant=context.tenant_id)
            self._owner(context, task)
            await self._active_tenant(session, task.tenant_id)
            return self._summary(task)

    async def replay(self, context: RequestContext, task_id: str) -> TaskStatus:
        """只有终态死信可以人工 replay，旧执行仍可审计，不自动复活成功任务。"""
        self.authorization.require_any_role(context, {"editor", "admin"})
        async with self.database.transaction() as session:
            task = await self._task(session, task_id, tenant=context.tenant_id)
            self._owner(context, task)
            await self._active_tenant(session, task.tenant_id)
            if task.status != "dead_letter":
                raise TaskConflict("当前任务不允许 replay")
            now = await self._now(session)
            task.execution += 1
            task.status, task.stage, task.error_code = "queued", STAGES[0], None
            task.replay_actor_id = context.actor_id
            self._stages(session, task, now)
            await self._enqueue(session, task, stage=STAGES[0], attempt=1, available_at=now)
            return self._summary(task)

    async def claim_stage(
        self, task_id: str, *, execution: int, stage: str, attempt: int,
    ) -> StageLease | None:
        """内部消费者原子领取阶段；旧消息/重复消息/未到时间均不能推进事实。"""
        async with self.database.transaction() as session:
            task = await self._task(session, task_id)
            return await self._claim(
                session, task, execution=execution, stage=stage, attempt=attempt,
            )

    async def claim_event(self, event_id: str) -> StageLease | None:
        """消费者只接受 opaque event ID，执行身份与阶段参数全部从 PG 重载。"""
        async with self.database.transaction() as session:
            event = await session.get(AsyncOutboxModel, event_id)
            if event is None:
                raise ResourceNotFound("资源不存在")
            task = await self._task(session, event.task_id, tenant=event.tenant_id)
            if _utc(event.available_at) > await self._now(session):
                return None
            # 不要求 sent_at 已落库：Worker 可能在 producer 标送达之前就消费成功。
            return await self._claim(
                session, task, execution=event.execution, stage=event.stage, attempt=event.attempt,
            )

    async def _claim(
        self, session: AsyncSession, task: AsyncTaskModel, *, execution: int, stage: str, attempt: int,
    ) -> StageLease | None:
        """在任务行锁内执行同一领取规则，避免消息入口与恢复入口不一致。"""
        await self._active_tenant(session, task.tenant_id)
        if task.execution != execution or task.stage != stage or task.status in {
            "succeeded", "dead_letter",
        }:
            return None
        row = await session.get(AsyncStageModel, (task.tenant_id, task.id, execution, stage))
        now = await self._now(session)
        if (
            row is None or row.status not in {"pending", "retry_wait"}
            or row.attempt >= 4 or row.attempt + 1 != attempt
            or _utc(row.available_at) > now
        ):
            return None
        row.attempt += 1
        row.fence += 1
        row.status, task.status = "running", "running"
        row.lease_token = secrets.token_hex(32)
        row.lease_until = now + timedelta(seconds=self.lease_seconds)
        return StageLease(
            task.tenant_id, task.id, execution, stage, row.attempt,
            row.fence, row.lease_token, row.lease_until,
        )

    async def _leased(self, session: AsyncSession, lease: StageLease):
        """在持锁事务中核对完整 lease/fence；调用者不能仅凭 Redis 锁发布。"""
        task = await self._task(session, lease.task_id, tenant=lease.tenant_id)
        await self._active_tenant(session, task.tenant_id)
        row = await session.get(
            AsyncStageModel, (lease.tenant_id, lease.task_id, lease.execution, lease.stage),
        )
        now = await self._now(session)
        if (
            task.execution != lease.execution or task.stage != lease.stage
            or task.status != "running" or row is None or row.status != "running"
            or row.fence != lease.fence or row.lease_token != lease.token
            or row.attempt != lease.attempt or _utc(row.lease_until) <= now
        ):
            raise StaleStageLease("阶段租约失效")
        return task, row, now

    async def checkpoint(
        self, lease: StageLease, batch: str, *, object_key: str, content_hash: str,
    ) -> None:
        """只提交已落盘产物的 locator/hash，过期 fence 不能覆盖新 Worker 结果。"""
        _safe_key(object_key)
        _hash(content_hash)
        if not isinstance(batch, str) or not batch.strip() or len(batch) > 128:
            raise ValueError("checkpoint 批次 identity 非法")
        async with self.database.transaction() as session:
            _, row, _ = await self._leased(session, lease)
            value = {"object_key": object_key, "content_hash": content_hash}
            if batch in row.checkpoints and row.checkpoints[batch] != value:
                raise TaskConflict("checkpoint identity 冲突")
            row.checkpoints = {**row.checkpoints, batch: value}

    async def checkpoints(self, lease: StageLease) -> dict:
        """只向当前租约提供已提交 checkpoint；产物内容仍须由业务层校验。"""
        async with self.database.transaction() as session:
            _, row, _ = await self._leased(session, lease)
            return {key: dict(value) for key, value in row.checkpoints.items()}

    async def renew(self, lease: StageLease) -> StageLease:
        """仅有效持有者可以续租，不复活已到期或已重领的阶段。"""
        async with self.database.transaction() as session:
            _, row, now = await self._leased(session, lease)
            row.lease_until = now + timedelta(seconds=self.lease_seconds)
            return StageLease(
                lease.tenant_id, lease.task_id, lease.execution, lease.stage,
                lease.attempt, lease.fence, lease.token, row.lease_until,
            )

    async def complete(self, lease: StageLease, *, commit=None) -> None:
        """记录阶段完成并事务生成下一阶段 outbox；不执行真实索引发布。"""
        async with self.database.transaction() as session:
            task, row, now = await self._leased(session, lease)
            if commit is not None:
                # 业务事实发布与租约核对共用当前短事务；回调严禁执行外部 I/O。
                await commit(session, task)
            row.status, row.completed_at = "succeeded", now
            row.lease_token, row.lease_until, row.error_code = None, None, None
            position = STAGES.index(lease.stage)
            task.error_code = None
            if position == len(STAGES) - 1:
                task.status = "succeeded"
            else:
                task.stage, task.status = STAGES[position + 1], "queued"
                await self._enqueue(session, task, stage=task.stage, attempt=1, available_at=now)

    async def task_input(self, lease: StageLease) -> dict:
        """有效 Worker 读取服务器保存的输入，不从消息自报 tenant/profile。"""
        async with self.database.transaction() as session:
            task, _, _ = await self._leased(session, lease)
            tenant = await session.get(TenantModel, lease.tenant_id)
            if task.request_metadata.get("policy_version") != str(tenant.policy_version):
                raise ResourceNotFound("任务授权版本失效")
            rows = (await session.scalars(select(AsyncStageModel).where(
                AsyncStageModel.tenant_id == lease.tenant_id,
                AsyncStageModel.task_id == lease.task_id,
                AsyncStageModel.execution == lease.execution,
            ))).all()
            return {
                "metadata": dict(task.request_metadata), "raw_object_key": task.raw_object_key,
                "actor_id": task.actor_id,
                "checkpoints": {row.name: dict(row.checkpoints) for row in rows},
            }

    async def _failed(
        self, session: AsyncSession, task: AsyncTaskModel, row: AsyncStageModel,
        failure: StageFailure, now: datetime,
    ) -> None:
        """同一次领取消耗预算；Worker lost 也计入预算，禁止无限 redelivery。"""
        row.error_code = task.error_code = failure.code
        row.lease_token, row.lease_until = None, None
        if not failure.retryable or row.attempt >= 4:
            task.status = row.status = "dead_letter"
            row.completed_at = now
            return
        row.status = task.status = "retry_wait"
        # PG 保存到期时间；不使用 Redis key 或 Celery ETA 作为唯一重试事实。
        delay = min(300, 5 * 2 ** (row.attempt - 1)) + secrets.randbelow(6)
        row.available_at = now + timedelta(seconds=delay)
        await self._enqueue(
            session, task, stage=row.name, attempt=row.attempt + 1, available_at=row.available_at,
        )

    async def fail(self, lease: StageLease, failure: StageFailure) -> None:
        """有效 Worker 只保存脱敏类别；过期 Worker 的异常不得覆盖后来者。"""
        async with self.database.transaction() as session:
            task, row, now = await self._leased(session, lease)
            await self._failed(session, task, row, failure, now)

    async def recover_expired(self, *, limit: int = 100) -> int:
        """扫描有界候选，逐任务短事务恢复过期阶段，重新检查时间与当前执行。"""
        self._limit(limit)
        async with self.database.session() as session:
            now = await self._now(session)
            candidates = (await session.execute(select(
                AsyncStageModel.tenant_id, AsyncStageModel.task_id,
                AsyncStageModel.execution, AsyncStageModel.name,
            ).where(
                AsyncStageModel.status == "running", AsyncStageModel.lease_until <= now,
            ).order_by(AsyncStageModel.lease_until).limit(limit))).all()
        count = 0
        for tenant, task_id, execution, stage in candidates:
            async with self.database.transaction() as session:
                task = await self._task(session, task_id, tenant=tenant)
                row = await session.get(AsyncStageModel, (tenant, task_id, execution, stage))
                now = await self._now(session)
                if (
                    task.execution != execution or task.stage != stage or row.status != "running"
                    or _utc(row.lease_until) > now
                ):
                    continue
                active = await session.scalar(select(TenantModel.is_active).where(
                    TenantModel.id == tenant,
                ))
                await self._failed(
                    session, task, row,
                    StageFailure("worker_lost", retryable=True) if active else
                    StageFailure("permission_denied", retryable=False), now,
                )
                count += 1
        return count

    async def recover_deliveries(self, *, limit: int = 100, stale_seconds: int = 60) -> int:
        """broker 丢失已确认消息时，从未运行的 PG 当前阶段重置相同 outbox 供重投。"""
        self._limit(limit)
        if type(stale_seconds) is not int or stale_seconds < 30:
            raise ValueError("stale_seconds 至少为 30")
        async with self.database.session() as session:
            now = await self._now(session)
            cutoff = now - timedelta(seconds=stale_seconds)
            candidates = (await session.execute(select(
                AsyncOutboxModel.id, AsyncOutboxModel.tenant_id, AsyncOutboxModel.task_id,
            ).join(
                AsyncTaskModel,
                (AsyncTaskModel.tenant_id == AsyncOutboxModel.tenant_id)
                & (AsyncTaskModel.id == AsyncOutboxModel.task_id),
            ).join(
                AsyncStageModel,
                (AsyncStageModel.tenant_id == AsyncOutboxModel.tenant_id)
                & (AsyncStageModel.task_id == AsyncOutboxModel.task_id)
                & (AsyncStageModel.execution == AsyncOutboxModel.execution)
                & (AsyncStageModel.name == AsyncOutboxModel.stage),
            ).join(
                TenantModel, TenantModel.id == AsyncOutboxModel.tenant_id,
            ).where(
                AsyncOutboxModel.sent_at <= cutoff,
                AsyncTaskModel.execution == AsyncOutboxModel.execution,
                AsyncTaskModel.stage == AsyncOutboxModel.stage,
                AsyncTaskModel.status.in_(("queued", "retry_wait")),
                AsyncStageModel.status.in_(("pending", "retry_wait")),
                AsyncStageModel.attempt + 1 == AsyncOutboxModel.attempt,
                AsyncStageModel.available_at <= now,
                TenantModel.is_active.is_(True),
            ).order_by(AsyncOutboxModel.sent_at).limit(limit))).all()
        count = 0
        for event_id, tenant, task_id in candidates:
            async with self.database.transaction() as session:
                task = await self._task(session, task_id, tenant=tenant)
                event = await session.get(AsyncOutboxModel, event_id, with_for_update=True)
                now = await self._now(session)
                if (
                    event.sent_at is None or _utc(event.sent_at) > now - timedelta(
                        seconds=stale_seconds,
                    )
                    or task.execution != event.execution or task.stage != event.stage
                    or task.status not in {"queued", "retry_wait"}
                ):
                    continue
                active = await session.scalar(select(TenantModel.is_active).where(
                    TenantModel.id == tenant,
                ))
                row = await session.get(
                    AsyncStageModel, (tenant, task.id, event.execution, event.stage),
                )
                if (
                    not active or row.status not in {"pending", "retry_wait"}
                    or row.attempt + 1 != event.attempt or _utc(row.available_at) > now
                ):
                    continue
                # 重投同一 attempt 不增加执行次数；真正领取/Worker lost 才消耗预算。
                event.sent_at = event.lease_token = event.lease_until = None
                count += 1
        return count

    @staticmethod
    def _limit(limit: int) -> None:
        """限制调度扫描批量，避免恢复风暴一次占满数据库连接。"""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")

    async def claim_outbox(self, *, limit: int) -> list[OutboxLease]:
        """有限候选用条件 UPDATE 领取，发送发生在提交之后，不持 DB 锁等待 broker。"""
        self._limit(limit)
        async with self.database.transaction() as session:
            now = await self._now(session)
            eligible = (
                AsyncOutboxModel.sent_at.is_(None),
                AsyncOutboxModel.available_at <= now,
                or_(AsyncOutboxModel.lease_until.is_(None), AsyncOutboxModel.lease_until <= now),
            )
            ids = (await session.scalars(select(AsyncOutboxModel.id).where(
                *eligible,
            ).order_by(AsyncOutboxModel.available_at).limit(limit))).all()
            leases = []
            for event_id in ids:
                token = secrets.token_hex(32)
                result = await session.execute(update(AsyncOutboxModel).where(
                    AsyncOutboxModel.id == event_id, *eligible,
                ).values(lease_token=token, lease_until=now + timedelta(seconds=30)))
                if result.rowcount:
                    leases.append(OutboxLease(event_id, token))
            return leases

    async def finish_outbox(self, lease: OutboxLease, *, sent: bool) -> bool:
        """broker 接受后才标送达；过期投递者不能覆盖另一 dispatcher 的租约。"""
        async with self.database.transaction() as session:
            now = await self._now(session)
            result = await session.execute(update(AsyncOutboxModel).where(
                AsyncOutboxModel.id == lease.event_id, AsyncOutboxModel.lease_token == lease.token,
                AsyncOutboxModel.sent_at.is_(None), AsyncOutboxModel.lease_until > now,
            ).values(
                lease_token=None, lease_until=None, sent_at=now if sent else None,
            ))
            return bool(result.rowcount)


class OutboxDispatcher:
    """仅投递 opaque event ID，producer 的网络调用不能占用 PG 事务。"""

    def __init__(self, ledger: TaskLedger, publish: Callable[[str], Awaitable[None]]) -> None:
        """接受具有 broker 确认和有界 timeout 的异步 producer；本模块不创建 Celery。"""
        self.ledger, self.publish = ledger, publish

    async def dispatch_once(self, *, limit: int = 100) -> int:
        """投递失败保留未发送事实；进程崩溃后由租约过期恢复，不清空 outbox。"""
        leases = await self.ledger.claim_outbox(limit=limit)
        count = 0
        for lease in leases:
            try:
                # producer 必须响应异步取消；本层额外保证投递等待有界。
                async with asyncio.timeout(10):
                    await self.publish(lease.event_id)
            except Exception:  # noqa: BLE001 - 外部 broker 故障保留待重试，不记录异常正文。
                await self.ledger.finish_outbox(lease, sent=False)
            else:
                committed = await self.ledger.finish_outbox(lease, sent=True)
                count += int(committed)
        return count
