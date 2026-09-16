"""持久化状态转换；执行器只负责保存，不自行扩大合法状态图。

状态图集中在此模块，确保 API、后台任务和重放消息共享同一语义。自转换用于幂等
重放；恢复边显式列出，终态不会因重复消息意外回到运行态。
"""

from __future__ import annotations

from enterprise_rag.domain.entities import DenseGenerationStatus, JobStatus, VersionStatus


class InvalidStateTransition(ValueError):
    """调用方请求了状态图未允许的转换。"""


_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset({JobStatus.PENDING, JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {JobStatus.RUNNING, JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.SUCCEEDED: frozenset({JobStatus.SUCCEEDED}),
    JobStatus.FAILED: frozenset({JobStatus.FAILED, JobStatus.PENDING}),
    JobStatus.CANCELLED: frozenset({JobStatus.CANCELLED, JobStatus.PENDING}),
}


def transition_job_status(current: JobStatus, target: JobStatus) -> JobStatus:
    """验证摄取任务状态转换，并返回可持久化的目标状态。"""

    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidStateTransition(f"非法任务状态转换: {current.value} -> {target.value}")
    return target


_ALLOWED_VERSION_TRANSITIONS: dict[VersionStatus, frozenset[VersionStatus]] = {
    VersionStatus.STAGED: frozenset({VersionStatus.STAGED, VersionStatus.PROCESSING, VersionStatus.FAILED}),
    VersionStatus.PROCESSING: frozenset({VersionStatus.PROCESSING, VersionStatus.READY, VersionStatus.FAILED}),
    VersionStatus.READY: frozenset({VersionStatus.READY, VersionStatus.PUBLISHED, VersionStatus.FAILED}),
    VersionStatus.PUBLISHED: frozenset({VersionStatus.PUBLISHED, VersionStatus.SUPERSEDED, VersionStatus.REVOKED}),
    VersionStatus.SUPERSEDED: frozenset({VersionStatus.SUPERSEDED}),
    VersionStatus.FAILED: frozenset({VersionStatus.FAILED, VersionStatus.STAGED}),
    VersionStatus.REVOKED: frozenset({VersionStatus.REVOKED}),
}


def transition_version_status(current: VersionStatus, target: VersionStatus) -> VersionStatus:
    """验证版本状态转换；发布必须经过 READY 前置状态。"""

    if target not in _ALLOWED_VERSION_TRANSITIONS[current]:
        raise InvalidStateTransition(f"非法版本状态转换: {current.value} -> {target.value}")
    return target


_ALLOWED_DENSE_GENERATION_TRANSITIONS: dict[
    DenseGenerationStatus, frozenset[DenseGenerationStatus]
] = {
    DenseGenerationStatus.BUILDING: frozenset(
        {DenseGenerationStatus.BUILDING, DenseGenerationStatus.READY, DenseGenerationStatus.FAILED}
    ),
    DenseGenerationStatus.READY: frozenset(
        {DenseGenerationStatus.READY, DenseGenerationStatus.ACTIVE, DenseGenerationStatus.FAILED}
    ),
    DenseGenerationStatus.ACTIVE: frozenset(
        {DenseGenerationStatus.ACTIVE, DenseGenerationStatus.RETIRED}
    ),
    DenseGenerationStatus.RETIRED: frozenset(
        {DenseGenerationStatus.RETIRED, DenseGenerationStatus.ACTIVE}
    ),
    DenseGenerationStatus.FAILED: frozenset({DenseGenerationStatus.FAILED}),
}


def transition_generation_status(
    current: DenseGenerationStatus, target: DenseGenerationStatus
) -> DenseGenerationStatus:
    """验证 Dense generation 生命周期，不允许跳过完整性检查直接发布。"""

    if target not in _ALLOWED_DENSE_GENERATION_TRANSITIONS[current]:
        raise InvalidStateTransition(f"非法 Dense generation 状态转换: {current.value} -> {target.value}")
    return target


__all__ = [
    "InvalidStateTransition",
    "transition_generation_status",
    "transition_job_status",
    "transition_version_status",
]
