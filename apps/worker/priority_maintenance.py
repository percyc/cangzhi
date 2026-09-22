"""Priority-aware maintenance scheduler that runs while the normal Worker stays stopped.

The normal cangzhi Worker consumes the shared processing queue. Operators
sometimes need to keep that Worker stopped (for example during a long
vector rebuild) but still want fresh uploads, AI understanding and
embedding work to keep moving. This module is that alternate entry
point: a dedicated CLI that only looks at *current*, *non-deleted*
versions created at or after an explicit ``--new-since`` cutoff,
together with an explicit ``--history-ids`` allow-list of documents whose
new-strategy chunking/embedding jobs may run.

It does not rewrite the global pipeline. It claims one job at a time
with ``FOR UPDATE SKIP LOCKED``, hands the work to the shared
:func:`apps.worker.services.processor.process_single_job` and never
touches the parser, the chunker or the vector store beyond what that
helper already does. The only state mutation specific to this
scheduler is the opt-in flag that flips a freshly-claimed PDF
chunking job to ``ASSISTED_CHUNKING_CONFIG`` so the shared chunker
takes the assisted path; the ``idempotency_key`` is preserved. Oversized
PDFs retain profile rules and record a fallback diagnostic in version metadata.

Three selection profiles are honoured:

* Foreground — current version, ``version.created_at >= cutoff``,
  stages listed in :data:`FOREGROUND_STAGES`. Old version jobs are
  excluded even when they are newly created retries, by joining the
  job's ``document_version_id`` to the document's current version.
* Background (assisted) — only jobs belonging to documents named by
  ``--history-ids``, restricted to chunking jobs whose
  ``config_version`` is ``ASSISTED_CHUNKING_CONFIG`` or embedding jobs
  whose version stamped ``chunk_config_version`` is the same constant.
  No other legacy jobs ever run on this scheduler.
* Background (resume) — only jobs belonging to documents named by
  the opt-in ``--resume-history-ids`` allow-list. The allow-list is
  explicit positive document IDs; the default is empty so the
  resume pool is dormant unless the operator opts in. The pool is
  restricted to current, non-deleted documents in active workspaces
  and to due ``created``/``retry`` tasks, with stages
  parsing/stored/chunking/understanding/preview/
  dataset_catalog/dataset_artifact/embedding (``knowledge_enhancement``
  is never resumed on this path). Embedding jobs additionally require
  ``embedding_profile_id`` to match the current active profile as
  recorded by ``AIRuntimeConfig.active_embedding_profile_id``. The
  resume pool never resets ``failed`` or ``processing`` jobs and
  never mutates source payloads. The pool round-robins through its
  eight stages so a vector backlog cannot starve parsing.

A weighted schedule (4 foreground, 1 history) keeps both pipelines
moving whenever both are non-empty; one pool draining lets the other
take over. The single history slot alternates resume and assisted
work when both pools are present, falling back when one is empty. The foreground
itself round-robins through the stage cycle, starting at parsing so
endless uploads cannot starve chunking and embedding.

At most one daemon is allowed at a time: the CLI takes a
session-level advisory lock on a dedicated connection and exits
with a clear error if another instance already holds it. The
daemon handles ``SIGTERM``/``SIGINT`` between jobs. Forced termination
and process crashes still require operator inspection of processing jobs.

Tests in ``apps/worker/tests/test_priority_maintenance.py`` exercise
the SQLite-backed scheduler logic and the cut-off/authorisation
filters. They never touch PostgreSQL.
"""

from __future__ import annotations

import argparse
import datetime
import json
import signal
import sys
import time
from pathlib import Path
from typing import Sequence

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import structlog
from sqlalchemy import and_, create_engine, or_, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from apps.api.models.auth import AIRuntimeConfig
from apps.api.models.documents import Document, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.models.workspaces import Workspace
from apps.api.services.workspaces import clear_workspace_context
from apps.worker.core.config import settings
from apps.api.services.chunking_candidate import MAX_BLOCKS
from apps.worker.services.processor import (
    ADAPTIVE_CHUNKING_CONFIG,
    ASSISTED_CHUNKING_CONFIG,
    CHUNKING_STAGE,
    DATASET_SEMANTICS_STAGE,
    process_single_job,
    utc_now,
)


