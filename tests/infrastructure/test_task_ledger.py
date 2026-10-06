"""持久任务/outbox 的离线契约；SQLite 不证明 PostgreSQL 并发锁语义。"""

from datetime import timedelta

import pytest
from sqlalchemy import inspect, select, update
from sqlalchemy.exc import IntegrityError

from enterprise_rag.db.async_ingestion import AsyncOutboxModel, AsyncStageModel, AsyncTaskModel
from enterprise_rag.db.migrations import (
    ASYNC_INGESTION_VERSION,
    LATEST_VERSION,
    check_database_readiness,
    upgrade_database,
)
from enterprise_rag.db.models import TenantModel
from enterprise_rag.db.session import Database
from enterprise_rag.ingestion.task_ledger import (
    OutboxDispatcher,
    StageFailure,
    StaleStageLease,
    TaskConflict,
    TaskLedger,
)
from enterprise_rag.security.authorization import ResourceNotFound
from enterprise_rag.security.context import RequestContext


def context(tenant="a", actor="editor"):
    """构造已验证身份的测试上下文，不让调用者自报 tenant。"""
    return RequestContext(
        tenant_id=tenant, actor_id=actor, roles=frozenset({"editor"}),
        issued_at=1, expires_at=2**31, token_version=1,
    )


@pytest.fixture
async def ledger(tmp_path):
    """仅在临时离线数据库应用显式新增 migration。"""
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}")
    await upgrade_database(database.engine, target_version=ASYNC_INGESTION_VERSION)
    async with database.transaction() as session:
        session.add_all([TenantModel(id="a", name="A"), TenantModel(id="b", name="B")])
    try:
        yield TaskLedger(database)
    finally:
        await database.dispose()


async def submit(ledger, *, ctx=None, key="request", fingerprint="1" * 64):
    """提交已落原文的请求事实；这里不执行解析或模型调用。"""
    return await ledger.submit(
        ctx or context(), idempotency_key=key, fingerprint=fingerprint,
        raw_object_key="raw/a/object.txt", metadata={"filename": "object.txt", "profile": "v1"},
    )


@pytest.mark.asyncio
async def test_additive_migration_is_explicit_and_repeatable(tmp_path) -> None:
    """默认仍为 0004，旧目标不能提前创建新表。"""
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'upgrade.db'}")
    try:
        await database.migrate()
        async with database.engine.connect() as connection:
            before = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
        assert "async_ingestion_tasks" not in before
        assert LATEST_VERSION == "0004_weaviate_hybrid_generations"
        await upgrade_database(database.engine, target_version=ASYNC_INGESTION_VERSION)
        await upgrade_database(database.engine, target_version=ASYNC_INGESTION_VERSION)
        assert (await check_database_readiness(
            database.engine, target_version=ASYNC_INGESTION_VERSION,
        )).ready
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_submission_atomically_creates_task_stages_and_outbox(ledger) -> None:
    """受理事务同时落 task/stage/outbox，不依赖 broker 可达。"""
    first = await submit(ledger)
    second = await submit(ledger)
    assert first.task_id == second.task_id
    assert first.status == "queued" and first.stage == "parse"
    async with ledger.database.session() as session:
        assert len((await session.scalars(select(AsyncTaskModel))).all()) == 1
        assert len((await session.scalars(select(AsyncStageModel))).all()) == 6
        assert len((await session.scalars(select(AsyncOutboxModel))).all()) == 1
    with pytest.raises(TaskConflict):
        await submit(ledger, fingerprint="2" * 64)


@pytest.mark.asyncio
async def test_task_status_and_replay_are_tenant_and_owner_scoped(ledger) -> None:
    """跨租户/非所有者查询或 replay 都隐藏存在性。"""
    task = await submit(ledger)
    for ctx in (context("b"), context("a", "another")):
        with pytest.raises(ResourceNotFound):
            await ledger.get(ctx, task.task_id)
        with pytest.raises(ResourceNotFound):
            await ledger.replay(ctx, task.task_id)
    other = await submit(ledger, ctx=context("b"))
    assert other.task_id != task.task_id


