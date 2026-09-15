from __future__ import annotations

import asyncio
import datetime
import hashlib
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Sequence

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from apps.api.ai import (
    AIProvider,
    UnderstandingResult,
    build_provider_from_session,
)
from apps.api.constants import INBOX_CATEGORY_NAME, INBOX_CATEGORY_SLUG
from apps.api.documents import (
    ChunkingConfig,
    DocumentProfile,
    detect_document_type,
    get_chunking_config,
)
from apps.api.embeddings.sampling import evenly_sample_chunks
from apps.api.extractors import extract_xinhua_html
from apps.api.models.auth import AIRuntimeConfig
from apps.api.models.blobs import Blob
from apps.api.models.chunks import DocumentChunk
from apps.api.models.datasets import DatasetField, KnowledgeDataset
from apps.api.models.documents import (
    Document,
    DocumentSourceType,
    DocumentVersion,
)
from apps.api.models.embedding_profiles import ChunkEmbedding, EmbeddingProfile
from apps.api.models.processing import ProcessingJob
from apps.api.models.table_rows import StructuredTableRow
from apps.api.models.taxonomy import (
    Category,
    DocumentCategory,
    DocumentSummary,
    DocumentTag,
    Tag,
)
from apps.api.models.webdav import WebDAVEntry, WebDAVSource
from apps.api.ocr import build_external_ocr_provider
from apps.api.parsers import get_parser_for_content
from apps.api.parsers.base import StructuredContent
from apps.api.parsers.pdf import PdfOcrOptions
from apps.api.parsers.text_sanitize import (
    UnicodeKeyCollisionError,
    UnicodeSanitizationReport,
    sanitize_unicode_payload,
    sanitize_unicode_text,
)
from apps.api.security import (
    URLFetchError,
    URLSecurityError,
    browser_profiles_for_url,
    fetch_url,
    looks_like_access_block,
)
from apps.api.security.secrets import decrypt_secret
from apps.api.services import build_chunk_specs
from apps.api.services.dataset_execution import (
    DatasetExecutionError,
    build_dataset_parquet,
)
from apps.api.services.structured_table import (
    _dataset_quality,
    _profile_dataset_fields,
    replace_version_table_rows,
)
from apps.api.services.webdav import WebDAVError, download_file
from apps.api.services.workspaces import bind_workspace_context, clear_workspace_context
from apps.api.storage.local import LocalBlobStorage
from apps.worker.core.config import pdf_ocr_options_summary, settings

logger = structlog.get_logger()

MAX_RETRIES = 3
BASE_RETRY_DELAY_MINUTES = 5
BATCH_SIZE = 10
LARGE_TABLE_EMBEDDING_THRESHOLD = 1_000
LARGE_TABLE_EMBEDDING_SAMPLE_SIZE = 256

UNDERSTANDING_STAGE = "understanding"
UNDERSTANDING_IDEMPOTENCY = "understanding:v1"
CHUNKING_STAGE = "chunking"
DATASET_CATALOG_STAGE = "dataset_catalog"
DATASET_ARTIFACT_STAGE = "dataset_artifact"
PREVIEW_STAGE = "preview"
PDF_PREVIEW_CONFIG = "pdf-v1"
PDF_PREVIEW_MAX_BYTES = 100 * 1024 * 1024
DATASET_ARTIFACT_VERSION = "dataset-parquet:v1"
CHUNKING_IDEMPOTENCY = "chunking:m3-v7-dataset-catalog"

# Each stage gets a turn; stored/parsing share two FIFO slots.
# Model backlogs cannot monopolize the queue, nor vice versa.
# Structural stages (especially OCR and preview) can still be slow.
STAGE_CYCLE: tuple[str, ...] = (
    "stored",
    "parsing",
    "chunking",
    DATASET_CATALOG_STAGE,
    DATASET_ARTIFACT_STAGE,
    PREVIEW_STAGE,
    UNDERSTANDING_STAGE,
    "embedding",
)

PARSING_CONFIG_VERSION = "url-html-v1"
MIN_USEFUL_URL_TEXT_LENGTH = 20
TERMINAL_PARSE_FAILURE_REASONS = frozenset({"spreadsheet_limit_exceeded"})

def _sanitize_structured_content(
    structured: StructuredContent,
) -> UnicodeSanitizationReport:
    """Normalize every derived string in a parser result before persistence.

    The text extraction path (PDF, docx, html, …) may emit
    malformed UTF-16 sequences — lone surrogates or well-formed
    surrogate pairs that were never collapsed to their scalar
    code point — and the PDF tesseract pipeline can occasionally
    drop a NUL byte. The chunker, the full-text index and the
    JSON column for ``structured_content`` all assume a valid
    Unicode string; without normalization a single rogue
    surrogate breaks :func:`json.dumps`, the search index and
    any downstream consumer that asks for ``str(block.text)``.

    We mutate the parser result in place so the rest of the
    pipeline (derivation of ``raw_content`` and ``title``,
    chunking, metadata) reads the same clean text. Original
    blob bytes and source hashes are untouched, so the source
    of truth stays the immutable blob.
    """

    aggregate = UnicodeSanitizationReport()
    for block in structured.blocks:
        if isinstance(block.text, str):
            new_text, report = sanitize_unicode_text(block.text)
            block.text = new_text
            aggregate = _merge_sanitization_reports(aggregate, report)
        if isinstance(block.heading_path, list):
            new_path, report = sanitize_unicode_payload(block.heading_path)
            block.heading_path = new_path
            aggregate = _merge_sanitization_reports(aggregate, report)
        if isinstance(block.extra, dict):
            new_extra, report = sanitize_unicode_payload(block.extra)
            block.extra = new_extra
            aggregate = _merge_sanitization_reports(aggregate, report)
    if isinstance(structured.metadata, dict):
        new_meta, report = sanitize_unicode_payload(structured.metadata)
        structured.metadata = new_meta
        aggregate = _merge_sanitization_reports(aggregate, report)
    return aggregate


def _merge_sanitization_reports(
    left: UnicodeSanitizationReport,
    right: UnicodeSanitizationReport,
) -> UnicodeSanitizationReport:
    return UnicodeSanitizationReport(
        nul_removed=left.nul_removed + right.nul_removed,
        surrogate_pairs_repaired=(
            left.surrogate_pairs_repaired + right.surrogate_pairs_repaired
        ),
        lone_surrogates_replaced=(
            left.lone_surrogates_replaced + right.lone_surrogates_replaced
        ),
        strings_visited=left.strings_visited + right.strings_visited,
    )


