"""Supervised task execution with a hard subprocess deadline (ADR-035).

The executor half of the ownership/lease protocol in
:mod:`apps.worker.services.lease`. It is **disabled by default**
(``task_execution_enabled`` is False — see :mod:`apps.worker.core.config`) and
only ever runs on a PostgreSQL engine, so the historical in-process serial
execution remains the shipped behaviour. This supervised path is an
**experimental development gate, not a delivered protocol**: until the
isolated PostgreSQL reliability integration tests
(:mod:`apps.worker.tests.test_reliability_postgres`) prove crash / late-commit
/ artifact safety AND the reusable-child transport overhead is measured on a
representative embedding batch (thousands of one-chunk jobs), it must not be
enabled in production. Nothing is silently bypassed: the unsafe path is simply
not activated yet.

Protocol and process-safety invariants (each is load-bearing):

* **Logs never corrupt the protocol.** The child writes JSON responses only to
  a dedicated pipe passed as an extra file descriptor (``--response-fd``); the
  child's real stdout (fd 1) carries processor/native logs which the parent
  drains and discards. Response frames are length-prefixed and read with a
  bounded deadline, so a partial line cannot block the parent forever.
* **Confirmed exit before requeue.** On timeout or cancel the parent terminates
  and joins the child first; if it cannot confirm (``wait`` still raises even
  after ``SIGKILL``) it *refuses recovery* — it keeps the claim and surfaces
  the job for operator inspection instead of handing it to a successor while
  the old process may still run.
* **Heartbeat loss stops the child promptly.** A failed lease refresh sets a
  shared ``cancel`` event; the parent's framed read loop notices it within a
  bounded poll and terminates the child rather than waiting out the deadline.
* **Requeue is fenced and stage-aware.** It only fires when the row is still
  ``processing`` with the exact attempt token (never clobbers a
  ``completed``/``retry`` result into a failure), never auto-resumes the
  billed ``knowledge_enhancement`` stage, and preserves the ordinary stage's
  legacy retry semantics (the child reuses ``_run_claimed_job``).

A subprocess started with ``Popen([sys.executable, "-m", ...])`` **exec's** a
fresh interpreter and never forks the multithreaded parent (scanner/monitor
threads), so it is safe to spawn. Recovery safety is a pipeline: hard per-stage
deadline + write fence + unique per-attempt artifacts; expiry alone proves
nothing.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

import structlog
from sqlalchemy.orm import Session

from apps.worker.core.config import settings, stage_hard_deadline_seconds
from apps.worker.services import lease

logger = structlog.get_logger()

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ONE_AT_A_TIME = threading.Lock()
_FRAME_PREFIX_BYTES = 10  # enough for an ASCII length prefix + newline


class _SupervisorStop(Exception):
    """Raised internally when a task must end: deadline or heartbeat loss."""


class _ChildGone(Exception):
    """Raised when the child exits without answering a request."""


def _deadline_remaining(hard_deadline, now) -> float:  # noqa: ANN001
    if hard_deadline is None:
        return 0.0
    if hard_deadline.tzinfo is None:
        hard_deadline = hard_deadline.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (hard_deadline - now).total_seconds())


def _write_frame(stream, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    stream.write(f"{len(data)}\n".encode("ascii"))
    stream.write(data)
    stream.flush()


def _read_frame(stream, timeout_total: float, cancel: threading.Event) -> dict:
    """Read one length-prefixed JSON frame within a bounded deadline.

    Every ``read``/``readline`` is fronted by ``select`` with a short poll and
    the overall deadline, so neither a silent child nor a partial frame can
    block the parent beyond the budget (or a heartbeat-loss cancel).
    """
    if cancel is None:
        cancel = threading.Event()
    deadline = time.monotonic() + max(timeout_total, 0.1)
    lineno = b""
    while not lineno.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        if cancel.is_set():
            raise _SupervisorStop()
        if remaining <= 0:
            raise _SupervisorStop()
        ready, _, _ = select.select([stream], [], [], min(remaining, 0.1))
        if not ready:
            continue
        byte = stream.read(1)
        if not byte:
            raise _ChildGone()
        lineno += byte
        if len(lineno) > _FRAME_PREFIX_BYTES:
            raise _ChildGone()
    try:
        length = int(lineno.strip())
    except ValueError:
        raise _ChildGone()  # protocol corruption
    body = b""
    while len(body) < length:
        remaining = deadline - time.monotonic()
        if cancel.is_set():
            raise _SupervisorStop()
        if remaining <= 0:
            raise _SupervisorStop()
        ready, _, _ = select.select([stream], [], [], min(remaining, 0.1))
        if not ready:
            continue
        chunk = stream.read(length - len(body))
        if not chunk:
            raise _ChildGone()
        body += chunk
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _ChildGone()  # protocol corruption


def _now_ts() -> datetime.datetime:
    return lease.utc_now()


def requeue_safely(
    session: Session,
    *,
    job_id: int,
    attempt_id: str,
    reason: str,
    dial: str,
) -> None:
    """Move a *confirmed-stopped* attempt out of ``processing``.

    Only fires when the row is still ``processing`` with the exact attempt
    token, so it can never overwrite a ``completed``/``retry`` result a
    successor already committed. The billed ``knowledge_enhancement`` stage is
    never auto-resumed: its outcome is uncertain, so it is marked failed for a
    human decision. Other bounded stages become an ordinary due ``retry`` for a
    fresh, newly-leased attempt; an exhausted ``attempt_count`` is failed.
    """
    from apps.api.models.processing import ProcessingJob
    from sqlalchemy import select

    job = session.scalar(
        select(ProcessingJob).where(
            ProcessingJob.id == job_id,
            ProcessingJob.attempt_id == attempt_id,
            ProcessingJob.status == "processing",
        )
    )
    if job is None:
        session.rollback()
        return
    policy = lease.lease_policy()
    exhausted = (job.attempt_count or 0) >= policy.max_attempts
    if job.stage == "knowledge_enhancement" or exhausted:
        job.status = "failed"
        job.next_retry_at = None
        job.last_error = "任务执行中止（{reason}），不自动重试"
        job.error_details = {
            "reason": reason,
            "recovered": True,
            "final": True,
        }
    else:
        job.status = "retry"
        job.last_error = f"任务执行中止（{reason}）"
        job.error_details = {"reason": reason, "recovered": True}
    job.finished_at = _now_ts()
    job.lease_until = None
    job.hard_deadline = None
    job.heartbeat_at = None
    session.add(job)
    session.commit()


def _heartbeat_loop(
    stop: threading.Event,
    cancel: threading.Event,
    *,
    job_id: int,
    attempt_id: str,
) -> None:
    """Extend the lease (clamped to the hard deadline) while the child works.

    On lease loss (refresh returns 0 or an error) it sets ``cancel`` so the
    parent *promptly* terminates the child instead of letting it run into the
    deadline with a dead lease (which would invite a double-executing recovery).
    """
    from apps.api.models.processing import ProcessingJob
    from sqlalchemy import select

    engine = lease.bounded_engine(settings.database_url)
    SessionLocal = __import__("sqlalchemy.orm", fromlist=["sessionmaker"]).sessionmaker(
        bind=engine, expire_on_commit=False
    )
    policy = lease.lease_policy()
    try:
        while not stop.is_set() and not cancel.is_set():
            if not stop.wait(policy.heartbeat_seconds):
                break
            if stop.is_set() or cancel.is_set():
                break
            try:
                with SessionLocal() as session:
                    job = session.scalar(
                        select(ProcessingJob).where(
                            ProcessingJob.id == job_id,
                            ProcessingJob.attempt_id == attempt_id,
                        )
                    )
                    if job is None:
                        cancel.set()
                        return
                    expectation = lease.capture_expectation(session, job)
                    if expectation.hard_deadline is None:
                        cancel.set()
                        return
                    db_now = lease.db_utc_now(session)
                    if _deadline_remaining(expectation.hard_deadline, db_now) <= 0:
                        pass  # the parent deadline watcher drives termination
                    result = lease.refresh_lease(session, expectation, db_now=db_now)
                    session.commit()
                    if result == 0:
                        cancel.set()
                        return
            except lease.StaleAttemptError:
                cancel.set()
                return
            except Exception as exc:  # noqa: BLE001
                # A single failed heartbeat extends nothing; a persistent
                # failure means we can no longer hold the lease, so stop.
                logger.warning(
                    "task_heartbeat_failed", job_id=job_id, reason=type(exc).__name__
                )
                cancel.set()
                return
    finally:
        engine.dispose()


class _SupervisedWorker:
    """A single reusable bounded child worker (one task at a time).

    Responses travel over a dedicated pipe (never fd 1), so processor/native
    logs cannot corrupt the protocol. The request channel is binary length-
    framed; the child's real stdout carries logs which a drain thread discards
    so a chatty processor can never block the child's own pipe. CPU concurrency
    is pinned to 1.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._resp_stream = None
        self._drain_thread: threading.Thread | None = None

    def _ensure_proc(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        read_fd, write_fd = os.pipe()
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "apps.worker.services.task_execution",
                 "--serve", "--response-fd", str(write_fd)],
                cwd=str(_PROJECT_ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                pass_fds=(write_fd,),
            )
        except Exception:
            os.close(read_fd)
            os.close(write_fd)
            raise
        os.close(write_fd)  # only the child holds the write end
        self._resp_stream = os.fdopen(read_fd, "rb", buffering=0)
        # Drain the child's log stdout so the pipe never fills and blocks it.
        self._drain_thread = threading.Thread(
            target=_drain_child_logs, args=(self._proc,), daemon=True
        )
        self._drain_thread.start()

    def run_one(self, request: dict, timeout_total: float, cancel: threading.Event) -> dict:
        """Send one task and return its framed response.

        Raises ``_SupervisorStop`` on deadline/heartbeat loss and ``_ChildGone``
        if the child died without answering. The caller decides requeue only
        after the child has been confirmed-terminated.
        """
        with _ONE_AT_A_TIME:
            self._ensure_proc()
            assert self._proc is not None and self._proc.stdin is not None
            self._write_request(request)
            return _read_frame(self._resp_stream, timeout_total, cancel)

    def _write_request(self, request: dict) -> None:
        data = json.dumps(request, ensure_ascii=True).encode("utf-8")
        self._proc.stdin.write(f"{len(data)}\n".encode("ascii"))
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def kill_and_join(self, grace: float) -> bool:
        """Terminate and join the child; True only on a confirmed exit.

        If we cannot confirm the child exited (``wait`` still raises even after
        ``SIGKILL``), we return False and the caller must **refuse recovery**
        rather than hand the job to a successor while the old process may still
        run.
        """
        proc = self._proc
        stream = self._resp_stream
        self._proc = None
        self._resp_stream = None
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        if proc is None:
            return True
        try:
            proc.terminate()
            proc.wait(timeout=max(grace, 0.1))
            return True
        except BaseException:  # noqa: BLE001
            try:
                proc.kill()
            except OSError:
                return False
            try:
                proc.wait(timeout=5.0)
                return True
            except BaseException:  # noqa: BLE001
                return False


