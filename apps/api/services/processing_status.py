"""Unified ingestion status and targeted vector repair."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass

from sqlalchemy import select
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


async def _active_profile(db: AsyncSession) -> EmbeddingProfile | None:
    config = (
        await db.execute(
            select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc()).limit(1)
        )
    ).scalars().first()
    if config is None or config.active_embedding_profile_id is None:
        return None
    return await db.get(EmbeddingProfile, config.active_embedding_profile_id)


async def _load_pipeline_inputs(
    db: AsyncSession, version_ids: list[int]
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

    versions = {
        version.id: version
        for version in (
            await db.execute(
                select(DocumentVersion).where(DocumentVersion.id.in_(unique_ids))
            )
        ).scalars().all()
    }
    chunks_by_version: dict[int, list[DocumentChunk]] = defaultdict(list)
    chunks = list(
        (
            await db.execute(
                select(DocumentChunk)
                .where(
                    DocumentChunk.document_version_id.in_(unique_ids),
                    DocumentChunk.role == "child",
                    DocumentChunk.is_current.is_(True),
                )
                .order_by(DocumentChunk.document_version_id, DocumentChunk.id)
            )
        ).scalars().all()
    )
    for chunk in chunks:
        chunks_by_version[chunk.document_version_id].append(chunk)

    latest_jobs: dict[int, dict[str, ProcessingJob]] = defaultdict(dict)
    stage_jobs = list(
        (
            await db.execute(
                select(ProcessingJob)
                .where(
                    ProcessingJob.document_version_id.in_(unique_ids),
                    ProcessingJob.stage.in_(PIPELINE_STAGES),
                )
                .order_by(ProcessingJob.id.desc())
            )
        ).scalars().all()
    )
    for job in stage_jobs:
        latest_jobs[job.document_version_id].setdefault(job.stage, job)

    profile = await _active_profile(db)
    embeddings_by_chunk: dict[int, ChunkEmbedding] = {}
    embedding_jobs_by_chunk: dict[int, ProcessingJob] = {}
    if profile is not None and chunks:
        chunk_ids = [chunk.id for chunk in chunks]
        embeddings_by_chunk = {
            row.chunk_id: row
            for row in (
                await db.execute(
                    select(ChunkEmbedding).where(
                        ChunkEmbedding.profile_id == profile.id,
                        ChunkEmbedding.chunk_id.in_(chunk_ids),
                    )
                )
            ).scalars().all()
        }
        embedding_jobs = list(
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
            ).scalars().all()
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
    db: AsyncSession, version_ids: list[int]
) -> dict[int, dict]:
    """Return pipeline status for many versions with a bounded query count."""

    (
        versions,
        chunks_by_version,
        latest_jobs,
        profile,
        embeddings_by_chunk,
        embedding_jobs_by_chunk,
    ) = await _load_pipeline_inputs(db, version_ids)

    output: dict[int, dict] = {}
    for version_id, version in versions.items():
        all_chunks = chunks_by_version.get(version_id, [])
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

        chunking = _stage_payload(jobs.get("chunking"))
        if all_chunks:
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
            job
            for chunk_id, job in embedding_jobs_by_chunk.items()
            if chunk_id in expected_ids
        ]
        failed = sum(job.status == "failed" for job in expected_jobs)
        running = sum(job.status in RUNNING_JOB_STATUSES for job in expected_jobs)
        total = len(expected)
        missing = max(total - completed, 0)

        if profile is None:
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
        if "failed" in statuses:
            overall = "failed"
        elif any(
            status in RUNNING_JOB_STATUSES or status == "pending"
            for status in statuses
        ):
            overall = "processing"
        else:
            overall = "completed"

        output[version_id] = {
            "document_id": version.document_id,
            "document_version_id": version_id,
            "overall_status": overall,
            "keyword_searchable": bool(all_chunks),
            "vector_searchable": total > 0 and missing == 0 and profile is not None,
            "stages": {
                "parsing": parsing,
                "understanding": understanding,
                "chunking": {**chunking, "child_chunks": len(all_chunks)},
                "embedding": embedding_stage,
            },
        }
    return output


async def compute_processing_status(
    db: AsyncSession, document_id: int
) -> dict:
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
        ).scalars().all()
    )
    version_ids = [
        document.current_version_id
        for document in documents
        if document.current_version_id is not None
    ]
    versions = {
        version.id: version
        for version in (
            await db.execute(
                select(DocumentVersion).where(DocumentVersion.id.in_(version_ids))
            )
        ).scalars().all()
    } if version_ids else {}
    chunks_by_version: dict[int, list[DocumentChunk]] = defaultdict(list)
    chunks = list(
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
        ).scalars().all()
    ) if version_ids else []
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
    embeddings = {
        row.chunk_id: row
        for row in (
            await db.execute(
                select(ChunkEmbedding).where(
                    ChunkEmbedding.profile_id == profile.id,
                    ChunkEmbedding.chunk_id.in_(chunk_ids),
                )
            )
        ).scalars().all()
    } if chunk_ids else {}
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
            ).scalars().all()
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
