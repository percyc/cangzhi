"""Task ownership, finite lease and write-fencing primitives (ADR-035).

This is the shared ownership layer used by the ordinary Worker and by the
priority-maintenance / rechunk executors so that "ordinary + maintenance"
jobs share one ownership protocol. A claim that participates in recovery
stamps a unique ``attempt_id`` and a finite ``lease_until`` +
``hard_deadline`` before handing work to a supervised executor, and every
database write issued under that attempt must pass a write fence so a stale
attempt can never commit after the lease has been handed to a successor.

Recovery and fencing rules (exact limits, see ADR-035):

* Only a ``processing`` row that **already carries a lease** (``attempt_id``
  and ``lease_until`` not NULL) can ever be recovered. A ``processing`` row
  whose ``lease_until`` is NULL is a *historical orphan* (pre-migration) and
  is intentionally never taken over: there is no fencing token proving the
  prior executor stopped, so re-claiming risks double-application of writes.
* **Lease expiry is NOT by itself proof the old subprocess exited.** Recovery
  is safe only when the write fence *and* unique per-attempt artifacts prove a
  late stale commit cannot be published. The fence re-asserts the attempt
  against the **database clock** (never a bare ``finally``) before any DML or
  commit, and the recovery path CAS-updates the row so an expired lease can
  never be resurrected.
* CPU concurrency stays at **one** executor initially; callers always dispatch
  serially and claims are serialized per document across distinct job rows.
"""

from __future__ import annotations

import datetime
import os
import secrets
import socket
import threading
from dataclasses import dataclass

import structlog
from sqlalchemy import event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from apps.api.models.documents import Document, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.worker.core.config import _task_execution_policy

logger = structlog.get_logger()

# Both ``worker_id`` and ``attempt_id`` are stored in String(64) columns. The
# hostname can itself be up to 63 chars, so no schema value may embed it raw.
_WORKER_PREFIX_LIMIT = 24
_ATTEMPT_LIMIT = 64


class StaleAttemptError(RuntimeError):
    """Raised when an executor's attempt no longer holds its lease/target.

    The write fence raises it inside a transaction that must be rolled back.
    It is never replayed as an application error and never billed to a
    model/parse retry.
    """


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _aware_utc(value) -> datetime.datetime:
    if value is None:
        return value
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def db_utc_now(session: Session) -> datetime.datetime:
    """Current UTC time from the database clock (falls back to Python).

    Uses PostgreSQL ``clock_timestamp()`` (a ``timestamptz``), **not**
    ``now()``: ``now()`` returns the transaction start time, so a long-running
    transaction could misjudge an actually-expired lease as still valid.
    ``clock_timestamp()`` reflects the real clock at the moment of call and is
    compared against the ``timestamptz`` columns directly (no ``AT TIME ZONE``
    stripping that would rely on the session time zone).
    """
    if _dialect_name(session).startswith("postgres"):
        value = session.execute(text("SELECT clock_timestamp()")).scalar()
        if value is not None:
            return _aware_utc(value)
    return utc_now()


_worker_id_lock = threading.Lock()
_worker_id: str | None = None


def worker_id() -> str:
    """Stable, process-unique executor identity, guaranteed <= 64 chars.

    The raw hostname can be 63 chars, so we never embed it verbatim. A short
    process id plus a random salt keep two processes (even after PID reuse)
    distinct while staying well inside the String(64) schema col====umn.
    """
    global _worker_id
    with _worker_id_lock:
        if _worker_id is None:
            host = (socket.gethostname() or "host")[:_WORKER_PREFIX_LIMIT]
            salt = secrets.token_hex(4)
            _worker_id = f"{host}-{os.getpid():06d}-{salt}"
            if len(_worker_id) > 64:  # pragma: no cover - defensive
                _worker_id = f"{secrets.token_hex(8)}-{os.getpid():06d}"
        return _worker_id


def new_attempt_id(job_id: int) -> str:
    """Unique attempt token that fits String(64).

    ``job_id`` is capped to 12 digits, the salt to 16 hex bytes; the result
    is always <= 64 chars. Two claims for the same job can never collide.
    """
    token = f"{int(job_id) % 10**12:012d}-{secrets.token_hex(16)}"
    return token[:_ATTEMPT_LIMIT]


def time_ns() -> int:  # keep for backward compatibility of the module surface
    return int(__import__("time").time_ns())


@dataclass(frozen=True)
class LeasePolicy:
    """Clamped task-execution policy from :data:`_task_execution_policy`."""

    enabled: bool
    lease_seconds: float
    heartbeat_seconds: float
    hard_deadline_seconds: float
    terminate_grace_seconds: float
    max_attempts: int
    db_timeout_ms: int