def _drain_child_logs(proc: subprocess.Popen) -> None:
    """Drain the child's log stdout to None (optionally to debug log)."""
    if proc.stdout is None:
        return
    try:
        for _line in proc.stdout:
            # Discard. If the processor ever emits a bounded debug line we could
            # re-log here, but the content may contain document data so we do
            # not persist it.
            pass
    except Exception:  # noqa: BLE001
        pass


_SUPERVISED_WORKER = _SupervisedWorker()


def _confirm_or_refuse_recovery(
    session: Session, *, job_id: int, attempt_id: str, reason: str, dial: str
) -> None:
    """After a stop, requeue only if the child's exit was confirmed.

    ``run_one`` raised (deadline/heartbeat loss or child gone) but the exit may
    not have been confirmed; we attempt the confirmed kill/join now and only
    requeue on success. Otherwise the job keeps its claim so no successor can
    double-run it while the old process may still be alive, and we log for
    operator inspection.
    """
    confirmed = _SUPERVISED_WORKER.kill_and_join(
        lease.lease_policy().terminate_grace_seconds
    )
    if not confirmed:
        # Refuse recovery: keep the claim (leave status processing). A fresh
        # successor must not be started while the old process is unconfirmed.
        session.rollback()
        _mark_unconfirmed(session, job_id, attempt_id)
        logger.error(
            "task_unconfirmed_exit_refusing_recovery",
            job_id=job_id, reason=reason,
        )
        return
    requeue_safely(session, job_id=job_id, attempt_id=attempt_id, reason=reason, dial=dial)


