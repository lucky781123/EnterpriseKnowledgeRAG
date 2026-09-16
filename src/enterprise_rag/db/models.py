"""SQLAlchemy 持久化模型。

本模块描述 PostgreSQL 中的业务事实：租户、文档、版本、ACL、摄取任务和发布产物。
对象存储、Weaviate 与 Redis 是事务外的派生系统，不在这些表中伪造跨系统原子性。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    JSON,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from enterprise_rag.domain.entities import (
    ACLPermission,
    ACLPrincipalType,
    ArtifactKind,
    ArtifactStatus,
    DenseGenerationStatus,
    DocumentStatus,
    JobStatus,
    VersionStatus,
)


def _enum_type(enum_cls: type, name: str) -> Enum:
    """把领域 StrEnum 存为稳定的小写字符串，并生成数据库 CHECK 约束。"""

    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda values: [item.value for item in values],
    )


def _utc_now() -> datetime:
    """统一生成带时区的 UTC 时间，避免写入运行主机的本地时区。"""

    return datetime.now(UTC)


class Base(DeclarativeBase):
    """所有持久化模型共享的 Declarative Base。"""


class TenantModel(Base):
    """租户根实体；下游资源必须通过外键回溯到该可信边界。"""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    policy_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now()
    )
    documents: Mapped[list[DocumentModel]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_tenants_name_not_blank"),
        CheckConstraint("policy_version > 0", name="ck_tenants_policy_version_positive"),
    )


class DocumentModel(Base):
    """租户内稳定的文档身份；当前可见版本由发布事务切换。"""

    __tablename__ = "documents"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        _enum_type(DocumentStatus, "document_status"),
        nullable=False,
        default=DocumentStatus.DRAFT,
        server_default=DocumentStatus.DRAFT.value,
    )
    current_version_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
        server_default=func.now(),
    )
    tenant: Mapped[TenantModel] = relationship(back_populates="documents")
    versions: Mapped[list[DocumentVersionModel]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        foreign_keys="[DocumentVersionModel.tenant_id, DocumentVersionModel.document_id]",
    )
    acl_entries: Mapped[list[DocumentACLModel]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        foreign_keys="[DocumentACLModel.tenant_id, DocumentACLModel.document_id]",
    )
    ingestion_jobs: Mapped[list[IngestionJobModel]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        foreign_keys="[IngestionJobModel.tenant_id, IngestionJobModel.document_id]",
    )

    __table_args__ = (
        # 复合外键同时绑定 tenant、document 和 version，阻断跨租户或跨文档的
        # current_version_id。使用延迟约束允许首次导入在同一事务内先写 document、
        # 再写 version；发布事务完成前数据库不会看到半成品指针。
        ForeignKeyConstraint(
            ["tenant_id", "id", "current_version_id"],
            [
                "document_versions.tenant_id",
                "document_versions.document_id",
                "document_versions.id",
            ],
            name="fk_documents_current_version_lineage",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint("tenant_id", "source_id", name="uq_documents_tenant_source"),
        CheckConstraint("length(trim(source_id)) > 0", name="ck_documents_source_not_blank"),
        CheckConstraint("length(trim(title)) > 0", name="ck_documents_title_not_blank"),
        CheckConstraint(
            "status != 'ready' OR current_version_id IS NOT NULL",
            name="ck_documents_ready_requires_current_version",
        ),
    )


class DocumentVersionModel(Base):
    """不可变内容版本及其发布生命周期。"""

    __tablename__ = "document_versions"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    source_revision: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[VersionStatus] = mapped_column(
        _enum_type(VersionStatus, "version_status"),
        nullable=False,
        default=VersionStatus.STAGED,
        server_default=VersionStatus.STAGED.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    document: Mapped[DocumentModel] = relationship(
        back_populates="versions", foreign_keys=[tenant_id, document_id]
    )
    artifacts: Mapped[list[VersionArtifactModel]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="CASCADE",
            name="fk_versions_document_tenant",
        ),
        UniqueConstraint(
            "tenant_id", "document_id", "content_hash", name="uq_versions_document_hash"
        ),
        CheckConstraint(
            "published_at IS NULL OR status IN ('published', 'superseded', 'revoked')",
            name="ck_versions_published_at_status",
        ),
        CheckConstraint(
            "status != 'published' OR published_at IS NOT NULL",
            name="ck_versions_published_requires_timestamp",
        ),
    )


Index(
    "uq_versions_one_published",
    DocumentVersionModel.tenant_id,
    DocumentVersionModel.document_id,
    unique=True,
    postgresql_where=DocumentVersionModel.status == VersionStatus.PUBLISHED,
    sqlite_where=DocumentVersionModel.status == VersionStatus.PUBLISHED,
)


class VersionArtifactModel(Base):
    """发布前置产物状态；只记录外部产物的事实，不参与跨系统事务。"""

    __tablename__ = "version_artifacts"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[ArtifactKind] = mapped_column(
        _enum_type(ArtifactKind, "artifact_kind"), primary_key=True
    )
    status: Mapped[ArtifactStatus] = mapped_column(
        _enum_type(ArtifactStatus, "artifact_status"),
        nullable=False,
        default=ArtifactStatus.PENDING,
        server_default=ArtifactStatus.PENDING.value,
    )
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
        server_default=func.now(),
    )
    version: Mapped[DocumentVersionModel] = relationship(
        back_populates="artifacts", foreign_keys=[tenant_id, document_id, version_id]
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id", "version_id"],
            [
                "document_versions.tenant_id",
                "document_versions.document_id",
                "document_versions.id",
            ],
            ondelete="CASCADE",
            name="fk_artifacts_version_tenant",
        ),
        CheckConstraint("item_count >= 0", name="ck_artifacts_item_count_nonnegative"),
        CheckConstraint(
            "status != 'failed' OR length(trim(failure_reason)) > 0",
            name="ck_artifacts_failed_requires_reason",
        ),
    )


class EmbeddingRecordModel(Base):
    """首期 PostgreSQL-only Dense 向量记录；embedding 使用 JSON 保存以兼容无 pgvector 环境。"""

    __tablename__ = "embedding_records"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    parent_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    heading_path: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_locators: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(JSON, nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id", "version_id"],
            ["document_versions.tenant_id", "document_versions.document_id", "document_versions.id"],
            ondelete="CASCADE",
            name="fk_embeddings_version_tenant",
        ),
        CheckConstraint("dimension > 0", name="ck_embeddings_dimension_positive"),
    )


class TenantDenseCollectionModel(Base):
    """租户到 Weaviate Dense collection 的稳定注册表。"""

    __tablename__ = "tenant_dense_collections"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    collection_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "collection_name", name="uq_tenant_dense_collections_scope"
        ),
        CheckConstraint(
            "length(trim(collection_name)) > 0",
            name="ck_tenant_dense_collections_name_not_blank",
        ),
        CheckConstraint(
            "length(trim(schema_version)) > 0",
            name="ck_tenant_dense_collections_schema_not_blank",
        ),
    )


class DenseGenerationModel(Base):
    """Weaviate Dense generation 的构建、完整性和发布事实。"""

    __tablename__ = "dense_generations"

    generation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    document_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    index_profile_key: Mapped[str] = mapped_column(String(512), nullable=False)
    collection_name: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    chunking_version: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    expected_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actual_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checkpoint: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    build_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    worker_lease: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[DenseGenerationStatus] = mapped_column(
        _enum_type(DenseGenerationStatus, "dense_generation_status"),
        nullable=False,
        default=DenseGenerationStatus.BUILDING,
        server_default=DenseGenerationStatus.BUILDING.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id", "version_id"],
            [
                "document_versions.tenant_id",
                "document_versions.document_id",
                "document_versions.id",
            ],
            ondelete="CASCADE",
            name="fk_dense_generations_version_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "collection_name"],
            ["tenant_dense_collections.tenant_id", "tenant_dense_collections.collection_name"],
            ondelete="RESTRICT",
            name="fk_dense_generations_collection_tenant",
        ),
        UniqueConstraint(
            "tenant_id",
            "document_id",
            "version_id",
            "index_profile_key",
            "generation_id",
            name="uq_dense_generations_scope_id",
        ),
        CheckConstraint("dimension > 0", name="ck_dense_generations_dimension_positive"),
        CheckConstraint(
            "expected_item_count >= 0", name="ck_dense_generations_expected_nonnegative"
        ),
        CheckConstraint(
            "actual_item_count >= 0", name="ck_dense_generations_actual_nonnegative"
        ),
        CheckConstraint("checkpoint >= 0", name="ck_dense_generations_checkpoint_nonnegative"),
        CheckConstraint("build_attempt > 0", name="ck_dense_generations_attempt_positive"),
        CheckConstraint(
            "status != 'failed' OR length(trim(failure_reason)) > 0",
            name="ck_dense_generations_failed_requires_reason",
        ),
    )


class DenseGenerationPointerModel(Base):
    """按 generation scope 保存 active/previous 发布指针。"""

    __tablename__ = "dense_generation_pointers"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    index_profile_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    active_generation_id: Mapped[str | None] = mapped_column(
        ForeignKey("dense_generations.generation_id", ondelete="RESTRICT"), nullable=True
    )
    previous_generation_id: Mapped[str | None] = mapped_column(
        ForeignKey("dense_generations.generation_id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id", "version_id"],
            [
                "document_versions.tenant_id",
                "document_versions.document_id",
                "document_versions.id",
            ],
            ondelete="CASCADE",
            name="fk_dense_generation_pointers_version_tenant",
        ),
        CheckConstraint(
            "active_generation_id IS NULL OR active_generation_id != previous_generation_id",
            name="ck_dense_generation_pointers_distinct_generations",
        ),
    )


class DocumentACLModel(Base):
    """文档级 allow ACL；首期只支持 READ，缺失授权由上层 fail closed。"""

    __tablename__ = "document_acl"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    principal_type: Mapped[ACLPrincipalType] = mapped_column(
        _enum_type(ACLPrincipalType, "acl_principal_type"), primary_key=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    permission: Mapped[ACLPermission] = mapped_column(
        _enum_type(ACLPermission, "acl_permission"), primary_key=True
    )
    policy_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now()
    )
    document: Mapped[DocumentModel] = relationship(
        back_populates="acl_entries", foreign_keys=[tenant_id, document_id]
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="CASCADE",
            name="fk_acl_document_tenant",
        ),
        CheckConstraint("length(trim(principal_id)) > 0", name="ck_acl_principal_not_blank"),
        CheckConstraint("policy_version > 0", name="ck_acl_policy_version_positive"),
    )


class IngestionJobModel(Base):
    """租户隔离的摄取任务事实；幂等键只在同一租户内唯一。"""

    __tablename__ = "ingestion_jobs"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        _enum_type(JobStatus, "job_status"),
        nullable=False,
        default=JobStatus.PENDING,
        server_default=JobStatus.PENDING.value,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
        server_default=func.now(),
    )
    document: Mapped[DocumentModel] = relationship(
        back_populates="ingestion_jobs", foreign_keys=[tenant_id, document_id]
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.id"],
            ondelete="CASCADE",
            name="fk_jobs_document_tenant",
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_jobs_tenant_idempotency"),
        CheckConstraint("attempt >= 0", name="ck_jobs_attempt_nonnegative"),
        CheckConstraint("length(trim(idempotency_key)) > 0", name="ck_jobs_idempotency_not_blank"),
    )


class SchemaMigrationModel(Base):
    """记录已成功应用的仓库内 migration contract 版本。"""

    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(128), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, server_default=func.now()
    )


__all__ = [
    "ACLPermission",
    "ACLPrincipalType",
    "ArtifactKind",
    "ArtifactStatus",
    "Base",
    "DocumentACLModel",
    "DocumentModel",
    "DocumentStatus",
    "DocumentVersionModel",
    "IngestionJobModel",
    "JobStatus",
    "SchemaMigrationModel",
    "TenantModel",
    "VersionArtifactModel",
    "EmbeddingRecordModel",
    "DenseGenerationModel",
    "DenseGenerationPointerModel",
    "TenantDenseCollectionModel",
    "VersionStatus",
]
