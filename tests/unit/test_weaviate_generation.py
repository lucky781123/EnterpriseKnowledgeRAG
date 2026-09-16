"""Weaviate Dense generation 领域契约测试。"""

import pytest

from enterprise_rag.db.models import (
    DenseGenerationModel,
    DenseGenerationPointerModel,
    TenantDenseCollectionModel,
)
from enterprise_rag.db.state import InvalidStateTransition, transition_generation_status
from enterprise_rag.domain.entities import DenseGenerationStatus
from enterprise_rag.retrieval.contracts import (
    GenerationScope,
    IndexProfile,
    collection_name_for_tenant,
)


def test_index_profile_key_and_collection_name_are_stable() -> None:
    """相同索引配置和租户必须产生稳定、可复用的标识。"""

    profile = IndexProfile(
        model_name="demo-embedding",
        dimension=2,
        chunking_version="parent-child-v1",
        schema_version="dense-v1",
    )
    scope = GenerationScope("tenant/a", "doc-1", "version-1", profile.key)

    assert profile.key == "demo-embedding:2:parent-child-v1:dense-v1"
    assert collection_name_for_tenant(scope.tenant_id) == collection_name_for_tenant("tenant/a")
    assert collection_name_for_tenant(scope.tenant_id).startswith("DenseTenant_")


def test_generation_scope_is_value_object_and_profile_rejects_invalid_dimension() -> None:
    """scope 应可比较，非法 embedding dimension 应在入口处拒绝。"""

    scope = GenerationScope("tenant-a", "doc-1", "version-1", "profile-1")
    assert scope == GenerationScope("tenant-a", "doc-1", "version-1", "profile-1")

    with pytest.raises(ValueError, match="dimension"):
        IndexProfile("demo-embedding", 0, "chunk-v1", "schema-v1")


def test_generation_status_requires_integrity_before_activation() -> None:
    """BUILDING 不能跳过 READY 直接进入 ACTIVE，RETIRED 可显式回滚为 ACTIVE。"""

    assert (
        transition_generation_status(DenseGenerationStatus.BUILDING, DenseGenerationStatus.READY)
        is DenseGenerationStatus.READY
    )
    with pytest.raises(InvalidStateTransition, match="building -> active"):
        transition_generation_status(DenseGenerationStatus.BUILDING, DenseGenerationStatus.ACTIVE)
    assert (
        transition_generation_status(DenseGenerationStatus.RETIRED, DenseGenerationStatus.ACTIVE)
        is DenseGenerationStatus.ACTIVE
    )


def test_generation_models_expose_scope_and_fencing_columns() -> None:
    """三张事实表必须包含 scope、collection、checkpoint 和 active pointer 字段。"""

    collection_columns = set(TenantDenseCollectionModel.__table__.c.keys())
    generation_columns = set(DenseGenerationModel.__table__.c.keys())
    pointer_columns = set(DenseGenerationPointerModel.__table__.c.keys())

    assert {"tenant_id", "collection_name", "schema_version", "status"} <= collection_columns
    assert {
        "generation_id",
        "tenant_id",
        "document_id",
        "version_id",
        "index_profile_key",
        "checkpoint",
        "build_attempt",
        "worker_lease",
        "status",
    } <= generation_columns
    assert {
        "tenant_id",
        "document_id",
        "version_id",
        "index_profile_key",
        "active_generation_id",
        "previous_generation_id",
    } <= pointer_columns
