"""Worker side of the M3-6b phase 2 embedding build.

This module is the worker half of the embedding pipeline. The
API side (``apps.api.embeddings.build_service``) enqueues
``processing_jobs`` rows with ``stage='embedding'`` and a
``embedding_profile_id`` foreign key; this module claims them,
performs the HTTP embedding call, validates the response, and
upserts the result into ``chunk_embeddings``.

The module deliberately keeps the HTTP call in a single private
function so tests can monkey-patch it. The pure helpers
(``_validate_and_shape``,
``_upsert_chunk_embedding``) are split out so the data plane
can be tested without any network at all.

The four non-negotiable rules the module enforces:

1. **Use the profile's recorded provider / base_url / model.**
   The profile row is the single source of truth for which
   vector space we are building. The operator's
   ``ai_runtime_configs`` row only tells us *how* to talk to
   the upstream; if its provider or base URL no longer
   matches the profile, we refuse the job rather than silently
   embed against a different model.

2. **Decrypt the API key from the current runtime config, never
   from the profile.** Profiles never store the key. The key
   is always re-decrypted at the moment of the call so a
   key rotation in the settings page is honoured without a
   rebuild.

3. **Validate the vector before persisting it.** Dim must
   match the profile, every coordinate must be a finite
   number. A bad vector is treated as a failure; the
   ``DocumentVersion`` is *not* marked failed (the keyword
   retrieval still works on the version) and the profile's
   ``failed_chunks`` counter is incremented.

4. **Upsert by (profile_id, chunk_id) with content_hash check.**
   If a row already exists with a different ``content_hash``,
   we recompute; if the hash matches, we skip the call. The
   worker's idempotency is at the row level rather than the
   job level so a manual ``retry`` after a code fix does not
   re-embed every unchanged chunk.
"""

from __future__ import annotations

import datetime as _dt_module
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

import httpx
from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.embeddings.build_service import (
    EMBEDDING_STAGE,
)
from apps.api.embeddings.compatibility import validate_embedding_vector
from apps.api.models.auth import AIRuntimeConfig
from apps.api.models.chunks import DocumentChunk
from apps.api.models.embedding_profiles import ChunkEmbedding, EmbeddingProfile
from apps.api.models.processing import ProcessingJob
from apps.api.security.secrets import (
    SecretDecryptError,
    SecretStoreError,
    decrypt_secret,
)

logger = logging.getLogger(__name__)


# Module-level constants kept here so the worker tests do not
# have to import the API package's private names.
EMBEDDING_HTTP_TIMEOUT_SECONDS = 30.0
EMBEDDING_MAX_RETRIES = 3
EMBEDDING_BASE_RETRY_MINUTES = 5


def _next_retry_at(retry_count: int) -> datetime:
    """Exponential backoff for embedding jobs.

    Mirrors the calculation in
    :func:`apps.worker.services.processor.calculate_next_retry_at`
    but is duplicated here so this module can stay free of the
    import cycle the legacy processor creates.
    """

    delay = EMBEDDING_BASE_RETRY_MINUTES * (2 ** max(retry_count, 0))
    return datetime.now(tz=timezone.utc) + _dt_module.timedelta(minutes=delay)


# --- Pure helpers ---------------------------------------------------------


def _is_finite_vector(vector: Sequence[float]) -> bool:
    for value in vector:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return False
    return True


def _vector_matches(
    stored: ChunkEmbedding | None, *, content_hash: str, dim: int
) -> bool:
    if stored is None:
        return False
    if stored.content_hash != content_hash:
        return False
    if not isinstance(stored.vector, list) or len(stored.vector) != dim:
        return False
    return _is_finite_vector(stored.vector)


# --- Public result types --------------------------------------------------


@dataclass(frozen=True)
class EmbeddingJobOutcome:
    """Outcome of running a single embedding job."""

    job_id: int
    status: str  # 'completed' or 'failed' or 'skipped'
    chunk_id: int
    profile_id: int
    reason: str | None = None


# --- HTTP transport -------------------------------------------------------


ClientFactory = Callable[..., httpx.Client]