EMBEDDING_STAGE = "embedding"

# Foreground cycle starts at parsing so freshly enqueued parsing jobs
# make progress before endless uploads refill the queue. ``stored`` is
# the legacy alias of parsing and shares the parsing slot.
FOREGROUND_STAGES: tuple[str, ...] = (
    "parsing",
    CHUNKING_STAGE,
    "dataset_catalog",
    "dataset_artifact",
    "preview",
    "understanding",
    EMBEDDING_STAGE,
    "stored",
    DATASET_SEMANTICS_STAGE,
    "knowledge_enhancement",
)

# Background resume pool runs the same eight production stages as the
# foreground cycle, but never the optional ``knowledge_enhancement``
# extension. The cycle order keeps parsing first so a vector backlog
# cannot starve parsing work, then rotates through chunking,
# dataset_catalog, dataset_artifact, preview, understanding, embedding
# and the legacy ``stored`` alias.
RESUME_STAGES: tuple[str, ...] = (
    "parsing",
    CHUNKING_STAGE,
    "dataset_catalog",
    "dataset_artifact",
    "preview",
    "understanding",
    EMBEDDING_STAGE,
    "stored",
    DATASET_SEMANTICS_STAGE,
)

FOREGROUND_WEIGHT = 4
HISTORY_WEIGHT = 1
PDF_DOCUMENT_TYPE = "pdf"

# A stable, project-private bigint identifying the priority daemon.
PRIORITY_DAEMON_LOCK_KEY = 89117342

DEFAULT_POLL_INTERVAL = 1.0

# Module-level shutdown flag manipulated by the SIGTERM/SIGINT handler.
_shutdown_requested = False


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )


logger = structlog.get_logger()


def _request_shutdown(*_args) -> None:
    global _shutdown_requested
    _shutdown_requested = True


def is_shutdown_requested() -> bool:
    return _shutdown_requested


def reset_shutdown_flag() -> None:
    """Test hook: clear the shutdown flag between invocations."""
    global _shutdown_requested
    _shutdown_requested = False


def parse_iso8601_utc(value: str) -> datetime.datetime:
    """Parse a strict ISO 8601 UTC timestamp; rejects naive datetimes."""
    text_value = (value or "").strip()
    if not text_value:
        raise argparse.ArgumentTypeError(
            "--new-since is required and must be ISO 8601"
        )
    # ``fromisoformat`` accepts trailing ``Z`` only on Python 3.11+.
    normalised = text_value.replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid ISO 8601 timestamp {value!r}: {exc}"
        ) from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"timestamp {value!r} must include a timezone (use 'Z' or '+00:00')"
        )
    return parsed.astimezone(datetime.timezone.utc)


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected positive integer, got {value!r}"
        ) from exc
    if number <= 0:
        raise argparse.ArgumentTypeError(
            f"expected positive integer, got {number}"
        )
    return number


def _is_pdf_version(session: Session, version_id: int) -> bool:
    version = session.get(DocumentVersion, version_id)
    payload = version.structured_content or {} if version is not None else {}
    if payload.get("document_type") != PDF_DOCUMENT_TYPE:
        return False
    if len(payload.get("blocks") or []) > MAX_BLOCKS:
        version.meta = {**(version.meta or {}), "chunking_policy_fallback": {
            "reason": "assisted_block_limit", "limit": MAX_BLOCKS,
            "blocks": len(payload["blocks"]), "strategy": "profile_rules",
        }}
        return False
    return True


def _due_filter(now: datetime.datetime):
    return or_(
        ProcessingJob.next_retry_at.is_(None),
        ProcessingJob.next_retry_at <= now,
    )


