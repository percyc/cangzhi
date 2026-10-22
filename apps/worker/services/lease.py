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
    distinct while staying well inside the String(64) schema column.
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

    Every connection sets ``statement_timeout`` and ``lock_timeout`` so a
    fence verification or heartbeat cannot block on a locked row forever.
    Read-only transactions may wait for a model; their lifetime is bounded by
    the supervisor instead of idle-in-transaction termination. The timeouts
    come from the clamped policy,
    never left to a mis-typed environment, so a fence or heartbeat call that
    exceeds the budget fails fast instead of hanging the supervisor.
    """
    from sqlalchemy import create_engine

    engine = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=0,
                          pool_timeout=3, connect_args={"connect_timeout": 3})
    ms = max(int(lease_policy().db_timeout_ms), 100)

    def _set_timeouts(dbapi_conn, record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute(f"SET statement_timeout = {ms}")
            cursor.execute(f"SET lock_timeout = {min(ms, 1000)}")
            # Existing stages may read metadata then wait for a model. Killing
            # that read transaction would discard an otherwise useful response.
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
    profile_fingerprint: str | None = None


def capture_expectation(session: Session, job: ProcessingJob) -> LeaseExpectation:
    """Build an immutable expectation snapshot for a held job/attempt."""
    doc = None
    if job.document_id is not None:
        doc = session.get(Document, job.document_id)
    version = None
    if job.document_version_id is not None:
        version = session.get(DocumentVersion, job.document_version_id)
    profile_fingerprint = None
    if job.embedding_profile_id is not None:
        from apps.api.models.embedding_profiles import EmbeddingProfile
        profile_fingerprint = session.scalar(select(EmbeddingProfile.config_fingerprint).where(
            EmbeddingProfile.id == job.embedding_profile_id))
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
        profile_fingerprint=profile_fingerprint,
    )


def _verify_connection(conn, expectation, *, lock_source=False, lock_job=False,
                       check_source=True, allow_terminal=False, protected_commit=False):
    """Core reads only: never refresh/flush pending ORM objects."""
    table = ProcessingJob.__table__
    stmt = select(table).where(table.c.id == expectation.job_id)
    row = conn.execute(stmt).mappings().first()
    # Match the application's document-before-job lock order. This only runs
    # at first write, never around a model request or source download.
    if row is not None and lock_source:
        conn.execute(select(Document.__table__.c.id).where(
            Document.__table__.c.id == row.document_id).with_for_update()).first()
    if lock_job:
        row = conn.execute(stmt.with_for_update()).mappings().first()
    if row is None or (
        row.attempt_id != expectation.attempt_id
        or row.worker_id != expectation.worker_id
        or row.config_version != expectation.config_version
        or row.document_version_id != expectation.document_version_id
        or row.embedding_profile_id != expectation.embedding_profile_id
        or row.quarantined_at is not None
        or row.status not in ({"processing", "completed", "retry", "failed"} if allow_terminal else {expectation.status})
    ):
        raise StaleAttemptError("task ownership changed")
    now = (conn.execute(text("SELECT clock_timestamp()")).scalar()
           if conn.dialect.name == "postgresql" else utc_now())
    deadlines = (row.hard_deadline,) if protected_commit else (row.lease_until, row.hard_deadline)
    if any(value is None or _aware_utc(value) <= now for value in deadlines):
        raise StaleAttemptError("task lease or deadline expired")
    doc_table, version_table = Document.__table__, DocumentVersion.__table__
    stmt = select(doc_table.c.current_version_id, doc_table.c.is_deleted).where(
        doc_table.c.id == row.document_id)
    if lock_source:
        stmt = stmt.with_for_update()
    doc = conn.execute(stmt).first()
    source_hash = conn.execute(select(version_table.c.content_hash).where(
        version_table.c.id == row.document_version_id)).scalar()
    if doc is None or doc.is_deleted:
        raise StaleAttemptError("source deleted")
    if check_source and (doc.current_version_id != expectation.current_version_id
                         or source_hash != expectation.content_hash):
        raise StaleAttemptError("source changed")
    if expectation.profile_fingerprint is not None:
        from apps.api.models.embedding_profiles import EmbeddingProfile
        profile = EmbeddingProfile.__table__
        stmt = select(profile.c.config_fingerprint).where(profile.c.id == expectation.embedding_profile_id)
        if lock_job:
            stmt = stmt.with_for_update()
        if conn.execute(stmt).scalar() != expectation.profile_fingerprint:
            raise StaleAttemptError("embedding profile changed")
    return doc.current_version_id, source_hash, row.status


def verify_against(session: Session, expectation: LeaseExpectation, *, db_now=None) -> None:
    with session.no_autoflush:
        _verify_connection(session.connection(), expectation)
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
    if job.quarantined_at is not None:
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
            text("SELECT pg_advisory_xact_lock(:namespace, :document)"),
            {"namespace": _DOC_CLAIM_LOCK_NAMESPACE, "document": doc_id},
        )


def busy_document_filter(doc_id_column):
    """``NOT EXISTS`` guard excluding docs with an active in-flight job.

    ``doc_id_column`` is the outer query's document identity column (either
    ``Document.id`` from a joined claim or ``ProcessingJob.document_id`` from a
    job-scoped claim). It serializes execution across distinct job rows for the
    same document (and therefore across executors, including maintenance /
    rechunk): a new claim for any stage is disallowed while the document has
    another ``processing`` job, even if its lease expired: that job must first
    go through owned recovery. The inner table is aliased so
    it cannot self-correlate against the outer job set.

    With CPU concurrency 1 this is a no-op in today's deployment, but it makes
    the same guarantee hold if a second executor is ever introduced.
    """
    from sqlalchemy import exists

    busy = ProcessingJob.__table__.alias("pj_busy")
    return ~exists(
        select(busy.c.id).where(
            busy.c.document_id == doc_id_column,
            busy.c.status == "processing",
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

    Expired or missing leases still block a different job until owned recovery
    or manual inspection. Maintenance and ordinary claims share this rule.
    """
    stmt = (
        select(ProcessingJob.id)
        .where(
            ProcessingJob.document_id == document_id,
            ProcessingJob.status == "processing",
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
    if not try_document_execution_lock(session.connection(), job.document_id, transaction=True):
        return False
    if job.quarantined_at is not None:
        return False
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
    """Fence all child DB engines, including async database-refresh sessions.

    First write locks the source document (not the job heartbeat row). Commit
    takes the job row briefly and validates the token/deadline. Core reads avoid
    ORM autoflush and never overwrite pending values. Self-authored hash/version
    changes (URL extraction/database refresh) advance the expectation only at
    commit, while the source lock excludes external updates.
    """

    def __init__(self, session, expectation, *, all_engines=False):
        self.session, self.expectation = session, expectation
        self.target = Engine if all_engines else session.get_bind()
        self._in_verify = False
        self._protected = set()

    def _check(self, conn, *, first=False, commit=False):
        if self._in_verify:
            return
        self._in_verify = True
        try:
            return _verify_connection(
                conn, self.expectation, lock_source=first, lock_job=first or commit,
                check_source=first, allow_terminal=commit, protected_commit=commit)
        finally:
            self._in_verify = False

    def _before_execute(self, conn, statement, multiparams, params, execution_options):
        if self._in_verify or not _is_dml(statement):
            return statement, multiparams, params
        # Admission uses an independent short transaction, not task data.
        table_name = getattr(getattr(statement, "table", None), "name", "")
        raw = str(getattr(statement, "text", ""))
        if table_name.startswith("model_admission_") or "model_admission_" in raw:
            return statement, multiparams, params
        if conn not in self._protected:
            self._check(conn, first=True)
            self._protected.add(conn)
        return statement, multiparams, params

    def _commit(self, conn):
        if conn in self._protected:
            target = self._check(conn, commit=True)
            from dataclasses import replace
            self.expectation = replace(self.expectation, current_version_id=target[0],
                                       content_hash=target[1], status=target[2])
            if conn.dialect.name == "postgresql":
                # Publication owns the row lock, so heartbeats may wait. Its
                # finite hard deadline still applies; renew before releasing
                # the atomic publication lock, not after a stale handoff.
                conn.execute(text("UPDATE processing_jobs SET heartbeat_at=clock_timestamp(), "
                    "lease_until=LEAST(hard_deadline,clock_timestamp()+(:seconds * interval '1 second')) "
                    "WHERE id=:id AND attempt_id=:attempt"),
                    {"id": self.expectation.job_id, "attempt": self.expectation.attempt_id,
                     "seconds": lease_policy().lease_seconds})
            self._protected.discard(conn)

    def _rollback(self, conn, *args):
        self._protected.discard(conn)

    def __enter__(self):
        event.listen(self.target, "before_execute", self._before_execute, retval=True)
        event.listen(self.target, "commit", self._commit)
        event.listen(self.target, "rollback", self._rollback)
        event.listen(self.target, "rollback_savepoint", self._rollback)
        return self

    def __exit__(self, *args):
        event.remove(self.target, "before_execute", self._before_execute)
        event.remove(self.target, "commit", self._commit)
        event.remove(self.target, "rollback", self._rollback)
        event.remove(self.target, "rollback_savepoint", self._rollback)
        self._protected.clear()


def try_document_execution_lock(connection, document_id: int, *, transaction=False) -> bool:
    """Same namespace for execution and recovery; nonblocking, no deadlock."""
    fn = "pg_try_advisory_xact_lock" if transaction else "pg_try_advisory_lock"
    return bool(connection.execute(text(f"SELECT {fn}(:namespace, :document)"),
        {"namespace": _DOC_CLAIM_LOCK_NAMESPACE, "document": int(document_id)}).scalar())


def release_document_execution_lock(connection, document_id: int) -> None:
    connection.execute(text("SELECT pg_advisory_unlock(:namespace, :document)"),
        {"namespace": _DOC_CLAIM_LOCK_NAMESPACE, "document": int(document_id)})
