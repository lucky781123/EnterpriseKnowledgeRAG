"""核心领域实体的轻量初稿，后续映射到 PostgreSQL。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class DocumentStatus(StrEnum):
    DRAFT = "draft"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    ARCHIVED = "archived"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VersionStatus(StrEnum):
    """文档版本从暂存、处理、就绪到发布/撤销的生命周期状态。"""

    STAGED = "staged"
    PROCESSING = "processing"
    READY = "ready"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    REVOKED = "revoked"


class ArtifactKind(StrEnum):
    """发布前置产物的稳定类别；每个版本都按类别单独记录状态。"""

    RAW = "raw"
    PARSED = "parsed"
    EMBEDDING = "embedding"
    SPARSE = "sparse"


class ArtifactStatus(StrEnum):
    """单个发布产物的待处理、就绪或失败状态。"""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class DenseGenerationStatus(StrEnum):
    """Weaviate Dense generation 的构建、发布和保留生命周期状态。"""

    BUILDING = "building"
    READY = "ready"
    ACTIVE = "active"
    RETIRED = "retired"
    FAILED = "failed"


class ACLPrincipalType(StrEnum):
    """文档 ACL 主体类型；主体 ID 由认证与成员关系层解释。"""

    USER = "user"
    GROUP = "group"
    ROLE = "role"


class ACLPermission(StrEnum):
    """首期文档 ACL 只授予读取权限，缺失权限时由上层拒绝访问。"""

    READ = "read"


@dataclass(frozen=True, slots=True)
class Tenant:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Document:
    id: str
    tenant_id: str
    source_id: str
    title: str
    status: DocumentStatus
    current_version_id: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    id: str
    document_id: str
    content_hash: str
    source_revision: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IngestionJob:
    id: str
    tenant_id: str
    document_id: str
    idempotency_key: str
    status: JobStatus
    attempt: int = 0