def lease_policy() -> LeasePolicy:
    return LeasePolicy(**_task_execution_policy())


def _dialect_name(session: Session) -> str:
    bind = session.bind if session is not None else None
    if bind is None:
        try:
            bind = session.get_bind()
        except Exception:  # noqa: BLE001 — defensive, never blocks ownership
            return ""
    return getattr(getattr(bind, "dialect", None), "name", "")


def supervised_claims_active(session: Session) -> bool:
    """True only on the supervised path: feature enabled AND PostgreSQL.

    SQLite (the unit-test suite) never activates supervision. If an operator
    enables the feature on a non-PostgreSQL engine there is no silent
    in-process fallback: :func:`require_supervised_supported` rejects it.
    """
    if not lease_policy().enabled:
        return False
    return _dialect_name(session).startswith("postgres")


def require_supervised_supported(session: Session) -> None:
    """Raise when supervision is requested but not supported.

    Prevents a silent in-process execution when the feature is enabled on a
    non-PostgreSQL engine. The explicit in-process hook is *only* the unit
    tests, which keep ``task_execution_enabled`` False.
    """
    if not lease_policy().enabled:
        return
    if not _dialect_name(session).startswith("postgres"):
        raise StaleAttemptError(
            "task_execution_enabled requires PostgreSQL; "
            "refusing to run the legacy in-process path silently"
        )