def _claim_one_foreground_job(
    session: Session,
    cutoff: datetime.datetime,
    stage: str | None = None,
    adaptive: bool = False,
) -> int | None:
    """Claim exactly one foreground job; commit before invoking the worker."""
    clear_workspace_context(session)
    now = utc_now()
    conditions = [
        Document.is_deleted.is_(False),
        Workspace.status == "active",
        Document.current_version_id == DocumentVersion.id,
        DocumentVersion.document_id == Document.id,
        or_(DocumentVersion.created_at >= cutoff, and_(
            ProcessingJob.stage == "knowledge_enhancement",
            ProcessingJob.created_at >= cutoff,
            ProcessingJob.config_version.like("enhancement:%"),
        )),
        ProcessingJob.status.in_(("created", "retry")),
        _due_filter(now),
    ]
    if stage is not None:
        if stage in ("parsing", "stored"):
            conditions.append(ProcessingJob.stage.in_(("parsing", "stored")))
        else:
            conditions.append(ProcessingJob.stage == stage)
    else:
        conditions.append(ProcessingJob.stage.in_(FOREGROUND_STAGES))
    job = session.scalar(
        select(ProcessingJob)
        .join(Document, Document.id == ProcessingJob.document_id)
        .join(
            DocumentVersion,
            DocumentVersion.id == ProcessingJob.document_version_id,
        )
        .join(Workspace, Workspace.id == Document.workspace_id)
        .where(*conditions)
        .order_by(ProcessingJob.id)
        .with_for_update(of=ProcessingJob, skip_locked=True)
        .limit(1)
    )
    if job is None:
        session.rollback()
        return None
    if job.config_version in {ASSISTED_CHUNKING_CONFIG, ADAPTIVE_CHUNKING_CONFIG}:
        pass  # A retry keeps its explicitly assigned policy across daemon restarts.
    elif adaptive and job.stage == CHUNKING_STAGE:
        version = session.get(DocumentVersion, job.document_version_id)
        payload = (version.structured_content or {}) if version else {}
        if payload.get("document_type") in {"pdf", "doc", "docx", "md", "markdown", "txt", "html", "note"}:
            if len(payload.get("blocks") or []) <= MAX_BLOCKS:
                job.config_version = ADAPTIVE_CHUNKING_CONFIG
            else:
                version.meta = {**(version.meta or {}), "chunking_policy_fallback": {
                    "reason": "assisted_block_limit", "limit": MAX_BLOCKS,
                    "blocks": len(payload["blocks"]), "strategy": "profile_rules",
                }}
    elif (
        job.stage == CHUNKING_STAGE
        and job.config_version != ASSISTED_CHUNKING_CONFIG
        and _is_pdf_version(session, job.document_version_id)
    ):
        job.config_version = ASSISTED_CHUNKING_CONFIG
    job.status = "processing"
    job.started_at = now
    job.finished_at = None
    session.add(job)
    session.commit()
    return job.id


def _claim_one_background_job(
    session: Session,
    history_ids: Sequence[int],
) -> int | None:
    """Claim exactly one background job from the explicit allow-list."""
    clear_workspace_context(session)
    if not history_ids:
        return None
    now = utc_now()
    chunk_config = DocumentVersion.meta["chunk_config_version"].as_string()
    job = session.scalar(
        select(ProcessingJob)
        .join(Document, Document.id == ProcessingJob.document_id)
        .join(
            DocumentVersion,
            DocumentVersion.id == ProcessingJob.document_version_id,
        )
        .join(Workspace, Workspace.id == Document.workspace_id)
        .where(
            Document.id.in_(tuple(history_ids)),
            Document.is_deleted.is_(False),
            Workspace.status == "active",
            Document.current_version_id == DocumentVersion.id,
            DocumentVersion.document_id == Document.id,
            ProcessingJob.status.in_(("created", "retry")),
            _due_filter(now),
            or_(
                and_(
                    ProcessingJob.stage == CHUNKING_STAGE,
                    ProcessingJob.config_version == ASSISTED_CHUNKING_CONFIG,
                ),
                and_(
                    ProcessingJob.stage == EMBEDDING_STAGE,
                    chunk_config == ASSISTED_CHUNKING_CONFIG,
                ),
            ),
        )
        .order_by(ProcessingJob.id)
        .with_for_update(of=ProcessingJob, skip_locked=True)
        .limit(1)
    )
    if job is None:
        session.rollback()
        return None
    job.status = "processing"
    job.started_at = now
    job.finished_at = None
    session.add(job)
    session.commit()
    return job.id