def _build_pdf_ocr_options(
    session: Session | None = None,
) -> PdfOcrOptions:
    """Compose the per-job PDF OCR options.

    Local settings (DPI, language, page cap, …) always come
    from the worker environment so the resource footprint is
    predictable; the external provider and its gating
    parameters are read from the database on every call so a
    fresh ``AIRuntimeConfig`` update is picked up without
    restarting the worker. Passing ``session=None`` falls back
    to the tesseract-only behaviour, which keeps unit tests and
    CLI/import paths free of database dependencies.
    """

    summary = pdf_ocr_options_summary()
    row = None
    external_provider = None
    if session is not None:
        try:
            row = session.execute(
                select(AIRuntimeConfig)
                .order_by(AIRuntimeConfig.id.desc())
                .limit(1)
            ).scalars().first()
        except Exception:  # noqa: BLE001 — table missing on older deployments
            row = None
        if row is not None:
            external_provider = build_external_ocr_provider(row)
    return PdfOcrOptions(
        enabled=summary["enabled"],
        language=summary["language"],
        dpi=summary["dpi"],
        min_native_chars=summary["min_native_chars"],
        max_pages=summary["max_pages"],
        timeout_seconds=summary["timeout_seconds"],
        external_provider=external_provider,
        external_min_chars=int(
            getattr(row, "ocr_min_chars", 8) if row is not None else 8
        ),
        external_confidence_threshold=int(
            getattr(row, "ocr_confidence_threshold", 600)
            if row is not None
            else 600
        ),
        external_max_pages=int(
            getattr(row, "ocr_max_external_pages", 20) if row is not None else 20
        ),
    )


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


def _is_terminal_parse_failure(details: dict | None) -> bool:
    return bool(
        isinstance(details, dict)
        and details.get("reason") in TERMINAL_PARSE_FAILURE_REASONS
    )


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


def _load_content_for_preview(
    session: Session,
    document: Document,
    version: DocumentVersion,
) -> tuple[bytes | str, str, str | None] | None:
    """Load a source for preview, fetching WebDAV bytes only for this task."""

    loaded = _load_content_for_parsing(session, document, version)
    if loaded is not None:
        return loaded
    document_meta = document.meta or {}
    if document_meta.get("external_source") != "webdav":
        return None
    source_id = document_meta.get("webdav_source_id")
    if not isinstance(source_id, int):
        return None
    source = session.get(WebDAVSource, source_id)
    entry = session.scalar(
        select(WebDAVEntry).where(
            WebDAVEntry.source_id == source_id,
            WebDAVEntry.document_id == document.id,
        )
    )
    if source is None or entry is None:
        return None
    try:
        payload, content_type = asyncio.run(
            download_file(
                base_url=source.base_url,
                remote_path=entry.remote_path,
                username=source.username,
                password=decrypt_secret(source.password_cipher),
                trusted_private_network=source.trusted_private_network,
                max_bytes=PDF_PREVIEW_MAX_BYTES,
                timeout_seconds=90,
            )
        )
    except (WebDAVError, URLSecurityError, ValueError) as exc:
        logger.warning(
            "webdav_preview_source_failed",
            document_id=document.id,
            error=str(exc),
        )
        version.meta = {
            **(version.meta or {}),
            "preview": {
                "status": "failed",
                "reason": "webdav_source_unavailable",
                "message": str(exc)[:1000],
            },
        }
        session.add(version)
        return None
    return payload, content_type, entry.remote_path


def _read_blob_bytes(blob: Blob) -> bytes:
    storage = LocalBlobStorage(settings.storage_path)
    with storage.open(blob.storage_key) as source:
        return source.read()