def bounded_engine(url: str) -> Engine:
    """PostgreSQL engine with a per-operation deadline.

    Every connection sets ``statement_timeout``, ``lock_timeout`` and
    ``idle_in_transaction_session_timeout`` so a fence verification, heartbeat
    refresh or an idle lease cannot block on a locked row (or an idle
    transaction) forever. The timeouts are hardcoded from the clamped policy,
    never left to a mis-typed environment, so a fence or heartbeat call that
    exceeds the budget fails fast instead of hanging the supervisor.
    """
    from sqlalchemy import create_engine

    engine = create_engine(url, pool_pre_ping=True)
    ms = max(int(lease_policy().db_timeout_ms), 100)

    def _set_timeouts(dbapi_conn, record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute(f"SET statement_timeout = {ms}")
            cursor.execute(f"SET lock_timeout = {ms}")
            cursor.execute(f"SET idle_in_transaction_session_timeout = {ms}")
        finally:
            cursor.close()

    event.listen(engine, "connect", _set_timeouts)
    return engine


# ---------------------------------------------------------------------------
# Lease / expectation primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeaseExpectation:
    """The contract a supervised attempt must hold to be allowed to write.

    Captured once per attempt (in the child right after an authoritative
    read). ``verify_against`` rechecks every field against the live row and
    its live document/version, so a change to the target — a replaced current
    version, a retargeted config_version or profile, a rotated hash, or a
    recovered lease/attempt — aborts the write (fail closed). The fields mirror
    the columns the pipeline actually publishes under.
    """

    job_id: int
    attempt_id: str
    worker_id: str
    status: str
    config_version: str | None
    document_version_id: int | None
    embedding_profile_id: int | None
    content_hash: str | None
    current_version_id: int | None
    lease_until: datetime.datetime
    hard_deadline: datetime.datetime


def capture_expectation(session: Session, job: ProcessingJob) -> LeaseExpectation:
    """Build an immutable expectation snapshot for a held job/attempt."""
    doc = None
    if job.document_id is not None:
        doc = session.get(Document, job.document_id)
    version = None
    if job.document_version_id is not None:
        version = session.get(DocumentVersion, job.document_version_id)
    return LeaseExpectation(
        job_id=job.id,
        attempt_id=job.attempt_id or "",
        worker_id=job.worker_id or "",
        status="processing",
        config_version=job.config_version,
        document_version_id=job.document_version_id,
        embedding_profile_id=job.embedding_profile_id,
        content_hash=getattr(version, "content_hash", None),
        current_version_id=getattr(doc, "current_version_id", None),
        lease_until=_aware_utc(job.lease_until),
        hard_deadline=_aware_utc(job.hard_deadline),
    )


def verify_against(
    session: Session, expectation: LeaseExpectation, *, db_now: datetime.datetime
) -> None:
    """Fencing proof: re-assert the live state still matches the expectation.

    Uses ``SELECT ... FOR UPDATE`` on the job row so the check sees the latest
    committed row version even within an older MVCC snapshot, and is short —
    it fires only at DML/commit moments, never across an external model wait.
    Document/version/hash are re-read with fresh SQL (``populate_existing``),
    never ``session.get``, so the identity map cannot hand back a cached old
    target. Raises :class:`StaleAttemptError` on any mismatch. ``db_now`` must
    come from :func:`db_utc_now` (``clock_timestamp()``) so lease/deadline
    comparisons use the DB clock at the actual fence moment.
    """
    row = session.execute(
        select(ProcessingJob)
        .where(ProcessingJob.id == expectation.job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().first()
    if row is None:
        raise StaleAttemptError(f"job {expectation.job_id} no longer exists")
    held = (
        row.attempt_id == expectation.attempt_id
        and row.worker_id == expectation.worker_id
        and row.status == "processing"
        and row.config_version == expectation.config_version
        and row.document_version_id == expectation.document_version_id
        and row.embedding_profile_id == expectation.embedding_profile_id
    )
    if not held:
        raise StaleAttemptError(
            f"attempt {expectation.attempt_id} lost ownership of job {expectation.job_id}"
        )
    lease_deadline = _aware_utc(row.lease_until)
    hard_deadline = _aware_utc(row.hard_deadline)
    if lease_deadline is None or hard_deadline is None:
        raise StaleAttemptError(f"job {expectation.job_id} lost its lease/deadline")
    if db_now >= lease_deadline:
        raise StaleAttemptError(f"lease for job {expectation.job_id} expired")
    if db_now >= hard_deadline:
        raise StaleAttemptError(f"hard deadline for job {expectation.job_id} passed")
    # Live document/version target must be unchanged — read fresh, not from the
    # ORM identity map, which could return a stale cached row.
    live_doc = None
    if row.document_id is not None:
        live_doc = session.execute(
            select(Document)
            .where(Document.id == row.document_id)
            .execution_options(populate_existing=True)
        ).scalars().first()
    if expectation.current_version_id is not None and (
        live_doc is None or live_doc.current_version_id != expectation.current_version_id
    ):
        raise StaleAttemptError(
            f"document {row.document_id} changed target version; refusing stale publish"
        )
    live_version = None
    if row.document_version_id is not None:
        live_version = session.execute(
            select(DocumentVersion)
            .where(DocumentVersion.id == row.document_version_id)
            .execution_options(populate_existing=True)
        ).scalars().first()
    live_hash = getattr(live_version, "content_hash", None)
    if expectation.content_hash is not None and live_hash != expectation.content_hash:
        raise StaleAttemptError(
            f"version {row.document_version_id} content_hash changed; refusing stale publish"
        )


def refresh_lease(
    session: Session, expectation: LeaseExpectation, *, db_now: datetime.datetime
) -> int:
    """Heartbeat: CAS-extend the lease, clamped to the hard deadline.

    The UPDATE only fires when the row is exactly the expected attempt, still
    ``processing`` and its lease/deadline are not already past — an expired
    lease can never be resurrected. The new lease is clamped to
    ``hard_deadline`` via ``LEAST``. Returns rows affected (0 => stop).
    """
    new_lease = db_now + datetime.timedelta(
        seconds=getattr(lease_policy(), "lease_seconds", 120.0)
    )
    result = session.execute(
        text(
            "UPDATE processing_jobs "
            "SET heartbeat_at = clock_timestamp(), "
            "    lease_until = LEAST(:new_lease, hard_deadline) "
            "WHERE id = :job_id AND attempt_id = :attempt AND worker_id = :worker "
            "  AND status = 'processing' "
            "  AND lease_until > clock_timestamp() "
            "  AND hard_deadline > clock_timestamp()"
        ),
        {
            "new_lease": new_lease,
            "job_id": expectation.job_id,
            "attempt": expectation.attempt_id,
            "worker": expectation.worker_id,
        },
    )
    return int(result.rowcount or 0)


def is_recoverable_claim(job: ProcessingJob, now: datetime.datetime) -> bool:
    """True when an in-flight job can be safely taken over.

    Only a job that already holds a lease (token present) whose lease has
    expired under the DB clock is recoverable. A ``processing`` row with
    ``lease_until`` NULL is a pre-migration orphan and is deliberately *not*
    taken over (no fence token proves the prior executor stopped). Expiry is a
    necessary condition; the actual safety of recovery comes from the write
    fence and unique per-attempt artifacts, validated in the reliability tests.
    """
    if job is None or job.status != "processing":
        return False
    if job.lease_until is None or job.attempt_id is None or job.worker_id is None:
        return False
    return _aware_utc(job.lease_until) < now


def stamp_lease(session: Session, job: ProcessingJob, now: datetime.datetime) -> None:
    """Stamp a fresh finite lease + unique attempt on ``job``.

    Only called on the supervised path. The finite ``lease_until`` and
    ``hard_deadline`` make the attempt recoverable; the unique ``attempt_id``
    and ``worker_id`` become the fence token the executor must hold to commit.
    """
    policy = lease_policy()
    job.worker_id = worker_id()
    job.attempt_id = new_attempt_id(job.id)
    job.lease_until = now + datetime.timedelta(seconds=policy.lease_seconds)
    job.heartbeat_at = now
    from apps.worker.core.config import stage_hard_deadline_seconds
    job.hard_deadline = now + datetime.timedelta(
        seconds=stage_hard_deadline_seconds(job.stage or "")
    )
    job.attempt_count = (job.attempt_count or 0) + 1
    session.add(job)


# ---------------------------------------------------------------------------
# Per-document serialization
# ---------------------------------------------------------------------------


# A fixed, project-private namespace for per-document claim locks. It is
# deliberately distinct from the priority daemon key (89117342) and the
# rechunk key (72403119) so a claim lock can never collide with those.
_DOC_CLAIM_LOCK_NAMESPACE = 67511247


def acquire_document_claim_lock(session: Session, document_ids) -> None:
    """Serialize claims for the *same document* via advisory transaction locks.

    ``NOT EXISTS`` on an in-flight job is not by itself atomic: two concurrent
    claim transactions can both observe "no processing job" and lock two
    *distinct* job rows for the same document. Acquiring a per-document
    advisory lock, held until the claim transaction commits, makes only one
    claimer of a document proceed at a time; the second, after acquiring the
    lock, re-checks the busy filter and skips the now-claimed document. The
    same namespace is used by the ordinary Worker, the priority-maintenance
    executors and the rechunk entrypoint so they serialize against each other.
    """
    ordered = sorted({int(doc_id) for doc_id in document_ids if doc_id is not None})
    for doc_id in ordered:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _DOC_CLAIM_LOCK_NAMESPACE + doc_id},
        )


def busy_document_filter(doc_id_column):
    """``NOT EXISTS`` guard excluding docs with an active in-flight job.

    ``doc_id_column`` is the outer query's document identity column (either
    ``Document.id`` from a joined claim or ``ProcessingJob.document_id`` from a
    job-scoped claim). It serializes execution across distinct job rows for the
    same document (and therefore across executors, including maintenance /
    rechunk): a new claim for any stage is disallowed while the document has
    another ``processing`` job whose lease is still valid (or pre-migration,
    never auto-reclaimed but also serializing). The inner table is aliased so
    it cannot self-correlate against the outer job set.

    With CPU concurrency 1 this is a no-op in today's deployment, but it makes
    the same guarantee hold if a second executor is ever introduced.
    """
    from sqlalchemy import alias, exists, or_

    busy = ProcessingJob.__table__.alias("pj_busy")
    return ~exists(
        select(busy.c.id).where(
            busy.c.document_id == doc_id_column,
            busy.c.status == "processing",
            or_(
                busy.c.lease_until.is_(None),
                busy.c.lease_until > text("(now() AT TIME ZONE 'UTC')"),
            ),
        )
    )


def busy_processing_filter():
    """``NOT EXISTS`` for claims that join :class:`Document` (maintenance)."""
    return busy_document_filter(Document.id)


def maybe_stamp_lease(
    session: Session, job: ProcessingJob, now: datetime.datetime
) -> None:
    """Stamp a fresh lease/attempt when the supervised path is active.

    A no-op otherwise (legacy claims keep their exact behaviour). Used by the
    maintenance / rechunk executors that have their own claim loops but must
    share the same ownership protocol with the ordinary Worker.
    """
    if supervised_claims_active(session):
        stamp_lease(session, job, now)


def document_has_inflight(
    session: Session, document_id: int, exclude_job_id: int | None = None
) -> bool:
    """True if the document already has an in-flight (processing) job.

    An in-flight job is ``processing`` with a lease that is NULL (pre-migration
    orphan, never auto-reclaimed but still serializing) or not yet expired
    under ``clock_timestamp()``. Used by the maintenance / rechunk executors
    after taking ``acquire_document_claim_lock`` to make the same serialization
    guarantee the ordinary Worker's two-step claim provides.
    """
    from sqlalchemy import or_

    stmt = (
        select(ProcessingJob.id)
        .where(
            ProcessingJob.document_id == document_id,
            ProcessingJob.status == "processing",
            or_(
                ProcessingJob.lease_until.is_(None),
                ProcessingJob.lease_until > text("clock_timestamp()"),
            ),
        )
        .limit(1)
    )
    if exclude_job_id is not None:
        stmt = stmt.where(ProcessingJob.id != exclude_job_id)
    return session.scalar(stmt) is not None


def claim_one_guarded(session: Session, job: ProcessingJob) -> bool:
    """Serialization guard for single-job claim loops (maintenance / rechunk).

    On the supervised path takes the shared per-document advisory lock and
    aborts (returns False) if a different job row for the same document is
    already in flight. On the legacy path this is a no-op returning True.
    Callers must roll back when it returns False.
    """
    if not supervised_claims_active(session):
        return True
    acquire_document_claim_lock(session, [job.document_id])
    if document_has_inflight(session, job.document_id, exclude_job_id=job.id):
        return False
    return True


# ---------------------------------------------------------------------------
# Write fence
# ---------------------------------------------------------------------------


def _is_dml(statement) -> bool:
    """True for UPDATE/DELETE/INSERT/MERGE, including raw ``text()`` DML.

    ``clauseelement.is_dml`` only covers ORM/bulk statements; raw SQL passed
    through ``session.execute(text(...))`` is a :class:`TextClause` that the
    fence must also guard, otherwise a bulk ``DELETE/UPDATE`` could commit
    without a fencing proof. CTE data-modifying statements (``WITH ... UPDATE``/
    ``DELETE``/``INSERT``) start with ``WITH``, so we also scan the tokenised
    text for a top-level DML keyword when the statement begins with ``WITH``.
    The ``before_commit`` gate is the final, unconditional fence, so an
    obfuscated DML statement that escapes detection here still cannot commit
    once the attempt is stale.
    """
    is_dml = getattr(statement, "is_dml", False)
    if is_dml:
        return True
    compiled = getattr(statement, "text", None)
    if not isinstance(compiled, str):
        return False
    head = compiled.lstrip().lower()
    if head.startswith(("insert", "update", "delete", "merge")):
        return True
    if head.startswith("with"):
        # ``WITH x AS (...) UPDATE ...`` is still a DML statement. Use word
        # boundaries so a literal column named e.g. ``update_at`` is not a hit.
        tokens = set(head.split())
        return bool(tokens & _DML_KEYWORDS)
    return False


_DML_KEYWORDS = {"insert", "update", "delete", "merge"}


class LeaseFence:
    """Write fence bound to an **isolated** executor engine.

    Registered on an engine that only the fencing session uses (the child's
    own engine), never a global engine that other threads share. Guards every
    DML — ORM flush via ``before_commit`` and bulk/raw statements via
    ``before_execute`` — by re-asserting :func:`verify_against`. The compare
    uses a short ``FOR UPDATE`` read with ``statement_timeout``/``lock_timeout``
    applied by :func:`bounded_engine`, so it cannot hang on a competing row
    lock across an external-model wait.
    """

    def __init__(self, session: Session, expectation: LeaseExpectation) -> None:
        self.session = session
        self.expectation = expectation
        self._installed = False
        self._in_verify = False

    def _assert_holds(self) -> None:
        if self._in_verify:
            return
        self._in_verify = True
        try:
            verify_against(self.session, self.expectation, db_now=db_utc_now(self.session))
        finally:
            self._in_verify = False

    def _before_execute(self, conn, statement, multiparams, params, execution_options):  # noqa: ANN001
        if _is_dml(statement):
            self._assert_holds()
        return statement, multiparams, params

    def _before_commit(self, session) -> None:  # noqa: ANN001
        self._assert_holds()

    def __enter__(self) -> "LeaseFence":
        bind = self.session.get_bind()
        event.listen(bind, "before_execute", self._before_execute, retval=True)
        # ``before_commit`` is a Session event, not a Connection event: listen
        # on the session *instance*. The ``before_execute`` hook guards bulk and
        # raw DML; this session-level hook is the final, unconditional fence for
        # ORM flush commits.
        event.listen(self.session, "before_commit", self._before_commit)
        self._installed = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._installed:
            return
        bind = self.session.get_bind()
        try:
            event.remove(bind, "before_execute", self._before_execute)
        except Exception:  # noqa: BLE001
            logger.warning(
                "lease_fence_remove_execute_failed", job_id=self.expectation.job_id
            )
        try:
            event.remove(self.session, "before_commit", self._before_commit)
        except Exception:  # noqa: BLE001
            logger.warning(
                "lease_fence_remove_commit_failed", job_id=self.expectation.job_id
            )