def _request_embedding(
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key: str,
    text: str,
    timeout_seconds: float,
    client_factory: ClientFactory | None = None,
) -> list[float]:
    """Call the configured embedding endpoint and return the vector.

    The factory parameter is what the unit tests monkey-patch;
    production code passes ``None`` and gets a real
    :class:`httpx.Client` with ``trust_env=False`` (proxy
    variables must not leak into the embedding call).
    """

    factory = client_factory or (lambda **kwargs: httpx.Client(**kwargs))
    headers = {"Accept": "application/json"}
    if provider == "openai":
        if not api_key:
            raise ValueError("Embedding 密钥为空，无法发起请求")
        url = f"{base_url.rstrip('/')}/embeddings"
        headers["Authorization"] = f"Bearer {api_key}"
        headers["Content-Type"] = "application/json"
        body = {"model": model, "input": text}
    elif provider == "ollama":
        url = f"{base_url.rstrip('/')}/api/embed"
        headers["Content-Type"] = "application/json"
        body = {"model": model, "input": text}
    else:
        raise ValueError(f"不支持的 embedding provider: {provider}")

    try:
        with factory(timeout=timeout_seconds, trust_env=False) as client:
            response = client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise ValueError(
            f"无法连接 Embedding 服务：{type(exc).__name__}"
        ) from exc

    if response.status_code >= 400:
        raise ValueError(f"Embedding 服务返回 HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("Embedding 服务返回的不是 JSON") from exc

    if not isinstance(payload, dict):
        raise ValueError("Embedding 服务返回的不是 JSON 对象")

    if provider == "openai":
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise ValueError("OpenAI 响应缺少 data 字段")
        first = data[0]
        if not isinstance(first, dict):
            raise ValueError("OpenAI 响应 data[0] 不是对象")
        vector = first.get("embedding")
    else:
        embeddings = payload.get("embeddings")
        if isinstance(embeddings, list) and embeddings:
            vector = embeddings[0]
        else:
            legacy = payload.get("embedding")
            if isinstance(legacy, list):
                vector = legacy
            else:
                raise ValueError("Ollama 响应缺少 embeddings/embedding 字段")
    if not isinstance(vector, list):
        raise ValueError("Embedding 响应不是数组")
    if not vector:
        raise ValueError("Embedding 响应为空")
    return [float(x) for x in vector]


# --- Key resolution -------------------------------------------------------


@dataclass(frozen=True)
class ResolvedEmbeddingKey:
    """The material needed to talk to the embedding endpoint."""

    provider: str
    base_url: str
    model: str
    api_key: str
    timeout_seconds: float


class EmbeddingKeyMismatchError(RuntimeError):
    """Raised when the operator's runtime config no longer matches the profile."""


class EmbeddingConfigUnavailableError(RuntimeError):
    """Raised when the operator has not configured the embedding channel."""


def resolve_embedding_runtime(
    session: Session,
    *,
    profile: EmbeddingProfile,
) -> ResolvedEmbeddingKey:
    """Return the materialised key the worker needs to embed ``profile``.

    The function:

    * Reads the current ``ai_runtime_configs`` row.
    * Checks the saved provider / base URL still match the
      profile (the operator may have moved on to a new
      configuration; in that case the old profile's jobs
      cannot be embedded against the new channel and we must
      wait for the operator to either revert the config or
      rebuild the profile).
    * Decrypts the API key on demand. The plaintext is
      returned to the caller and never persisted.
    """

    if profile.provider not in ("openai", "ollama"):
        raise EmbeddingConfigUnavailableError(
            f"profile {profile.id} 记录的 provider {profile.provider} 不受支持"
        )

    config = session.execute(
        select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc()).limit(1)
    ).scalars().first()
    if config is None:
        raise EmbeddingConfigUnavailableError("尚未配置 AI runtime")

    if config.embedding_provider != profile.provider:
        raise EmbeddingKeyMismatchError(
            f"runtime embedding_provider={config.embedding_provider} "
            f"与 profile.provider={profile.provider} 不一致"
        )
    runtime_base_url = (config.embedding_base_url or "").strip().rstrip("/")
    profile_base_url = (profile.base_url or "").strip().rstrip("/")
    if not runtime_base_url or not profile_base_url:
        raise EmbeddingConfigUnavailableError("embedding base_url 尚未配置")
    if runtime_base_url.lower() != profile_base_url.lower():
        raise EmbeddingKeyMismatchError(
            f"runtime embedding_base_url={runtime_base_url} "
            f"与 profile.base_url={profile_base_url} 不一致"
        )
    runtime_model = (config.embedding_model or "").strip()
    if not runtime_model or runtime_model != (profile.model or "").strip():
        raise EmbeddingKeyMismatchError(
            f"runtime embedding_model={runtime_model} "
            f"与 profile.model={profile.model} 不一致"
        )

    api_key = ""
    if profile.provider == "openai":
        if not config.has_embedding_api_key or not config.embedding_api_key_cipher:
            raise EmbeddingConfigUnavailableError(
                "尚未保存 OpenAI 兼容 embedding 密钥"
            )
        try:
            api_key = decrypt_secret(config.embedding_api_key_cipher)
        except (SecretStoreError, SecretDecryptError) as exc:
            raise EmbeddingConfigUnavailableError(
                "主密钥不匹配或密文已损坏"
            ) from exc
        if not api_key:
            raise EmbeddingConfigUnavailableError("embedding 密钥为空")

    return ResolvedEmbeddingKey(
        provider=profile.provider,
        base_url=runtime_base_url,
        model=runtime_model,
        api_key=api_key,
        timeout_seconds=(
            float(config.embedding_timeout_seconds)
            if config.embedding_timeout_seconds
            else float(EMBEDDING_HTTP_TIMEOUT_SECONDS)
        ),
    )