def _is_word_document(content_type: str, filename: str | None) -> bool:
    lower_name = (filename or "").lower()
    lower_type = (content_type or "").lower()
    return lower_name.endswith((".doc", ".docx")) or lower_type in {
        "application/msword",
        "application/doc",
        "application/vnd.ms-word",
        "application/vnd.msword",
        "application/winword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }


def _ensure_word_pdf_preview(
    session: Session,
    version: DocumentVersion,
    content: bytes | str,
    content_type: str,
    filename: str | None,
) -> bool:
    """Create a version-scoped PDF view without changing the source document.

    The conversion writes a fresh blob row and stamps
    ``version.preview_blob_id`` + ``version.meta.preview``. To make
    sure a flush error (constraint violation, connection drop,
    etc.) does not poison the outer transaction or mask the real
    failure, the database side effects are isolated in a nested
    savepoint via :meth:`Session.begin_nested`. On any failure the
    savepoint is rolled back first, the original exception class
    is captured before the session is touched again. Diagnostics
    exclude SQL parameters and converter output.
    """

    if version.preview_blob_id is not None or not _is_word_document(
        content_type, filename
    ):
        return version.preview_blob_id is not None
    if not isinstance(content, bytes):
        return False

    executable = shutil.which("soffice") or shutil.which("libreoffice")
    if executable is None:
        version.meta = {
            **(version.meta or {}),
            "preview": {"status": "failed", "reason": "converter_unavailable"},
        }
        session.add(version)
        return False

    # begin_nested flushes pending outer writes before creating its savepoint.
    # Let a body persistence failure reach the parsing rollback.
    session.flush()
    storage_key_to_cleanup: str | None = None
    preview_blob_id: int | None = None
    document_version_id = getattr(version, "id", None)
    try:
        with tempfile.TemporaryDirectory(prefix="cangzhi-preview-") as directory:
            workdir = Path(directory)
            suffix = ".doc" if (filename or "").lower().endswith(".doc") else ".docx"
            source_path = workdir / f"source{suffix}"
            output_path = workdir / "source.pdf"
            profile_path = workdir / "libreoffice-profile"
            source_path.write_bytes(content)
            completed = subprocess.run(
                [
                    executable,
                    f"-env:UserInstallation={profile_path.as_uri()}",
                    "--headless",
                    "--nologo",
                    "--nodefault",
                    "--nolockcheck",
                    "--norestore",
                    "--convert-to",
                    "pdf:writer_pdf_Export",
                    "--outdir",
                    str(workdir),
                    str(source_path),
                ],
                check=False,
                capture_output=True,
                timeout=90,
            )
            if completed.returncode != 0 or not output_path.is_file():
                converter_output = (
                    completed.stderr.decode("utf-8", errors="replace")
                    or completed.stdout.decode("utf-8", errors="replace")
                ).strip()
                raise RuntimeError(converter_output[-1000:] or "未生成 PDF 文件")

            storage = LocalBlobStorage(settings.storage_path)
            stored = storage.save(
                BytesIO(output_path.read_bytes()), max_bytes=PDF_PREVIEW_MAX_BYTES
            )
            storage_key_to_cleanup = stored.storage_key

            try:
                with session.begin_nested():
                    preview_blob = (
                        session.query(Blob)
                        .filter(Blob.sha256 == stored.sha256)
                        .one_or_none()
                    )
                    if preview_blob is None:
                        source_stem = Path(filename or "document").stem or "document"
                        preview_blob = Blob(
                            sha256=stored.sha256,
                            storage_key=stored.storage_key,
                            content_type="application/pdf",
                            file_size=stored.size,
                            original_filename=f"{source_stem}.pdf",
                        )
                        session.add(preview_blob)
                        session.flush()
                        preview_blob_id = preview_blob.id
                    else:
                        # Storage was just written but is unreferenced; clean
                        # it up before we lose track of the key. The
                        # already-persisted blob row stays.
                        storage.delete(stored.storage_key)
                        storage_key_to_cleanup = None
                        preview_blob_id = preview_blob.id

                    version.preview_blob_id = preview_blob_id
                    version.meta = {
                        **(version.meta or {}),
                        "preview": {
                            "status": "ready",
                            "format": "pdf",
                            "config_version": PDF_PREVIEW_CONFIG,
                        },
                    }
                    session.add(version)
            except Exception:
                # Drop any orphan storage entry written before the
                # savepoint failed. ``storage_key_to_cleanup`` is reset
                # when we already cleaned it up inside the savepoint.
                if storage_key_to_cleanup is not None:
                    try:
                        LocalBlobStorage(settings.storage_path).delete(
                            storage_key_to_cleanup
                        )
                    except Exception:
                        logger.warning(
                            "word_preview_orphan_cleanup_failed",
                            document_version_id=document_version_id,
                        )
                storage_key_to_cleanup = None
                raise

            storage_key_to_cleanup = None
            return True
    except Exception as exc:  # noqa: BLE001
        # SQL parameters and converter output may contain document content.
        reason = type(exc).__name__
        message = "PDF 预览生成失败，正文解析结果已保留"
        logger.warning(
            "word_preview_failed",
            document_version_id=document_version_id,
            reason=reason,
            error=message,
        )
        if not session.is_active:
            raise
        version.meta = {
            **(version.meta or {}),
            "preview": {"status": "failed", "reason": reason, "message": message},
        }
        session.add(version)
        return False


def _fetch_and_store_url(
    session: Session,
    document: Document,
    version: DocumentVersion,
) -> tuple[bytes, str]:
    assert document.source_url, "URL document must have a source_url"
    try:
        page = None
        attempted_profiles: list[str] = []
        last_fetch_error: URLFetchError | None = None
        for profile in browser_profiles_for_url(document.source_url):
            attempted_profiles.append(profile.name)
            try:
                candidate = fetch_url(
                    document.source_url,
                    max_bytes=settings.url_fetch_max_bytes,
                    timeout_seconds=settings.url_fetch_timeout_seconds,
                    max_redirects=settings.url_fetch_max_redirects,
                    user_agent=profile.user_agent,
                    request_headers=profile.headers,
                    accept=profile.headers["Accept"],
                )
            except URLFetchError as exc:
                last_fetch_error = exc
                continue
            if not looks_like_access_block(candidate.raw_bytes):
                page = candidate
                break
            logger.warning(
                "url_access_challenge",
                document_id=document.id,
                browser_profile=profile.name,
            )
        if page is None:
            profiles = "、".join(attempted_profiles)
            if last_fetch_error is not None:
                raise URLFetchError(
                    f"浏览器模式抓取失败（{profiles}）：{last_fetch_error}"
                )
            raise URLFetchError(f"网站返回访问验证页面（已尝试浏览器模式：{profiles}）")
    except URLSecurityError as exc:
        raise _FatalFetchError(str(exc)) from None
    except URLFetchError as exc:
        raise _FetchAttemptError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        raise _FetchAttemptError(str(exc)) from None

    storage = LocalBlobStorage(settings.storage_path)
    from io import BytesIO

    stored = storage.save(
        BytesIO(page.raw_bytes), max_bytes=settings.url_fetch_max_bytes
    )
    blob = session.query(Blob).filter(Blob.sha256 == stored.sha256).one_or_none()
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
        select(DocumentSummary).where(DocumentSummary.document_version_id == version.id)
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
            select(DocumentTag).where(DocumentTag.document_version_id == version.id)
        ).all()
    )
    if existing_summary is not None or existing_categories or existing_tags:
        return _complete_understanding_job(session, job, document, version)

    metadata = (
        (structured.get("metadata") or {}) if isinstance(structured, dict) else {}
    )
    title = (
        document.title
        or (metadata.get("title") if isinstance(metadata, dict) else None)
        or ""
    )
    content_for_ai = _build_ai_input(title, raw_text, metadata)

    provider = build_provider_from_session(session)
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

    _persist_understanding_result(
        session, version, document, result, categories, provider
    )
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
    except Exception as exc:
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

    seed_preview_parts: list[str] = []
    seed_preview_chars = 0
    for block in structured.blocks:
        if not block.text or seed_preview_chars >= 1_000:
            continue
        piece = block.text[: 1_000 - seed_preview_chars]
        seed_preview_parts.append(piece)
        seed_preview_chars += len(piece)
    seed_input = "|".join(
        [
            str(version.id),
            version.content_hash or "",
            "\n".join(seed_preview_parts),
        ]
    )
    content_hash_seed = hashlib.sha256(seed_input.encode("utf-8")).hexdigest()

    # Step 1: Detect document type and check for structure anomalies
    profile: DocumentProfile
    chunking_config: ChunkingConfig
    structure_anomaly: str | None = None

    try:
        # Extract features for detection
        block_types = [b.type for b in structured.blocks]
        headings: list[str] = []
        first_paragraphs: list[str] = []
        for b in structured.blocks:
            if b.type == "heading" and b.text:
                headings.append(b.text)
            elif b.type == "paragraph" and b.text:
                if len(first_paragraphs) < 10:
                    first_paragraphs.append(b.text)

        profile = detect_document_type(
            parser_type=structured.document_type,
            block_types=block_types,
            title=document.title,
            headings=headings,
            first_paragraphs=first_paragraphs,
        )

        chunking_config = get_chunking_config(profile)

        # Only flag shapes where an optional future AI boundary pass may help.
        if not structured.blocks:
            structure_anomaly = "empty_document"
        elif (
            not headings
            and profile.detected_type not in {"code", "table"}
            and max(len(block.text or "") for block in structured.blocks)
            > chunking_config.child_hard_max_chars * 2
        ):
            structure_anomaly = "long_unstructured_text"
    except Exception as exc:
        # Fall back to safe defaults if anything goes wrong
        logger.warning(
            "document_detection_failed",
            document_id=document.id,
            version_id=version.id,
            error=str(exc),
        )
        profile = DocumentProfile(
            detected_type="general",
            confidence=0.0,
            scores={},
            evidence={"fallback_reason": "detection_error"},
        )
        chunking_config = get_chunking_config(profile)
        structure_anomaly = f"detection_error: {exc}"

    # Step 2: Build chunks with detected type's config
    try:
        specs = build_chunk_specs(
            structured,
            content_hash_seed=content_hash_seed,
            child_max_chars=chunking_config.child_target_max_chars,
            child_hard_max_chars=chunking_config.child_hard_max_chars,
            child_min_chars=chunking_config.child_target_min_chars,
            child_overlap_chars=chunking_config.child_overlap_chars,
        )
    except Exception as exc:
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

    _replace_version_chunks(session, document, version, specs, profile, chunking_config)
    table_row_count = replace_version_table_rows(
        session,
        document_id=document.id,
        document_version_id=version.id,
        structured_content=structured,
    )

    job.status = "completed"
    job.finished_at = utc_now()
    job.next_retry_at = None
    job.last_error = None
    job.error_details = None
    session.add(job)
    if version.processing_status in ("ready", "chunking", "processing"):
        version.processing_status = "ready"

    # Write detection results and config to DocumentVersion.meta.
    meta = dict(version.meta or {})
    meta["document_profile"] = profile.to_dict()
    meta["chunking_config"] = chunking_config.to_dict()
    meta["structure_anomaly"] = structure_anomaly
    meta["chunk_count"] = sum(1 for spec in specs if spec.role == "child")
    meta["parent_count"] = sum(1 for spec in specs if spec.role == "parent")
    meta["chunk_config_version"] = CHUNKING_IDEMPOTENCY
    meta["structured_table_row_count"] = table_row_count
    version.meta = meta
    session.add(version)

    session.commit()

    if table_row_count:
        _enqueue_dataset_artifact_job(session, document, version)

    # Auto-enqueue embedding work for the freshly-materialised
    # child chunks. The enqueue is a no-op when no profile is
    # currently ``active`` or ``building``; otherwise every
    # new child chunk becomes a pending embedding job per
    # profile. The helper is imported lazily because the
    # ``build_service`` module already pulls in the rest of
    # the embedding pipeline.
    _enqueue_embedding_jobs_for_new_chunks(session, document, version)

    session.commit()
    logger.info(
        "chunking_completed",
        document_id=document.id,
        version_id=version.id,
        detected_type=profile.detected_type,
        confidence=profile.confidence,
        chunks=len(specs),
        anomaly=structure_anomaly,
    )
    return True


