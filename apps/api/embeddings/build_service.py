"""Build / retry / activate / rollback service for embedding profiles (ADR-015).

This module is the M3-6b phase 2 counterpart to
:mod:`apps.api.embeddings.service`. Phase 1 only knew how to test
a candidate configuration; phase 2 is what actually builds the
index, swaps the active profile in, and rolls it back when the
operator changes their mind.

The module is split out from ``service.py`` on purpose: ``service``
already owns the canary probe, the compatibility judgement, the
secret resolution, and the candidate fingerprinting. The build
pipeline does very different things — it iterates the corpus,
materialises ``processing_jobs`` rows, drives the state machine,
and coordinates with the worker through the
``embedding_profile_id`` foreign key introduced in migration 0009.
Mixing them in the same file would inflate ``service.py`` past
~1000 lines and obscure the state machine.

State machine summary
=====================

* ``tested`` → ``building`` via :func:`start_build`
* ``building`` → ``ready`` (computed when all child chunks have
  embeddings AND zero failures) or → ``failed`` (terminal
  when an unrecoverable error is recorded; ``retry`` resets
  the failed jobs and resumes)
* ``ready`` / ``retired`` → ``active`` via :func:`activate_profile`
* ``active`` → ``retired`` (the previous active profile is
  demoted by ``activate_profile``)

Public surface
==============

The module exposes four async functions the API layer calls:

* :func:`start_build` — promote a ``tested`` or ``failed`` profile
  to ``building`` and enqueue one ``processing_jobs`` row per
  current child chunk.
* :func:`retry_profile` — reset the failed jobs of a profile and
  mark it ``building`` again. The candidate must currently be in
  ``building`` or ``failed``; ``ready`` / ``active`` / ``retired``
  profiles are not retry targets.
* :func:`activate_profile` — swap a ``ready`` profile to
  ``active``, demoting the current active to ``retired``. The
  function takes a row-level lock on the ``ai_runtime_configs``
  singleton so two concurrent activations cannot race.
* :func:`rollback_profile` — same machinery as
  :func:`activate_profile`, but the source profile must already
  be ``ready`` or ``retired``. The semantic is "swap the active
  pointer back to this historical profile" rather than "promote
  a fresh build".

The module also exposes :func:`enqueue_embedding_jobs_for_chunk`
so the chunking stage can auto-enqueue work for every active
or building profile when a new chunk is materialised.

Why we lock ``ai_runtime_configs`` for the activation
=====================================================

Two operators clicking "activate" simultaneously must not both
succeed. PostgreSQL serialises the work cleanly with
``SELECT ... FOR UPDATE``; SQLite (used by tests) does not honour
that syntax, so we additionally use the ``with_for_update`` flag
plus a database-agnostic application-level check that the
profile we are about to activate is still in ``ready`` and
matches the corpus at the time of the swap. The tests confirm
this contract.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.auth import AIRuntimeConfig
from ..models.chunks import DocumentChunk
from ..models.documents import DocumentVersion
from ..models.embedding_profiles import ChunkEmbedding, EmbeddingProfile
from ..models.processing import ProcessingJob
from .sampling import evenly_sample_chunks

logger = logging.getLogger(__name__)


# Status constants used by the state machine. They are also
# defined on :class:`EmbeddingProfile` (``READY_STATUSES``) but
# we keep separate module-level constants so the service can
# reference them without going through the ORM model.
BUILDABLE_STATUSES = ("tested", "failed")
RETRYABLE_STATUSES = ("building", "failed")
ACTIVATABLE_STATUSES = ("ready", "retired", "active")

# Idempotency tag for the ``embedding`` stage. The build service
# uses ``{profile_id}:{chunk_id}:embedding:{config_fingerprint}``
# so the same chunk can be re-embedded for a new profile without
# colliding with the old profile's jobs.
EMBEDDING_STAGE = "embedding"
EMBEDDING_IDEMPOTENCY_PREFIX = "embedding:v1"


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _idempotency_key(profile_id: int, chunk_id: int, config_fingerprint: str) -> str:
    return f"{profile_id}:{chunk_id}:{EMBEDDING_STAGE}:{EMBEDDING_IDEMPOTENCY_PREFIX}:{config_fingerprint}"


# --- Errors ---------------------------------------------------------------


class BuildServiceError(Exception):
    """Base error for the build service. Carries an HTTP-friendly message."""

    code: str = "embeddings_error"

    def __init__(self, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status


class ProfileNotFound(BuildServiceError):
    code = "profile_not_found"

    def __init__(self, profile_id: int) -> None:
        super().__init__(f"embedding profile {profile_id} 不存在", http_status=404)
        self.profile_id = profile_id


class ProfileNotBuildable(BuildServiceError):
    code = "profile_not_buildable"


class ProfileNotReady(BuildServiceError):
    code = "profile_not_ready"


class ProfileNotActivatable(BuildServiceError):
    code = "profile_not_activatable"


class ConfigLocked(BuildServiceError):
    code = "config_locked"


# --- Result dataclasses ---------------------------------------------------


@dataclass(frozen=True)
class BuildResult:
    """The public view of a build or retry operation."""

    profile_id: int
    status: str
    enqueued: int
    skipped: int
    total_chunks: int
    completed_chunks: int
    failed_chunks: int
    started_at: str
    finished_at: str | None

    def to_public_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "status": self.status,
            "enqueued": self.enqueued,
            "skipped": self.skipped,
            "total_chunks": self.total_chunks,
            "completed_chunks": self.completed_chunks,
            "failed_chunks": self.failed_chunks,
            "build_started_at": self.started_at,
            "build_finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class ActivationResult:
    """The public view of an activate or rollback operation."""

    active_profile_id: int
    previous_profile_id: int | None
    profile_status: str
    activated_at: str
    config_fingerprint: str

    def to_public_dict(self) -> dict:
        return {
            "active_profile_id": self.active_profile_id,
            "previous_profile_id": self.previous_profile_id,
            "profile_status": self.profile_status,
            "activated_at": self.activated_at,
            "config_fingerprint": self.config_fingerprint,
        }


@dataclass(frozen=True)
class ProfileSummary:
    """Status view of a profile returned by :func:`get_profile_summaries`."""

    profile_id: int
    status: str
    provider: str
    base_url: str
    model: str
    dim: int
    config_fingerprint: str
    total_chunks: int | None
    completed_chunks: int | None
    failed_chunks: int | None
    build_started_at: str | None
    build_finished_at: str | None
    activated_at: str | None
    is_active: bool
    pending_jobs: int
    processing_jobs: int
    failure_reasons: tuple[tuple[str, int], ...]
    last_error: str | None
    available_actions: tuple[str, ...]

    def to_public_dict(self) -> dict:
        return {
            "id": self.profile_id,
            "status": self.status,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "dim": self.dim,
            "config_fingerprint": self.config_fingerprint,
            "total_chunks": self.total_chunks,
            "completed_chunks": self.completed_chunks,
            "failed_chunks": self.failed_chunks,
            "build_started_at": self.build_started_at,
            "build_finished_at": self.build_finished_at,
            "activated_at": self.activated_at,
            "is_active": self.is_active,
            "pending_jobs": self.pending_jobs,
            "processing_jobs": self.processing_jobs,
            "failure_reasons": [
                {"message": message, "count": count}
                for message, count in self.failure_reasons
            ],
            "last_error": self.last_error,
            "available_actions": list(self.available_actions),
        }


# --- Helpers --------------------------------------------------------------


def _available_actions(
    status: str,
    is_active: bool,
    *,
    processing_jobs: int = 0,
) -> tuple[str, ...]:
    """Return the set of POST endpoints the operator may call next."""

    actions: list[str] = []
    if status in BUILDABLE_STATUSES:
        actions.append("build")
    if status in RETRYABLE_STATUSES:
        actions.append("retry")
    if status == "ready" and not is_active:
        actions.append("activate")
    if status == "retired" and not is_active:
        actions.append("rollback")
    # Queued/retry jobs can be cancelled by deleting their profile. A job
    # already executing must finish first so the worker never writes against
    # a profile that disappeared underneath it.
    if not is_active and processing_jobs == 0:
        actions.append("delete")
    return tuple(actions)


async def _load_profile(
    db: AsyncSession, profile_id: int, *, with_for_update: bool = False
) -> EmbeddingProfile:
    stmt = select(EmbeddingProfile).where(EmbeddingProfile.id == profile_id)
    if with_for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalars().first()
    if row is None:
        raise ProfileNotFound(profile_id)
    return row


async def _current_child_chunks(db: AsyncSession) -> list[DocumentChunk]:
    """Return every child chunk that should be embedded.

    The build only cares about chunks that are currently visible to
    retrieval: ``role == 'child'`` and ``is_current`` is true. The
    worker will skip chunks whose ``content_hash`` already matches a
    stored ``chunk_embeddings`` row, so re-running the build on a
    fresh profile is safe.
    """

    rows = (
        await db.execute(
            select(DocumentChunk, DocumentVersion.meta)
            .join(
                DocumentVersion,
                DocumentVersion.id == DocumentChunk.document_version_id,
            )
            .where(
                and_(
                    DocumentChunk.role == "child",
                    DocumentChunk.is_current.is_(True),
                )
            )
            .order_by(DocumentChunk.id)
        )
    ).all()
    grouped: dict[int, list[DocumentChunk]] = defaultdict(list)
    strategies: dict[int, dict] = {}
    for chunk, version_meta in rows:
        grouped[chunk.document_version_id].append(chunk)
        strategy = (version_meta or {}).get("embedding_strategy") or {}
        strategies[chunk.document_version_id] = (
            strategy if isinstance(strategy, dict) else {}
        )
    selected: list[DocumentChunk] = []
    for version_id, chunks in grouped.items():
        strategy = strategies.get(version_id) or {}
        if strategy.get("mode") == "sampled":
            sample_size = int(strategy.get("selected_chunks") or 0)
            selected.extend(evenly_sample_chunks(chunks, sample_size))
        else:
            selected.extend(chunks)
    return selected


async def _load_active_config(
    db: AsyncSession, *, with_for_update: bool = False
) -> AIRuntimeConfig | None:
    stmt = select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc()).limit(1)
    if with_for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalars().first()


# --- Build / Retry --------------------------------------------------------


def _enqueue_embedding_job(
    *,
    profile: EmbeddingProfile,
    chunk: DocumentChunk,
) -> ProcessingJob:
    """Build the ``processing_jobs`` row that will embed ``chunk``.

    The job is intentionally per-chunk (not per-profile) so the
    worker can claim and retry one chunk at a time. The
    ``document_id`` / ``document_version_id`` columns are populated
    with the chunk's own values so the existing worker pipeline
    can still call ``session.get(DocumentVersion, ...)`` when it
    needs to record a failure against the version.
    """

    return ProcessingJob(
        document_id=chunk.document_id,
        document_version_id=chunk.document_version_id,
        stage=EMBEDDING_STAGE,
        status="created",
        idempotency_key=_idempotency_key(
            profile.id, chunk.id, profile.config_fingerprint
        ),
        config_version=profile.config_fingerprint,
        embedding_profile_id=profile.id,
        embedding_chunk_id=chunk.id,
    )


async def _enqueue_jobs_for_chunks(
    db: AsyncSession,
    *,
    profile: EmbeddingProfile,
    chunks: Iterable[DocumentChunk],
) -> tuple[int, int]:
    """Insert one ``processing_jobs`` row per chunk, idempotently.

    Returns ``(enqueued, skipped)``. ``skipped`` counts jobs that
    already exist for the (profile, chunk) pair, whether in
    completed or any other state. The worker is responsible for
    re-embedding if the chunk's ``content_hash`` changed; the
    service never deletes a previous job.
    """

    enqueued = 0
    skipped = 0
    for chunk in chunks:
        key = _idempotency_key(profile.id, chunk.id, profile.config_fingerprint)
        existing = await db.execute(
            select(ProcessingJob).where(ProcessingJob.idempotency_key == key)
        )
        existing_row = existing.scalars().first()
        if existing_row is not None:
            # If a previous run failed permanently, the operator
            # uses the retry endpoint to reset; building again
            # would not change that.
            skipped += 1
            continue
        db.add(
            _enqueue_embedding_job(profile=profile, chunk=chunk)
        )
        enqueued += 1
    return enqueued, skipped


async def start_build(
    db: AsyncSession, profile_id: int
) -> BuildResult:
    """Enqueue a fresh build for ``profile_id``.

    Allowed source statuses: ``tested`` and ``failed``. A
    ``building`` profile can be re-fed by calling
    :func:`retry_profile` instead, which keeps the
    ``build_started_at`` audit trail intact.
    """

    profile = await _load_profile(db, profile_id)
    if profile.status not in BUILDABLE_STATUSES:
        raise ProfileNotBuildable(
            f"profile {profile_id} 当前状态 {profile.status} 不允许构建"
        )

    chunks = await _current_child_chunks(db)
    if not chunks:
        # An empty corpus is a legitimate, no-op build: the
        # profile is trivially ``ready`` without needing the
        # worker. We still stamp the timestamps so the audit
        # trail is consistent.
        now = utc_now()
        profile.status = "ready"
        profile.total_chunks = 0
        profile.completed_chunks = 0
        profile.failed_chunks = 0
        profile.build_started_at = now
        profile.build_finished_at = now
        profile.activated_at = None
        profile.last_error = None
        await db.commit()
        await db.refresh(profile)
        return BuildResult(
            profile_id=profile.id,
            status=profile.status,
            enqueued=0,
            skipped=0,
            total_chunks=0,
            completed_chunks=0,
            failed_chunks=0,
            started_at=profile.build_started_at.isoformat(),
            finished_at=profile.build_finished_at.isoformat(),
        )

    total = len(chunks)
    profile.total_chunks = total
    profile.completed_chunks = 0
    profile.failed_chunks = 0
    profile.build_started_at = utc_now()
    profile.build_finished_at = None
    profile.activated_at = None
    profile.last_error = None
    profile.status = "building"
    enqueued, skipped = await _enqueue_jobs_for_chunks(
        db, profile=profile, chunks=chunks
    )
    await db.commit()
    await db.refresh(profile)
    logger.info(
        "embedding_build_started profile=%s total=%s enqueued=%s skipped=%s",
        profile.id,
        total,
        enqueued,
        skipped,
    )
    return BuildResult(
        profile_id=profile.id,
        status=profile.status,
        enqueued=enqueued,
        skipped=skipped,
        total_chunks=total,
        completed_chunks=0,
        failed_chunks=0,
        started_at=(
            profile.build_started_at.isoformat() if profile.build_started_at else ""
        ),
        finished_at=None,
    )


async def retry_profile(
    db: AsyncSession, profile_id: int
) -> BuildResult:
    """Reset failed embedding jobs for ``profile_id`` and resume.

    Only the jobs that ended in ``failed`` are touched; completed
    jobs are left alone. The counters on the profile row are
    decremented by the number of jobs we are about to retry, so
    the progress view stays consistent as the worker re-runs them.
    """

    profile = await _load_profile(db, profile_id)
    if profile.status not in RETRYABLE_STATUSES:
        raise ProfileNotBuildable(
            f"profile {profile_id} 当前状态 {profile.status} 不允许重试"
        )

    failed_jobs = list(
        (
            await db.execute(
                select(ProcessingJob).where(
                    and_(
                        ProcessingJob.embedding_profile_id == profile.id,
                        ProcessingJob.stage == EMBEDDING_STAGE,
                        ProcessingJob.status == "failed",
                    )
                )
            )
        ).scalars().all()
    )

    reset = 0
    for job in failed_jobs:
        job.status = "created"
        job.retry_count = 0
        job.next_retry_at = None
        job.last_error = None
        job.error_details = None
        job.started_at = None
        job.finished_at = None
        db.add(job)
        reset += 1

    if profile.failed_chunks is not None and reset:
        profile.failed_chunks = max(profile.failed_chunks - reset, 0)
    if profile.total_chunks is not None and profile.completed_chunks is not None:
        # The build is back to "in progress".
        profile.build_finished_at = None
    if profile.status == "failed":
        profile.status = "building"
    profile.last_error = None
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    logger.info(
        "embedding_build_retried profile=%s reset=%s", profile.id, reset
    )
    return BuildResult(
        profile_id=profile.id,
        status=profile.status,
        enqueued=reset,
        skipped=0,
        total_chunks=profile.total_chunks or 0,
        completed_chunks=profile.completed_chunks or 0,
        failed_chunks=profile.failed_chunks or 0,
        started_at=(
            profile.build_started_at.isoformat()
            if profile.build_started_at
            else ""
        ),
        finished_at=(
            profile.build_finished_at.isoformat()
            if profile.build_finished_at
            else None
        ),
    )


# --- Activate / Rollback --------------------------------------------------


async def _verify_profile_is_complete(
    db: AsyncSession, profile: EmbeddingProfile
) -> tuple[bool, int, int]:
    """Confirm a profile covers the live corpus with no failed jobs.

    Returns ``(ready, current_child_count, missing_count)``.

    The "current coverage" is the count of child chunks with
    ``is_current=True``. The profile must have a
    ``chunk_embeddings`` row for every one of them, and the row
    must store a matching ``content_hash`` so a chunk whose
    content was edited after the build does not silently serve
    a stale vector.
    """

    current_chunks = await _current_child_chunks(db)
    current = len(current_chunks)
    if current == 0:
        return True, 0, 0

    chunk_id_to_hash = {row.id: row.content_hash for row in current_chunks}
    stored = list(
        (
            await db.execute(
                select(ChunkEmbedding).where(ChunkEmbedding.profile_id == profile.id)
            )
        ).scalars().all()
    )
    stored_by_chunk: dict[int, ChunkEmbedding] = {row.chunk_id: row for row in stored}
    missing = 0
    for chunk_id, content_hash in chunk_id_to_hash.items():
        row = stored_by_chunk.get(chunk_id)
        if row is None or row.content_hash != content_hash:
            missing += 1
    return missing == 0, current, missing


async def _recompute_profile_counters(
    db: AsyncSession, profile: EmbeddingProfile
) -> None:
    """Recompute the profile's build counters against the live eligible corpus.

    The eligible corpus is :func:`_current_child_chunks`, which
    already honours per-version ``sampled`` strategies.
    ``completed_chunks`` counts eligible chunks with a matching
    stored embedding; ``failed_chunks`` is recomputed from the
    profile's live ``processing_jobs`` rows so a previous run
    that ended in a couple of failures is not silently wiped
    out by an activation-time counter refresh. ``total_chunks``
    is the count of eligible chunks the profile is supposed to
    cover.

    The function does not commit; the caller's transaction
    controls durability.
    """

    current = await _current_child_chunks(db)
    total = len(current)
    if total == 0:
        profile.total_chunks = 0
        profile.completed_chunks = 0
        profile.failed_chunks = 0
        return
    chunk_id_to_hash = {row.id: row.content_hash for row in current}
    stored = list(
        (
            await db.execute(
                select(ChunkEmbedding).where(
                    ChunkEmbedding.profile_id == profile.id
                )
            )
        ).scalars().all()
    )
    stored_by_chunk = {row.chunk_id: row for row in stored}
    completed = sum(
        1
        for chunk_id, content_hash in chunk_id_to_hash.items()
        if stored_by_chunk.get(chunk_id) is not None
        and stored_by_chunk[chunk_id].content_hash == content_hash
    )
    failed = list(
        (
            await db.execute(
                select(ProcessingJob).where(
                    and_(
                        ProcessingJob.embedding_profile_id == profile.id,
                        ProcessingJob.stage == EMBEDDING_STAGE,
                        ProcessingJob.status == "failed",
                        ProcessingJob.embedding_chunk_id.in_(
                            list(chunk_id_to_hash)
                        ),
                    )
                )
            )
        ).scalars().all()
    )
    profile.total_chunks = total
    profile.completed_chunks = completed
    profile.failed_chunks = len(failed)


async def _backfill_missing_jobs(
    db: AsyncSession, profile: EmbeddingProfile
) -> tuple[int, int]:
    """Idempotently enqueue embedding jobs for eligible chunks that are
    missing or whose ``content_hash`` is stale.

    The function is the activation-time self-heal for two
    distinct failure modes:

    * A chunk exists that the profile has never embedded:
      enqueue a fresh job.
    * A chunk's ``content_hash`` rotated after the previous
      build: the profile's ``chunk_embeddings`` row is stale,
      and a previously-completed job would otherwise sit
      forever with a vector that no longer matches the live
      chunk. The existing job is reset to ``created`` so the
      worker will re-execute it; jobs already in
      ``created``/``retry``/``processing`` are left alone to
      avoid an unnecessary duplicate. A permanently ``failed``
      job is also reset to ``created`` so the new content hash
      gets a real second chance.

    Returns ``(enqueued, skipped)``. Re-running this on a
    profile whose missing jobs already exist is a no-op, so
    repeated activation attempts never duplicate work.

    The function does not commit: the caller's transaction
    controls durability. ``activate_profile`` deliberately
    commits before raising so the operator-visible "已自动补建"
    message reflects a real state change.
    """

    current_chunks = await _current_child_chunks(db)
    if not current_chunks:
        return 0, 0
    chunk_id_to_hash = {row.id: row.content_hash for row in current_chunks}
    stored = list(
        (
            await db.execute(
                select(ChunkEmbedding).where(
                    ChunkEmbedding.profile_id == profile.id
                )
            )
        ).scalars().all()
    )
    stored_by_chunk = {row.chunk_id: row for row in stored}

    # Locate any jobs the profile has already issued for these
    # chunks. We will reset jobs whose stored embedding (if
    # any) no longer matches the live ``content_hash``; jobs
    # still in flight are skipped.
    chunk_ids = list(chunk_id_to_hash.keys())
    existing_jobs: list[ProcessingJob] = list(
        (
            await db.execute(
                select(ProcessingJob).where(
                    and_(
                        ProcessingJob.embedding_profile_id == profile.id,
                        ProcessingJob.stage == EMBEDDING_STAGE,
                        ProcessingJob.embedding_chunk_id.in_(chunk_ids),
                    )
                )
            )
        ).scalars().all()
    )
    job_by_chunk: dict[int, ProcessingJob] = {
        job.embedding_chunk_id: job
        for job in existing_jobs
        if job.embedding_chunk_id is not None
    }

    enqueued = 0
    skipped = 0
    for chunk in current_chunks:
        stored_row = stored_by_chunk.get(chunk.id)
        stored_hash = stored_row.content_hash if stored_row is not None else None
        hash_stale = stored_row is None or stored_hash != chunk.content_hash
        if not hash_stale:
            # Chunk already has a matching embedding and we
            # have no work to do for it.
            continue
        existing_job = job_by_chunk.get(chunk.id)
        if existing_job is None:
            db.add(
                _enqueue_embedding_job(profile=profile, chunk=chunk)
            )
            enqueued += 1
            continue
        if existing_job.status in ("created", "retry", "processing"):
            # A worker (or the previous backfill) is already
            # responsible for this chunk; do not create a
            # duplicate and do not perturb its state.
            skipped += 1
            continue
        # The job has terminated (``completed`` / ``failed``)
        # but the vector is no longer current. Reset it so the
        # worker re-runs with the new content hash.
        _reset_processing_job(existing_job)
        db.add(existing_job)
        enqueued += 1
    return enqueued, skipped


def _reset_processing_job(job: ProcessingJob) -> None:
    """Reset a ``processing_jobs`` row back to the ``created`` state.

    Mirrors the helper in
    :mod:`apps.worker.services.processor` so the API path can
    reuse the same bookkeeping. The caller is responsible for
    adding the row to its session and committing.
    """

    job.status = "created"
    job.retry_count = 0
    job.next_retry_at = None
    job.last_error = None
    job.error_details = None
    job.started_at = None
    job.finished_at = None


async def activate_profile(
    db: AsyncSession, profile_id: int
) -> ActivationResult:
    """Swap ``profile_id`` to ``active`` and demote the previous one.

    The function takes a row-level lock on the
    ``ai_runtime_configs`` singleton so two concurrent activations
    cannot race. The function refuses to activate profiles in
    states other than ``ready``, ``retired`` or the already-active
    profile (which is the rollback source).
    """

    profile = await _load_profile(db, profile_id)
    if profile.status not in ACTIVATABLE_STATUSES:
        raise ProfileNotActivatable(
            f"profile {profile_id} 当前状态 {profile.status} 不能激活"
        )
    if profile.status not in ("ready", "retired", "active"):
        # Defensive: the ACTIVATABLE_STATUSES check above is
        # the contract; this is a redundant guard so a future
        # status addition cannot accidentally bypass the
        # readiness check.
        raise ProfileNotReady(
            f"profile {profile_id} 当前状态 {profile.status}，请先完成构建"
        )

    complete, current_chunks, missing = await _verify_profile_is_complete(
        db, profile
    )
    if not complete:
        # A ``ready`` / ``retired`` profile that no longer covers the
        # live corpus (new chunks, or chunks whose content_hash
        # rotated after the build) is repaired on demand: we
        # idempotently enqueue the missing chunks, flip the profile
        # back to ``building``, and let the operator activate once
        # the worker has finished the backfill.
        enqueued, _skipped = await _backfill_missing_jobs(db, profile)
        if enqueued:
            profile.status = "building"
            profile.build_finished_at = None
            await _recompute_profile_counters(db, profile)
            await db.commit()
            await db.refresh(profile)
            raise ProfileNotReady(
                f"profile {profile_id} 覆盖率不足，已自动补建 {enqueued} 个任务，"
                f"完成后可启用"
            )
        raise ProfileNotReady(
            f"profile {profile_id} 覆盖率不足："
            f"{current_chunks - missing}/{current_chunks}"
        )

    config = await _load_active_config(db, with_for_update=True)
    if config is None:
        raise ConfigLocked("尚未配置 AI runtime，无法切换索引")
    previous_id = config.active_embedding_profile_id

    now = utc_now()
    if previous_id is not None and previous_id != profile.id:
        previous = await _load_profile(db, previous_id, with_for_update=True)
        if previous.status == "active":
            previous.status = "retired"
            db.add(previous)
    # Idempotent re-activation (previous_id == profile.id) does
    # not need a previous-profile lookup; we still refresh the
    # ``activated_at`` timestamp below so the audit log reflects
    # the latest confirmation.

    config.active_embedding_profile_id = profile.id
    profile.status = "active"
    profile.activated_at = now
    db.add_all([config, profile])
    await db.commit()
    await db.refresh(profile)
    logger.info(
        "embedding_profile_activated profile=%s previous=%s",
        profile.id,
        previous_id,
    )
    return ActivationResult(
        active_profile_id=profile.id,
        previous_profile_id=previous_id,
        profile_status=profile.status,
        activated_at=now.isoformat(),
        config_fingerprint=profile.config_fingerprint,
    )


async def rollback_profile(
    db: AsyncSession, profile_id: int) -> ActivationResult:
    """Restore ``profile_id`` as the active profile.

    The semantics are identical to :func:`activate_profile`; the
    name signals intent. The candidate must be in ``ready`` or
    ``retired`` and must cover the current corpus 100%.
    """

    profile = await _load_profile(db, profile_id)
    if profile.status not in ("ready", "retired"):
        raise ProfileNotActivatable(
            f"profile {profile_id} 当前状态 {profile.status}，不能回滚到该档案"
        )
    complete, current_chunks, missing = await _verify_profile_is_complete(
        db, profile
    )
    if not complete:
        raise ProfileNotReady(
            f"profile {profile_id} 覆盖率不足："
            f"{current_chunks - missing}/{current_chunks}"
        )
    return await activate_profile(db, profile_id)


# --- Auto-enqueue ---------------------------------------------------------


async def enqueue_embedding_jobs_for_chunk(
    db: AsyncSession, *, chunk: DocumentChunk
) -> int:
    """Enqueue embedding work for ``chunk`` against every relevant profile.

    The function is called by the chunking stage after a new
    child chunk is materialised. It returns the number of
    profiles it enqueued for. Relevant profiles are ``active``,
    ``building`` and ``ready``: ``tested`` / ``failed`` / ``draft``
    are not yet ready to consume work.

    A ``ready`` profile that actually gains new work is demoted back
    to ``building`` (its ``build_finished_at`` cleared) and its
    counters are recomputed against the current eligible corpus so
    the progress view stays truthful.
    """

    relevant_statuses = ("active", "building", "ready")
    profiles = list(
        (
            await db.execute(
                select(EmbeddingProfile).where(
                    EmbeddingProfile.status.in_(relevant_statuses)
                )
            )
        ).scalars().all()
    )
    if not profiles:
        return 0

    enqueued = 0
    for profile in profiles:
        key = _idempotency_key(profile.id, chunk.id, profile.config_fingerprint)
        existing = await db.execute(
            select(ProcessingJob).where(ProcessingJob.idempotency_key == key)
        )
        if existing.scalars().first() is not None:
            continue
        db.add(_enqueue_embedding_job(profile=profile, chunk=chunk))
        enqueued += 1
        if profile.status == "ready":
            profile.status = "building"
            profile.build_finished_at = None
            await _recompute_profile_counters(db, profile)
            db.add(profile)
    return enqueued


# --- Status read-out ------------------------------------------------------


async def get_profile_summaries(db: AsyncSession) -> list[ProfileSummary]:
    """Return every profile annotated with the actions the operator can take."""

    rows = list(
        (
            await db.execute(
                select(EmbeddingProfile).order_by(
                    EmbeddingProfile.id.desc()
                )
            )
        ).scalars().all()
    )
    if not rows:
        return []
    config = await _load_active_config(db)
    active_id = config.active_embedding_profile_id if config else None
    profile_ids = [row.id for row in rows]
    job_rows = (
        await db.execute(
            select(
                ProcessingJob.embedding_profile_id,
                ProcessingJob.status,
                ProcessingJob.last_error,
            ).where(
                ProcessingJob.embedding_profile_id.in_(profile_ids),
                ProcessingJob.stage == EMBEDDING_STAGE,
            )
        )
    ).all()
    job_statuses: dict[int, Counter[str]] = defaultdict(Counter)
    job_errors: dict[int, Counter[str]] = defaultdict(Counter)
    for profile_id, status, last_error in job_rows:
        if profile_id is None:
            continue
        job_statuses[profile_id][status] += 1
        if status == "failed":
            message = (last_error or "未记录具体错误").strip()[:500]
            job_errors[profile_id][message] += 1
    summaries: list[ProfileSummary] = []
    for row in rows:
        is_active = active_id is not None and active_id == row.id
        statuses = job_statuses[row.id]
        pending_jobs = sum(statuses[value] for value in ("created", "retry"))
        processing_jobs = statuses["processing"]
        failure_reasons = tuple(
            job_errors[row.id].most_common(5)
        )
        summaries.append(
            ProfileSummary(
                profile_id=row.id,
                status=row.status,
                provider=row.provider,
                base_url=row.base_url,
                model=row.model,
                dim=row.dim,
                config_fingerprint=row.config_fingerprint,
                total_chunks=row.total_chunks,
                completed_chunks=row.completed_chunks,
                failed_chunks=row.failed_chunks,
                build_started_at=(
                    row.build_started_at.isoformat() if row.build_started_at else None
                ),
                build_finished_at=(
                    row.build_finished_at.isoformat() if row.build_finished_at else None
                ),
                activated_at=(
                    row.activated_at.isoformat() if row.activated_at else None
                ),
                is_active=is_active,
                pending_jobs=pending_jobs,
                processing_jobs=processing_jobs,
                failure_reasons=failure_reasons,
                last_error=row.last_error,
                available_actions=_available_actions(
                    row.status,
                    is_active,
                    processing_jobs=processing_jobs,
                ),
            )
        )
    return summaries


__all__ = [
    "ACTIVATABLE_STATUSES",
    "BUILDABLE_STATUSES",
    "EMBEDDING_IDEMPOTENCY_PREFIX",
    "EMBEDDING_STAGE",
    "RETRYABLE_STATUSES",
    "ActivationResult",
    "BuildResult",
    "BuildServiceError",
    "ConfigLocked",
    "ProfileNotActivatable",
    "ProfileNotBuildable",
    "ProfileNotFound",
    "ProfileNotReady",
    "ProfileSummary",
    "activate_profile",
    "enqueue_embedding_jobs_for_chunk",
    "get_profile_summaries",
    "retry_profile",
    "rollback_profile",
    "start_build",
]
