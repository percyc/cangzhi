from __future__ import annotations

import datetime
import hashlib
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from apps.api.ai import AIProviderError, UnderstandingResult, build_provider
from apps.api.constants import INBOX_CATEGORY_NAME, INBOX_CATEGORY_SLUG
from apps.api.extractors import extract_xinhua_html
from apps.api.models.blobs import Blob
from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import (
    Document,
    DocumentSourceType,
    DocumentVersion,
)
from apps.api.models.processing import ProcessingJob
from apps.api.models.taxonomy import (
    Category,
    DocumentCategory,
    DocumentSummary,
    DocumentTag,
    Tag,
)
from apps.api.parsers import get_parser_for_content
from apps.api.parsers.base import StructuredContent
from apps.api.security import (
    URLFetchError,
    URLSecurityError,
    fetch_url,
)
from apps.api.services import build_chunk_specs
from apps.api.storage.local import LocalBlobStorage
from apps.worker.core.config import settings

logger = structlog.get_logger()

MAX_RETRIES = 3
BASE_RETRY_DELAY_MINUTES = 5
BATCH_SIZE = 10

UNDERSTANDING_STAGE = "understanding"
UNDERSTANDING_IDEMPOTENCY = "understanding:v1"
CHUNKING_STAGE = "chunking"
CHUNKING_IDEMPOTENCY = "chunking:m3-v1"

PARSING_CONFIG_VERSION = "url-html-v1"
MIN_USEFUL_URL_TEXT_LENGTH = 20


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def calculate_next_retry_at(
    retry_count: int,
    *,
    now: datetime.datetime | None = None,
) -> datetime.datetime:
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


def _normalize_stage(stage: str) -> str:
    """Legacy ``stored`` jobs are treated as the modern ``parsing`` stage."""

    return "parsing" if stage == "stored" else stage


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
        job.next_retry_at = calculate_next_retry_at(job.retry_count - 1)
        version.processing_status = "retry"

    session.add_all([job, version])
    session.commit()
    return False


def _load_content_for_parsing(
    session: Session,
    document: Document,
    version: DocumentVersion,
) -> tuple[bytes | str, str, str | None] | None:
    """Return ``(content, content_type, filename)`` or ``None`` if missing."""

    if document.source_type == DocumentSourceType.note:
        if version.raw_content is None:
            return None
        return version.raw_content, "application/x-note", None
    if document.source_type == DocumentSourceType.url:
        # URLs are fetched fresh at parse time; the raw_content is the
        # page text. If we have a blob, prefer the blob bytes.
        if version.blob_id is not None:
            blob = session.get(Blob, version.blob_id)
            if blob is not None:
                return _read_blob_bytes(blob), blob.content_type, blob.original_filename
        return None
    if version.blob_id is None:
        return None
    blob = session.get(Blob, version.blob_id)
    if blob is None:
        return None
    return _read_blob_bytes(blob), blob.content_type, blob.original_filename


def _read_blob_bytes(blob: Blob) -> bytes:
    storage = LocalBlobStorage(settings.storage_path)
    with storage.open(blob.storage_key) as source:
        return source.read()


def _fetch_and_store_url(
    session: Session,
    document: Document,
    version: DocumentVersion,
) -> tuple[bytes, str]:
    assert document.source_url, "URL document must have a source_url"
    try:
        page = fetch_url(
            document.source_url,
            max_bytes=settings.url_fetch_max_bytes,
            timeout_seconds=settings.url_fetch_timeout_seconds,
            max_redirects=settings.url_fetch_max_redirects,
        )
    except URLSecurityError as exc:
        raise _FatalFetchError(str(exc)) from None
    except URLFetchError as exc:
        raise _FetchAttemptError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        raise _FetchAttemptError(str(exc)) from None

    storage = LocalBlobStorage(settings.storage_path)
    from io import BytesIO

    stored = storage.save(BytesIO(page.raw_bytes), max_bytes=settings.url_fetch_max_bytes)
    blob = (
        session.query(Blob).filter(Blob.sha256 == stored.sha256).one_or_none()
    )
    if blob is None:
        blob = Blob(
            sha256=stored.sha256,
            storage_key=stored.storage_key,
            content_type=page.content_type or "text/html",
            file_size=stored.size,
            original_filename=page.final_url,
        )
        session.add(blob)
        session.flush()
    else:
        # Storage was just written but unreferenced; clean it up.
        storage.delete(stored.storage_key)

    version.blob_id = blob.id
    version.content_hash = stored.sha256
    session.add(version)
    return page.raw_bytes, page.content_type or "text/html"