def _enqueue_dataset_artifact_job(
    session: Session, document: Document, version: DocumentVersion
) -> None:
    key = f"{version.id}:{DATASET_ARTIFACT_STAGE}:{DATASET_ARTIFACT_VERSION}"
    existing = session.scalar(
        select(ProcessingJob).where(ProcessingJob.idempotency_key == key)
    )
    if existing is None:
        session.add(
            ProcessingJob(
                document_id=document.id,
                document_version_id=version.id,
                stage=DATASET_ARTIFACT_STAGE,
                status="created",
                idempotency_key=key,
                retry_count=0,
                max_retries=MAX_RETRIES,
                config_version=DATASET_ARTIFACT_VERSION,
            )
        )
    elif existing.status in {"failed", "completed"}:
        _reset_processing_job(existing)
        session.add(existing)


def _enqueue_embedding_jobs_for_new_chunks(
    session: Session,
    document: Document,
    version: DocumentVersion,
) -> None:
    """For each new child chunk, enqueue work for active/building/ready profiles.

    The enqueue is a no-op when no profile is currently
    ``active``, ``building`` or ``ready`` (e.g. before the
    operator has ever run a build, or after a failed build the
    operator has not retried). Otherwise every new child chunk
    becomes a pending embedding job per profile.

    ``ready`` profiles that actually gain new work are demoted
    back to ``building`` and their ``build_finished_at`` is
    cleared. The profile's build counters are recomputed against
    the live eligible corpus so the progress view stays
    truthful; the API side reuses this metadata in
    :func:`_current_child_chunks` to honour the same sampled
    strategy the worker just stamped on the version.

    The helper is intentionally local to the worker: the API
    side has its own ``enqueue_embedding_jobs_for_chunk`` for
    tests that do not need the rest of the worker pipeline.
    """

    try:
        from apps.api.embeddings.build_service import (
            EMBEDDING_STAGE,
            _idempotency_key,
        )
    except ImportError:  # pragma: no cover - defensive
        return

    profiles = list(
        session.scalars(
            select(EmbeddingProfile).where(
                EmbeddingProfile.status.in_(("active", "building", "ready"))
            )
        ).all()
    )
    if not profiles:
        return

    child_chunks = list(
        session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id,
                DocumentChunk.role == "child",
                DocumentChunk.is_current.is_(True),
            )
        ).all()
    )
    if not child_chunks:
        return

    total_child_chunks = len(child_chunks)
    table_row_count = int((version.meta or {}).get("structured_table_row_count") or 0)
    use_sampling = (
        table_row_count > LARGE_TABLE_EMBEDDING_THRESHOLD
        and total_child_chunks > LARGE_TABLE_EMBEDDING_THRESHOLD
    )
    selected_chunks = (
        evenly_sample_chunks(child_chunks, LARGE_TABLE_EMBEDDING_SAMPLE_SIZE)
        if use_sampling
        else child_chunks
    )
    version.meta = {
        **(version.meta or {}),
        "embedding_strategy": {
            "mode": "sampled" if use_sampling else "full",
            "total_child_chunks": total_child_chunks,
            "selected_chunks": len(selected_chunks),
            "structured_table_rows": table_row_count,
        },
    }
    session.add(version)

    for profile in profiles:
        profile_flipped = False
        for chunk in selected_chunks:
            key = _idempotency_key(
                profile.id, chunk.id, profile.config_fingerprint
            )
            existing = session.scalar(
                select(ProcessingJob).where(ProcessingJob.idempotency_key == key)
            )
            if existing is not None:
                continue
            session.add(
                ProcessingJob(
                    document_id=document.id,
                    document_version_id=version.id,
                    stage=EMBEDDING_STAGE,
                    status="created",
                    idempotency_key=key,
                    config_version=profile.config_fingerprint,
                    embedding_profile_id=profile.id,
                    embedding_chunk_id=chunk.id,
                )
            )
            if profile.status == "ready":
                profile.status = "building"
                profile.build_finished_at = None
                profile_flipped = True
        if profile_flipped:
            _recompute_profile_counters_sync(session, profile)
            session.add(profile)