def _mark_unconfirmed(session: Session, job_id: int, attempt_id: str) -> None:
    """Keep an unconfirmed-exit job out of the recovery pool for a grace window.

    Calls :func:`lease.refresh_lease` to push a fresh lease so no successor
    claims it while the old process exit is unconfirmed; the operator resolves
    it (a manual requeue, or a later recovery after the child has truly died).
    """
    from apps.api.models.processing import ProcessingJob
    from sqlalchemy import select

    job = session.scalar(
        select(ProcessingJob).where(
            ProcessingJob.id == job_id,
            ProcessingJob.attempt_id == attempt_id,
            ProcessingJob.status == "processing",
        )
    )
    if job is None:
        return
    db_now = lease.db_utc_now(session)
    expectation = lease.capture_expectation(session, job)
    lease.refresh_lease(session, expectation, db_now=db_now)
    session.commit()


def run_task_with_supervisor(session: Session, job_id: int) -> bool:
    """Parent-side supervised execution of one already-claimed job.

    Only reached on the supervised path (PostgreSQL + enabled). The pipeline
    runs in a reusable bounded child; the parent enforces the stage-aware hard
    deadline and a heartbeat, and only requeues after a confirmed child exit.
    """
    import threading

    from apps.api.models.processing import ProcessingJob

    policy = lease.lease_policy()
    # Do not carry a stale ORM snapshot across the network round-trip: the
    # child re-reads authoritative state itself.
    session.rollback()
    job = session.get(ProcessingJob, job_id)
    if job is None or job.attempt_id is None:
        session.rollback()
        return False
    attempt_id = job.attempt_id
    stage_deadline = stage_hard_deadline_seconds(job.stage or "")

    cancel = threading.Event()
    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(stop, cancel),
        kwargs={"job_id": job_id, "attempt_id": attempt_id},
        daemon=True,
    )
    try:
        heartbeat.start()
        request = {"cmd": "run", "job": job_id, "attempt": attempt_id}
        response = None
        try:
            response = _SUPERVISED_WORKER.run_one(
                request, timeout_total=stage_deadline, cancel=cancel
            )
        except _SupervisorStop:
            reason = "cancel_heartbeat" if cancel.is_set() else "hard_deadline"
            _confirm_or_refuse_recovery(
                session, job_id=job_id, attempt_id=attempt_id, reason=reason, dial="supervisor"
            )
            session.expire_all()
            return False
        except _ChildGone:
            _confirm_or_refuse_recovery(
                session, job_id=job_id, attempt_id=attempt_id, reason="child_gone", dial="supervisor"
            )
            session.expire_all()
            return False

        if response is None:
            session.expire_all()
            return False
        session.expire_all()
        ok = bool(response.get("ok"))
        if ok:
            return True
        handled = bool(response.get("handled"))
        if handled:
            # The child already persisted the ordinary stage's retry/failed
            # result (or a lost-lease abort). Do not requeue on top of it.
            return False
        # Not handled ==> the attempt is no longer the holder (lost lease /
        # source changed); a successor already owns it. Do not requeue.
        return False
    finally:
        stop.set()
        if heartbeat and heartbeat.is_alive():
            heartbeat.join(timeout=1.0)