def _active_embedding_profile_id(session: Session) -> int | None:
    """Return the currently active embedding profile ID, or ``None``.

    The resume pool only runs embedding work for the active profile
    so a build in progress or a freshly retired profile cannot pick
    up new resume candidates. A missing or empty ``AIRuntimeConfig``
    row disables the embedding slot of the resume pool but does not
    raise: parsing, chunking and the other stages keep draining.
    """

    row = session.scalar(select(AIRuntimeConfig.active_embedding_profile_id).limit(1))
    if row is None:
        return None
    return int(row)


def _claim_one_resume_job(
    session: Session,
    resume_ids: Sequence[int],
    stage: str,
) -> int | None:
    """Claim exactly one resume-pool job; commit before invoking the worker.

    The pool never resets ``failed`` or ``processing`` jobs and never
    mutates the source payload or the job's ``config_version``:
    everything is funneled through the shared processor and the same
    ``FOR UPDATE SKIP LOCKED`` discipline used elsewhere.
    """

    clear_workspace_context(session)
    if not resume_ids or stage not in RESUME_STAGES:
        return None
    now = utc_now()
    active_profile_id = _active_embedding_profile_id(session) if stage == EMBEDDING_STAGE else None
    if stage == EMBEDDING_STAGE and active_profile_id is None:
        # No active profile means retrieval has no vector space to
        # read from; resume embedding would be a no-op for the
        # operator and could leave the chunk stranded in
        # ``processing`` if a crash happened mid-call. Skip the slot
        # for this iteration; the other seven stages keep draining.
        return None
    conditions = [
        Document.id.in_(tuple(resume_ids)),
        Document.is_deleted.is_(False),
        Workspace.status == "active",
        Document.current_version_id == DocumentVersion.id,
        DocumentVersion.document_id == Document.id,
        ProcessingJob.status.in_(("created", "retry")),
        _due_filter(now),
    ]
    if stage in ("parsing", "stored"):
        conditions.append(ProcessingJob.stage.in_(("parsing", "stored")))
    else:
        conditions.append(ProcessingJob.stage == stage)
    if stage == EMBEDDING_STAGE:
        conditions.append(
            ProcessingJob.embedding_profile_id == active_profile_id
        )
    job = session.scalar(
        select(ProcessingJob)
        .join(Document, Document.id == ProcessingJob.document_id)
        .join(
            DocumentVersion,
            DocumentVersion.id == ProcessingJob.document_version_id,
        )
        .join(Workspace, Workspace.id == Document.workspace_id)
        .where(*conditions)
        .order_by(ProcessingJob.id)
        .with_for_update(of=ProcessingJob, skip_locked=True)
        .limit(1)
    )
    if job is None:
        session.rollback()
        return None
    job.status = "processing"
    job.started_at = now
    job.finished_at = None
    session.add(job)
    session.commit()
    return job.id


def _dispatch(session: Session, job_id: int, *, pool: str) -> bool:
    try:
        outcome = process_single_job(session, job_id)
    except Exception as exc:
        session.rollback()
        job = session.get(ProcessingJob, job_id)
        if job is not None and job.stage == "knowledge_enhancement":
            from apps.worker.services.enhancement_processor import fail_enhancement_job
            fail_enhancement_job(session, job_id)
            logger.warning("priority_enhancement_error", job_id=job_id)
            return False
        if job is not None:
            job.status = "failed"
            job.finished_at = utc_now()
            job.next_retry_at = None
            job.last_error = "维护执行异常，需检查后重试"
            job.error_details = {"reason": "priority_executor_exception", "type": type(exc).__name__}
            session.commit()
        logger.warning(
            "priority_job_exception",
            pool=pool,
            job_id=job_id,
            reason=type(exc).__name__,
        )
        return False
    logger.info(
        "priority_job_done",
        pool=pool,
        job_id=job_id,
        result="completed" if outcome else "failed",
    )
    return bool(outcome)


def _run_foreground_step(
    session: Session,
    cutoff: datetime.datetime,
    state: dict,
) -> bool:
    """Try to claim one foreground job; advance the round-robin cursor."""
    for _ in FOREGROUND_STAGES:
        stage = FOREGROUND_STAGES[state["fg_cursor"] % len(FOREGROUND_STAGES)]
        state["fg_cursor"] = (state["fg_cursor"] + 1) % len(FOREGROUND_STAGES)
        options = {"adaptive": True} if state.get("adaptive") else {}
        job_id = _claim_one_foreground_job(session, cutoff, stage=stage, **options)
        if job_id is not None:
            _dispatch(session, job_id, pool="foreground")
            return True  # Count attempted work, not only successful jobs.
    return False