def _recompute_profile_counters_sync(
    session: Session,
    profile: EmbeddingProfile,
) -> None:
    """Sync mirror of :func:`build_service._recompute_profile_counters`.

    Used by the worker's chunking hook to refresh the
    ``total_chunks`` / ``completed_chunks`` / ``failed_chunks``
    counters when a ``ready`` profile gets demoted to
    ``building``. The eligible corpus is the union of every
    child chunk that is still current and that the profile
    already has an embedding job for (large structured tables
    intentionally sample embeddings; their unsampled rows stay
    available through lexical and exact-table indexes and must
    not leave the vector profile permanently "incomplete").

    ``failed_chunks`` is recomputed from the live job state so
    a profile that had a couple of jobs fail in a previous run
    still reports the right number; we never zero the counter
    unconditionally.
    """

    try:
        from apps.api.embeddings.build_service import EMBEDDING_STAGE
    except ImportError:  # pragma: no cover - defensive
        return

    all_current_chunks = list(
        session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.role == "child",
                DocumentChunk.is_current.is_(True),
            )
        ).all()
    )
    jobs = list(
        session.scalars(
            select(ProcessingJob).where(
                ProcessingJob.embedding_profile_id == profile.id,
                ProcessingJob.stage == EMBEDDING_STAGE,
            )
        ).all()
    )
    eligible_ids = {job.embedding_chunk_id for job in jobs if job.embedding_chunk_id is not None}
    eligible_ids &= {chunk.id for chunk in all_current_chunks}
    if not eligible_ids:
        profile.total_chunks = 0
        profile.completed_chunks = 0
        profile.failed_chunks = 0
        return
    current_chunks = [chunk for chunk in all_current_chunks if chunk.id in eligible_ids]
    current_ids = {chunk.id for chunk in current_chunks}
    embeddings = list(
        session.scalars(
            select(ChunkEmbedding).where(
                ChunkEmbedding.profile_id == profile.id,
                ChunkEmbedding.chunk_id.in_(current_ids),
            )
        ).all()
    )
    current_hashes = {chunk.id: chunk.content_hash for chunk in current_chunks}
    completed = sum(
        1
        for row in embeddings
        if current_hashes.get(row.chunk_id) == row.content_hash
        and isinstance(row.vector, list)
        and len(row.vector) == profile.dim
    )
    failed = sum(
        1
        for job in jobs
        if job.status == "failed" and job.embedding_chunk_id in current_ids
    )
    profile.total_chunks = len(current_chunks)
    profile.completed_chunks = completed
    profile.failed_chunks = failed