def _execute_one(request: dict) -> dict:
    job_id = int(request.get("job") or 0)
    attempt = request.get("attempt") or ""
    try:
        # The child must run the pipeline in-process (never recurse into this
        # supervisor), preserving the ordinary stage's exact legacy retry
        # semantics by reusing ``_run_claimed_job``.
        from apps.worker.core.config import settings as _cfg
        _cfg.task_execution_enabled = False

        from apps.worker.services.processor import _run_claimed_job

        engine = lease.bounded_engine(settings.database_url)
        SessionLocal = __import__("sqlalchemy.orm", fromlist=["sessionmaker"]).sessionmaker(
            bind=engine, expire_on_commit=False
        )
        try:
            with SessionLocal() as session:
                from apps.api.models.processing import ProcessingJob
                from sqlalchemy import select

                job = session.scalar(
                    select(ProcessingJob).where(
                        ProcessingJob.id == job_id,
                        ProcessingJob.attempt_id == attempt,
                        ProcessingJob.status == "processing",
                    )
                )
                if job is None:
                    return {"ok": False, "handled": False, "reason": "lost_lease"}
                expectation = lease.capture_expectation(session, job)
                # Gate before starting: DB-clock valid lease + unchanged
                # target/config/version/hash/profile and live current version.
                lease.verify_against(
                    session, expectation, db_now=lease.db_utc_now(session)
                )
                from apps.worker.services.processor import _supervised_source_ok
                if not _supervised_source_ok(session, job):
                    return {"ok": False, "handled": False, "reason": "source_changed"}
                with lease.LeaseFence(session, expectation):
                    outcome = _run_claimed_job(session, job_id)
                return {
                    "ok": bool(outcome),
                    "handled": True,
                    "reason": "completed" if outcome else "stage_finalized",
                }
        finally:
            engine.dispose()
    except lease.StaleAttemptError:
        return {"ok": False, "handled": False, "reason": "lost_lease"}
    except Exception as exc:  # noqa: BLE001
        logger.error("task_child_failed", job_id=job_id, reason=type(exc).__name__)
        return {"ok": False, "handled": False, "reason": type(exc).__name__}


