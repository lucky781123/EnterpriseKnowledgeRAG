from datetime import datetime, timezone

from enterprise_rag.domain.entities import Document, DocumentStatus, IngestionJob, JobStatus


def test_document_and_job_have_explicit_lifecycle_states():
    document = Document("doc-1", "tenant-a", "git:repo/path", "标题", DocumentStatus.PROCESSING)
    job = IngestionJob("job-1", "tenant-a", document.id, "hash-1", JobStatus.PENDING)
    assert document.status is DocumentStatus.PROCESSING
    assert job.status is JobStatus.PENDING
    assert isinstance(datetime.now(timezone.utc), datetime)