def _replace_version_chunks(
    session: Session,
    document: Document,
    version: DocumentVersion,
    specs,
    profile: DocumentProfile,
    chunking_config: ChunkingConfig,
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
            select(DocumentChunk).where(DocumentChunk.document_version_id == version.id)
        ).all()
    )
    # A document may receive a new version from WebDAV or manual reprocessing.
    # Keep historical chunks for traceability, but only the current version may
    # participate in retrieval.
    session.execute(
        update(DocumentChunk)
        .where(
            DocumentChunk.document_id == document.id,
            DocumentChunk.document_version_id != version.id,
            DocumentChunk.is_current.is_(True),
        )
        .values(is_current=False),
        execution_options={"synchronize_session": False},
    )
    session.execute(
        delete(DocumentChunk).where(DocumentChunk.document_version_id == version.id),
        execution_options={"synchronize_session": False},
    )
    session.flush()
    for existing_chunk in existing_chunks:
        if existing_chunk in session:
            session.expunge(existing_chunk)

    parent_id_by_external: dict[str, int] = {}
    title = (document.title or "").strip()
    # Add profile info to each chunk's extra
    chunk_extra = {
        "document_profile": {
            "detected_type": profile.detected_type,
            "confidence": profile.confidence,
            "profile_version": chunking_config.profile_version,
        },
    }
    for spec in specs:
        search_text = _build_search_text(title, spec)
        extra = dict(chunk_extra)
        if spec.extra:
            extra.update(spec.extra)
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
            extra=extra,
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
            extra=dict(item.get("extra") or {}),
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
    regions = metadata.get("regions") if isinstance(metadata, dict) else None
    if isinstance(regions, list) and regions:
        structure_lines = []
        for region in regions[:20]:
            if not isinstance(region, dict):
                continue
            columns = "、".join(
                str(value) for value in region.get("column_names") or []
            )
            structure_lines.append(
                f"- 工作表 {region.get('sheet_name') or '未命名'}，"
                f"第 {region.get('row_start')}–{region.get('row_end')} 行，"
                f"列：{columns or '未识别'}"
            )
        lines = [line for line in raw_text.splitlines() if line.strip()]
        if len(lines) > 60:
            middle = max(0, len(lines) // 2 - 10)
            sampled_lines = [
                "【表格开头行】",
                *lines[:25],
                "【表格中部行】",
                *lines[middle : middle + 20],
                "【表格结尾行】",
                *lines[-15:],
            ]
        else:
            sampled_lines = lines
        table_sample = "\n".join(sampled_lines)
        if len(table_sample) > 6_500:
            table_sample = table_sample[:6_500].rsplit("\n", 1)[0]
        structure_text = "\n".join(structure_lines)
        return (
            f"标题：{title}\n\n"
            "表格结构：\n"
            f"{structure_text}\n\n"
            "代表性数据行：\n"
            f"{table_sample}"
        )

    max_chars = 8_000
    if len(raw_text) <= max_chars:
        sample = raw_text
    else:
        head = raw_text[:3_000]
        middle_start = max(0, len(raw_text) // 2 - 1_000)
        middle = raw_text[middle_start : middle_start + 2_000]
        tail = raw_text[-3_000:]
        sample = (
            "【文档开头】\n"
            f"{head}\n\n"
            "【文档中部抽样】\n"
            f"{middle}\n\n"
            "【文档结尾】\n"
            f"{tail}"
        )
    return f"标题：{title}\n\n正文代表性内容：\n{sample}"


def _apply_inbox_fallback(
    session: Session,
    version: DocumentVersion,
    document: Document,
    *,
    reason: str,
    details: dict | None = None,
) -> None:
    inbox = session.scalar(select(Category).where(Category.slug == INBOX_CATEGORY_SLUG))
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
    provider: AIProvider | None = None,
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
            model=(provider.name if provider else settings.ai_provider or None),
            prompt_version=(provider.prompt_version if provider else "v1"),
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
        delete(DocumentSummary).where(DocumentSummary.document_version_id == version_id)
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
    # One worker session may process jobs from several workspaces. Resolve the
    # job and its owning document without a stale filter, then bind all
    # subsequent taxonomy/document reads and writes to that document's space.
    clear_workspace_context(session)
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

    bind_workspace_context(session, document.workspace_id)

    stage = _normalize_stage(job.stage)

    if stage == UNDERSTANDING_STAGE:
        return _process_understanding(session, job, document, version)

    if stage == CHUNKING_STAGE:
        return _process_chunking(session, job, document, version)

    if stage == DATASET_CATALOG_STAGE:
        return _process_dataset_catalog(session, job, version)

    if stage == DATASET_ARTIFACT_STAGE:
        return _process_dataset_artifacts(session, job, version)

    if stage == PREVIEW_STAGE:
        loaded = _load_content_for_preview(session, document, version)
        if loaded is not None:
            content, content_type, filename = loaded
            _ensure_word_pdf_preview(session, version, content, content_type, filename)
        job.status = "completed"
        job.finished_at = utc_now()
        job.next_retry_at = None
        job.last_error = None
        session.add(job)
        session.commit()
        return True

    if stage == "embedding":
        # Lazy import: ``embedding_processor`` imports
        # ``calculate_next_retry_at`` from this module, so a
        # top-level import would create a cycle.
        from apps.worker.services.embedding_processor import (
            process_embedding_job,
            reconcile_profile_progress,
        )

        outcome = process_embedding_job(session, job)
        if outcome.status == "completed":
            reconcile_profile_progress(session, outcome.profile_id)
            session.commit()
            return True
        if outcome.status == "skipped":
            session.commit()
            return True
        session.commit()
        return False

    return _process_parsing(session, job, document, version)


def _process_dataset_catalog(
    session: Session,
    job: ProcessingJob,
    version: DocumentVersion,
) -> bool:
    """Backfill field profiles without rebuilding chunks or vectors."""

    datasets = list(
        session.scalars(
            select(KnowledgeDataset)
            .where(KnowledgeDataset.document_version_id == version.id)
            .order_by(KnowledgeDataset.id)
        ).all()
    )
    for dataset in datasets:
        rows = list(
            session.scalars(
                select(StructuredTableRow)
                .where(StructuredTableRow.dataset_id == dataset.id)
                .order_by(StructuredTableRow.row_number)
            ).all()
        )
        if not rows:
            continue
        payloads = [
            {"row_number": row.row_number, "values": dict(row.values or {})}
            for row in rows
        ]
        columns = tuple(str(item) for item in payloads[0]["values"])
        session.execute(
            delete(DatasetField).where(DatasetField.dataset_id == dataset.id)
        )
        session.add_all(_profile_dataset_fields(dataset.id, payloads, columns))
        dataset.row_count = len(rows)
        dataset.column_count = len(columns)
        dataset.source_row_start = rows[0].row_number
        dataset.source_row_end = rows[-1].row_number
        dataset.profile = {
            "profile_version": 1,
            "quality": _dataset_quality(payloads, columns),
            "catalog_source": "background_backfill",
        }
        session.add(dataset)
    job.status = "completed"
    job.finished_at = utc_now()
    job.next_retry_at = None
    job.last_error = None
    job.error_details = None
    session.add(job)
    session.commit()
    return True


def _process_dataset_artifacts(
    session: Session,
    job: ProcessingJob,
    version: DocumentVersion,
) -> bool:
    """Build every dataset snapshot before marking the stage completed."""

    datasets = list(
        session.scalars(
            select(KnowledgeDataset)
            .where(KnowledgeDataset.document_version_id == version.id)
            .order_by(KnowledgeDataset.id)
        ).all()
    )
    published_paths: list[Path] = []
    try:
        for dataset in datasets:
            artifact = build_dataset_parquet(
                session,
                dataset,
                storage_root=settings.storage_path,
            )
            if artifact.storage_key:
                published_paths.append(
                    (
                        Path(settings.storage_path).resolve() / artifact.storage_key
                    ).resolve()
                )
        job.status = "completed"
        job.finished_at = utc_now()
        job.next_retry_at = None
        job.last_error = None
        job.error_details = {
            "dataset_count": len(datasets),
            "backend": "duckdb",
            "format": "parquet",
        }
        session.add(job)
        session.commit()
        return True
    except Exception as exc:
        session.rollback()
        storage_root = Path(settings.storage_path).resolve()
        for artifact_path in published_paths:
            if storage_root in artifact_path.parents and artifact_path.is_file():
                artifact_path.unlink()
        job = session.get(ProcessingJob, job.id)
        if job is None:
            return False
        job.retry_count += 1
        job.finished_at = utc_now()
        job.last_error = str(exc)
        job.error_details = {
            "code": (
                exc.code
                if isinstance(exc, DatasetExecutionError)
                else "artifact_build_failed"
            ),
            "exception": str(exc),
        }
        if job.retry_count >= (job.max_retries or MAX_RETRIES):
            job.status = "failed"
            job.next_retry_at = None
        else:
            job.status = "retry"
            job.next_retry_at = calculate_next_retry_at(job.retry_count - 1)
        session.add(job)
        session.commit()
        return False


def _process_parsing(
    session: Session,
    job: ProcessingJob,
    document: Document,
    version: DocumentVersion,
) -> bool:
    job_id = job.id
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
                    session,
                    job,
                    version,
                    f"无法抓取网页：{exc}",
                    details={"reason": "ssrf_or_fatal"},
                )
            except _FetchAttemptError as exc:
                return _mark_parse_failure(
                    session,
                    job,
                    version,
                    f"抓取失败：{exc}",
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
            pdf_ocr_options = _build_pdf_ocr_options(session)
            parser = get_parser_for_content(
                content_type,
                filename,
                pdf_ocr_options=pdf_ocr_options,
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
            if _is_terminal_parse_failure(result.error_details):
                return _mark_terminal_failure(
                    session,
                    job,
                    version,
                    result.error_message or "解析失败",
                    details=result.error_details,
                )
            return _mark_parse_failure(
                session,
                job,
                version,
                result.error_message or "解析失败",
                result.error_details,
            )

        structured_content = result.structured_content
        sanitization_report = _sanitize_structured_content(structured_content)
        _log_pdf_ocr_summary(document, version, structured_content)
        if (
            document.source_type == DocumentSourceType.url
            and len(structured_content.full_text().strip()) < MIN_USEFUL_URL_TEXT_LENGTH
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
                        adapted_report = _sanitize_structured_content(
                            structured_content
                        )
                        sanitization_report = _merge_sanitization_reports(
                            sanitization_report, adapted_report
                        )
            except (URLFetchError, URLSecurityError) as exc:
                logger.warning(
                    "url_adapter_failed",
                    document_id=document.id,
                    error=str(exc),
                )

        _apply_parse_result(
            session,
            document,
            version,
            structured_content,
            sanitization_report=sanitization_report,
        )
        _ensure_word_pdf_preview(
            session, version, content_bytes, content_type, filename
        )

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
            and len((version.raw_content or "").strip()) >= MIN_USEFUL_URL_TEXT_LENGTH
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
        _release_webdav_source_blob(session, version)
        logger.info(
            "parse_completed",
            job_id=job.id,
            document_id=document.id,
        )
        return True
    except Exception as exc:
        exception_class = type(getattr(exc, "orig", exc)).__name__
        logger.warning("parse_unexpected_error", job_id=job_id, reason=exception_class)
        session.rollback()
        if isinstance(exc, UnicodeKeyCollisionError):
            return _mark_terminal_failure(
                session, job, version, "规范化后的元数据字段名冲突",
                details={"exception_class": exception_class},
            )
        return _mark_parse_failure(
            session, job, version, "处理任务发生异常",
            {"exception_class": exception_class},
        )


def _release_webdav_source_blob(
    session: Session,
    version: DocumentVersion,
) -> None:
    """Release the temporary WebDAV original after parsed content is durable."""

    if (version.meta or {}).get(
        "external_source"
    ) != "webdav" or version.blob_id is None:
        return
    blob = session.get(Blob, version.blob_id)
    if blob is None:
        version.blob_id = None
        session.commit()
        return
    blob_id = blob.id
    storage_key = blob.storage_key
    version.blob_id = None
    session.add(version)
    session.commit()
    remaining = session.scalar(
        select(func.count(DocumentVersion.id)).where(DocumentVersion.blob_id == blob_id)
    )
    if remaining:
        return
    session.delete(blob)
    session.commit()
    LocalBlobStorage(settings.storage_path).delete(storage_key)


def _log_pdf_ocr_summary(
    document: Document,
    version: DocumentVersion,
    structured: StructuredContent,
) -> None:
    """Emit a compact PDF extraction summary when a PDF was just parsed.

    Parsers that do not produce ``pdf_extraction`` metadata are skipped so the
    log stays useful even when the parser pipeline falls back to a generic
    type. Failed or skipped pages are listed by number rather than dumping
    per-page detail to keep the line bounded.
    """

    metadata = structured.metadata if isinstance(structured.metadata, dict) else {}
    summary = metadata.get("pdf_extraction")
    if not isinstance(summary, dict):
        return
    def _count_pages(key: str) -> int:
        pages = summary.get(key)
        return len(pages) if isinstance(pages, list) else 0

    logger.info(
        "pdf_ocr_summary",
        document_id=document.id,
        version_id=version.id,
        version=summary.get("version"),
        engine=summary.get("engine"),
        page_count=summary.get("page_count"),
        native_text_page_count=_count_pages("native_text_pages"),
        image_page_count=_count_pages("image_pages"),
        ocr_candidate_page_count=_count_pages("ocr_candidate_pages"),
        ocr_completed_page_count=_count_pages("ocr_completed_pages"),
        ocr_failed_pages=summary.get("ocr_failed_pages"),
        ocr_skipped_pages=summary.get("ocr_skipped_pages"),
        ocr_status=summary.get("ocr_status"),
        external_provider=(
            (summary.get("external_provider") or {}).get("provider")
        ),
        external_model=(
            (summary.get("external_provider") or {}).get("model")
        ),
        external_attempted_pages=_count_pages("external_attempted_pages"),
        external_completed_pages=_count_pages("external_completed_pages"),
        external_failed_pages=summary.get("external_failed_pages"),
        external_skipped_pages=summary.get("external_skipped_pages"),
        external_trigger_reasons=summary.get("external_trigger_reasons"),
    )


def _apply_parse_result(
    session: Session,
    document: Document,
    version: DocumentVersion,
    structured: StructuredContent,
    *,
    sanitization_report: UnicodeSanitizationReport | None = None,
) -> None:
    if sanitization_report is None:
        report = _sanitize_structured_content(structured)
        if report.nul_removed or report.surrogate_pairs_repaired or report.lone_surrogates_replaced:
            sanitization_report = report
    payload = structured.to_dict()
    if sanitization_report is not None:
        metadata_block = payload.get("metadata")
        if not isinstance(metadata_block, dict):
            metadata_block = {}
            payload["metadata"] = metadata_block
        sanitization_meta = metadata_block.get("sanitization")
        if not isinstance(sanitization_meta, dict):
            sanitization_meta = {}
            metadata_block["sanitization"] = sanitization_meta
        sanitization_meta["unicode"] = sanitization_report.as_dict()
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
        if "canonical_url" in metadata and isinstance(
            metadata.get("canonical_url"), str
        ):
            doc_meta = dict(document.meta or {})
            doc_meta["canonical_url"] = metadata.get("canonical_url")
            document.meta = doc_meta
        if "raw_text" in metadata and isinstance(metadata.get("raw_text"), str):
            version.raw_content = metadata["raw_text"]

    version.processing_status = "ready"
    version.content_hash = (
        version.content_hash
        or hashlib.sha256((full_text or "").encode("utf-8")).hexdigest()
    )
    session.add(version)
    session.add(document)

    if sanitization_report is not None and (
        sanitization_report.nul_removed
        or sanitization_report.surrogate_pairs_repaired
        or sanitization_report.lone_surrogates_replaced
    ):
        logger.info(
            "parse_unicode_sanitized",
            document_id=document.id,
            version_id=version.id,
            nul_removed=sanitization_report.nul_removed,
            surrogate_pairs_repaired=sanitization_report.surrogate_pairs_repaired,
            lone_surrogates_replaced=sanitization_report.lone_surrogates_replaced,
            strings_visited=sanitization_report.strings_visited,
        )


def claim_pending_jobs(
    session: Session,
    limit: int = BATCH_SIZE,
    *,
    stages: Sequence[str] | None = None,
) -> list[int]:
    """Atomically claim up to ``limit`` due processing jobs.

    The claim is implemented with ``with_for_update(skip_locked=True)``
    so a second worker cannot steal the same row. The query
    always honours the existing due-retry filter
    (``next_retry_at IS NULL`` or already past). Passing
    ``stages`` restricts the claim to the given stage values
    (the stored/parsing aliases are expanded so legacy rows are reachable via
    ``stages=("parsing",)``). ``stages=None`` keeps the legacy
    FIFO-across-all-stages behaviour used by the operator path
    and external callers.

    A non-positive ``limit`` short-circuits without touching
    any row, which keeps the scheduler testable and lets an
    operator abort a batch cleanly.
    """
    if limit <= 0 or (stages is not None and not stages):
        return []
    now = utc_now()
    conditions = [
        ProcessingJob.status.in_(("created", "retry")),
        (
            ProcessingJob.next_retry_at.is_(None)
            | (ProcessingJob.next_retry_at <= now)
        ),
    ]
    if stages is not None:
        stage_values = set(stages)
        # Legacy ``stored`` rows are written with that exact
        # label on disk; the rest of the pipeline treats them
        # as ``parsing``. If the caller asks for either side
        # of that alias, claim both so a legacy backlog is
        # still reachable through the modern filter.
        if "parsing" in stage_values:
            stage_values.add("stored")
        if "stored" in stage_values:
            stage_values.add("parsing")
        if stage_values:
            conditions.append(ProcessingJob.stage.in_(tuple(stage_values)))
    jobs = list(
        session.scalars(
            select(ProcessingJob)
            .where(*conditions)
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


def _claim_one_unknown_stage_job(session: Session) -> int | None:
    """Claim the oldest due job whose ``stage`` is not in
    :data:`STAGE_CYCLE`.

    The cycle is the canonical stage set today; future stages
    added without updating the constant would otherwise sit
    in the queue forever. Falling back to one off-cycle job
    per cycle keeps them moving while still preserving the
    fair-share budget of the known stages.
    """
    now = utc_now()
    job = session.scalar(
        select(ProcessingJob)
        .where(
            ProcessingJob.status.in_(("created", "retry")),
            (
                ProcessingJob.next_retry_at.is_(None)
                | (ProcessingJob.next_retry_at <= now)
            ),
            ~ProcessingJob.stage.in_(tuple(STAGE_CYCLE)),
        )
        .order_by(ProcessingJob.created_at, ProcessingJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        return None
    job.status = "processing"
    job.started_at = now
    job.finished_at = None
    session.add(job)
    session.commit()
    return job.id


def _run_claimed_job(session: Session, job_id: int) -> int:
    """Run :func:`process_single_job` for one already-claimed
    job, keeping the legacy per-job exception handling so the
    scheduler loop stays readable.

    The function is deliberately synchronous and the caller is
    expected to invoke it serially. The original behaviour for
    embedding failures (reconcile profile progress on every
    retry/failure path) and for non-embedding failures (route
    through :func:`_mark_parse_failure` / :func:`_mark_terminal_failure`)
    is preserved unchanged.
    """
    try:
        return int(process_single_job(session, job_id))
    except Exception as exc:
        logger.exception("unexpected_job_error", job_id=job_id)
        session.rollback()
        job = session.get(ProcessingJob, job_id)
        if job is not None and job.stage == "embedding":
            job.retry_count += 1
            job.finished_at = utc_now()
            job.last_error = "向量任务发生异常"
            job.error_details = {"exception": str(exc)}
            if job.retry_count >= (job.max_retries or MAX_RETRIES):
                job.status = "failed"
                job.next_retry_at = None
            else:
                job.status = "retry"
                job.next_retry_at = calculate_next_retry_at(job.retry_count - 1)
            session.add(job)
            session.commit()
            from apps.worker.services.embedding_processor import (
                reconcile_profile_progress,
            )

            if job.embedding_profile_id is not None:
                reconcile_profile_progress(session, job.embedding_profile_id)
                session.commit()
            return 0
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
        return 0


def process_pending_jobs(
    session: Session,
    max_jobs: int | None = BATCH_SIZE,
) -> int:
    """Run one stage-fair pass over due processing jobs.

    The previous implementation claimed a fixed batch of ten
    jobs in FIFO order and then processed them sequentially,
    which let a long embedding backlog starve freshly enqueued
    parsing or chunking work and let an endless structural
    backlog starve model-driven understanding jobs.

    This scheduler iterates :data:`STAGE_CYCLE` in a fixed
    order, claiming **exactly one** job per stage slot and
    running it before advancing. Empty stages are skipped, so
    a quiet stage does not block the rest of the cycle. The
    cycle ends with an off-cycle slot that picks up due jobs
    whose stage value is not in the cycle (the unknown-stage
    fallback), guaranteeing that a future stage added without
    updating the constant still gets serviced.

    ``max_jobs`` bounds attempts, including failed jobs; None uses BATCH_SIZE.
    A session-local cursor preserves fairness across calls with small budgets.
    Each claim commits one job; no later slot is claimed before it executes.
    This does not reclaim existing orphaned processing jobs or preempt slow work.
    """
    budget = BATCH_SIZE if max_jobs is None else max_jobs
    if budget <= 0:
        return 0
    completed = 0
    attempted = 0
    empty_slots = 0
    slot_count = len(STAGE_CYCLE) + 1
    cursor_key = "cangzhi_stage_cursor"
    cursor = session.info.get(cursor_key, 0) % slot_count
    while attempted < budget and empty_slots < slot_count:
        if cursor == len(STAGE_CYCLE):
            job_id = _claim_one_unknown_stage_job(session)
        else:
            ids = claim_pending_jobs(session, limit=1, stages=(STAGE_CYCLE[cursor],))
            job_id = ids[0] if ids else None
        cursor = (cursor + 1) % slot_count
        session.info[cursor_key] = cursor
        if job_id is None:
            empty_slots += 1
            continue
        empty_slots = 0
        attempted += 1
        completed += _run_claimed_job(session, job_id)
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
