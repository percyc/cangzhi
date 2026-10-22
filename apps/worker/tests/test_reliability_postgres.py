"""ADR-035 reliability: explicit isolated migrated PostgreSQL only.

These are the *supervised* customer-facing safety scenarios — competing
claims over a shared document, crash recovery (expired lease -> new attempt ->
old attempt fenced off), heartbeat CAS (an expired lease can never be
resurrected), hard-deadline requeue with confirmed exit, late-commit refusal
after recovery, restart non-claim, and source-change fencing — plus a real
subprocess child-protocol smoke test over the dedicated response FD to prove
processor/native logs cannot corrupt the JSON protocol.

Hard gate: they only run when ``CANGZHI_PG_RELIABILITY_DATABASE_URL`` points at
an explicit, isolated ``cangzhi_test`` database (never the runtime one), reached
on the ``cz-reliability-validation`` network via ``cz-reliability-postgres``.
Skipped by default. They require parent/maintainer coordination to provision
that gig so they are exercised exactly once the isolated schema (incl. the
ADR-035 columns) is available. They never run in production.

Boundary honesty: these validate the ownership/fencing orchestration and the
supervised transport on real PostgreSQL. Running the **full document pipeline**
inside the supervised child additionally needs model-service + storage
fixtures (the isolated container has no outbound model access), so true
end-to-end pipeline throughput/safety remains a separate, not-yet-validated
enablement gate (see ADR-035); this module is its precondition.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.worker.core.config import settings
from apps.worker.services import lease
from apps.worker.services.processor import claim_pending_jobs

_LEASE_COLUMNS = [
    "worker_id VARCHAR(64)",
    "attempt_id VARCHAR(64)",
    "lease_until TIMESTAMPTZ",
    "heartbeat_at TIMESTAMPTZ",
    "hard_deadline TIMESTAMPTZ",
    "attempt_count INTEGER NOT NULL DEFAULT 0",
]


def _url() -> str | None:
    url = os.getenv("CANGZHI_PG_RELIABILITY_DATABASE_URL")
    return url or None


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    url = _url()
    if not url:
        pytest.skip("explicit isolated database required (CANGZHI_PG_RELIABILITY_DATABASE_URL)")
    assert url.endswith("/cangzhi_test"), "refusing to run against any non-isolated database"
    engine = create_engine(url)
    with engine.begin() as db:
        for col in _LEASE_COLUMNS:
            db.execute(text(f"ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS {col}"))
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield Session
    engine.dispose()


@pytest.fixture
def session_area(isolated_db, monkeypatch):
    monkeypatch.setattr(settings, "task_execution_enabled", True)
    yield isolated_db
    monkeypatch.setattr(settings, "task_execution_enabled", False)


def _make_doc_job(
    Session,
    *,
    stage: str = "parsing",
    status: str = "created",
    with_lease: bool = False,
    lease_until_in_past: bool = False,
    doc_count: int = 1,
    key_prefix: str = "k",
):
    """Create one document version and ``doc_count`` processing job(s) for it."""
    with Session() as s:
        doc = Document(title=f"t-{key_prefix}", source_type=DocumentSourceType.note)
        s.add(doc)
        s.flush()
        version = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            content_hash=f"h-{key_prefix}",
            raw_content="x",
            processing_status="ready",
        )
        s.add(version)
        s.flush()
        jobs = []
        for i in range(doc_count):
            job = ProcessingJob(
                document_id=doc.id,
                document_version_id=version.id,
                stage=stage,
                status=status,
                idempotency_key=f"{key_prefix}-{i}",
                config_version="1",
                next_retry_at=None,
            )
            if with_lease:
                now = lease.db_utc_now(s)
                job.worker_id = lease.worker_id()
                job.attempt_id = lease.new_attempt_id(job.id)
                job.lease_until = now - (timedelta(seconds=5) if lease_until_in_past else timedelta(seconds=300))
                job.hard_deadline = now + timedelta(seconds=1800)
                job.attempt_count = 1
            s.add(job)
            s.flush()
            jobs.append(job)
        s.commit()
    with Session() as s:
        return (s.get(Document, doc.id), s.get(DocumentVersion, version.id),
                [s.get(ProcessingJob, j.id) for j in jobs])


def test_competing_claims_same_document_serialize(session_area):
    Session = session_area
    _, _, jobs = _make_doc_job(Session, doc_count=2, key_prefix="compete")
    ids = {j.id for j in jobs}
    assert ids, "expected two jobs for the same document"

    def claim():
        with Session() as s:
            return claim_pending_jobs(s, limit=10, stages=["parsing"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(claim) for _ in range(2)]
        claimed = set()
        for f in results:
            claimed |= set(f.result())

    # Only one of the two same-document jobs is claimed: serialization holds.
    assert len(claimed) == 1
    with Session() as s:
        row = s.get(ProcessingJob, jobs[0].id)
        other = s.get(ProcessingJob, jobs[1].id)
        processing = [j for j in (row, other) if j.status == "processing"]
        assert len(processing) == 1
        for j in processing:
            assert j.attempt_id and j.lease_until  # stamped by the two-step claim


def test_crash_recovery_new_attempt_fences_old(session_area):
    Session = session_area
    _, version, jobs = _make_doc_job(
        Session, status="processing", with_lease=True, lease_until_in_past=True,
        key_prefix="crash",
    )
    job = jobs[0]
    old_attempt = job.attempt_id
    old_worker = job.worker_id

    # A recovery claim takes the expired-lease job over with a NEW attempt.
    with Session() as s:
        claimed = claim_pending_jobs(s, limit=10, stages=["parsing"])
    assert job.id in claimed
    with Session() as s:
        recovered = s.get(ProcessingJob, job.id)
        assert recovered.attempt_id != old_attempt
        assert recovered.worker_id != old_worker
        assert recovered.status == "processing"

        # The old attempt must now be fenced off: a verify fails closed.
        expectation = lease.LeaseExpectation(
            job_id=job.id,
            attempt_id=old_attempt,
            worker_id=old_worker,
            status="processing",
            config_version="1",
            document_version_id=version.id,
            embedding_profile_id=None,
            content_hash=f"h-crash",
            current_version_id=recovered.document_id,
            lease_until=datetime.now(timezone.utc) + timedelta(seconds=30),
            hard_deadline=datetime.now(timezone.utc) + timedelta(seconds=1800),
        )
        with pytest.raises(lease.StaleAttemptError):
            lease.verify_against(s, expectation, db_now=lease.db_utc_now(s))


def test_late_commit_refused_after_recovery(session_area):
    Session = session_area
    _, version, jobs = _make_doc_job(
        Session, status="processing", with_lease=True, lease_until_in_past=True,
        key_prefix="latewrite",
    )
    job = jobs[0]
    old_attempt = (job.attempt_id, job.worker_id)

    with Session() as s:
        claim_pending_jobs(s, limit=10, stages=["parsing"])
    with Session() as s:
        expectation = lease.capture_expectation(s, s.get(ProcessingJob, job.id))
        # Suppose an old in-flight executor somehow held this expectation; the
        # live attempt has moved on, so a bulk DML write under its fence fails
        # closed (its own attempt no longer matches).
        recovered = s.get(ProcessingJob, job.id)
        stale_expectation = lease.LeaseExpectation(
            job_id=job.id,
            attempt_id=old_attempt[0],
            worker_id=old_attempt[1],
            status="processing",
            config_version=recovered.config_version,
            document_version_id=recovered.document_version_id,
            embedding_profile_id=recovered.embedding_profile_id,
            content_hash=getattr(s.get(DocumentVersion, recovered.document_version_id), "content_hash", None),
            current_version_id=recovered.document_id,
            lease_until=datetime.now(timezone.utc) + timedelta(seconds=30),
            hard_deadline=datetime.now(timezone.utc) + timedelta(seconds=1800),
        )
        with lease.LeaseFence(s, expectation=stale_expectation):
            with pytest.raises(lease.StaleAttemptError):
                s.execute(text("UPDATE processing_jobs SET last_error = 'late'"))


def test_heartbeat_refresh_cas_expiry(session_area):
    Session = session_area
    _, version, jobs = _make_doc_job(
        Session, status="processing", with_lease=True, lease_until_in_past=False,
        key_prefix="hb-cas",
    )
    job = jobs[0]
    with Session() as s:
        expectation = lease.capture_expectation(s, s.get(ProcessingJob, job.id))
        now = lease.db_utc_now(s)
        # Valid lease -> refresh returns 1 (rows affected).
        assert lease.refresh_lease(s, expectation, db_now=now) == 1
        s.commit()
        # Now force the lease into the past on the live row.
        s.execute(
            text(
                "UPDATE processing_jobs SET lease_until = lease_until - interval '10 minutes', "
                "hard_deadline = hard_deadline + interval '1 day' WHERE id = :id"
            ),
            {"id": job.id},
        )
        s.commit()
        # An expired lease can never be resurrected: refresh returns 0.
        expired = lease.LeaseExpectation(
            job_id=job.id,
            attempt_id=expectation.attempt_id,
            worker_id=expectation.worker_id,
            status="processing",
            config_version=expectation.config_version,
            document_version_id=expectation.document_version_id,
            embedding_profile_id=expectation.embedding_profile_id,
            content_hash=expectation.content_hash,
            current_version_id=expectation.current_version_id,
            lease_until=expectation.lease_until,
            hard_deadline=datetime.now(timezone.utc) + timedelta(days=1),
        )
        assert lease.refresh_lease(s, expired, db_now=lease.db_utc_now(s)) == 0


def test_hard_deadline_requeue_preserves_outcomes(session_area):
    Session = session_area
    _, _, jobs = _make_doc_job(Session, status="processing", with_lease=True, key_prefix="req")
    job = jobs[0]

    # Simulate a confirmed child exit after a hard-deadline stop: requeue.
    with Session() as s:
        from apps.worker.services.processor import requeue_safely
        requeue_safely(
            s, job_id=job.id, attempt_id=job.attempt_id, reason="hard_deadline", dial="supervisor"
        )
        row = s.get(ProcessingJob, job.id)
        assert row.status == "retry"
        assert row.attempt_id is None  # token cleared so a successor claims fresh
        assert row.lease_until is None


def test_requeue_never_clobbers_success_or_other_attempt(session_area):
    Session = session_area
    from apps.worker.services.processor import requeue_safely

    # A completed job is untouched by a late (wrong-token) requeue.
    _, _, jobs = _make_doc_job(Session, status="processing", with_lease=True, key_prefix="noclobber")
    job = jobs[0]
    with Session() as s:
        s.execute(
            text(
                "UPDATE processing_jobs SET status='completed' WHERE id=:id "
                "AND attempt_id=:a"
            ),
            {"id": job.id, "a": job.attempt_id},
        )
        s.commit()
        requeue_safely(s, job_id=job.id, attempt_id="OTHER_TOKEN", reason="x", dial="supervisor")
        assert s.get(ProcessingJob, job.id).status == "completed"


def test_requeue_never_autoresumes_enhancement(session_area):
    Session = session_area
    from apps.worker.services.processor import requeue_safely

    _, _, jobs = _make_doc_job(Session, stage="knowledge_enhancement",
                               status="processing", with_lease=True, key_prefix="enh")
    job = jobs[0]
    with Session() as s:
        requeue_safely(s, job_id=job.id, attempt_id=job.attempt_id, reason="x", dial="supervisor")
        row = s.get(ProcessingJob, job.id)
        assert row.status == "failed"  # uncertain billing, never auto-resumed
        assert row.next_retry_at is None


def test_restart_does_not_double_run_live_claim(session_area):
    Session = session_area
    _, _, jobs = _make_doc_job(
        Session, status="processing", with_lease=True, lease_until_in_past=False,
        key_prefix="restart",
    )
    job = jobs[0]
    with Session() as s:
        claimed = claim_pending_jobs(s, limit=10, stages=["parsing"])
        # A live (unexpired) lease is NOT recoverable, so nothing is claimed.
        assert job.id not in claimed


def test_source_change_fences_stale_publish(session_area):
    Session = session_area
    doc, version, jobs = _make_doc_job(Session, status="processing", with_lease=True, key_prefix="srcc")
    job = jobs[0]
    with Session() as s:
        expectation = lease.capture_expectation(s, s.get(ProcessingJob, job.id))
        # A new current_version is published for the document mid-attempt.
        s.execute(
            text("UPDATE documents SET current_version_id = :v WHERE id = :d"),
            {"v": version.id + 99999, "d": doc.id},
        )
        s.commit()
        # The stale attempt can no longer publish.
        with pytest.raises(lease.StaleAttemptError):
            lease.verify_against(s, expectation, db_now=lease.db_utc_now(s))


def test_child_protocol_response_fd_smoke(session_area):
    """Real subprocess: dedicated response FD decouples logs from JSON.

    We spawn the actual child entrypoint (the same module the supervisor uses)
    and drive two requests over the request channel. Because the jobs do not
    exist, ``_execute_one`` returns a fast ``lost_lease``/``bad_frame`` handled
    response without entering the model pipeline. The child also writes noisy
    frames to its *log* stdout (fd 1) which the parent drains; the response FD
    must still yield intact, length-prefixed JSON. Proves processor logs cannot
    corrupt the protocol and the child stays alive (reusable) across requests.
    """
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [sys.executable, "-m", "apps.worker.services.task_execution",
         "--serve", "--response-fd", str(write_fd)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        pass_fds=(write_fd,),
    )
    os.close(write_fd)
    resp = os.fdopen(read_fd, "rb", buffering=0)

    def drain():
        try:
            for _ in proc.stdout:
                pass
        except Exception:
            pass

    import threading
    threading.Thread(target=drain, daemon=True).start()

    def send(request: dict) -> dict:
        data = __import__("json").dumps(request).encode("utf-8")
        proc.stdin.write(f"{len(data)}\n".encode("ascii"))
        proc.stdin.write(data)
        proc.stdin.flush()
        # Bounded framed read of one JSON response.
        head = b""
        while not head.endswith(b"\n"):
            head += resp.read(1)
        body = resp.read(int(head.strip()))
        return __import__("json").loads(body.decode("utf-8"))

    try:
        # Unknown job -> fast lost_lease, no pipeline.
        r1 = send({"cmd": "run", "job": -1, "attempt": "does-not-exist"})
        assert r1["ok"] is False and r1["handled"] is False

        # A subsequent request still works: the child is reusable.
        r2 = send({"cmd": "run", "job": -2, "attempt": "deadbeef"})
        assert r2["ok"] is False and r2["handled"] is False
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        try:
            resp.close()
        except Exception:
            pass