# --- Persistence helpers --------------------------------------------------


def _upsert_chunk_embedding(
    session: Session,
    *,
    profile: EmbeddingProfile,
    chunk: DocumentChunk,
    vector: list[float],
) -> ChunkEmbedding:
    """Insert or update the ``(profile_id, chunk_id)`` row.

    The function relies on the
    ``uix_chunk_embeddings_profile_chunk`` unique constraint
    to make the upsert atomic. If a row already exists with
    a different ``content_hash`` we update it; if the hash
    matches, we keep the existing row and return it.
    """

    row = session.execute(
        select(ChunkEmbedding).where(
            and_(
                ChunkEmbedding.profile_id == profile.id,
                ChunkEmbedding.chunk_id == chunk.id,
            )
        )
    ).scalars().first()
    if row is None:
        row = ChunkEmbedding(
            profile_id=profile.id,
            chunk_id=chunk.id,
            content_hash=chunk.content_hash,
            vector=list(vector),
        )
        session.add(row)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            row = session.execute(
                select(ChunkEmbedding).where(
                    and_(
                        ChunkEmbedding.profile_id == profile.id,
                        ChunkEmbedding.chunk_id == chunk.id,
                    )
                )
            ).scalars().first()
            if row is None:
                raise
            row.content_hash = chunk.content_hash
            row.vector = list(vector)
            session.add(row)
    else:
        row.content_hash = chunk.content_hash
        row.vector = list(vector)
        session.add(row)
    return row


def _increment_progress(
    session: Session,
    *,
    profile: EmbeddingProfile,
    completed_delta: int,
    failed_delta: int,
    finished_now: bool,
) -> None:
    """Update the profile's build counters / status.

    ``finished_now`` is True when the caller has just observed
    the last outstanding job for this profile, False otherwise.
    A profile becomes ``ready`` only when its counters report
    ``completed == total`` and ``failed == 0``; otherwise it
    stays in ``building`` (or, if the operator asked for
    a retry, back in ``building``).
    """

    if profile.total_chunks is None:
        profile.total_chunks = 0
    if profile.completed_chunks is None:
        profile.completed_chunks = 0
    if profile.failed_chunks is None:
        profile.failed_chunks = 0
    if completed_delta:
        profile.completed_chunks = min(
            profile.completed_chunks + completed_delta,
            profile.total_chunks,
        )
    if failed_delta:
        profile.failed_chunks = min(
            profile.failed_chunks + failed_delta,
            profile.total_chunks,
        )
    total = profile.total_chunks or 0
    completed = profile.completed_chunks or 0
    failed = profile.failed_chunks or 0
    if finished_now and total > 0 and completed == total and failed == 0:
        profile.status = "ready"
        profile.build_finished_at = datetime.now(tz=timezone.utc)
    elif finished_now and total > 0 and completed + failed >= total:
        # The build ran to completion but at least one chunk
        # failed. The profile remains in ``building`` so the
        # operator can decide whether to retry or mark it
        # failed manually; the worker does not invent a new
        # terminal state on its own.
        profile.build_finished_at = datetime.now(tz=timezone.utc)
    session.add(profile)