def _run_history_step(
    session: Session,
    history_ids: Sequence[int],
) -> bool:
    if not history_ids:
        return False
    job_id = _claim_one_background_job(session, history_ids)
    if job_id is None:
        return False
    _dispatch(session, job_id, pool="history")
    return True


def _run_resume_step(
    session: Session,
    state: dict,
    resume_ids: Sequence[int],
) -> bool:
    """Try to claim one resume-pool job; advance the round-robin cursor.

    The cursor is held in ``state["resume_cursor"]`` so a long-lived
    serve loop rotates through the eight production stages and a
    vector backlog cannot starve parsing.
    """

    if not resume_ids:
        return False
    for _ in RESUME_STAGES:
        cursor = state.get("resume_cursor", 0)
        stage = RESUME_STAGES[cursor % len(RESUME_STAGES)]
        state["resume_cursor"] = (cursor + 1) % len(RESUME_STAGES)
        job_id = _claim_one_resume_job(session, resume_ids, stage=stage)
        if job_id is not None:
            _dispatch(session, job_id, pool="resume")
            return True
    return False


def run_priority_iteration(
    session: Session,
    *,
    cutoff: datetime.datetime,
    history_ids: Sequence[int] | None,
    state: dict,
) -> int:
    """One weighted cycle: up to 4 foreground claims, then 1 history claim.

    The history slot alternates the opt-in resume pool
    (``state["resume_ids"]``) and the assisted
    pool (``history_ids``), so an empty resume allow-list keeps the
    original assisted-only behaviour. ``resume_ids`` is carried in
    the ``state`` dict so the public signature stays stable for
    tests that monkeypatch this function directly.

    Returns the number of jobs processed in this iteration. The cycle
    falls back to the other pool when the preferred one is empty so a
    one-sided backlog keeps moving instead of idling.
    """
    history_ids = list(history_ids or [])
    resume_ids = list(state.get("resume_ids") or [])
    processed = 0
    for slot in range(FOREGROUND_WEIGHT + HISTORY_WEIGHT):
        if is_shutdown_requested():
            break
        foreground = lambda: _run_foreground_step(session, cutoff, state)
        def history():
            resume = lambda: _run_resume_step(session, state, resume_ids)
            assisted = lambda: _run_history_step(session, history_ids)
            prefer_resume = not state.get("history_prefer_assisted", False)
            state["history_prefer_assisted"] = prefer_resume
            first, second = (resume, assisted) if prefer_resume else (assisted, resume)
            return first() or second()
        preferred, fallback = (foreground, history) if slot < FOREGROUND_WEIGHT else (history, foreground)
        if preferred() or fallback():
            processed += 1
        else:
            break
    return processed


def acquire_daemon_lock(engine: Engine) -> Connection | None:
    """Take a session-level advisory lock on a dedicated connection.

    Returns the connection (caller is responsible for releasing it) or
    ``None`` when the database engine does not support advisory locks
    (e.g. SQLite in the test suite).
    """
    if not engine.dialect.name.startswith("postgres"):
        return None
    connection = engine.connect()
    try:
        got = connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": PRIORITY_DAEMON_LOCK_KEY},
        ).scalar()
    except Exception:
        connection.close()
        raise
    if not got:
        connection.close()
        raise RuntimeError(
            "another priority maintenance daemon is already running"
        )
    connection.commit()  # Session-level lock survives commit; avoid idle transactions.
    return connection


def release_daemon_lock(connection: Connection | None) -> None:
    if connection is None:
        return
    try:
        connection.execute(
            text("SELECT pg_advisory_unlock(:key)"),
            {"key": PRIORITY_DAEMON_LOCK_KEY},
        )
    finally:
        connection.close()


