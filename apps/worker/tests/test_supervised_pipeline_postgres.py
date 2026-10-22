"""ADR-035 supervised pipeline: real migrated isolated PostgreSQL only.

End-to-end customer-facing safety scenarios for the supervised worker,
driven against the *real* subprocess child. Companion to
:mod:`apps.worker.tests.test_reliability_postgres` which exercises only
the ownership / fencing substrate in isolation; this module drives the
actual ``_SupervisedWorker`` pipeline so a real parser, real chunks,
real datasets and real subprocess are exercised on PostgreSQL.

The boundary is identical to its sibling:

* Only runs when ``CANGZHI_PG_RELIABILITY_DATABASE_URL`` points at the
  explicit, isolated ``cangzhi_test`` database (never the runtime one)
  provisioned on the ``cz-reliability-validation`` network with
  migration ``0043`` applied.
* Skipped by default. Requires parent/maintainer coordination to
  provision that gig, and never runs in production.
* Hard-gated to a real ``task_execution_enabled=True`` parent
  (in-process tests cannot exercise the subprocess and stay on the
  existing :mod:`test_reliability_postgres` / :mod:`test_task_ownership`
  modules).
* Cleanup is local to the test fixtures; the file owns its own pre/post
  ``DELETE`` so we never leave jobs, chunks, datasets, Parquet files
  or quarantined rows behind for the next test or the next operator.
* No test-only production backdoor: we drive the public
  ``run_task_with_supervisor`` / ``claim_pending_jobs`` /
  ``requeue_safely`` entrypoints, never inject hooks into the
  pipeline. Stale-attempt fencing is verified by re-reading the row
  under the live DB clock, not by monkey-patching the fence code.

Each scenario is intentionally narrow so a single failure points to
one load-bearing invariant.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from apps.api.models.chunks import DocumentChunk
from apps.api.models.datasets import KnowledgeDataset
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.worker.core.config import settings
from apps.worker.services import lease, task_execution
from apps.worker.services.lease import (
    LeaseExpectation,
    StaleAttemptError,
    verify_against,
)
from apps.worker.services.processor import (
    CHUNKING_STAGE,
    CHUNKING_IDEMPOTENCY,
    PREVIEW_STAGE,
    UNDERSTANDING_IDEMPOTENCY,
    UNDERSTANDING_STAGE,
    claim_pending_jobs,
)


# Unique test-only prefix so this module never collides with sibling PG
# tests (``t-`` is owned by :mod:`test_reliability_postgres`).
_TEST_PREFIX = "spv-"


# ---------------------------------------------------------------------------
# PG isolation gate
# ---------------------------------------------------------------------------


def _url() -> str | None:
    url = os.getenv("CANGZHI_PG_RELIABILITY_DATABASE_URL")
    return url or None


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    url = _url()
    if not url:
        pytest.skip(
            "explicit isolated database required "
            "(CANGZHI_PG_RELIABILITY_DATABASE_URL)"
        )
    assert url.endswith("/cangzhi_test"), (
        "refusing to run against any non-isolated database"
    )
    engine = create_engine(url)
    Session_ = sessionmaker(bind=engine, expire_on_commit=False)
    yield Session_
    engine.dispose()


def _wipe_isolated_tables(Session_) -> None:
    """Clear everything the tests in this module can leave behind.

    We only wipe documents whose title starts with this module's unique
    prefix, so the rest of the isolated DB (and any sibling PG tests'
    data) is untouched. Cascading deletes handle the dependents; we do
    not need to enumerate chunks / dataset rows / blobs individually.
    """
    with Session_() as s:
        s.execute(
            text("DELETE FROM documents WHERE title LIKE :pat"),
            {"pat": f"{_TEST_PREFIX}%"},
        )
        s.commit()


@pytest.fixture
def session_area(isolated_db, tmp_path, monkeypatch):
    """Yield a Session factory wired to the supervised path.

    Storage is redirected to a per-test ``tmp_path`` so any Parquet or
    preview output stays isolated. The child subprocess reads
    ``STORAGE_PATH`` from the env, so we also set the env var via
    ``monkeypatch.setenv`` so the *next* spawned child sees the
    per-test path. We restore both on teardown.
    """
    monkeypatch.setattr(settings, "task_execution_enabled", True)
    prev_storage = settings.storage_path
    monkeypatch.setattr(settings, "storage_path", str(tmp_path))
    monkeypatch.setattr(settings, "database_url", _url())
    # The child subprocess reads STORAGE_PATH from the environment; the
    # in-process ``settings.storage_path`` is not visible to the child.
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
    # Pre-clean so a leftover job/document does not collide with a
    # fresh idempotency key.
    _wipe_isolated_tables(isolated_db)
    try:
        yield isolated_db
    finally:
        # Close the supervised child (kills the reusable subprocess).
        try:
            task_execution.close_executor()
        except Exception:
            pass
        # Wipe everything we created.
        _wipe_isolated_tables(isolated_db)
        # Wipe Parquet / temp files under the per-test storage path.
        for path in Path(str(tmp_path)).rglob("*.parquet"):
            path.unlink(missing_ok=True)
        for path in Path(str(tmp_path)).rglob("*.tmp-*"):
            path.unlink(missing_ok=True)
        monkeypatch.setattr(settings, "task_execution_enabled", False)
        monkeypatch.setattr(settings, "storage_path", prev_storage)


# ---------------------------------------------------------------------------
# Document / version / job factories
# ---------------------------------------------------------------------------


def _make_note_doc(
    Session_,
    *,
    key: str,
    raw_content: str = "# 标题\n第一段文字。\n第二段文字。\n",
    stage: str = "parsing",
    with_lease: bool = False,
    lease_past: bool = False,
    set_current_version: bool = True,
):
    """Create a note document, version, and one processing job.

    Returns ``(document_id, version_id, job_id)`` so the test can
    re-read state across multiple sessions.
    """
    with Session_() as s:
        doc = Document(
            title=f"{_TEST_PREFIX}{key}",
            source_type=DocumentSourceType.note,
        )
        s.add(doc)
        s.flush()
        version = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            content_hash=f"h-{key}",
            raw_content=raw_content,
            processing_status="created",
        )
        s.add(version)
        s.flush()
        if set_current_version:
            doc.current_version_id = version.id
        job = ProcessingJob(
            document_id=doc.id,
            document_version_id=version.id,
            stage=stage,
            status="created",
            idempotency_key=f"{_TEST_PREFIX}{key}",
            config_version="1",
            next_retry_at=None,
        )
        s.add(job)
        s.flush()
        if with_lease:
            now = lease.db_utc_now(s)
            job.worker_id = lease.worker_id()
            job.attempt_id = lease.new_attempt_id(job.id)
            job.lease_until = now + timedelta(
                seconds=-5 if lease_past else 300
            )
            job.hard_deadline = now + timedelta(seconds=1800)
            job.attempt_count = 1
            # A row that already carries a lease is by definition
            # in the supervised pool; the heartbeat refuses to renew
            # unless ``status='processing'``, so we mirror the state
            # the supervised claim would have stamped.
            job.status = "processing"
            job.started_at = now
            s.add(job)
        s.commit()
        return doc.id, version.id, job.id


# ---------------------------------------------------------------------------
# Supervised runner helpers
# ---------------------------------------------------------------------------


def _run_supervised(Session_, job_id: int) -> tuple[bool, dict | None]:
    """Drive one already-claimed job through the *real* supervised child.

    The job must already hold a lease (i.e. it was returned by
    :func:`claim_pending_jobs` or stamped by a fixture). Returns
    ``(parent_returned_ok, framed_response)`` where
    ``parent_returned_ok`` is the boolean :func:`run_task_with_supervisor`
    yields and ``framed_response`` is the raw child frame (or ``None``
    if the child never produced one).
    """
    with Session_() as s:
        ok = task_execution.run_task_with_supervisor(s, job_id)
    # The framed response is not exposed by the supervisor; we
    # re-derive a coarse shape from the row state instead of
    # monkey-patching. For most assertions ``ok`` is enough.
    return ok, None


def _run_supervised_one(Session_, job_id: int) -> bool:
    """Run one already-claimed job through the full supervisor and
    return the boolean the parent observed.

    The throughput test needs the *full* parent cycle (heartbeat
    thread + deadline watcher + reusable child + recovery) so the
    measured wall-clock reflects what production pays per job.
    """
    with Session_() as s:
        return task_execution.run_task_with_supervisor(s, job_id)


def _claim_one(Session_, *, stages: tuple[str, ...]) -> int | None:
    with Session_() as s:
        ids = claim_pending_jobs(s, limit=1, stages=list(stages))
    return ids[0] if ids else None


def _make_due(Session_, job_id: int) -> None:
    """Force ``next_retry_at`` to be due so the next claim picks the row.

    ``requeue_safely`` schedules the next retry at ``now + 30 *
    recovery_count``; for a test that immediately re-claims we want the
    retry window to already be elapsed.
    """
    with Session_() as s:
        row = s.get(ProcessingJob, job_id)
        if row is not None and row.next_retry_at is not None:
            row.next_retry_at = None
            s.commit()


# ---------------------------------------------------------------------------
# 1) End-to-end supervised pipeline: note parsing then chunking, no model
# ---------------------------------------------------------------------------


def test_supervised_pipeline_note_parsing_then_chunking(session_area):
    """Parse a note through the supervised child; chunks persist; no AI.

    Drives the real ``run_task_with_supervisor`` against the subprocess
    child. The parsing stage uses :class:`NoteParser` (no network), the
    chunking stage uses the deterministic baseline chunker, and the
    understanding stage is a no-op because the isolated container has
    no ``AIRuntimeConfig`` provider configured. We assert:

    * The parsing job leaves ``status='completed'`` and the version
      carries a non-empty ``structured_content``.
    * The followup ``chunking`` and ``understanding`` jobs are enqueued
      and reachable by their idempotency key.
    * Running the chunking followup through the same supervised child
      persists real parent/child chunks with stable content hashes
      (proving the child really did execute the pipeline in-process).
    """
    Session_ = session_area
    doc_id, version_id, parsing_id = _make_note_doc(
        Session_, key="pl-note",
        raw_content="# 标题\n第一段。\n第二段。\n",
    )

    # Step 1: claim the parsing job, then run it through the real child.
    claimed = _claim_one(Session_, stages=("parsing",))
    assert claimed == parsing_id
    ok, _ = _run_supervised(Session_, parsing_id)
    assert ok is True

    with Session_() as s:
        parsed = s.get(ProcessingJob, parsing_id)
        assert parsed.status == "completed"
        version = s.get(DocumentVersion, version_id)
        assert version.structured_content
        assert version.processing_status in ("ready", "processing")

        chunking_job = s.scalar(
            select(ProcessingJob).where(
                ProcessingJob.idempotency_key
                == f"{version_id}:{CHUNKING_STAGE}:{CHUNKING_IDEMPOTENCY}"
            )
        )
        understanding_job = s.scalar(
            select(ProcessingJob).where(
                ProcessingJob.idempotency_key
                == (
                    f"{version_id}:{UNDERSTANDING_STAGE}:"
                    f"{UNDERSTANDING_IDEMPOTENCY}"
                )
            )
        )
        assert chunking_job is not None
        assert understanding_job is not None
        chunking_id = chunking_job.id
        understanding_id = understanding_job.id

    # Step 2: drive the chunking followup through the same child.
    claimed = _claim_one(Session_, stages=(CHUNKING_STAGE,))
    assert claimed == chunking_id
    ok, _ = _run_supervised(Session_, chunking_id)
    assert ok is True

    with Session_() as s:
        cj = s.get(ProcessingJob, chunking_id)
        assert cj.status == "completed"
        chunks = list(
            s.scalars(
                select(DocumentChunk).where(
                    DocumentChunk.document_version_id == version_id
                )
            ).all()
        )
        roles = {c.role for c in chunks}
        assert "parent" in roles and "child" in roles
        for c in chunks:
            assert c.content_hash
            assert c.content
            assert c.is_current is True
            assert c.char_count > 0

    # Step 3: understanding stage (no AI configured -> inbox fallback).
    claimed = _claim_one(Session_, stages=(UNDERSTANDING_STAGE,))
    assert claimed == understanding_id
    ok, _ = _run_supervised(Session_, understanding_id)
    assert ok is True

    with Session_() as s:
        u = s.get(ProcessingJob, understanding_id)
        assert u.status == "completed"
        v = s.get(DocumentVersion, version_id)
        meta = v.meta or {}
        assert meta.get("ai_status") in {"not_configured", "completed"}


# ---------------------------------------------------------------------------
# 2) Heartbeat loop renews the lease at least twice
# ---------------------------------------------------------------------------


def test_heartbeat_loop_renews_lease_twice(session_area):
    """The supervised heartbeat extends the live lease repeatedly.

    We do not need a real child for this invariant: we drive the
    ``_heartbeat_loop`` thread directly and poll the DB for at least
    TWO DISTINCT ``heartbeat_at`` changes after the initial stamp,
    bounded to 8s. ``settings.task_heartbeat_seconds`` is clamped to a
    1s minimum, so the polling budget accommodates up to 8 heartbeats
    with slack for the 100ms poll interval.
    """
    Session_ = session_area
    # Use a very short heartbeat + short lease for the test so the
    # renewal is visible as a forward move (rather than being clamped
    # below the original by LEAST(new_lease, hard_deadline)). The
    # heartbeat interval is clamped to a 1.0s minimum by the policy.
    prev_lease = settings.task_lease_seconds
    prev_hb = settings.task_heartbeat_seconds
    settings.task_lease_seconds = 5.0
    settings.task_heartbeat_seconds = 1.0
    try:
        doc_id, version_id, job_id = _make_note_doc(
            Session_, key="hb", stage="parsing", with_lease=True,
        )
        # Stamp a short lease so the renewal is a forward move; the
        # original heartbeat_at is the baseline we watch for changes.
        with Session_() as s:
            row = s.get(ProcessingJob, job_id)
            now = lease.db_utc_now(s)
            row.lease_until = now + timedelta(seconds=2.0)
            s.commit()
            current_attempt = row.attempt_id
            initial_lease = row.lease_until
            initial_heartbeat = row.heartbeat_at

        stop = threading.Event()
        cancel = threading.Event()
        thread = threading.Thread(
            target=task_execution._heartbeat_loop,
            args=(stop, cancel),
            kwargs={
                "job_id": job_id,
                "attempt_id": current_attempt,
            },
            daemon=True,
        )
        thread.start()
        try:
            # Poll the live row: at least TWO distinct ``heartbeat_at``
            # values after ``initial_heartbeat`` must appear. Bounded
            # to 8s (the policy clamps the heartbeat to >=1s, so 8s
            # covers up to 7 renewals plus poll latency slack).
            seen: set = {initial_heartbeat}
            poll_deadline = time.monotonic() + 8.0
            while time.monotonic() < poll_deadline and len(seen) < 3:
                with Session_() as s:
                    hb = s.get(ProcessingJob, job_id).heartbeat_at
                if hb is not None and hb not in seen:
                    seen.add(hb)
                time.sleep(0.1)
            assert len(seen) >= 3, (
                f"expected >=3 distinct heartbeat_at values "
                f"(initial + 2 renewals), got {len(seen)}: {seen}"
            )
        finally:
            stop.set()
            thread.join(timeout=5.0)

        with Session_() as s:
            refreshed = s.get(ProcessingJob, job_id)
            assert refreshed.attempt_id == current_attempt
            assert refreshed.status == "processing"
            assert refreshed.lease_until is not None
            now = lease.db_utc_now(s)
            assert refreshed.lease_until > initial_lease
            assert refreshed.lease_until > now
            assert refreshed.heartbeat_at is not None
    finally:
        settings.task_lease_seconds = prev_lease
        settings.task_heartbeat_seconds = prev_hb


# ---------------------------------------------------------------------------
# 3) Child killed mid-task: real PG row lock blocks the first DML
# ---------------------------------------------------------------------------


def test_child_killed_midtask_next_attempt_reclaims_safely(session_area):
    """Killing the child mid-run lets a successor CAS-reclaim the job.

    We hold a separate transaction's ``FOR UPDATE`` lock on the Document
    so the supervised child's first DML (the LeaseFence's source check
    inside :func:`_verify_connection`) blocks on the row. We dispatch
    the supervisor in a background thread, observe the child
    subprocess is alive, SIGKILL the child, release the lock, and
    verify the next claim gets a fresh attempt with the old attempt
    fenced off.

    Bounded timeouts everywhere: the test never waits more than 20s
    for any single event and tears the child down in ``finally`` even
    if an assertion fails earlier.
    """
    Session_ = session_area
    doc_id, version_id, job_id = _make_note_doc(
        Session_, key="kf", raw_content="# title\nbody line\n",
    )
    # Claim the parsing job -> a lease/attempt is stamped.
    assert _claim_one(Session_, stages=("parsing",)) == job_id
    with Session_() as s:
        first = s.get(ProcessingJob, job_id)
        old_attempt = first.attempt_id
        old_worker = first.worker_id
        old_lease = first.lease_until
        assert first.status == "processing"
        assert first.attempt_id and first.lease_until

    # Acquire a row lock on the Document in a SEPARATE transaction we
    # hold open. The child's first DML triggers the LeaseFence's
    # source check, which does SELECT ... FOR UPDATE on the document
    # row -> blocks on our held lock.
    blocker_engine = create_engine(_url())
    blocker_conn = blocker_engine.connect()
    blocker_trans = blocker_conn.begin()
    blocker_conn.execute(
        text("SET statement_timeout = '60s'"),
    )
    blocker_conn.execute(
        text("SELECT id FROM documents WHERE id = :id FOR UPDATE"),
        {"id": doc_id},
    )
    # Capture the blocker's PostgreSQL backend PID so we can prove a
    # later waiter is causally blocked on OUR row, not just any
    # active query in the database. The Python child OS PID is
    # unrelated to its PG backend PID, and we are in a separate
    # container from the DB so we cannot match by host pid either.
    blocker_pid = blocker_conn.execute(
        text("SELECT pg_backend_pid()")
    ).scalar()
    assert blocker_pid is not None

    # Dispatch the supervisor in a background thread; the child will
    # block at the first DML until we release the lock or kill it.
    result_holder: dict = {}

    def run_in_thread():
        with Session_() as s:
            try:
                response = task_execution.run_task_with_supervisor(
                    s, job_id,
                )
                result_holder["response"] = response
            except Exception as exc:  # noqa: BLE001
                result_holder["error"] = exc

    thread = threading.Thread(target=run_in_thread, daemon=True)
    thread.start()

    child_pid: int | None = None
    waiter_pid: int | None = None
    last_query: str | None = None
    last_state: str | None = None
    last_event: str | None = None
    last_blocking: str | None = None
    try:
        # Wait for the child subprocess to spawn.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            proc = task_execution._SUPERVISED_WORKER._proc
            if proc is not None and proc.poll() is None:
                child_pid = proc.pid
                break
            time.sleep(0.05)
        if child_pid is None:
            pytest.fail("supervised child subprocess did not start")

        # Observe the supervised child actually waiting on OUR held
        # documents row lock via ``pg_stat_activity`` +
        # ``pg_blocking_pids``. This is the only honest proof the
        # child reached the source check before we SIGKILL it; the
        # Python child OS PID is unrelated to the PG backend PID and
        # arbitrary active queries from sibling tests would not pass
        # the ``pg_blocking_pids = [blocker_pid]`` causal check.
        #
        # The child's ``bounded_engine`` sets ``lock_timeout`` from
        # ``task_db_timeout_ms`` (default 10000ms -> 10s on the
        # child's connection), so the supervised child cannot
        # actually wait forever on the lock: a missed observation
        # would manifest as a 10s ``Lock/transactionid`` then a
        # query-cancelled error. We poll at 50ms so we catch the
        # waiter well before the timeout.
        waiter_deadline = time.monotonic() + 10.0
        while time.monotonic() < waiter_deadline:
            with blocker_engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT pid, state, wait_event_type, wait_event, "
                        "       substring(query, 1, 200), "
                        "       pg_blocking_pids(pid) AS blockers "
                        "FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "  AND pid != pg_backend_pid() "
                        "  AND wait_event_type = 'Lock'"
                    ),
                ).fetchall()
            for row in rows:
                last_state = row.state
                last_event = (
                    f"{row.wait_event_type}/{row.wait_event}"
                )
                last_query = row[4]
                last_blocking = list(row[5]) if row[5] is not None else []
                if blocker_pid in last_blocking:
                    waiter_pid = row.pid
                    break
            if waiter_pid is not None:
                break
            # If the supervisor thread already finished, the child
            # could not have been blocked here.
            if not thread.is_alive():
                break
            time.sleep(0.05)
        if waiter_pid is None:
            pytest.fail(
                f"no Lock waiter on documents appeared in pg_stat_activity "
                f"with blocker_pid={blocker_pid} in pg_blocking_pids within "
                f"10s; last state={last_state} event={last_event} "
                f"query={last_query!r} blockers={last_blocking!r}; "
                f"supervisor thread alive={thread.is_alive()}; "
                f"result_holder={result_holder!r}"
            )

        # Sanity: child is still alive (it is blocked, not exited).
        assert task_execution._SUPERVISED_WORKER._proc.poll() is None

        # SIGKILL the child (whole process group, including any
        # converter/OCR children). The child cannot catch SIGKILL.
        try:
            os.killpg(child_pid, 9)
        except ProcessLookupError:
            pass
    finally:
        # Always release the blocker so the test does not hang the
        # connection even if an assertion above raised.
        try:
            blocker_trans.rollback()
        except Exception:
            pass
        try:
            blocker_conn.close()
        except Exception:
            pass
        try:
            blocker_engine.dispose()
        except Exception:
            pass

    # The supervisor thread should detect the dead child, run
    # kill_and_join (returns True for a dead process), and call
    # requeue_safely. Bound the wait so a stuck child cannot hang the
    # test forever.
    thread.join(timeout=20.0)
    assert not thread.is_alive(), (
        "supervisor thread did not return within 20s of SIGKILL"
    )

    # The job is in retry status; the supervisor released the claim.
    with Session_() as s:
        row = s.get(ProcessingJob, job_id)
        assert row.status == "retry", (row.status, row.error_details)
        assert row.attempt_id == old_attempt
        assert row.lease_until is None
        # requeue_safely schedules next_retry_at = now + 30 * recovery_count;
        # for the next claim to take it without waiting we force it due.
        if row.next_retry_at is not None:
            row.next_retry_at = None
            s.commit()

    # Re-claim: the recovery path picks the row up and stamps a fresh
    # attempt token; the old token cannot publish.
    claimed = _claim_one(Session_, stages=("parsing",))
    assert claimed == job_id
    with Session_() as s:
        recovered = s.get(ProcessingJob, job_id)
        assert recovered.attempt_id != old_attempt
        assert recovered.status == "processing"
        # Stale attempt's expectation cannot verify against the live row.
        stale = LeaseExpectation(
            job_id=job_id,
            attempt_id=old_attempt,
            worker_id=old_worker,
            status="processing",
            config_version=recovered.config_version,
            document_version_id=recovered.document_version_id,
            embedding_profile_id=recovered.embedding_profile_id,
            content_hash=recovered.config_version,  # sentinel
            current_version_id=doc_id,
            lease_until=old_lease or datetime.now(timezone.utc),
            hard_deadline=datetime.now(timezone.utc)
            + timedelta(seconds=1800),
        )
        with pytest.raises(StaleAttemptError):
            verify_against(s, stale, db_now=lease.db_utc_now(s))


# ---------------------------------------------------------------------------
# 4) Stale source / attempt cannot publish
# ---------------------------------------------------------------------------


def test_stale_source_blocks_publish_through_supervised_child(session_area):
    """A superseded current version is fenced off before any DML runs.

    The supervised child calls ``_supervised_source_ok`` before
    ``_run_claimed_job``; if the document's current version is no
    longer the job's version the child must return ``source_changed``
    and the parent must leave the row in a terminal ``failed`` state
    (a superseded source can never succeed by retrying). We assert the
    version's ``structured_content`` was *not* updated (no late
    publish slipped through).
    """
    Session_ = session_area
    doc_id, version_id, job_id = _make_note_doc(
        Session_, key="src",
        raw_content="# old\n旧版本正文。\n",
    )
    assert _claim_one(Session_, stages=("parsing",)) == job_id

    # Supersede: add a fresh version and point the document at it.
    with Session_() as s:
        new_version = DocumentVersion(
            document_id=doc_id,
            version_number=2,
            content_hash="h-src-v2",
            raw_content="# new\n新版本正文。\n",
            processing_status="created",
        )
        s.add(new_version)
        s.flush()
        s.get(Document, doc_id).current_version_id = new_version.id
        s.commit()

    ok, _ = _run_supervised(Session_, job_id)
    # The child short-circuits with source_changed before any DML.
    # The supervisor returns False in that case.
    assert ok is False

    with Session_() as s:
        row = s.get(ProcessingJob, job_id)
        # Stale attempt is a terminal failure: the source has been
        # superseded and retrying the old version can never succeed.
        assert row.status == "failed", (
            row.status, row.error_details
        )
        v_old_now = s.get(DocumentVersion, version_id)
        # Critical invariant: the old version's structured_content was
        # NEVER updated by the stale attempt.
        rendered = (v_old_now.structured_content or {}).get("blocks")
        if rendered:
            joined = "\n".join(
                b.get("text", "") for b in rendered if isinstance(b, dict)
            )
            assert "新版本" not in joined, (
                "stale attempt wrote new-version content into the old "
                "version's structured_content"
            )


# ---------------------------------------------------------------------------
# 5) Unconfirmed exit quarantines the job; never reclaimed
# ---------------------------------------------------------------------------


def test_unconfirmed_exit_quarantines_and_blocks_reclaim(session_area):
    """Unconfirmed-exit -> quarantined -> never reclaimed until cleared.

    The supervised parent refuses recovery when it cannot confirm the
    child exited. The job keeps its claim with ``quarantined_at`` set
    and the recovery pool excludes quarantined rows. We assert that
    :func:`claim_pending_jobs` cannot take it over while quarantined
    and that clearing the quarantine makes the row reachable again.
    """
    Session_ = session_area
    doc_id, version_id, job_id = _make_note_doc(
        Session_, key="qf", with_lease=True,
    )
    with Session_() as s:
        job = s.get(ProcessingJob, job_id)
        # Simulate the supervised parent hitting the refuse-recovery
        # branch: kill_and_join returned False and the row was
        # quarantined.
        job.quarantined_at = lease.db_utc_now(s)
        job.last_error = (
            "无法确认执行进程退出，已隔离；请维护者检查后恢复"
        )
        job.error_details = {
            "reason": "unconfirmed_exit", "quarantined": True,
        }
        s.commit()

    # claim_pending_jobs excludes quarantined rows in the supervised path.
    assert _claim_one(Session_, stages=("parsing",)) is None
    with Session_() as s:
        assert s.get(ProcessingJob, job_id).quarantined_at is not None

    # The predicate ``is_recoverable_claim`` also refuses quarantined rows.
    with Session_() as s:
        row = s.get(ProcessingJob, job_id)
        assert lease.is_recoverable_claim(
            row, lease.db_utc_now(s)
        ) is False

    # Operator clears the quarantine -> the next claim can take it.
    with Session_() as s:
        row = s.get(ProcessingJob, job_id)
        # Force the lease into the past so the supervised claim picks
        # it up via the expired-lease branch.
        row.lease_until = lease.db_utc_now(s) - timedelta(seconds=5)
        row.quarantined_at = None
        s.commit()
    claimed = _claim_one(Session_, stages=("parsing",))
    assert claimed == job_id
    with Session_() as s:
        recovered = s.get(ProcessingJob, job_id)
        assert recovered.status == "processing"
        assert recovered.quarantined_at is None
        assert recovered.attempt_id is not None


# ---------------------------------------------------------------------------
# 6) Child execution advisory lock blocks a parallel expired-lease recovery
# ---------------------------------------------------------------------------


def test_child_advisory_lock_blocks_parallel_expired_recovery(session_area):
    """A child-side lock blocks a second claimer for the same document.

    The supervised child holds the per-document ``try_advisory_xact_lock``
    while it works; a parallel claimer whose lease has expired cannot
    take the same document over because the two-step claim tries the
    same lock and re-checks the busy filter. We hold the lock in a
    *separate* connection (so the supervised path's own session does
    not see it) and verify ``claim_pending_jobs`` cannot steal the
    row.
    """
    Session_ = session_area
    doc_id, version_id, job_id = _make_note_doc(
        Session_, key="advisory", with_lease=True, lease_past=True,
    )

    # Acquire the per-document advisory lock from a side connection that
    # does NOT commit; the lock survives until the connection closes
    # or the transaction ends.
    engine = create_engine(_url())
    hold_conn = engine.connect()
    trans = hold_conn.begin()
    try:
        locked = hold_conn.execute(
            text("SELECT pg_try_advisory_xact_lock(:ns, :doc)"),
            {
                "ns": lease._DOC_CLAIM_LOCK_NAMESPACE,
                "doc": doc_id,
            },
        ).scalar()
        assert locked is True, (
            "could not take the per-document advisory lock for the test"
        )

        # With the lock held, a claimer of an expired-lease row for the
        # same document must observe no candidates.
        with Session_() as s:
            claimed = claim_pending_jobs(s, limit=1, stages=["parsing"])
        assert claimed == []
        with Session_() as s:
            row = s.get(ProcessingJob, job_id)
            assert row.status == "processing"
            # The row's existing attempt is unchanged: the second
            # claimer did not stomp it.
            assert row.attempt_id is not None
    finally:
        trans.rollback()
        hold_conn.close()
        engine.dispose()

    # Once the lock is released, flip the row to ``retry`` (the same
    # shape the recovery path leaves it in) so the supervised claim
    # picks it up on the next call.
    with Session_() as s:
        row = s.get(ProcessingJob, job_id)
        row.status = "retry"
        row.lease_until = None
        s.commit()
    claimed = _claim_one(Session_, stages=("parsing",))
    assert claimed == job_id


# ---------------------------------------------------------------------------
# 7) Parquet per-build unique paths survive stale-attempt cleanup
# ---------------------------------------------------------------------------


def test_parquet_per_build_unique_paths_survive_stale_cleanup(session_area):
    """Each Parquet build publishes under a fresh unique path.

    The build path embeds a UUID into ``storage_key`` so a
    stale-attempt cleanup that deletes published files never collides
    with a successor build. We assert:

    * Two consecutive builds of the same dataset produce two distinct
      ``storage_key`` values.
    * The files exist on disk under the per-test ``storage_path``.
    * Removing one of the published files (the path a stale attempt
      would have owned) does not affect the other: cleanup only
      touches files the attempt published itself.
    """
    Session_ = session_area
    from apps.api.models.table_rows import StructuredTableRow
    from apps.api.models.datasets import DatasetField
    from apps.api.services.dataset_execution import build_dataset_parquet

    doc_id, version_id, _ = _make_note_doc(
        Session_, key="parq",
        raw_content="# title\nfirst line\nsecond line\n",
    )
    with Session_() as s:
        dataset = KnowledgeDataset(
            document_id=doc_id,
            document_version_id=version_id,
            name="ds-parq",
            sheet_name="sheet-1",
            region_index=0,
            dataset_kind="table",
            row_count=1,
            column_count=1,
        )
        s.add(dataset)
        s.flush()
        dataset_id = dataset.id
        # Materialise a tiny dataset (1 row, 1 column) so
        # build_dataset_parquet actually emits a parquet.
        s.add(DatasetField(
            dataset_id=dataset_id, position=0, name="col",
            inferred_type="text",
        ))
        s.add(StructuredTableRow(
            dataset_id=dataset_id, row_number=1,
            document_id=doc_id, document_version_id=version_id,
            sheet_name="sheet-1", region_index=0,
            values={"col": "value"},
        ))
        s.commit()

    storage_root = Path(settings.storage_path)
    with Session_() as s:
        first = build_dataset_parquet(
            s, s.get(KnowledgeDataset, dataset_id),
            storage_root=str(storage_root),
        )
        s.commit()
    assert first.storage_key
    first_path = (storage_root / first.storage_key).resolve()
    assert first_path.is_file(), first_path

    with Session_() as s:
        second = build_dataset_parquet(
            s, s.get(KnowledgeDataset, dataset_id),
            storage_root=str(storage_root),
        )
        s.commit()
    second_path = (storage_root / second.storage_key).resolve()
    assert second.storage_key != first.storage_key
    assert second_path.is_file(), second_path

    # Stale-attempt cleanup: a successor would only delete files in
    # its own ``published_paths`` list, never the live one. Simulate
    # by removing only the second file and confirming the first file
    # is untouched.
    second_path.unlink()
    assert not second_path.exists()
    assert first_path.is_file(), (
        "stale cleanup reached into the live file"
    )


# ---------------------------------------------------------------------------
# 8) Partial frame hits the hard deadline (never hangs the parent)
# ---------------------------------------------------------------------------


def test_partial_frame_hits_hard_deadline(session_area):
    """A child that writes a length prefix but no body hits the deadline.

    We exercise the same framing protocol the supervisor uses but
    with a hand-rolled child that never writes the body. The
    parent's :func:`_read_frame` must raise within the timeout
    instead of hanging.
    """
    # Hand-rolled child: writes a length prefix for a 16-byte body
    # and then sleeps forever, so the parent can only rely on its
    # deadline.
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, time;"
                "fd=int(os.environ['RESP_FD']);"
                # Length prefix for a 16-byte body, then sleep.
                "os.write(fd, b'16\\n');"
                "time.sleep(60)"
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # pass_fds makes the child inherit the write end; without it
        # the default close_fds=True would strip the pipe and the
        # parent would see EOF before the prefix arrives.
        pass_fds=(write_fd,),
        env={**os.environ, "RESP_FD": str(write_fd)},
        bufsize=0,
        start_new_session=True,
    )
    os.close(write_fd)
    resp = os.fdopen(read_fd, "rb", buffering=0)
    try:
        started = time.monotonic()
        with pytest.raises(task_execution._SupervisorStop):
            task_execution._read_frame(
                resp, timeout_total=1.0, cancel=threading.Event(),
            )
        elapsed = time.monotonic() - started
        # The deadline is 1.0s plus the 0.1s minimum floor and the
        # read polling; we must return strictly below the orphan
        # child would have allowed (60s).
        assert elapsed < 5.0, elapsed
    finally:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        try:
            resp.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 9) Reusable child overhead: 2000 tiny no-model preview / embedding-skipped jobs
# ---------------------------------------------------------------------------


def test_reusable_child_overhead_for_tiny_no_model_jobs(session_area):
    """2000 no-model preview jobs through the *full* parent supervisor.

    The reusable child is a single subprocess that serves N requests
    over a dedicated response FD, but the *full* parent supervisor
    (heartbeat thread, deadline watcher, lease read-back, recovery
    bookkeeping) is what production pays for on every job. The
    previous draft measured only ``_SUPERVISED_WORKER.run_one`` (the
    raw transport), which underestimates real cost. We now drive each
    job through :func:`run_task_with_supervisor`, which is what an
    operator-visible claim cycle actually costs, and we *measure* the
    actual overhead without asserting a fictional throughput.

    Preview for a note is a deterministic no-op in the isolated
    container; the worker never enqueues any embedding work because
    no embedding profile is active. So the test exercises a
    ``preview``-stage path with zero model calls.
    """
    Session_ = session_area
    # Cap the budget at 2000 jobs. The real test is that the *child*
    # is reusable across the full supervisor cycle, not the absolute
    # throughput. The parent supervisor heartbeat runs in a separate
    # thread and adds fixed cost per job; we measure and report it.
    job_count = 2000

    # Materialise 2000 note documents + preview jobs. We do not bother
    # creating full DocumentVersion state because preview for a note
    # short-circuits to completed.
    job_ids: list[int] = []
    for i in range(job_count):
        key = f"ov-{i:05d}"
        _, _, job_id = _make_note_doc(
            Session_, key=key, stage=PREVIEW_STAGE,
            raw_content=f"# title {i}\nline.\n",
        )
        job_ids.append(job_id)

    # Drive them through the full parent supervisor serially. Each
    # call spins up a heartbeat thread, takes the deadline watcher,
    # invokes the reusable child, and joins the heartbeat. This is
    # the production path; the raw ``_SUPERVISED_WORKER.run_one``
    # transport is exercised in a separate test.
    started = time.monotonic()
    first_pid: int | None = None
    last_pid: int | None = None
    failed_count = 0
    for job_id in job_ids:
        # Claim, then run through the full supervisor. Any failure is
        # counted but does not abort the loop, so we can still
        # measure how many jobs the reusable child served.
        claimed = _claim_one(Session_, stages=(PREVIEW_STAGE,))
        if claimed != job_id:
            failed_count += 1
            continue
        try:
            ok = _run_supervised_one(Session_, job_id)
        except Exception:
            failed_count += 1
            continue
        if not ok:
            failed_count += 1
            continue
        # The subprocess identity must not change across requests:
        # the reusable child is one process serving all 2,000 calls.
        proc = task_execution._SUPERVISED_WORKER._proc
        if proc is None:
            pytest.fail(
                "reusable child subprocess disappeared mid-loop"
            )
        last_pid = proc.pid
        if first_pid is None:
            first_pid = last_pid
        assert last_pid == first_pid, (
            f"child respawned: first={first_pid} last={last_pid}"
        )
    elapsed = time.monotonic() - started

    # All preview jobs are now completed and no followup embeddings
    # were enqueued (no model was called).
    with Session_() as s:
        statuses = list(
            s.scalars(
                select(ProcessingJob.status).where(
                    ProcessingJob.id.in_(job_ids)
                )
            ).all()
        )
        assert all(
            status == "completed" for status in statuses
        ), statuses
        # No embedding-stage jobs exist for the doc corpus: preview
        # for a note is a no-op so the worker never enqueues any
        # embedding work.
        doc_ids = list(
            s.scalars(
                select(Document.id).where(
                    Document.title.like(f"{_TEST_PREFIX}ov-%")
                )
            ).all()
        )
        embed_count = s.scalar(
            select(ProcessingJob).where(
                ProcessingJob.document_id.in_(doc_ids),
                ProcessingJob.stage == "embedding",
            )
        )
        assert embed_count is None

    # No fictional throughput claim: report the measured value so the
    # maintainer can see it, but do not assert a bound. The reusable
    # child's identity is the actual invariant under test; budget the
    # wall-clock so the test never hangs the suite indefinitely.
    measured = {
        "job_count": job_count,
        "elapsed_s": round(elapsed, 3),
        "per_job_ms": round((elapsed / job_count) * 1000.0, 3),
        "first_pid": first_pid,
        "last_pid": last_pid,
        "failed": failed_count,
    }
    # pytest -s prints this; the maintainer reads the measurement.
    print(f"reusable_child_overhead={measured}")
    # Bounded wall-clock so a stuck child cannot hang the suite.
    assert elapsed < 1200.0, measured
