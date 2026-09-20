"""Unified ingestion status and targeted vector repair."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any
from types import SimpleNamespace

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import load_only
from sqlalchemy.ext.asyncio import AsyncSession

from ..embeddings.build_service import _enqueue_embedding_job
from ..embeddings.sampling import evenly_sample_chunks
from ..models.auth import AIRuntimeConfig
from ..models.chunks import DocumentChunk
from ..models.documents import Document, DocumentVersion
from ..models.embedding_profiles import ChunkEmbedding, EmbeddingProfile
from ..models.processing import ProcessingJob

RUNNING_JOB_STATUSES = {"created", "processing", "retry"}
PIPELINE_STAGES = ("parsing", "chunking", "understanding")


class ProcessingStatusError(Exception):
    """Base error raised by the processing-status service."""


class NoActiveEmbeddingProfile(ProcessingStatusError):
    """Raised when vector repair is requested without an active profile."""


# Compatibility name used by the first service-level tests.
DocumentStatusError = ProcessingStatusError


@dataclass(frozen=True)
class VectorRepairResult:
    documents_requested: int
    documents_eligible: int
    chunks_expected: int
    already_fresh: int
    enqueued: int
    reset: int
    skipped: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def _stage_payload(job: ProcessingJob | None) -> dict:
    if job is None:
        return {
            "status": "pending",
            "message": "等待前一阶段完成",
            "last_error": None,
        }
    labels = {
        "created": "等待处理",
        "processing": "正在处理",
        "retry": "等待重试",
        "completed": "已完成",
        "failed": "处理失败",
    }
    return {
        "status": job.status,
        "message": labels.get(job.status, job.status),
        "last_error": job.last_error,
    }


def _expected_chunks(
    version: DocumentVersion, chunks: list[DocumentChunk]
) -> list[DocumentChunk]:
    strategy = (version.meta or {}).get("embedding_strategy") or {}
    if isinstance(strategy, dict) and strategy.get("mode") == "sampled":
        try:
            sample_size = max(int(strategy.get("selected_chunks") or 0), 0)
        except (TypeError, ValueError):
            sample_size = 0
        return evenly_sample_chunks(chunks, sample_size)
    return chunks


def _pdf_extraction_summary(version: DocumentVersion) -> dict | None:
    """Compact PDF extraction summary from the parser's structured metadata.

    Returns ``None`` for non-PDF documents or older payloads that pre-date the
    hybrid OCR metadata. The summary is intentionally bounded: successful page
    groups become counts, while only failed or skipped page numbers are kept
    for diagnosis. The ``external_*`` block mirrors what the parser
    (``pdf_extraction.external_*``) recorded so the front-end can show
    "本地识别" vs "外部识别" without ever seeing the upstream key.
    """

    structured = version.structured_content
    if not isinstance(structured, dict):
        return None
    metadata = structured.get("metadata")
    if not isinstance(metadata, dict):
        return None
    extraction = metadata.get("pdf_extraction")
    if not isinstance(extraction, dict):
        return None

    def _as_list(value: Any) -> list:
        return value if isinstance(value, list) else []

    page_count = extraction.get("page_count")
    native_text_pages = _as_list(extraction.get("native_text_pages"))
    image_pages = _as_list(extraction.get("image_pages"))
    candidate = _as_list(extraction.get("ocr_candidate_pages"))
    completed = _as_list(extraction.get("ocr_completed_pages"))
    failed = _as_list(extraction.get("ocr_failed_pages"))
    skipped = _as_list(extraction.get("ocr_skipped_pages"))

    summary: dict[str, Any] = {
        "version": extraction.get("version"),
        "engine": extraction.get("engine"),
        "ocr_status": extraction.get("ocr_status"),
        "page_count": page_count if isinstance(page_count, int) else None,
        "native_text_pages": len(native_text_pages),
        "image_pages": len(image_pages),
        "ocr_candidate_pages": len(candidate),
        "ocr_completed_pages": len(completed),
        "ocr_failed_pages": failed,
        "ocr_skipped_pages": skipped,
    }

    # External OCR stage 2 fields. The keys are only attached
    # when the document was parsed after the new code shipped,
    # which keeps the public payload back-compatible for older
    # structured_content blobs.
    external_provider = extraction.get("external_provider")
    if isinstance(external_provider, dict):
        summary["external_provider"] = {
            "provider": str(external_provider.get("provider") or ""),
            "model": str(external_provider.get("model") or ""),
        }
        summary["external_attempted_pages"] = len(
            _as_list(extraction.get("external_attempted_pages"))
        )
        summary["external_completed_pages"] = len(
            _as_list(extraction.get("external_completed_pages"))
        )
        summary["external_failed_pages"] = _as_list(
            extraction.get("external_failed_pages")
        )
        summary["external_skipped_pages"] = _as_list(
            extraction.get("external_skipped_pages")
        )
        triggers = extraction.get("external_trigger_reasons")
        if isinstance(triggers, dict):
            summary["external_trigger_reasons"] = {
                str(key): _as_list(value) for key, value in triggers.items()
            }
    return summary


async def _active_profile(db: AsyncSession) -> EmbeddingProfile | None:
    config = (
        (
            await db.execute(
                select(AIRuntimeConfig)
                .options(
                    load_only(
                        AIRuntimeConfig.id,
                        AIRuntimeConfig.active_embedding_profile_id,
                        raiseload=True,
                    )
                )
                .order_by(AIRuntimeConfig.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if config is None or config.active_embedding_profile_id is None:
        return None
    return await db.get(EmbeddingProfile, config.active_embedding_profile_id)


async def _load_pipeline_inputs(
    db: AsyncSession, version_ids: list[int], *, include_extraction: bool = True
) -> tuple[
    dict[int, DocumentVersion],
    dict[int, list[DocumentChunk]],
    dict[int, dict[str, ProcessingJob]],
    EmbeddingProfile | None,
    dict[int, ChunkEmbedding],
    dict[int, ProcessingJob],
]:
    unique_ids = list(dict.fromkeys(version_ids))
    if not unique_ids:
        return {}, {}, {}, None, {}, {}

    # Status needs presence/metadata, never the original body or parsed blocks.
    version_columns = [
        DocumentVersion.id,
        DocumentVersion.document_id,
        DocumentVersion.processing_status,
        DocumentVersion.meta,
        (func.length(DocumentVersion.raw_content) > 0).label("has_raw"),
        cast(DocumentVersion.structured_content, String)
        .not_in(["null", "{}", "[]"])
        .label("has_structured"),
    ]
    if include_extraction:
        version_columns.append(
            DocumentVersion.structured_content["metadata"]["pdf_extraction"].label(
                "extraction"
            )
        )
    version_rows = (
        await db.execute(
            select(*version_columns).where(DocumentVersion.id.in_(unique_ids))
        )
    ).all()
    versions = {}
    for row in version_rows:
        extraction = row.extraction if include_extraction else None
        versions[row.id] = SimpleNamespace(
            id=row.id,
            document_id=row.document_id,
            processing_status=row.processing_status,
            meta=row.meta,
            raw_content=bool(row.has_raw),
            structured_content={"metadata": {"pdf_extraction": extraction}}
            if row.has_structured
            else None,
        )
    chunks_by_version: dict[int, list[DocumentChunk]] = defaultdict(list)
    chunks = list(
        (
            await db.execute(
                select(
                    DocumentChunk.id,
                    DocumentChunk.document_version_id,
                    DocumentChunk.order_index,
                    DocumentChunk.content_hash,
                )
                .where(
                    DocumentChunk.document_version_id.in_(unique_ids),
                    DocumentChunk.role == "child",
                    DocumentChunk.is_current.is_(True),
                )
                .order_by(DocumentChunk.document_version_id, DocumentChunk.id)
            )
        ).all()
    )
    for chunk in chunks:
        chunks_by_version[chunk.document_version_id].append(chunk)

    latest_jobs: dict[int, dict[str, ProcessingJob]] = defaultdict(dict)
    stage_jobs = list(
        (
            await db.execute(
                select(
                    ProcessingJob.id,
                    ProcessingJob.document_version_id,
                    ProcessingJob.stage,
                    ProcessingJob.status,
                    ProcessingJob.last_error,
                )
                .where(
                    ProcessingJob.document_version_id.in_(unique_ids),
                    ProcessingJob.stage.in_(PIPELINE_STAGES),
                )
                .order_by(ProcessingJob.id.desc())
            )
        ).all()
    )
    for job in stage_jobs:
        latest_jobs[job.document_version_id].setdefault(job.stage, job)

    profile = await _active_profile(db)
    embeddings_by_chunk: dict[int, ChunkEmbedding] = {}
    embedding_jobs_by_chunk: dict[int, ProcessingJob] = {}
    if profile is not None and chunks:
        chunk_ids = select(DocumentChunk.id).where(
            DocumentChunk.document_version_id.in_(unique_ids),
            DocumentChunk.role == "child",
            DocumentChunk.is_current.is_(True),
        )
        embeddings_by_chunk = {
            row.chunk_id: row
            for row in (
                await db.execute(
                    select(
                        ChunkEmbedding.id,
                        ChunkEmbedding.chunk_id,
                        ChunkEmbedding.content_hash,
                    ).where(
                        ChunkEmbedding.profile_id == profile.id,
                        ChunkEmbedding.chunk_id.in_(chunk_ids),
                    )
                )
            ).all()
        }
        embedding_jobs = list(
            (
                await db.execute(
                    select(
                        ProcessingJob.id,
                        ProcessingJob.embedding_chunk_id,
                        ProcessingJob.status,
                    )
                    .where(
                        ProcessingJob.stage == "embedding",
                        ProcessingJob.embedding_profile_id == profile.id,
                        ProcessingJob.embedding_chunk_id.in_(chunk_ids),
                    )
                    .order_by(ProcessingJob.id.desc())
                )
            ).all()
        )
        for job in embedding_jobs:
            if job.embedding_chunk_id is not None:
                embedding_jobs_by_chunk.setdefault(job.embedding_chunk_id, job)

    return (
        versions,
        chunks_by_version,
        latest_jobs,
        profile,
        embeddings_by_chunk,
        embedding_jobs_by_chunk,
    )


async def load_pipeline_statuses(
    db: AsyncSession, version_ids: list[int], *, include_extraction: bool = True
) -> dict[int, dict]:
    """Return pipeline status for many versions with a bounded query count."""

    (
        versions,
        chunks_by_version,
        latest_jobs,
        profile,
        embeddings_by_chunk,
        embedding_jobs_by_chunk,
    ) = await _load_pipeline_inputs(
        db, version_ids, include_extraction=include_extraction
    )

    output: dict[int, dict] = {}
    for version_id, version in versions.items():
        all_chunks = chunks_by_version.get(version_id, [])
        spreadsheet = (version.meta or {}).get("spreadsheet_processing") or {}
        governance_only = bool(
            isinstance(spreadsheet, dict)
            and spreadsheet.get("mode") in {"governance", "empty"}
        )
        expected = _expected_chunks(version, all_chunks)
        jobs = latest_jobs.get(version_id, {})

        parsing = _stage_payload(jobs.get("parsing"))
        if (
            jobs.get("parsing") is None
            and version.processing_status in {"ready", "unsupported"}
            and (version.raw_content or version.structured_content)
        ):
            parsing = {
                "status": "completed",
                "message": "已提取正文",
                "last_error": None,
            }

        pdf_extraction = _pdf_extraction_summary(version)
        if pdf_extraction is not None:
            parsing = {**parsing, "extraction": pdf_extraction}

        chunking = _stage_payload(jobs.get("chunking"))
        if governance_only and getattr(jobs.get("chunking"), "status", None) == "completed":
            chunking = {
                "status": "skipped",
                "message": "表格结构需要治理，未生成知识切片",
                "last_error": None,
            }
        elif all_chunks:
            chunking = {
                "status": "completed",
                "message": "已生成知识切片",
                "last_error": None,
            }

        understanding = _stage_payload(jobs.get("understanding"))
        ai_status = (version.meta or {}).get("ai_status")
        if ai_status in {"completed", "not_configured", "deterministic"}:
            understanding = {
                "status": "completed" if ai_status == "completed" else "skipped",
                "message": (
                    "AI 整理已完成"
                    if ai_status == "completed"
                    else "结构化数据已按目录规则整理"
                    if ai_status == "deterministic"
                    else "未配置对话模型，已按默认规则整理"
                ),
                "last_error": None,
            }
        elif jobs.get("understanding") is None and all_chunks:
            understanding = {
                "status": "skipped",
                "message": "历史资料无 AI 整理任务记录",
                "last_error": None,
            }

        completed = sum(
            1
            for chunk in expected
            if (embedding := embeddings_by_chunk.get(chunk.id)) is not None
            and embedding.content_hash == chunk.content_hash
        )
        expected_ids = {chunk.id for chunk in expected}
        expected_jobs = [
            embedding_jobs_by_chunk[chunk_id]
            for chunk_id in expected_ids
            if chunk_id in embedding_jobs_by_chunk
        ]
        failed = sum(job.status == "failed" for job in expected_jobs)
        running = sum(job.status in RUNNING_JOB_STATUSES for job in expected_jobs)
        total = len(expected)
        missing = max(total - completed, 0)

        if governance_only:
            embedding_stage = {
                "status": "skipped",
                "message": "表格结构需要治理，未生成向量",
                "profile_id": profile.id if profile is not None else None,
                "model": profile.model if profile is not None else None,
                "completed": 0,
                "total": 0,
                "missing": 0,
                "failed": 0,
            }
        elif profile is None:
            embedding_stage = {
                "status": "disabled",
                "message": "未启用向量索引，仍可使用关键词检索",
                "profile_id": None,
                "model": None,
                "completed": 0,
                "total": total,
                "missing": total,
                "failed": 0,
            }
        elif failed:
            embedding_stage = {
                "status": "failed",
                "message": f"{failed} 个向量任务失败",
                "profile_id": profile.id,
                "model": profile.model,
                "completed": completed,
                "total": total,
                "missing": missing,
                "failed": failed,
            }
        elif total > 0 and missing == 0:
            embedding_stage = {
                "status": "completed",
                "message": "向量已完成，可进行语义检索",
                "profile_id": profile.id,
                "model": profile.model,
                "completed": completed,
                "total": total,
                "missing": 0,
                "failed": 0,
            }
        else:
            embedding_stage = {
                "status": "processing" if running else "pending",
                "message": (
                    "正在生成向量"
                    if running
                    else "存在缺失向量，可进行补建"
                    if total
                    else "等待切片完成后生成向量"
                ),
                "profile_id": profile.id,
                "model": profile.model,
                "completed": completed,
                "total": total,
                "missing": missing,
                "failed": 0,
            }

        statuses = [
            parsing["status"],
            understanding["status"],
            chunking["status"],
            embedding_stage["status"],
        ]
        # The version status is the authoritative ingestion outcome. A stale
        # running stage record must not mask a terminal version failure (this
        # can happen when a worker crashes after marking the version failed).
        if version.processing_status == "failed" or "failed" in statuses:
            overall = "failed"
        elif any(
            status in RUNNING_JOB_STATUSES or status == "pending" for status in statuses
        ):
            overall = "processing"
        else:
            overall = "completed"

        output[version_id] = {
            "document_id": version.document_id,
            "document_version_id": version_id,
            "overall_status": overall,
            "keyword_searchable": bool(all_chunks) and not governance_only,
            "vector_searchable": (
                total > 0
                and missing == 0
                and profile is not None
                and not governance_only
            ),
            "stages": {
                "parsing": parsing,
                "understanding": understanding,
                "chunking": {
                    **chunking,
                    "child_chunks": 0 if governance_only else len(all_chunks),
                },
                "embedding": embedding_stage,
            },
        }
    return output


async def compute_processing_status(db: AsyncSession, document_id: int) -> dict:
    document = await db.get(Document, document_id)
    if document is None or document.is_deleted:
        raise ProcessingStatusError("资料不存在")
    if document.current_version_id is None:
        raise ProcessingStatusError("资料没有当前版本")
    statuses = await load_pipeline_statuses(db, [document.current_version_id])
    try:
        return statuses[document.current_version_id]
    except KeyError as exc:
        raise ProcessingStatusError("当前版本不存在") from exc


async def repair_document_vectors(
    db: AsyncSession, document_ids: list[int]
) -> VectorRepairResult:
    """Queue only missing or stale vectors for the active profile."""

    requested_ids = list(dict.fromkeys(document_ids))
    profile = await _active_profile(db)
    if profile is None:
        raise NoActiveEmbeddingProfile("尚未启用向量模型，无法补建向量")

    documents = list(
        (
            await db.execute(
                select(Document).where(
                    Document.id.in_(requested_ids),
                    Document.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    version_ids = [
        document.current_version_id
        for document in documents
        if document.current_version_id is not None
    ]
    versions = (
        {
            version.id: version
            for version in (
                await db.execute(
                    select(DocumentVersion).where(DocumentVersion.id.in_(version_ids))
                )
            )
            .scalars()
            .all()
        }
        if version_ids
        else {}
    )
    chunks_by_version: dict[int, list[DocumentChunk]] = defaultdict(list)
    chunks = (
        list(
            (
                await db.execute(
                    select(DocumentChunk)
                    .where(
                        DocumentChunk.document_version_id.in_(version_ids),
                        DocumentChunk.role == "child",
                        DocumentChunk.is_current.is_(True),
                    )
                    .order_by(DocumentChunk.document_version_id, DocumentChunk.id)
                )
            )
            .scalars()
            .all()
        )
        if version_ids
        else []
    )
    for chunk in chunks:
        chunks_by_version[chunk.document_version_id].append(chunk)

    expected: list[DocumentChunk] = []
    eligible_documents = 0
    for version_id, version in versions.items():
        selected = _expected_chunks(version, chunks_by_version.get(version_id, []))
        if selected:
            eligible_documents += 1
            expected.extend(selected)

    chunk_ids = [chunk.id for chunk in expected]
    embeddings = (
        {
            row.chunk_id: row
            for row in (
                await db.execute(
                    select(ChunkEmbedding).where(
                        ChunkEmbedding.profile_id == profile.id,
                        ChunkEmbedding.chunk_id.in_(chunk_ids),
                    )
                )
            )
            .scalars()
            .all()
        }
        if chunk_ids
        else {}
    )
    jobs: dict[int, ProcessingJob] = {}
    if chunk_ids:
        job_rows = list(
            (
                await db.execute(
                    select(ProcessingJob)
                    .where(
                        ProcessingJob.stage == "embedding",
                        ProcessingJob.embedding_profile_id == profile.id,
                        ProcessingJob.embedding_chunk_id.in_(chunk_ids),
                    )
                    .order_by(ProcessingJob.id.desc())
                )
            )
            .scalars()
            .all()
        )
        for job in job_rows:
            if job.embedding_chunk_id is not None:
                jobs.setdefault(job.embedding_chunk_id, job)

    already_fresh = enqueued = reset = skipped = 0
    for chunk in expected:
        embedding = embeddings.get(chunk.id)
        if embedding is not None and embedding.content_hash == chunk.content_hash:
            already_fresh += 1
            continue
        job = jobs.get(chunk.id)
        if job is None:
            db.add(_enqueue_embedding_job(profile=profile, chunk=chunk))
            enqueued += 1
            continue
        if job.status in RUNNING_JOB_STATUSES:
            skipped += 1
            continue
        job.status = "created"
        job.retry_count = 0
        job.next_retry_at = None
        job.last_error = None
        job.error_details = None
        job.started_at = None
        job.finished_at = None
        db.add(job)
        reset += 1

    await db.commit()
    return VectorRepairResult(
        documents_requested=len(requested_ids),
        documents_eligible=eligible_documents,
        chunks_expected=len(expected),
        already_fresh=already_fresh,
        enqueued=enqueued,
        reset=reset,
        skipped=skipped,
    )


async def repair_active_vector(
    db: AsyncSession, document_id: int
) -> VectorRepairResult:
    """Compatibility wrapper for the original single-document service API."""

    return await repair_document_vectors(db, [document_id])
