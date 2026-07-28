import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.models.blobs import Blob
from apps.api.models.documents import (
    Document,
    DocumentSourceType,
    DocumentVersion,
)
from apps.api.models.processing import ProcessingJob
from apps.api.parsers import get_parser_for_content
from apps.api.storage.local import LocalBlobStorage
from apps.worker.core.config import settings

logger = structlog.get_logger()

MAX_RETRIES = 3
BASE_RETRY_DELAY_MINUTES = 5
BATCH_SIZE = 10


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def calculate_next_retry_at(
    retry_count: int,
    *,
    now: datetime.datetime | None = None,
) -> datetime.datetime:
    """Return an aware UTC timestamp using 5, 10, 20... minute backoff."""
    base = now or utc_now()
    delay_minutes = BASE_RETRY_DELAY_MINUTES * (2 ** max(retry_count, 0))
    return base + datetime.timedelta(minutes=delay_minutes)


def _is_retry_due(
    next_retry_at: datetime.datetime | None,
    now: datetime.datetime,
) -> bool:
    if next_retry_at is None:
        return True
    if next_retry_at.tzinfo is None:
        next_retry_at = next_retry_at.replace(tzinfo=datetime.timezone.utc)
    return next_retry_at <= now


def _mark_terminal_failure(
    session: Session,
    job: ProcessingJob,
    version: DocumentVersion | None,
    message: str,
    *,
    details: dict | None = None,
) -> bool:
    job.status = "failed"
    job.finished_at = utc_now()
    job.next_retry_at = None
    job.last_error = message
    job.error_details = details
    if version is not None:
        version.processing_status = "failed"
        session.add(version)
    session.add(job)
    session.commit()
    return False


def _mark_parse_failure(
    session: Session,
    job: ProcessingJob,
    version: DocumentVersion,
    message: str,
    details: dict | None,
) -> bool:
    job.retry_count += 1
    job.finished_at = utc_now()
    job.last_error = message
    job.error_details = details

    if job.retry_count >= (job.max_retries or MAX_RETRIES):
        job.status = "failed"
        job.next_retry_at = None
        version.processing_status = "failed"
    else:
        job.status = "retry"
        # retry_count=1 means the first retry, which is five minutes later.
        job.next_retry_at = calculate_next_retry_at(job.retry_count - 1)
        version.processing_status = "retry"

    session.add_all([job, version])
    session.commit()
    return False


def process_single_job(session: Session, job_id: int) -> bool:
    """Parse one claimed or pending job and persist its final state."""
    job = session.get(ProcessingJob, job_id)
    if job is None:
        logger.warning("job_not_found", job_id=job_id)
        return False

    if job.status in ("created", "retry"):
        now = utc_now()
        if not _is_retry_due(job.next_retry_at, now):
            return False
        job.status = "processing"
        job.started_at = now
        job.finished_at = None
        session.add(job)
        session.commit()
    elif job.status != "processing":
        return False

    version = session.get(DocumentVersion, job.document_version_id)
    if version is None:
        return _mark_terminal_failure(
            session,
            job,
            None,
            f"文档版本 {job.document_version_id} 不存在",
        )

    document = session.get(Document, job.document_id)
    if document is None:
        return _mark_terminal_failure(
            session,
            job,
            version,
            f"文档 {job.document_id} 不存在",
        )

    version.processing_status = "processing"
    session.add(version)
    session.commit()

    blob: Blob | None = None
    if document.source_type == DocumentSourceType.note:
        if version.raw_content is None:
            return _mark_terminal_failure(
                session,
                job,
                version,
                "笔记内容为空",
            )
        content: bytes | str = version.raw_content
        content_type = "application/x-note"
    else:
        if version.blob_id is None:
            return _mark_terminal_failure(
                session,
                job,
                version,
                "资料没有关联原文件",
            )
        blob = session.get(Blob, version.blob_id)
        if blob is None:
            return _mark_terminal_failure(
                session,
                job,
                version,
                f"原文件记录 {version.blob_id} 不存在",
            )
        try:
            storage = LocalBlobStorage(settings.storage_path)
            with storage.open(blob.storage_key) as source:
                content = source.read()
        except (FileNotFoundError, OSError) as exc:
            return _mark_terminal_failure(
                session,
                job,
                version,
                "原文件内容不存在或无法读取",
                details={"exception": str(exc)},
            )
        content_type = blob.content_type

    try:
        parser = get_parser_for_content(
            content_type,
            blob.original_filename if blob else None,
        )
        result = parser.parse(content, content_type)
    except Exception as exc:
        logger.exception("parser_crashed", job_id=job_id)
        return _mark_parse_failure(
            session,
            job,
            version,
            "解析器发生异常",
            {"exception": str(exc)},
        )

    if not result.success or result.structured_content is None:
        return _mark_parse_failure(
            session,
            job,
            version,
            result.error_message or "解析失败",
            result.error_details,
        )

    version.structured_content = result.structured_content.to_dict()
    version.raw_content = result.structured_content.full_text()
    version.processing_status = "ready"
    job.status = "completed"
    job.finished_at = utc_now()
    job.next_retry_at = None
    job.last_error = None
    job.error_details = None
    session.add_all([version, job])
    session.commit()
    logger.info(
        "job_completed",
        job_id=job_id,
        document_id=job.document_id,
    )
    return True


def claim_pending_jobs(session: Session, limit: int = BATCH_SIZE) -> list[int]:
    """Atomically claim jobs so another worker cannot process the same rows."""
    now = utc_now()
    jobs = list(
        session.scalars(
            select(ProcessingJob)
            .where(
                ProcessingJob.status.in_(("created", "retry")),
                (
                    ProcessingJob.next_retry_at.is_(None)
                    | (ProcessingJob.next_retry_at <= now)
                ),
            )
            .order_by(ProcessingJob.created_at, ProcessingJob.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
        ).all()
    )
    for job in jobs:
        job.status = "processing"
        job.started_at = now
        job.finished_at = None
        session.add(job)
    session.commit()
    return [job.id for job in jobs]


def process_pending_jobs(session: Session) -> int:
    """Claim a bounded batch and return the number successfully parsed."""
    job_ids = claim_pending_jobs(session)
    completed = 0
    for job_id in job_ids:
        try:
            completed += int(process_single_job(session, job_id))
        except Exception as exc:
            logger.exception("unexpected_job_error", job_id=job_id)
            session.rollback()
            job = session.get(ProcessingJob, job_id)
            version = (
                session.get(DocumentVersion, job.document_version_id)
                if job is not None
                else None
            )
            if job is not None and version is not None:
                _mark_parse_failure(
                    session,
                    job,
                    version,
                    "处理任务发生异常",
                    {"exception": str(exc)},
                )
            elif job is not None:
                _mark_terminal_failure(
                    session,
                    job,
                    None,
                    "处理任务关联的文档版本不存在",
                    details={"exception": str(exc)},
                )
    return completed