# --- Per-job handler ------------------------------------------------------


def process_embedding_job(
    session: Session,
    job: ProcessingJob,
    *,
    client_factory: ClientFactory | None = None,
) -> EmbeddingJobOutcome:
    """Run a single ``embedding`` stage job and return its outcome.

    The function is sync (the worker is sync). It never marks the
    ``DocumentVersion`` failed even when the embedding call
    errors: the version's keyword retrieval is unaffected, and
    the only thing that changes is the embedding profile's
    ``failed_chunks`` counter.
    """

    profile_id = job.embedding_profile_id
    chunk_id = job.embedding_chunk_id
    if profile_id is None or chunk_id is None:
        job.status = "failed"
        job.finished_at = datetime.now(tz=timezone.utc)
        job.next_retry_at = None
        job.last_error = "embedding 任务缺少 profile 或 chunk 关联"
        job.error_details = {
            "embedding_profile_id": profile_id,
            "embedding_chunk_id": chunk_id,
        }
        session.add(job)
        return EmbeddingJobOutcome(
            job_id=job.id,
            status="failed",
            chunk_id=0,
            profile_id=profile_id or 0,
            reason="missing_embedding_reference",
        )

    profile = session.get(EmbeddingProfile, profile_id)
    if profile is None:
        job.status = "failed"
        job.finished_at = datetime.now(tz=timezone.utc)
        job.next_retry_at = None
        job.last_error = f"profile {profile_id} 不存在"
        job.error_details = {"profile_id": profile_id}
        session.add(job)
        return EmbeddingJobOutcome(
            job_id=job.id,
            status="failed",
            chunk_id=chunk_id,
            profile_id=profile_id,
            reason="profile_missing",
        )

    chunk = session.get(DocumentChunk, chunk_id)
    if chunk is None or chunk.role != "child" or not chunk.is_current:
        # A chunk was deleted or marked historical between
        # the time the job was enqueued and the time it ran.
        # That is not an embedding failure: just skip the job
        # and let the worker's progress reconciliation
        # decrement the appropriate counter.
        job.status = "completed"
        job.finished_at = datetime.now(tz=timezone.utc)
        job.next_retry_at = None
        job.last_error = None
        job.error_details = {"reason": "chunk_missing_or_historical"}
        session.add(job)
        return EmbeddingJobOutcome(
            job_id=job.id,
            status="skipped",
            chunk_id=chunk_id,
            profile_id=profile_id,
            reason="chunk_missing",
        )

    stored = session.execute(
        select(ChunkEmbedding).where(
            and_(
                ChunkEmbedding.profile_id == profile.id,
                ChunkEmbedding.chunk_id == chunk.id,
            )
        )
    ).scalars().first()
    if _vector_matches(stored, content_hash=chunk.content_hash, dim=profile.dim):
        # Already up-to-date. Complete the job without an HTTP
        # call.
        job.status = "completed"
        job.finished_at = datetime.now(tz=timezone.utc)
        job.next_retry_at = None
        job.last_error = None
        job.error_details = {"reason": "already_current"}
        session.add(job)
        return EmbeddingJobOutcome(
            job_id=job.id,
            status="skipped",
            chunk_id=chunk_id,
            profile_id=profile_id,
            reason="already_current",
        )

    try:
        runtime = resolve_embedding_runtime(session, profile=profile)
    except (EmbeddingKeyMismatchError, EmbeddingConfigUnavailableError) as exc:
        # The operator's runtime config has moved on. The
        # job is left in ``retry`` so the next operator
        # action can resume it; we also count it as a
        # failure for progress tracking purposes.
        job.retry_count += 1
        job.last_error = str(exc)
        job.error_details = {"reason": "runtime_mismatch", "exception": str(exc)}
        if job.retry_count >= (job.max_retries or EMBEDDING_MAX_RETRIES):
            job.status = "failed"
            job.next_retry_at = None
        else:
            job.status = "retry"
            job.next_retry_at = _next_retry_at(job.retry_count - 1)
        job.finished_at = datetime.now(tz=timezone.utc)
        session.add(job)
        if job.status == "failed":
            _increment_progress(
                session,
                profile=profile,
                completed_delta=0,
                failed_delta=1,
                finished_now=False,
            )
        return EmbeddingJobOutcome(
            job_id=job.id,
            status=job.status,
            chunk_id=chunk_id,
            profile_id=profile_id,
            reason=str(exc),
        )

    try:
        vector = _request_embedding(
            provider=runtime.provider,
            base_url=runtime.base_url,
            model=runtime.model,
            api_key=runtime.api_key,
            text=chunk.search_text or chunk.content or "",
            timeout_seconds=runtime.timeout_seconds,
            client_factory=client_factory,
        )
    except ValueError as exc:
        job.retry_count += 1
        job.last_error = str(exc)
        job.error_details = {"reason": "embedding_call_failed", "exception": str(exc)}
        if job.retry_count >= (job.max_retries or EMBEDDING_MAX_RETRIES):
            job.status = "failed"
            job.next_retry_at = None
        else:
            job.status = "retry"
            job.next_retry_at = _next_retry_at(job.retry_count - 1)
        job.finished_at = datetime.now(tz=timezone.utc)
        session.add(job)
        if job.status == "failed":
            _increment_progress(
                session,
                profile=profile,
                completed_delta=0,
                failed_delta=1,
                finished_now=False,
            )
        return EmbeddingJobOutcome(
            job_id=job.id,
            status=job.status,
            chunk_id=chunk_id,
            profile_id=profile_id,
            reason=str(exc),
        )

    # Validate dim + finite values. A dimension mismatch is a
    # hard error: the profile says the vector space has ``dim``
    # dimensions and the upstream is contradicting us. We
    # fail the job and increment the failure counter.
    try:
        observed = validate_embedding_vector(vector, dim=profile.dim)
    except ValueError as exc:
        job.retry_count += 1
        job.last_error = str(exc)
        job.error_details = {
            "reason": "vector_invalid",
            "exception": str(exc),
            "expected_dim": profile.dim,
        }
        if job.retry_count >= (job.max_retries or EMBEDDING_MAX_RETRIES):
            job.status = "failed"
            job.next_retry_at = None
        else:
            job.status = "retry"
            job.next_retry_at = _next_retry_at(job.retry_count - 1)
        job.finished_at = datetime.now(tz=timezone.utc)
        session.add(job)
        if job.status == "failed":
            _increment_progress(
                session,
                profile=profile,
                completed_delta=0,
                failed_delta=1,
                finished_now=False,
            )
        return EmbeddingJobOutcome(
            job_id=job.id,
            status=job.status,
            chunk_id=chunk_id,
            profile_id=profile_id,
            reason=str(exc),
        )
    if observed != profile.dim:
        # Defensive: ``validate_embedding_vector`` already
        # enforces ``dim`` but be explicit in case the
        # function is ever relaxed.
        reason = f"向量维度 {observed} 与 profile.dim {profile.dim} 不一致"
        job.retry_count += 1
        job.last_error = reason
        job.error_details = {"reason": "dim_mismatch", "observed": observed}
        if job.retry_count >= (job.max_retries or EMBEDDING_MAX_RETRIES):
            job.status = "failed"
            job.next_retry_at = None
        else:
            job.status = "retry"
            job.next_retry_at = _next_retry_at(job.retry_count - 1)
        job.finished_at = datetime.now(tz=timezone.utc)
        session.add(job)
        if job.status == "failed":
            _increment_progress(
                session,
                profile=profile,
                completed_delta=0,
                failed_delta=1,
                finished_now=False,
            )
        return EmbeddingJobOutcome(
            job_id=job.id,
            status=job.status,
            chunk_id=chunk_id,
            profile_id=profile_id,
            reason=reason,
        )

    # Persist the vector. We do not touch DocumentVersion:
    # keyword retrieval is unaffected by an embedding failure.
    _upsert_chunk_embedding(
        session, profile=profile, chunk=chunk, vector=vector
    )
    job.status = "completed"
    job.finished_at = datetime.now(tz=timezone.utc)
    job.next_retry_at = None
    job.last_error = None
    job.error_details = None
    session.add(job)
    _increment_progress(
        session,
        profile=profile,
        completed_delta=1,
        failed_delta=0,
        finished_now=False,
    )
    return EmbeddingJobOutcome(
        job_id=job.id,
        status="completed",
        chunk_id=chunk_id,
        profile_id=profile_id,
    )