def _serve_child(response_fd: int) -> int:
    """Reusable child loop over a dedicated response pipe.

    JSON responses go only to ``response_fd``; the child's real stdout (fd 1)
    carries processor/native logs, which the parent drains and discards, so
    logs can never corrupt the protocol.
    """
    from apps.worker.core.config import settings as _cfg
    _cfg.task_execution_enabled = False  # the child always runs in-process

    is_pipe = os.fdopen(response_fd, "wb", buffering=0)
    try:
        inbound = sys.stdin.buffer
        while True:
            head = b""
            while not head.endswith(b"\n"):
                byte = inbound.read(1)
                if not byte:
                    return 0  # parent closed the channel
                head += byte
                if len(head) > _FRAME_PREFIX_BYTES:
                    head += b"\n"
                    break
            head = head.rstrip(b"\n")
            try:
                frame_len = int(head)
            except ValueError:
                _write_frame(is_pipe, {"ok": False, "handled": True, "reason": "bad_frame"})
                continue
            if frame_len <= 0 or frame_len > 4 * 1024 * 1024:
                _write_frame(is_pipe, {"ok": False, "handled": True, "reason": "bad_frame"})
                continue
            body = b""
            while len(body) < frame_len:
                chunk = inbound.read(frame_len - len(body))
                if not chunk:
                    return 0
                body += chunk
            try:
                request = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                _write_frame(is_pipe, {"ok": False, "handled": True, "reason": "bad_frame"})
                continue
            response = _execute_one(request)
            _write_frame(is_pipe, response)
    finally:
        try:
            is_pipe.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "--serve":
        parser = argparse.ArgumentParser(prog="cangzhi-task-execution-serve")
        parser.add_argument("--response-fd", type=int, required=True)
        args, _ = parser.parse_known_args(argv[1:])
        return _serve_child(args.response_fd)
    print("cangzhi-task-execution: expected --serve --response-fd <fd>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())