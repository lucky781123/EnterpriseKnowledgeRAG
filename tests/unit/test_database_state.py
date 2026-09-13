"""持久化状态机的允许与拒绝路径。"""

import pytest

from enterprise_rag.db.state import (
    InvalidStateTransition,
    transition_job_status,
    transition_version_status,
)
from enterprise_rag.domain.entities import JobStatus, VersionStatus


def test_job_retry_and_terminal_state_semantics_are_explicit():
    """失败任务可以显式重试，成功任务不能回到运行态。"""

    assert transition_job_status(JobStatus.FAILED, JobStatus.PENDING) is JobStatus.PENDING
    with pytest.raises(InvalidStateTransition, match="succeeded -> running"):
        transition_job_status(JobStatus.SUCCEEDED, JobStatus.RUNNING)


def test_version_publish_cannot_skip_ready_state():
    """版本必须完成 processing -> ready，不能直接从 processing 发布。"""

    assert (
        transition_version_status(VersionStatus.PROCESSING, VersionStatus.READY)
        is VersionStatus.READY
    )
    with pytest.raises(InvalidStateTransition, match="processing -> published"):
        transition_version_status(VersionStatus.PROCESSING, VersionStatus.PUBLISHED)