# --- Reconciliation after the per-job path -------------------------------


def _dialect_name(session: Session) -> str:
    """Return the bound dialect name for ``session`` (empty when unknown).

    The worker hands reconcile a sync ``Session`` whose ``bind`` is the
    configured engine; ``dialect.name`` is ``"postgresql"`` in production
    and ``"sqlite"`` in the test path. Centralised here so the aggregate
    builder and the SQLite safe fallback agree on which dim function to
    use.
    """

    bind = session.bind if session is not None else None
    if bind is None and hasattr(session, "get_bind"):
        bind = session.get_bind()
    if bind is None:
        return ""
    return getattr(getattr(bind, "dialect", None), "name", "")


def _build_progress_aggregate_stmt(dialect_name: str, profile: EmbeddingProfile):
    """Build the SQL aggregate that powers :func:`reconcile_profile_progress`.

    The query joins ``processing_jobs`` with current child
    ``document_chunks`` and outer-joins ``chunk_embeddings`` for the
    matching (profile, chunk) pair. The four counters — total distinct
    chunks, failed jobs, pending jobs and completed embeddings — are
    returned in a single round-trip. Only aggregate expressions appear
    in the SELECT clause; ``chunk_embeddings.vector`` is referenced
    solely via the dim function inside the CASE expression that powers
    the completed counter, so production (PostgreSQL + pgvector) never
    materialises vector floats and the test path (SQLite) does not
    select DocumentChunk text.

    The dim function is dialect-specific: ``vector_dims`` on
    PostgreSQL (pgvector's stored-dim function) and
    ``json_array_length`` on SQLite (the test path's JSON column has
    no pgvector-equivalent invariant). Both run server-side and let
    the database keep the vector payload off the wire.
    """

    if dialect_name == "postgresql":
        dim_expr = func.vector_dims(ChunkEmbedding.vector)
    else:
        dim_expr = case(
            (func.json_valid(ChunkEmbedding.vector) == 1,
             func.json_array_length(ChunkEmbedding.vector)),
            else_=None,
        )

    job_chunk_join = and_(
        DocumentChunk.id == ProcessingJob.embedding_chunk_id,
        DocumentChunk.role == "child",
        DocumentChunk.is_current.is_(True),
    )
    emb_join = and_(
        ChunkEmbedding.profile_id == ProcessingJob.embedding_profile_id,
        ChunkEmbedding.chunk_id == ProcessingJob.embedding_chunk_id,
    )
    job_filter = and_(
        ProcessingJob.embedding_profile_id == profile.id,
        ProcessingJob.stage == EMBEDDING_STAGE,
    )

    return (
        select(
            func.count(func.distinct(ProcessingJob.embedding_chunk_id)).label(
                "total"
            ),
            func.sum(
                case((ProcessingJob.status == "failed", 1), else_=0)
            ).label("failed"),
            func.sum(
                case(
                    (
                        ProcessingJob.status.in_(
                            ("created", "retry", "processing")
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("pending"),
            func.count(func.distinct(
                case(
                    (
                        and_(
                            ChunkEmbedding.id.isnot(None),
                            ChunkEmbedding.content_hash
                            == DocumentChunk.content_hash,
                            dim_expr == profile.dim,
                        ),
                        ChunkEmbedding.id,
                    ),
                    else_=None,
                )
            )).label("completed"),
        )
        .select_from(ProcessingJob)
        .join(DocumentChunk, job_chunk_join)
        .outerjoin(ChunkEmbedding, emb_join)
        .where(job_filter)
    )


def _revalidate_finite_sqlite(session: Session, profile: EmbeddingProfile) -> int:
    """Recompute the SQLite ``completed`` counter with a finite re-check.

    pgvector enforces finiteness at insert time, so production never
    needs this. The JSON column on SQLite has no such invariant, so
    the test path keeps the existing per-row Python safety net:
    re-query only hash-matching eligible rows, then verify their
    list type, dimension and finiteness in Python. The SELECT pulls just ``(chunk_id,
    content_hash, vector)`` — DocumentChunk text and any other payload
    are not materialised.
    """

    candidate_stmt = (
        select(
            ChunkEmbedding.chunk_id,
            ChunkEmbedding.content_hash,
            ChunkEmbedding.vector,
        )
        .select_from(ChunkEmbedding)
        .join(
            ProcessingJob,
            and_(
                ProcessingJob.embedding_profile_id == ChunkEmbedding.profile_id,
                ProcessingJob.embedding_chunk_id == ChunkEmbedding.chunk_id,
                ProcessingJob.stage == EMBEDDING_STAGE,
            ),
        )
        .join(
            DocumentChunk,
            and_(
                DocumentChunk.id == ChunkEmbedding.chunk_id,
                DocumentChunk.role == "child",
                DocumentChunk.is_current.is_(True),
            ),
        )
        .where(
            and_(
                ChunkEmbedding.profile_id == profile.id,
                ChunkEmbedding.content_hash == DocumentChunk.content_hash,
            )
        )
    )
    rows = session.execute(candidate_stmt.distinct()).all()
    return sum(1 for row in rows if isinstance(row.vector, list)
               and len(row.vector) == profile.dim and _is_finite_vector(row.vector))


def _aggregate_progress(
    session: Session, profile: EmbeddingProfile
) -> tuple[int, int, int, int]:
    """Return ``(total, completed, failed, pending)`` for ``profile``.

    PostgreSQL runs a single aggregate round-trip; the dim check uses
    ``vector_dims`` server-side and no vector floats or chunk text are
    materialised. SQLite runs the same aggregate (``json_array_length``
    replaces ``vector_dims``) and then a small per-row finite re-check
    is layered on top of the completed counter, since the JSON column
    has no storage-side invariant against non-finite values.
    """

    dialect_name = _dialect_name(session)
    stmt = _build_progress_aggregate_stmt(dialect_name, profile)
    row = session.execute(stmt).one()
    total = int(row.total or 0)
    failed = int(row.failed or 0)
    pending = int(row.pending or 0)
    completed = int(row.completed or 0)
    if dialect_name != "postgresql":
        # SQLite safe fallback: pgvector's storage contract is replaced
        # by explicit type/dimension/finite checks on hash-matching rows.
        completed = _revalidate_finite_sqlite(session, profile)
    return total, completed, failed, pending


def reconcile_profile_progress(session: Session, profile_id: int) -> None:
    """Reconcile counters against the actual job status for a profile.

    Called after :func:`process_embedding_job` returned so the
    "finished" branch can flip the profile to ``ready`` if every
    job is now in a terminal state. The function is idempotent
    and safe to call multiple times.

    Implementation note: counters are computed by a single SQL
    aggregate that joins ``processing_jobs`` with current child
    ``document_chunks`` and outer-joins ``chunk_embeddings``. The
    SELECT clause emits only aggregate expressions, so the vector
    payload is never pulled into Python on the PostgreSQL production
    path. On SQLite, a small per-row re-check re-validates
    finiteness, since the JSON column has no server-side invariant
    against non-finite values. The public status / hash / dim /
    count semantics — including sampled datasets, retired profiles
    being skipped, empty cases and ``active`` profiles staying
    ``active`` — are preserved.
    """

    profile = session.get(EmbeddingProfile, profile_id)
    if profile is None or profile.status not in (
        "active",
        "building",
        "ready",
        "failed",
    ):
        return
    remains_active = profile.status == "active"

    total, completed, failed, pending = _aggregate_progress(session, profile)

    profile.completed_chunks = completed
    profile.failed_chunks = failed
    profile.total_chunks = total
    if remains_active:
        # An active profile keeps serving completed vectors while newly
        # ingested or re-chunked content is filled in.  Counters must still
        # reflect the live corpus, but an incremental update must never
        # silently deactivate the profile selected by the user.
        profile.status = "active"
        if pending == 0 and failed == 0 and completed == total:
            profile.build_finished_at = datetime.now(tz=timezone.utc)
        else:
            profile.build_finished_at = None
    elif pending == 0 and failed == 0 and completed == total:
        profile.status = "ready"
        if profile.build_finished_at is None:
            profile.build_finished_at = datetime.now(tz=timezone.utc)
    elif pending == 0 and failed > 0:
        profile.status = "failed"
        profile.build_finished_at = datetime.now(tz=timezone.utc)
    else:
        profile.status = "building"
        profile.build_finished_at = None
    session.add(profile)


__all__ = [
    "EMBEDDING_STAGE",
    "EmbeddingConfigUnavailableError",
    "EmbeddingJobOutcome",
    "EmbeddingKeyMismatchError",
    "ResolvedEmbeddingKey",
    "_is_finite_vector",
    "_request_embedding",
    "_upsert_chunk_embedding",
    "process_embedding_job",
    "reconcile_profile_progress",
    "resolve_embedding_runtime",
]