@pytest.mark.asyncio
async def test_fencing_and_checkpoints_reject_expired_worker(ledger) -> None:
    """过期后重新领取增加 fence，旧 Worker 不可记录 checkpoint 或完成阶段。"""
    task = await submit(ledger)
    old = await ledger.claim_stage(task.task_id, execution=1, stage="parse", attempt=1)
    assert old is not None
    assert await ledger.claim_stage(task.task_id, execution=1, stage="parse", attempt=1) is None
    await ledger.checkpoint(old, "batch-1", object_key="checkpoints/one", content_hash="3" * 64)
    async with ledger.database.transaction() as session:
        await session.execute(update(AsyncStageModel).values(
            lease_until=old.lease_until - timedelta(days=1),
        ).where(AsyncStageModel.status == "running"))
    assert await ledger.recover_expired(limit=10) == 1
    async with ledger.database.transaction() as session:
        # 只推进重试到期时间；过期恢复本身必须使用持久退避，而非立即无限重领。
        await session.execute(update(AsyncStageModel).where(
            AsyncStageModel.task_id == task.task_id, AsyncStageModel.name == "parse",
        ).values(available_at=old.lease_until - timedelta(days=1)))
    new = await ledger.claim_stage(task.task_id, execution=1, stage="parse", attempt=2)
    assert new is not None and new.fence > old.fence
    assert (await ledger.checkpoints(new))["batch-1"]["object_key"] == "checkpoints/one"
    with pytest.raises(StaleStageLease):
        await ledger.complete(old)
    with pytest.raises(StaleStageLease):
        await ledger.checkpoint(old, "batch-2", object_key="blocked", content_hash="4" * 64)
    await ledger.complete(new)
    assert (await ledger.get(context(), task.task_id)).stage == "embedding"


@pytest.mark.asyncio
async def test_retry_budget_dead_letter_and_replay_preserve_history(ledger) -> None:
    """最多四次领取；人工 replay 新执行，旧死信与 checkpoint 不被抹除。"""
    task = await submit(ledger)
    for attempt in range(1, 5):
        lease = await ledger.claim_stage(task.task_id, execution=1, stage="parse", attempt=attempt)
        assert lease is not None
        await ledger.fail(lease, StageFailure("dependency_unavailable", retryable=True))
        async with ledger.database.transaction() as session:
            # 测试仅推进可运行时间，不等待真实退避；生产仍使用 DB 时间。
            await session.execute(update(AsyncStageModel).values(
                available_at=lease.lease_until - timedelta(days=1),
            ))
    failed = await ledger.get(context(), task.task_id)
    assert failed.status == "dead_letter" and failed.error_code == "dependency_unavailable"
    assert await ledger.claim_stage(task.task_id, execution=1, stage="parse", attempt=5) is None
    replayed = await ledger.replay(context(), task.task_id)
    assert replayed.execution == 2 and replayed.status == "queued"
    assert await ledger.claim_stage(task.task_id, execution=1, stage="parse", attempt=1) is None
    async with ledger.database.session() as session:
        previous = await session.get(AsyncStageModel, ("a", task.task_id, 1, "parse"))
        assert previous.status == "dead_letter" and previous.attempt == 4


@pytest.mark.asyncio
async def test_nonretryable_failure_never_requeues(ledger) -> None:
    """永久性格式/权限错误不自动重试。"""
    task = await submit(ledger)
    lease = await ledger.claim_stage(task.task_id, execution=1, stage="parse", attempt=1)
    await ledger.fail(lease, StageFailure("invalid_input", retryable=False))
    assert (await ledger.get(context(), task.task_id)).status == "dead_letter"
    assert await ledger.claim_stage(task.task_id, execution=1, stage="parse", attempt=2) is None