def run_priority_daemon(
    engine: Engine,
    *,
    cutoff: datetime.datetime,
    history_ids: Sequence[int] | None = None,
    once: bool = False,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    install_signal_handlers: bool = True,
    lock_connection: Connection | None = None,
    adaptive: bool = False,
    resume_ids: Sequence[int] | None = None,
) -> int:
    """Main loop: claim a job, hand it to the worker, repeat until shutdown.

    ``install_signal_handlers`` is exposed so tests can drive the loop
    without fighting the real signal infrastructure. ``resume_ids`` is
    the opt-in allow-list for the new background resume pool; it
    defaults to ``None`` so the pool stays dormant unless the
    operator passes it via the CLI (``--resume-history-ids``).
    """
    if install_signal_handlers:
        try:
            signal.signal(signal.SIGTERM, _request_shutdown)
            signal.signal(signal.SIGINT, _request_shutdown)
        except ValueError:
            # signal handlers can only be installed from the main
            # thread; secondary callers should use the flag directly.
            pass
    Session = sessionmaker(bind=engine)
    state = {
        "fg_cursor": 0,
        "fg_weight": 0,
        "adaptive": adaptive,
        "resume_ids": list(resume_ids or []),
        "resume_cursor": 0,
    }
    processed = 0
    with Session() as session:
        while not is_shutdown_requested():
            if lock_connection is not None:
                if lock_connection.closed or lock_connection.invalidated:
                    raise RuntimeError("priority daemon lock connection lost")
                lock_connection.execute(text("SELECT 1"))
                lock_connection.commit()
            try:
                session.rollback()
                processed += run_priority_iteration(
                    session,
                    cutoff=cutoff,
                    history_ids=history_ids,
                    state=state,
                )
            except Exception as exc:
                session.rollback()
                logger.warning(
                    "priority_iteration_error",
                    reason=type(exc).__name__,
                )
            if once or is_shutdown_requested():
                break
            if poll_interval > 0:
                time.sleep(poll_interval)
    logger.info("priority_daemon_stopped", processed=processed)
    return processed


def main(argv: Sequence[str] | None = None) -> int:
    reset_shutdown_flag()
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--new-since",
        required=True,
        type=parse_iso8601_utc,
        help="ISO 8601 UTC cutoff; only versions created at or after this "
        "moment are eligible for foreground work.",
    )
    parser.add_argument(
        "--history-ids",
        nargs="*",
        type=_positive_int,
        default=[],
        help="Optional positive document IDs the maintenance scheduler "
        "is allowed to drain in the assisted history pool (chunking "
        "with the new config and embedding whose version stamped "
        "chunk_config_version is the same constant). Without this "
        "argument the assisted background pool is empty.",
    )
    parser.add_argument(
        "--resume-history-ids",
        nargs="*",
        type=_positive_int,
        default=[],
        help="Optional positive document IDs the maintenance scheduler "
        "is allowed to drain in the opt-in resume pool. The resume "
        "pool is dormant by default; the allow-list is a separate "
        "set of document IDs from --history-ids. Only current, "
        "non-deleted documents in active workspaces are eligible, "
        "only due created/retry tasks run, and only the production "
        "stages (parsing/stored/chunking/understanding/preview/"
        "dataset_catalog/dataset_artifact/embedding) are claimed; "
        "knowledge_enhancement never resumes on this path. Embedding "
        "tasks additionally require the active embedding profile. "
        "Failed and processing jobs are never reset and source "
        "payloads are never mutated.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single weighted cycle and exit (test-friendly).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="Seconds to sleep between iterations in serve mode.",
    )
    parser.add_argument(
        "--adaptive-chunking", action="store_true",
        help="Opt into structure-aware adaptive-v2 for fresh text documents; does not reparse history.",
    )
    args = parser.parse_args(argv)
    if not 0.1 <= args.poll_interval <= 30:
        parser.error("--poll-interval must be between 0.1 and 30 seconds")
    engine = create_engine(settings.database_url)
    connection: Connection | None = None
    try:
        connection = acquire_daemon_lock(engine)
    except RuntimeError as exc:
        logger.error("priority_daemon_lock_failed", reason=str(exc))
        engine.dispose()
        return 2
    try:
        processed = run_priority_daemon(
            engine,
            cutoff=args.new_since,
            history_ids=args.history_ids,
            once=args.once,
            poll_interval=args.poll_interval,
            lock_connection=connection,
            adaptive=args.adaptive_chunking,
            resume_ids=args.resume_history_ids,
        )
    finally:
        release_daemon_lock(connection)
        engine.dispose()
    print(json.dumps({"processed": processed}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
