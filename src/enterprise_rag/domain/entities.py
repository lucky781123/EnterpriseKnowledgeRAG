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