@pytest.mark.asyncio
async def test_outbox_failure_is_durable_and_next_dispatch_can_retry(ledger) -> None:
    """broker 故障不删除事实，重试只发送 opaque event ID、不发送正文或 token。"""
    await submit(ledger)
    calls = []

    async def broken(event_id):
        """模拟提交后 broker 不可用。"""
        calls.append(event_id)
        raise ConnectionError("secret endpoint")

    dispatcher = OutboxDispatcher(ledger, broken)
    assert await dispatcher.dispatch_once(limit=5) == 0
    async with ledger.database.session() as session:
        event = (await session.scalars(select(AsyncOutboxModel))).one()
        assert event.sent_at is None and event.lease_token is None
        event_id = event.id
    assert calls == [event_id]

    async def accepted(event_id):
        """模拟 broker 已确认接收，不访问真实 Redis。"""
        calls.append(event_id)

    assert await OutboxDispatcher(ledger, accepted).dispatch_once(limit=5) == 1
    assert calls == [event_id, event_id]
    assert await OutboxDispatcher(ledger, accepted).dispatch_once(limit=5) == 0


@pytest.mark.asyncio
async def test_stages_advance_once_and_old_delivery_cannot_run_next_stage(ledger) -> None:
    """阶段按固定顺序推进，旧消息不重做成功阶段或提前领取未来阶段。"""
    task = await submit(ledger)
    assert await ledger.claim_stage(task.task_id, execution=1, stage="dense", attempt=1) is None
    for name in ("parse", "embedding", "dense", "sparse", "verify", "publish"):
        lease = await ledger.claim_stage(task.task_id, execution=1, stage=name, attempt=1)
        assert lease is not None
        await ledger.complete(lease)
        assert await ledger.claim_stage(task.task_id, execution=1, stage=name, attempt=1) is None
        with pytest.raises(StaleStageLease):
            await ledger.complete(lease)
    assert (await ledger.get(context(), task.task_id)).status == "succeeded"
    with pytest.raises(TaskConflict):
        await ledger.replay(context(), task.task_id)


@pytest.mark.asyncio
async def test_disabled_tenant_cannot_query_or_execute_task(ledger) -> None:
    """租户关闭不能被已排队任务或已有身份绕过。"""
    task = await submit(ledger)
    async with ledger.database.transaction() as session:
        await session.execute(update(TenantModel).where(
            TenantModel.id == "a",
        ).values(is_active=False))
    with pytest.raises(ResourceNotFound):
        await ledger.get(context(), task.task_id)
    with pytest.raises(ResourceNotFound):
        await ledger.claim_stage(task.task_id, execution=1, stage="parse", attempt=1)


@pytest.mark.asyncio
async def test_old_dispatcher_token_cannot_ack_new_claim(ledger) -> None:
    """dispatcher 租约过期重新领取后，旧 ACK 不得覆盖新租约。"""
    await submit(ledger)
    old = (await ledger.claim_outbox(limit=1))[0]
    async with ledger.database.transaction() as session:
        now = await ledger._now(session)
        await session.execute(update(AsyncOutboxModel).where(
            AsyncOutboxModel.id == old.event_id,
        ).values(lease_until=now - timedelta(seconds=1)))
    new = (await ledger.claim_outbox(limit=1))[0]
    assert old.token != new.token and old.event_id == new.event_id
    await ledger.finish_outbox(old, sent=True)
    async with ledger.database.session() as session:
        row = await session.get(AsyncOutboxModel, old.event_id)
        assert row.sent_at is None and row.lease_token == new.token
    await ledger.finish_outbox(new, sent=True)
    assert not await ledger.claim_outbox(limit=1)