class _FetchAttemptError(RuntimeError):
    """Wraps a transient fetch failure so the caller can decide to retry."""


class _FatalFetchError(RuntimeError):
    """Wraps a permanent fetch failure (e.g. SSRF violation)."""


def _enqueue_understanding_job(
    session: Session,
    *,
    document_id: int,
    version_id: int,
) -> ProcessingJob:
    idempotency_key = f"{version_id}:{UNDERSTANDING_STAGE}:{UNDERSTANDING_IDEMPOTENCY}"
    existing = session.scalar(
        select(ProcessingJob).where(ProcessingJob.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if existing.status in ("completed", "failed"):
            return existing
        existing.status = "created"
        existing.retry_count = 0
        existing.next_retry_at = None
        existing.last_error = None
        existing.error_details = None
        existing.started_at = None
        existing.finished_at = None
        session.add(existing)
        return existing

    job = ProcessingJob(
        document_id=document_id,
        document_version_id=version_id,
        stage=UNDERSTANDING_STAGE,
        status="created",
        idempotency_key=idempotency_key,
        config_version=UNDERSTANDING_IDEMPOTENCY,
    )
    session.add(job)
    return job


def _enqueue_chunking_job(
    session: Session,
    *,
    document_id: int,
    version_id: int,
) -> ProcessingJob:
    idempotency_key = f"{version_id}:{CHUNKING_STAGE}:{CHUNKING_IDEMPOTENCY}"
    existing = session.scalar(
        select(ProcessingJob).where(ProcessingJob.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if existing.status in ("completed", "failed"):
            return existing
        existing.status = "created"
        existing.retry_count = 0
        existing.next_retry_at = None
        existing.last_error = None
        existing.error_details = None
        existing.started_at = None
        existing.finished_at = None
        session.add(existing)
        return existing

    job = ProcessingJob(
        document_id=document_id,
        document_version_id=version_id,
        stage=CHUNKING_STAGE,
        status="created",
        idempotency_key=idempotency_key,
        config_version=CHUNKING_IDEMPOTENCY,
    )
    session.add(job)
    return job


def _process_understanding(
    session: Session,
    job: ProcessingJob,
    document: Document,
    version: DocumentVersion,
) -> bool:
    """Generate summary/category/tags for a parsed version. Idempotent."""

    structured = version.structured_content or {}
    raw_text = (version.raw_content or "").strip()

    # If understanding already exists for this version, do not redo it.
    existing_summary = session.scalar(
        select(DocumentSummary).where(
            DocumentSummary.document_version_id == version.id
        )
    )
    existing_categories = list(
        session.scalars(
            select(DocumentCategory).where(
                DocumentCategory.document_version_id == version.id
            )
        ).all()
    )
    existing_tags = list(
        session.scalars(
            select(DocumentTag).where(
                DocumentTag.document_version_id == version.id
            )
        ).all()
    )
    if existing_summary is not None or existing_categories or existing_tags:
        return _complete_understanding_job(session, job, document, version)

    metadata = (structured.get("metadata") or {}) if isinstance(structured, dict) else {}
    title = document.title or (metadata.get("title") if isinstance(metadata, dict) else None) or ""
    content_for_ai = _build_ai_input(title, raw_text, metadata)

    provider = build_provider()
    if provider is None or not provider.is_configured():
        # No provider → mark the document as 'inbox' but keep it usable.
        _apply_inbox_fallback(
            session,
            version,
            document,
            reason="无可用模型，跳过 AI 整理",
        )
        return _complete_understanding_job(session, job, document, version)

    categories = list(
        session.scalars(
            select(Category).order_by(Category.sort_order, Category.id)
        ).all()
    )
    if not categories:
        _apply_inbox_fallback(
            session,
            version,
            document,
            reason="尚未配置分类，跳过 AI 整理",
        )
        return _complete_understanding_job(session, job, document, version)

    allowed_slugs = [cat.slug for cat in categories]
    display = {cat.slug: cat.name for cat in categories}
    try:
        result = provider.generate_understanding(
            content=content_for_ai,
            title=title,
            category_slugs=allowed_slugs,
            category_display=display,
        )
    except Exception as exc:  # Provider outages must never hide parsed content.
        logger.warning(
            "ai_understanding_failed",
            document_id=document.id,
            error=str(exc),
        )
        _apply_inbox_fallback(
            session,
            version,
            document,
            reason=f"AI 整理失败：{exc}",
            details={"provider": provider.name, "error": str(exc)},
        )
        return _complete_understanding_job(session, job, document, version)

    _persist_understanding_result(session, version, document, result, categories)
    return _complete_understanding_job(session, job, document, version)


def _complete_understanding_job(
    session: Session,
    job: ProcessingJob,
    document: Document,
    version: DocumentVersion,
) -> bool:
    job.status = "completed"
    job.finished_at = utc_now()
    job.next_retry_at = None
    job.last_error = None
    job.error_details = None
    session.add(job)
    if version.processing_status == "ready":
        session.add(version)
    session.commit()
    return True


def _process_chunking(
    session: Session,
    job: ProcessingJob,
    document: Document,
    version: DocumentVersion,
) -> bool:
    """Materialize parent/child chunks for the document version.

    The function is idempotent: it always replaces the current set of
    chunks for the version so re-runs converge to the same result.
    The chunk content is derived from ``version.structured_content``
    and a SHA-256 seed so the external_id and content_hash are
    stable across runs.
    """

    structured_payload = version.structured_content or {}
    if not structured_payload:
        job.status = "failed"
        job.finished_at = utc_now()
        job.next_retry_at = None
        job.last_error = "缺少结构化内容，无法生成切片"
        job.error_details = {"reason": "missing_structured_content"}
        version.processing_status = "failed"
        session.add_all([job, version])
        session.commit()
        return False

    try:
        structured = _structured_content_from_payload(structured_payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "chunking_invalid_payload",
            document_id=document.id,
            version_id=version.id,
        )
        job.status = "failed"
        job.finished_at = utc_now()
        job.next_retry_at = None
        job.last_error = f"无法读取结构化内容：{exc}"
        job.error_details = {"exception": str(exc)}
        version.processing_status = "failed"
        session.add_all([job, version])
        session.commit()
        return False

    seed_input = "|".join(
        [
            str(version.id),
            version.content_hash or "",
            structured.full_text()[:1000],
        ]
    )
    content_hash_seed = hashlib.sha256(seed_input.encode("utf-8")).hexdigest()

    try:
        specs = build_chunk_specs(
            structured,
            content_hash_seed=content_hash_seed,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "chunking_failed",
            document_id=document.id,
            version_id=version.id,
        )
        job.retry_count += 1
        job.finished_at = utc_now()
        job.last_error = f"切片失败：{exc}"
        job.error_details = {"exception": str(exc)}
        if job.retry_count >= (job.max_retries or MAX_RETRIES):
            job.status = "failed"
            job.next_retry_at = None
            version.processing_status = "failed"
        else:
            job.status = "retry"
            job.next_retry_at = calculate_next_retry_at(job.retry_count - 1)
            version.processing_status = "retry"
        session.add_all([job, version])
        session.commit()
        return False

    _replace_version_chunks(session, document, version, specs)

    job.status = "completed"
    job.finished_at = utc_now()
    job.next_retry_at = None
    job.last_error = None
    job.error_details = None
    session.add(job)
    if version.processing_status in ("ready", "chunking", "processing"):
        version.processing_status = "ready"
    version.meta = {
        **(version.meta or {}),
        "chunk_count": sum(1 for spec in specs if spec.role == "child"),
        "parent_count": sum(1 for spec in specs if spec.role == "parent"),
        "chunk_config_version": CHUNKING_IDEMPOTENCY,
    }
    session.add(version)
    session.commit()
    logger.info(
        "chunking_completed",
        document_id=document.id,
        version_id=version.id,
        chunks=len(specs),
    )
    return True


def _replace_version_chunks(
    session: Session,
    document: Document,
    version: DocumentVersion,
    specs,
) -> None:
    """Replace all chunks for a version, preserving stable ids.

    We delete the existing rows first because the chunker is
    deterministic but the content hash or structure can change as
    the parser improves. The unique constraint on
    ``(document_version_id, external_id)`` keeps accidental
    concurrent runs from creating duplicates.
    """

    existing_chunks = list(
        session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id
            )
        ).all()
    )
    session.execute(
        delete(DocumentChunk).where(
            DocumentChunk.document_version_id == version.id
        ),
        execution_options={"synchronize_session": False},
    )
    session.flush()
    for existing_chunk in existing_chunks:
        if existing_chunk in session:
            session.expunge(existing_chunk)

    parent_id_by_external: dict[str, int] = {}
    title = (document.title or "").strip()
    for spec in specs:
        search_text = _build_search_text(title, spec)
        chunk = DocumentChunk(
            document_id=document.id,
            document_version_id=version.id,
            parent_id=None,
            external_id=spec.external_id,
            role=spec.role,
            chunk_type=spec.chunk_type,
            order_index=spec.order_index,
            content=spec.content,
            search_text=search_text,
            content_hash=spec.content_hash,
            heading_path=list(spec.heading_path),
            page=spec.page,
            paragraph_index=spec.paragraph_index,
            source_start=spec.source_start,
            source_end=spec.source_end,
            char_count=spec.char_count,
            token_estimate=spec.token_estimate,
            language=spec.language,
            extra={},
            is_current=True,
        )
        session.add(chunk)
        session.flush()
        if spec.role == "parent":
            parent_id_by_external[spec.external_id] = chunk.id

    # Wire parent_id now that all rows have integer ids.
    for spec in specs:
        if spec.role != "child" or not spec.parent_external_id:
            continue
        parent_pk = parent_id_by_external.get(spec.parent_external_id)
        if parent_pk is None:
            continue
        # Locate the persisted child row and patch its parent_id.
        child_row = session.scalar(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id,
                DocumentChunk.external_id == spec.external_id,
            )
        )
        if child_row is None:
            continue
        child_row.parent_id = parent_pk
        session.add(child_row)


def _build_search_text(title: str, spec) -> str:
    heading = " > ".join(spec.heading_path or [])
    pieces: list[str] = []
    if title:
        pieces.append(title)
    if heading:
        pieces.append(heading)
    pieces.append(spec.content)
    return "\n".join(piece for piece in pieces if piece).strip()


def _structured_content_from_payload(payload: dict) -> StructuredContent:
    """Build a :class:`StructuredContent` from a stored dict payload.

    The chunker accepts a dict directly, but the dataclass form is
    convenient for the worker. We use it as a compatibility shim for
    older payloads that pre-date the typed parsers.
    """

    from apps.api.parsers.base import Block

    blocks_payload = payload.get("blocks") or []
    blocks = [
        Block(
            type=item.get("type") or "paragraph",
            text=item.get("text") or "",
            heading_path=list(item.get("heading_path") or []),
            page=item.get("page"),
            paragraph_index=item.get("paragraph_index"),
            level=item.get("level"),
        )
        for item in blocks_payload
        if isinstance(item, dict)
    ]
    metadata = payload.get("metadata") or {}
    return StructuredContent(
        document_type=payload.get("document_type") or "txt",
        blocks=blocks,
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
        schema_version=payload.get("schema_version", 1),
    )


def _build_ai_input(title: str, raw_text: str, metadata: dict) -> str:
    if not raw_text and not title:
        return ""
    if not raw_text:
        return f"标题：{title}"
    head = raw_text[:4000]
    return f"标题：{title}\n\n正文摘要：\n{head}"


def _apply_inbox_fallback(
    session: Session,
    version: DocumentVersion,
    document: Document,
    *,
    reason: str,
    details: dict | None = None,
) -> None:
    inbox = session.scalar(
        select(Category).where(Category.slug == INBOX_CATEGORY_SLUG)
    )
    if inbox is None:
        inbox = Category(
            slug=INBOX_CATEGORY_SLUG,
            name=INBOX_CATEGORY_NAME,
            sort_order=999,
            is_default=True,
        )
        session.add(inbox)
        session.flush()

    existing_primary = session.scalar(
        select(DocumentCategory).where(
            DocumentCategory.document_version_id == version.id,
            DocumentCategory.is_primary.is_(True),
        )
    )
    if existing_primary is None:
        session.add(
            DocumentCategory(
                document_id=document.id,
                document_version_id=version.id,
                category_id=inbox.id,
                is_primary=True,
                confidence=None,
                source="fallback",
                rationale=reason,
            )
        )
    else:
        existing_primary.category_id = inbox.id
        existing_primary.source = "fallback"
        existing_primary.rationale = reason
        existing_primary.is_primary = True
        session.add(existing_primary)

    meta = dict(version.meta or {})
    meta["ai_status"] = "failed" if details else "not_configured"
    meta["ai_reason"] = reason
    version.meta = meta
    session.add(version)


def _persist_understanding_result(
    session: Session,
    version: DocumentVersion,
    document: Document,
    result: UnderstandingResult,
    categories: list[Category],
) -> None:
    by_slug = {cat.slug: cat for cat in categories}
    primary = by_slug.get(result.category_slug)
    if primary is None:
        # Provider returned a slug not in our list — fall back to inbox.
        _apply_inbox_fallback(
            session,
            version,
            document,
            reason=f"模型返回了未知分类：{result.category_slug}",
            details={"returned_slug": result.category_slug},
        )
        return

    session.add(
        DocumentCategory(
            document_id=document.id,
            document_version_id=version.id,
            category_id=primary.id,
            is_primary=True,
            confidence=result.confidence,
            source="model",
            rationale=result.rationale or None,
        )
    )

    for tag_slug in result.tags:
        cleaned = tag_slug.strip()
        if not cleaned:
            continue
        normalized = cleaned.lower()
        tag = session.scalar(select(Tag).where(Tag.slug == normalized))
        if tag is None:
            tag = Tag(slug=normalized, name=cleaned[:255])
            session.add(tag)
            session.flush()
        session.add(
            DocumentTag(
                document_id=document.id,
                document_version_id=version.id,
                tag_id=tag.id,
                confidence=result.confidence,
                source="model",
            )
        )

    session.add(
        DocumentSummary(
            document_id=document.id,
            document_version_id=version.id,
            summary=result.summary,
            model=(settings.ai_provider or None),
            prompt_version=settings.ai_prompt_version,
            confidence=result.confidence,
            source="model",
            extra={"doc_type": result.doc_type, "rationale": result.rationale},
        )
    )
    meta = dict(version.meta or {})
    meta["ai_status"] = "completed"
    meta.pop("ai_reason", None)
    version.meta = meta
    session.add(version)


def _clear_understanding_results(
    session: Session,
    version_id: int,
) -> None:
    """Remove derived semantics before an explicit same-version reprocess."""
    session.execute(
        delete(DocumentTag).where(DocumentTag.document_version_id == version_id)
    )
    session.execute(
        delete(DocumentCategory).where(
            DocumentCategory.document_version_id == version_id
        )
    )
    session.execute(
        delete(DocumentSummary).where(
            DocumentSummary.document_version_id == version_id
        )
    )
    _reset_understanding_job(session, version_id)
    _reset_chunking_job(session, version_id)


def _reset_understanding_job(session: Session, version_id: int) -> None:
    understanding_job = session.scalar(
        select(ProcessingJob).where(
            ProcessingJob.idempotency_key
            == f"{version_id}:{UNDERSTANDING_STAGE}:{UNDERSTANDING_IDEMPOTENCY}"
        )
    )
    if understanding_job is not None:
        _reset_processing_job(understanding_job)
        session.add(understanding_job)


def _reset_chunking_job(session: Session, version_id: int) -> None:
    chunking_job = session.scalar(
        select(ProcessingJob).where(
            ProcessingJob.idempotency_key
            == f"{version_id}:{CHUNKING_STAGE}:{CHUNKING_IDEMPOTENCY}"
        )
    )
    if chunking_job is not None:
        _reset_processing_job(chunking_job)
        session.add(chunking_job)


def _reset_processing_job(job: ProcessingJob) -> None:
    job.status = "created"
    job.retry_count = 0
    job.next_retry_at = None
    job.last_error = None
    job.error_details = None
    job.started_at = None
    job.finished_at = None


def process_single_job(session: Session, job_id: int) -> bool:
    """Process a single claimed or pending job.

    ``stored`` and ``parsing`` jobs are treated identically. After parsing
    succeeds, a follow-up ``understanding`` job is enqueued so the worker
    pipeline remains idempotent.
    """
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

    stage = _normalize_stage(job.stage)

    if stage == UNDERSTANDING_STAGE:
        return _process_understanding(session, job, document, version)

    if stage == CHUNKING_STAGE:
        return _process_chunking(session, job, document, version)

    return _process_parsing(session, job, document, version)


def _process_parsing(
    session: Session,
    job: ProcessingJob,
    document: Document,
    version: DocumentVersion,
) -> bool:
    version.processing_status = "processing"
    session.add(version)
    session.commit()

    try:
        if document.source_type == DocumentSourceType.url:
            try:
                content_bytes, content_type = _fetch_and_store_url(
                    session, document, version
                )
            except _FatalFetchError as exc:
                return _mark_terminal_failure(
                    session, job, version, f"无法抓取网页：{exc}",
                    details={"reason": "ssrf_or_fatal"},
                )
            except _FetchAttemptError as exc:
                return _mark_parse_failure(
                    session, job, version, f"抓取失败：{exc}",
                    {"exception": str(exc)},
                )
            filename = document.source_url
        else:
            loaded = _load_content_for_parsing(session, document, version)
            if loaded is None:
                return _mark_terminal_failure(
                    session,
                    job,
                    version,
                    "资料没有可解析的内容",
                )
            content_bytes, content_type, filename = loaded

        try:
            parser = get_parser_for_content(
                content_type,
                filename,
            )
            result = parser.parse(content_bytes, content_type)
        except Exception as exc:
            logger.exception("parser_crashed", job_id=job.id)
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

        structured_content = result.structured_content
        if (
            document.source_type == DocumentSourceType.url
            and len(structured_content.full_text().strip())
            < MIN_USEFUL_URL_TEXT_LENGTH
        ):
            try:
                adapted_html = extract_xinhua_html(
                    document.source_url or "",
                    max_bytes=settings.url_fetch_max_bytes,
                    timeout_seconds=settings.url_fetch_timeout_seconds,
                )
                if adapted_html is not None:
                    adapted = get_parser_for_content(
                        "text/html", document.source_url
                    ).parse(adapted_html, "text/html")
                    if adapted.success and adapted.structured_content is not None:
                        structured_content = adapted.structured_content
            except (URLFetchError, URLSecurityError) as exc:
                logger.warning(
                    "url_adapter_failed",
                    document_id=document.id,
                    error=str(exc),
                )

        _apply_parse_result(session, document, version, structured_content)

        job.status = "completed"
        job.finished_at = utc_now()
        job.next_retry_at = None
        job.last_error = None
        job.error_details = None
        session.add(job)
        if job.config_version == "2":
            _clear_understanding_results(session, version.id)
        has_useful_text = bool(
            (version.raw_content or "").strip()
            and len((version.raw_content or "").strip())
            >= MIN_USEFUL_URL_TEXT_LENGTH
        )
        if document.source_type != DocumentSourceType.url or has_useful_text:
            _enqueue_chunking_job(
                session,
                document_id=document.id,
                version_id=version.id,
            )
            _enqueue_understanding_job(
                session,
                document_id=document.id,
                version_id=version.id,
            )
        else:
            version.processing_status = "unsupported"
            version.meta = {
                **(version.meta or {}),
                "extraction_status": "dynamic_page",
                "extraction_message": (
                    "该网页需要执行脚本加载正文，目前还没有适配此网站。"
                ),
            }
            session.add(version)
        session.commit()
        logger.info(
            "parse_completed",
            job_id=job.id,
            document_id=document.id,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.exception("parse_unexpected_error", job_id=job.id)
        session.rollback()
        return _mark_parse_failure(
            session, job, version, "处理任务发生异常", {"exception": str(exc)}
        )


def _apply_parse_result(
    session: Session,
    document: Document,
    version: DocumentVersion,
    structured: StructuredContent,
) -> None:
    payload = structured.to_dict()
    version.structured_content = payload
    full_text = structured.full_text()
    version.raw_content = full_text

    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if isinstance(metadata, dict):
        title = metadata.get("title")
        if isinstance(title, str) and title.strip():
            document.title = title.strip()[:1024]
        if "description" in metadata and isinstance(metadata.get("description"), str):
            document.description = (metadata.get("description") or "")[:2000]
        if "author" in metadata and isinstance(metadata.get("author"), str):
            doc_meta = dict(document.meta or {})
            doc_meta["author"] = metadata.get("author")
            document.meta = doc_meta
        if "published_at" in metadata and isinstance(metadata.get("published_at"), str):
            doc_meta = dict(document.meta or {})
            doc_meta["published_at"] = metadata.get("published_at")
            document.meta = doc_meta
        if "canonical_url" in metadata and isinstance(metadata.get("canonical_url"), str):
            doc_meta = dict(document.meta or {})
            doc_meta["canonical_url"] = metadata.get("canonical_url")
            document.meta = doc_meta
        if "raw_text" in metadata and isinstance(metadata.get("raw_text"), str):
            version.raw_content = metadata["raw_text"]

    version.processing_status = "ready"
    version.content_hash = version.content_hash or hashlib.sha256(
        (full_text or "").encode("utf-8")
    ).hexdigest()
    session.add(version)
    session.add(document)


def claim_pending_jobs(session: Session, limit: int = BATCH_SIZE) -> list[int]:
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


def run_understanding_for_version(
    session: Session,
    *,
    document_id: int,
    version_id: int,
) -> bool:
    """Public entrypoint for forcing an understanding pass (idempotent)."""

    job = _enqueue_understanding_job(
        session, document_id=document_id, version_id=version_id
    )
    session.commit()
    return process_single_job(session, job.id)
