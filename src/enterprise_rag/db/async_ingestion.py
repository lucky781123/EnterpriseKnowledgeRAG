"""显式 0005 migration 的异步任务事实；不改变既有同步摄取表。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from enterprise_rag.db.models import Base, _utc_now


class AsyncTaskModel(Base):
    """任务受理与当前执行事实；raw/metadata 不输出到状态 API。"""

    __tablename__ = "async_ingestion_tasks"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), primary_key=True,
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    request_metadata: Mapped[dict] = mapped_column(JSON, nullable=False)
    execution: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="parse")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    replay_actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now,
    )

    __table_args__ = (
        UniqueConstraint("id", name="uq_async_tasks_global_id"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_async_tasks_idempotency"),
        CheckConstraint("execution > 0", name="ck_async_tasks_execution_positive"),
        CheckConstraint("length(trim(actor_id)) > 0", name="ck_async_tasks_actor"),
        CheckConstraint("length(trim(idempotency_key)) > 0", name="ck_async_tasks_idempotency"),
        CheckConstraint("length(fingerprint) = 64", name="ck_async_tasks_fingerprint"),
        CheckConstraint("length(trim(raw_object_key)) > 0", name="ck_async_tasks_raw_key"),
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', 'dead_letter')",
            name="ck_async_tasks_status",
        ),
        CheckConstraint(
            "stage IN ('parse', 'embedding', 'dense', 'sparse', 'verify', 'publish')",
            name="ck_async_tasks_stage",
        ),
    )


class AsyncStageModel(Base):
    """每次执行的阶段尝试、有限租约和产物 locator，旧执行历史不被 replay 删除。"""

    __tablename__ = "async_ingestion_stages"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    execution: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    checkpoints: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    replay_actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["async_ingestion_tasks.tenant_id", "async_ingestion_tasks.id"],
            ondelete="RESTRICT", name="fk_async_stages_task_tenant",
        ),
        CheckConstraint("execution > 0", name="ck_async_stages_execution"),
        CheckConstraint("attempt BETWEEN 0 AND 4", name="ck_async_stages_attempt"),
        CheckConstraint("fence >= 0", name="ck_async_stages_fence"),
        CheckConstraint(
            "name IN ('parse', 'embedding', 'dense', 'sparse', 'verify', 'publish')",
            name="ck_async_stages_name",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'retry_wait', 'succeeded', 'dead_letter')",
            name="ck_async_stages_status",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) "
            "OR (status != 'running' AND lease_token IS NULL AND lease_until IS NULL)",
            name="ck_async_stages_lease",
        ),
        Index("ix_async_stages_recovery", "status", "lease_until"),
    )


class AsyncOutboxModel(Base):
    """有界 dispatcher 租约；消息只含 opaque ID，其余上下文由 PG 重新加载。"""

    __tablename__ = "async_ingestion_outbox"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    execution: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "task_id", "execution", "stage"],
            [
                "async_ingestion_stages.tenant_id", "async_ingestion_stages.task_id",
                "async_ingestion_stages.execution", "async_ingestion_stages.name",
            ],
            ondelete="RESTRICT", name="fk_async_outbox_stage_tenant",
        ),
        UniqueConstraint(
            "tenant_id", "task_id", "execution", "stage", "attempt",
            name="uq_async_outbox_delivery",
        ),
        CheckConstraint("attempt BETWEEN 1 AND 4", name="ck_async_outbox_attempt"),
        CheckConstraint(
            "(lease_token IS NULL AND lease_until IS NULL) "
            "OR (lease_token IS NOT NULL AND lease_until IS NOT NULL)",
            name="ck_async_outbox_lease",
        ),
        Index("ix_async_outbox_dispatch", "sent_at", "available_at", "lease_until"),
    )