@pytest.mark.asyncio
async def test_checkpoint_conflict_and_renewal_require_valid_lease(ledger) -> None:
    """相同批次只允许相同产物重放，续租不能改变 fence 或复活过期持有者。"""
    task = await submit(ledger)
    lease = await ledger.claim_stage(task.task_id, execution=1, stage="parse", attempt=1)
    await ledger.checkpoint(lease, "one", object_key="cp/one", content_hash="5" * 64)
    with pytest.raises(TaskConflict):
        await ledger.checkpoint(lease, "one", object_key="cp/two", content_hash="6" * 64)
    renewed = await ledger.renew(lease)
    assert renewed.fence == lease.fence and renewed.token == lease.token
    await ledger.complete(renewed)
    with pytest.raises(StaleStageLease):
        await ledger.renew(lease)


@pytest.mark.asyncio
async def test_outbox_creation_failure_rolls_back_entire_submission(ledger, monkeypatch) -> None:
    """task 和阶段不能在 outbox 未创建时提交，避免任务永久悬空。"""
    async def broken(*args, **kwargs):
        """在提交前注入阶段投递事实写入故障。"""
        raise RuntimeError("injected")

    monkeypatch.setattr(ledger, "_enqueue", broken)
    with pytest.raises(RuntimeError):
        await submit(ledger)
    async with ledger.database.session() as session:
        assert not (await session.scalars(select(AsyncTaskModel))).all()
        assert not (await session.scalars(select(AsyncStageModel))).all()
        assert not (await session.scalars(select(AsyncOutboxModel))).all()


def test_raw_errors_cannot_be_saved_as_failure_categories() -> None:
    """错误类别只接受白名单，永远不保存任意上游异常内容。"""
    with pytest.raises(ValueError):
        StageFailure("secret endpoint", retryable=True)
    with pytest.raises(ValueError):
        StageFailure("permission_denied", retryable=True)


@pytest.mark.asyncio
async def test_event_context_loaded_from_pg_and_duplicate_consumption_is_noop(ledger) -> None:
    """消息只有 event ID，消费者不能自行给定 tenant/stage/attempt。"""
    task = await submit(ledger)
    async with ledger.database.session() as session:
        event = (await session.scalars(select(AsyncOutboxModel))).one()
    lease = await ledger.claim_event(event.id)
    assert lease.task_id == task.task_id and lease.tenant_id == "a" and lease.stage == "parse"
    assert await ledger.claim_event(event.id) is None
    with pytest.raises(ResourceNotFound):
        await ledger.claim_event("untrusted-id")


@pytest.mark.asyncio
async def test_broker_message_loss_requeues_only_unclaimed_current_stage(ledger) -> None:
    """已确认但未运行的消息可恢复，运行中任务与旧消息不能被恢复器重复领取。"""
    await submit(ledger)
    event = (await ledger.claim_outbox(limit=1))[0]
    assert await ledger.finish_outbox(event, sent=True)
    async with ledger.database.transaction() as session:
        now = await ledger._now(session)
        await session.execute(update(AsyncOutboxModel).where(
            AsyncOutboxModel.id == event.event_id,
        ).values(sent_at=now - timedelta(minutes=2)))
    assert await ledger.recover_deliveries(limit=5) == 1
    redelivered = (await ledger.claim_outbox(limit=1))[0]
    assert redelivered.event_id == event.event_id
    assert await ledger.finish_outbox(redelivered, sent=True)
    lease = await ledger.claim_event(event.event_id)
    assert lease.attempt == 1
    async with ledger.database.transaction() as session:
        now = await ledger._now(session)
        await session.execute(update(AsyncOutboxModel).where(
            AsyncOutboxModel.id == event.event_id,
        ).values(sent_at=now - timedelta(minutes=2)))
    assert await ledger.recover_deliveries(limit=5) == 0


@pytest.mark.asyncio
async def test_composite_stage_fk_rejects_other_tenant_task(ledger) -> None:
    """服务层之外，数据库复合外键也拒绝跨 tenant 的阶段绑定。"""
    task = await submit(ledger)
    with pytest.raises(IntegrityError):
        async with ledger.database.transaction() as session:
            now = await ledger._now(session)
            session.add(AsyncStageModel(
                tenant_id="b", task_id=task.task_id, execution=1, name="parse", available_at=now,
            ))
