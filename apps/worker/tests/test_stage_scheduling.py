"""Tests for the stage-fair scheduling in
:mod:`apps.worker.services.processor`.

The previous implementation claimed a fixed batch of ten
processing jobs in FIFO order before running any of them, which
let a long embedding backlog starve freshly enqueued parsing or
chunking work and let an endless structural backlog starve the
model-driven understanding stage. These tests pin the new
behaviour so the regressions CZ-Q05 set out to fix cannot
silently return:

* the scheduler cycles through every stage in
  :data:`apps.worker.services.processor.STAGE_CYCLE` and
  processes at most one job per slot,
* structural stages keep the majority of the cycle, so old
  embedding jobs cannot block new parsing or chunking,
* understanding and embedding still get a dedicated slot per
  cycle, so they keep moving under an endless structural
  backlog,
* exactly one job is in the ``processing`` state at any time
  (no bulk pre-claim of ten),
* due-retry timing is honoured by the claim filter,
* a non-positive ``limit`` short-circuits without touching a
  row,
* jobs whose stage is not in :data:`STAGE_CYCLE` are still
  serviced via the off-cycle fallback,
* the order in which stages are visited is deterministic and
  follows the cycle.

The tests stay isolated from the rest of the worker pipeline:
they drive the scheduler through the public
:func:`process_pending_jobs` / :func:`claim_pending_jobs`
functions only and stub :func:`process_single_job` so the
actual parsing/chunking/embedding workers never run.
"""

from __future__ import annotations

import datetime
import itertools

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import apps.api.models  # noqa: F401 - register all mapped tables
from apps.api.core.db import Base
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.worker.services import processor as processor_module
from apps.worker.services.processor import (
    BATCH_SIZE,
    STAGE_CYCLE,
    claim_pending_jobs,
    process_pending_jobs,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    return create_engine("sqlite:///:memory:")


@pytest.fixture
def session(engine):
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        yield session


def _make_version(
    session,
    *,
    title: str,
    raw_content: str = "stub",
    content_hash: str | None = None,
) -> tuple[Document, DocumentVersion]:
    document = Document(
        title=title,
        source_type=DocumentSourceType.note,
    )
    session.add(document)
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        version_number=1,
        content_hash=content_hash or f"hash-{document.id}",
        raw_content=raw_content,
        processing_status="ready",
    )
    session.add(version)
    session.flush()
    return document, version


def _make_job(
    session,
    *,
    version: DocumentVersion,
    stage: str,
    key: str,
    created_at: datetime.datetime | None = None,
    status: str = "created",
    next_retry_at: datetime.datetime | None = None,
) -> ProcessingJob:
    job = ProcessingJob(
        document_id=version.document_id,
        document_version_id=version.id,
        stage=stage,
        status=status,
        idempotency_key=key,
        config_version="1",
        next_retry_at=next_retry_at,
    )
    if created_at is not None:
        job.created_at = created_at
    session.add(job)
    session.flush()
    return job


class _StubRunner:
    """Stub of :func:`process_single_job` that just marks the
    claimed job as completed while recording the in-flight
    count. The scheduler is expected to call this exactly once
    per job, in serial order, and the in-flight count must
    never exceed one because the new scheduler claims one job
    at a time."""

    def __init__(self) -> None:
        self.processed: list[int] = []
        self.claim_limit: list[int] = []
        self.claim_stages: list[tuple[str, ...] | None] = []
        self.max_in_flight = 0
        self._in_flight = 0

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(
            processor_module,
            "process_single_job",
            self._run,
        )
        original_claim = processor_module.claim_pending_jobs

        def spy_claim(session, limit=BATCH_SIZE, *, stages=None):
            self.claim_limit.append(limit)
            self.claim_stages.append(tuple(stages) if stages is not None else None)
            return original_claim(session, limit, stages=stages)

        monkeypatch.setattr(
            processor_module, "claim_pending_jobs", spy_claim
        )

    def _run(self, session, job_id: int) -> bool:
        # Verify persisted claims, not just synchronous Python call depth.
        processing_ids = list(session.scalars(select(ProcessingJob.id).where(
            ProcessingJob.status == "processing"
        )))
        assert processing_ids == [job_id]
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            job = session.get(ProcessingJob, job_id)
            if job is None:
                return False
            job.status = "completed"
            job.finished_at = datetime.datetime.now(datetime.timezone.utc)
            session.add(job)
            session.commit()
            self.processed.append(job_id)
            return True
        finally:
            self._in_flight -= 1


# ---------------------------------------------------------------------------
# ``claim_pending_jobs`` contracts
# ---------------------------------------------------------------------------


class TestClaimPendingJobs:
    """The claim helper is still part of the public surface and
    is called by the operator path, so its contract is pinned
    separately from the scheduler tests below."""

    def test_non_positive_limit_short_circuits(self, session, monkeypatch):
        _, version = _make_version(session, title="note")
        job = _make_job(session, version=version, stage="parsing", key="k1")
        session.commit()
        original_job_status = job.status

        # limit=0 and limit=-1 must not touch any row and must
        # not even open a claim transaction.
        assert claim_pending_jobs(session, limit=0) == []
        assert claim_pending_jobs(session, limit=-5) == []
        # The job must still be untouched (status never moved
        # to ``processing``).
        session.refresh(job)
        assert job.status == original_job_status

    def test_due_retry_is_claimed(self, session):
        _, version = _make_version(session, title="note")
        now = datetime.datetime.now(datetime.timezone.utc)
        # ``retry`` job whose next_retry_at is in the past
        # must be claimable, and ``retry`` with a future
        # next_retry_at must not.
        due_job = _make_job(
            session,
            version=version,
            stage="parsing",
            key="due",
            status="retry",
            next_retry_at=now - datetime.timedelta(minutes=5),
        )
        future_job = _make_job(
            session,
            version=version,
            stage="parsing",
            key="future",
            status="retry",
            next_retry_at=now + datetime.timedelta(minutes=30),
        )
        session.commit()

        claimed = claim_pending_jobs(session, limit=5)

        assert claimed == [due_job.id]
        session.refresh(due_job)
        session.refresh(future_job)
        assert due_job.status == "processing"
        # The not-due retry must be left in its original state.
        assert future_job.status == "retry"

    def test_stage_filter_isolates_to_requested_stage(self, session):
        _, version = _make_version(session, title="note")
        parsing_job = _make_job(
            session,
            version=version,
            stage="parsing",
            key="parse-1",
        )
        embedding_job = _make_job(
            session,
            version=version,
            stage="embedding",
            key="embed-1",
        )
        session.commit()

        claimed = claim_pending_jobs(
            session, limit=5, stages=("embedding",)
        )

        assert claimed == [embedding_job.id]
        session.refresh(parsing_job)
        session.refresh(embedding_job)
        assert parsing_job.status == "created"
        assert embedding_job.status == "processing"

    def test_legacy_stored_stage_is_normalised_to_parsing(
        self, session
    ):
        """Legacy ``stored`` rows must still be reachable via
        the modern ``parsing`` filter; the cycle relies on the
        same normalisation as the worker."""

        _, version = _make_version(session, title="note")
        stored_job = _make_job(
            session,
            version=version,
            stage="stored",
            key="legacy-stored",
        )
        session.commit()

        claimed = claim_pending_jobs(
            session, limit=5, stages=("parsing",)
        )

        assert claimed == [stored_job.id]
        session.refresh(stored_job)
        assert stored_job.status == "processing"


# ---------------------------------------------------------------------------
# ``process_pending_jobs`` cycle behaviour
# ---------------------------------------------------------------------------


class TestProcessPendingJobsCycle:
    """The new scheduler is a deterministic stage cycle. These
    tests pin the fairness properties CZ-Q05 set out to fix."""

    def test_cycle_visits_stages_in_deterministic_order(
        self, session, monkeypatch
    ):
        """Stages are visited in the order declared in
        :data:`STAGE_CYCLE`, regardless of when each job was
        inserted."""

        runner = _StubRunner()
        runner.install(monkeypatch)

        document, version = _make_version(session, title="note")
        # Insert jobs in shuffled order to make sure the
        # scheduler is not falling back to FIFO. The
        # ``stored`` slot is kept empty on purpose so we can
        # tell apart "no work" from "wrong order".
        reversed_stages = list(reversed(STAGE_CYCLE))
        for index, stage in enumerate(reversed_stages):
            if stage == "stored":
                # The legacy alias is exercised separately in
                # the claim helper tests; here we leave the
                # ``stored`` slot empty.
                continue
            _make_job(
                session,
                version=version,
                stage=stage,
                key=f"k-{stage}",
                created_at=datetime.datetime(
                    2026, 1, 1, tzinfo=datetime.timezone.utc
                )
                + datetime.timedelta(minutes=index),
            )
        session.commit()

        # ``max_jobs`` bounds the cycle to exactly the
        # non-empty stages so the assertion below is precise.
        non_empty_count = sum(1 for stage in STAGE_CYCLE if stage != "stored")
        completed = process_pending_jobs(session, max_jobs=non_empty_count)

        assert completed == non_empty_count
        # The first ``len(STAGE_CYCLE)`` claim calls describe
        # the first cycle; the cycle iterates every stage in
        # order, including the empty ``stored`` slot. The
        # recorded order must therefore equal the cycle.
        observed = [
            stages[0] if stages else None
            for stages in runner.claim_stages[: len(STAGE_CYCLE)]
        ]
        assert observed == list(STAGE_CYCLE)

    def test_old_embedding_backlog_does_not_starve_parsing_and_chunking(
        self, session, monkeypatch
    ):
        """Twenty old embedding jobs plus one new parsing and
        one new chunking job. The new parsing/chunking jobs
        must be processed within a single cycle even though
        FIFO would have starved them behind the embedding
        backlog."""

        runner = _StubRunner()
        runner.install(monkeypatch)

        # 20 old embedding jobs from a long-finished reprocess.
        old_document, old_version = _make_version(session, title="old")
        for index in range(20):
            _make_job(
                session,
                version=old_version,
                stage="embedding",
                key=f"old-embed-{index}",
                created_at=datetime.datetime(
                    2026, 1, 1, tzinfo=datetime.timezone.utc
                )
                + datetime.timedelta(minutes=index),
            )
        # 1 new parsing + 1 new chunking job, enqueued later
        # than the embedding backlog.
        new_document, new_version = _make_version(session, title="new")
        parsing_job = _make_job(
            session,
            version=new_version,
            stage="parsing",
            key="new-parsing",
            created_at=datetime.datetime(
                2026, 9, 1, tzinfo=datetime.timezone.utc
            ),
        )
        chunking_job = _make_job(
            session,
            version=new_version,
            stage="chunking",
            key="new-chunking",
            created_at=datetime.datetime(
                2026, 9, 1, tzinfo=datetime.timezone.utc
            )
            + datetime.timedelta(seconds=1),
        )
        session.commit()

        # ``max_jobs=3`` is enough to surface the parsing and
        # chunking jobs through the cycle, plus one embedding
        # row. The new structural jobs must show up before the
        # scheduler ever gets to drain the embedding backlog.
        completed = process_pending_jobs(session, max_jobs=3)

        assert completed == 3
        session.refresh(parsing_job)
        session.refresh(chunking_job)
        assert parsing_job.status == "completed"
        assert chunking_job.status == "completed"
        # Verify the order: the two structural jobs precede
        # the first embedding in the processing list.
        parsed_id = parsing_job.id
        chunked_id = chunking_job.id
        embed_ids = {job.id for job in session.scalars(
            select(ProcessingJob).where(ProcessingJob.stage == "embedding")
        ).all() if job.status == "completed"}
        assert parsed_id in runner.processed
        assert chunked_id in runner.processed
        parsed_pos = runner.processed.index(parsed_id)
        chunked_pos = runner.processed.index(chunked_id)
        # All completed embedding ids come after the structural
        # ones.
        for embed_id in embed_ids:
            assert runner.processed.index(embed_id) > parsed_pos
            assert runner.processed.index(embed_id) > chunked_pos

    def test_understanding_progresses_under_endless_structural_backlog(
        self, session, monkeypatch
    ):
        """An endless structural backlog must not starve the
        model-driven understanding stage. The cycle gives
        understanding a dedicated slot, so one cycle is
        enough to surface a queued understanding job."""

        runner = _StubRunner()
        runner.install(monkeypatch)

        document, version = _make_version(session, title="note")
        # 30 parsing + 15 chunking + 10 preview jobs form an
        # "endless" structural backlog.
        structural_layout = (
            ("parsing", 30),
            ("chunking", 15),
            ("preview", 10),
        )
        counter = itertools.count()
        for stage, count in structural_layout:
            for _ in range(count):
                _make_job(
                    session,
                    version=version,
                    stage=stage,
                    key=f"struct-{stage}-{next(counter)}",
                    created_at=datetime.datetime(
                        2026, 1, 1, tzinfo=datetime.timezone.utc
                    )
                    + datetime.timedelta(seconds=next(counter)),
                )
        # 1 understanding job enqueued after the structural
        # backlog.
        understanding_job = _make_job(
            session,
            version=version,
            stage="understanding",
            key="model-understanding",
            created_at=datetime.datetime(
                2026, 12, 1, tzinfo=datetime.timezone.utc
            ),
        )
        session.commit()

        # One full cycle visits every stage exactly once, so
        # ``max_jobs=8`` is enough to cover the understanding
        # slot regardless of how many structural rows are
        # ahead of it.
        completed = process_pending_jobs(session, max_jobs=8)

        session.refresh(understanding_job)
        assert understanding_job.status == "completed"
        assert completed == 8

    def test_cycle_never_preclaims_more_than_one_job(
        self, session, monkeypatch
    ):
        """The scheduler must claim exactly one job per stage
        slot, then run it, then advance. The previous
        implementation reserved ten rows in one transaction
        before doing any work; this test guards against
        re-introducing that behaviour."""

        runner = _StubRunner()
        runner.install(monkeypatch)

        document, version = _make_version(session, title="note")
        # One job per known stage to keep the fixture small.
        for stage in STAGE_CYCLE:
            _make_job(
                session,
                version=version,
                stage=stage if stage != "stored" else "parsing",
                key=f"k-{stage}",
            )
        session.commit()

        process_pending_jobs(session)

        # Every claim call from the cycle must ask for
        # exactly one row. The off-cycle fallback also claims
        # one row at a time, so the cap holds globally.
        assert runner.claim_limit, "claim_pending_jobs was not called"
        assert all(limit == 1 for limit in runner.claim_limit), (
            "scheduler pre-claimed more than one job: "
            f"{runner.claim_limit!r}"
        )
        # The stub's in-flight counter must never exceed one
        # because the previous job has already been marked
        # completed by the time the next one is claimed.
        assert runner.max_in_flight == 1, (
            "more than one job was in the ``processing`` "
            f"state concurrently (max_in_flight={runner.max_in_flight})"
        )

    def test_retries_not_due_are_left_alone(
        self, session, monkeypatch
    ):
        """A ``retry`` job whose ``next_retry_at`` is still in
        the future must not be claimed, processed or
        modified."""

        runner = _StubRunner()
        runner.install(monkeypatch)

        _, version = _make_version(session, title="note")
        not_due = _make_job(
            session,
            version=version,
            stage="parsing",
            key="not-due",
            status="retry",
            next_retry_at=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=1),
        )
        # A second, due job to make sure the cycle runs at
        # all (otherwise the early return would mask a broken
        # filter).
        due = _make_job(
            session,
            version=version,
            stage="chunking",
            key="due",
            status="retry",
            next_retry_at=datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=1),
        )
        session.commit()

        completed = process_pending_jobs(session)

        session.refresh(not_due)
        session.refresh(due)
        assert not_due.status == "retry"
        assert not_due.started_at is None
        assert due.status == "completed"
        assert completed == 1
        assert runner.processed == [due.id]

    def test_max_jobs_zero_is_a_noop(self, session, monkeypatch):
        runner = _StubRunner()
        runner.install(monkeypatch)

        _, version = _make_version(session, title="note")
        job = _make_job(session, version=version, stage="parsing", key="k")
        session.commit()

        assert process_pending_jobs(session, max_jobs=0) == 0
        session.refresh(job)
        assert job.status == "created"
        # ``claim_pending_jobs`` must not have been called at
        # all when ``max_jobs`` is non-positive.
        assert runner.claim_limit == []

    def test_unknown_stage_fallback_processes_off_cycle_jobs(
        self, session, monkeypatch
    ):
        """Jobs whose ``stage`` value is not in the cycle (for
        example a future stage introduced after the constant
        was last touched) must still be serviced. The cycle's
        off-cycle slot is responsible for this fallback."""

        runner = _StubRunner()
        runner.install(monkeypatch)

        _, version = _make_version(session, title="note")
        # An off-cycle stage, simulating a future addition.
        future_job = _make_job(
            session,
            version=version,
            stage="vector_index_v2",
            key="future",
        )
        session.commit()

        completed = process_pending_jobs(session)

        session.refresh(future_job)
        assert future_job.status == "completed"
        assert completed == 1
        # The cycle visits the off-cycle slot exactly once
        # and the fallback is what actually claims this job.
        assert future_job.id in runner.processed

    def test_unknown_stage_does_not_run_when_known_stages_have_work(
        self, session, monkeypatch
    ):
        """When a known stage already has work, the off-cycle
        fallback must not steal a slot: the cycle still gives
        it exactly one slot per pass, preserving the budget
        for the known stages."""

        runner = _StubRunner()
        runner.install(monkeypatch)

        _, version = _make_version(session, title="note")
        parsing_job = _make_job(
            session, version=version, stage="parsing", key="parse"
        )
        future_job = _make_job(
            session,
            version=version,
            stage="vector_index_v2",
            key="future",
        )
        session.commit()

        # ``max_jobs=1`` makes sure the cycle stops after the
        # first stage slot, so the off-cycle fallback cannot
        # have run.
        completed = process_pending_jobs(session, max_jobs=1)

        session.refresh(parsing_job)
        session.refresh(future_job)
        assert completed == 1
        assert parsing_job.status == "completed"
        assert future_job.status == "created"
        assert parsing_job.id in runner.processed
        assert future_job.id not in runner.processed

    def test_empty_queue_is_an_immediate_noop(
        self, session, monkeypatch
    ):
        runner = _StubRunner()
        runner.install(monkeypatch)

        assert process_pending_jobs(session) == 0
        # The cycle walks the stages once to discover the
        # queue is empty and then exits; the off-cycle
        # fallback is the very last call before the
        # ``cycle_work == 0`` check fires. No job is ever
        # marked ``processing`` and no job is ever reported
        # as completed.
        assert runner.processed == []

    def test_failed_run_keeps_other_jobs_moving(
        self, monkeypatch
    ):
        """If :func:`process_single_job` raises for one job,
        the scheduler must still process the rest of the
        cycle. The previous implementation only handled
        exceptions inside the per-batch loop, so a single
        crashing job could not stop the rest of the batch
        either; this test guards the same property for the
        cycle shape."""

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            _, version = _make_version(session, title="note")
            boom_job = _make_job(
                session, version=version, stage="parsing", key="boom"
            )
            healthy_job = _make_job(
                session, version=version, stage="chunking", key="ok"
            )
            session.commit()
            boom_id = boom_job.id
            healthy_id = healthy_job.id

        # The first job crashes; the second one succeeds. The
        # scheduler must keep moving after the exception.
        crashed_ids: set[int] = set()

        def selective(_session, job_id: int) -> bool:
            if job_id in crashed_ids:
                raise RuntimeError("simulated crash")
            return True

        # Mark the parsing job as the one that crashes before
        # the worker stub sees it.
        with Session() as session:
            crashing_job = session.get(ProcessingJob, boom_id)
            crashed_ids.add(crashing_job.id)

        monkeypatch.setattr(processor_module, "process_single_job", selective)
        # Stub the parsing-failure helper so the per-job
        # recovery path in :func:`_run_claimed_job` can run
        # without needing the full document/version context.
        def fake_mark_failure(session, job, version, message, details):
            job.status = "failed"
            job.finished_at = datetime.datetime.now(
                datetime.timezone.utc
            )
            job.last_error = message
            job.error_details = details
            session.add(job)
            session.commit()
            return False

        monkeypatch.setattr(
            processor_module, "_mark_parse_failure", fake_mark_failure
        )

        # Successful chunking path needs the version to be
        # in a valid state; mark the job as completed in the
        # stub directly so the scheduler's success branch
        # can run.
        def healthy_stub(_session, job_id: int) -> bool:
            if job_id in crashed_ids:
                raise RuntimeError("simulated crash")
            with Session() as session:
                job = session.get(ProcessingJob, job_id)
                job.status = "completed"
                job.finished_at = datetime.datetime.now(
                    datetime.timezone.utc
                )
                session.add(job)
                session.commit()
            return True

        monkeypatch.setattr(processor_module, "process_single_job", healthy_stub)

        with Session() as session:
            completed = process_pending_jobs(session, max_jobs=2)

        assert completed == 1
        # Re-read both jobs and check the scheduler made
        # progress on the healthy one even though the first
        # one crashed.
        with Session() as session:
            boom_row = session.get(ProcessingJob, boom_id)
            healthy_row = session.get(ProcessingJob, healthy_id)
        assert boom_row.status in {"failed", "retry"}
        assert healthy_row.status == "completed"


def test_empty_stage_filter_claims_nothing(session):
    _, version = _make_version(session, title="empty filter")
    job = _make_job(session, version=version, stage="parsing", key="empty")
    session.commit()
    assert claim_pending_jobs(session, stages=()) == []
    session.refresh(job)
    assert job.status == "created"


@pytest.mark.parametrize("budget", [None, BATCH_SIZE, 2])
def test_failed_attempts_obey_budget(session, monkeypatch, budget):
    _, version = _make_version(session, title="failed attempts")
    for index in range(30):
        _make_job(session, version=version, stage="parsing", key=f"fail-{index}")
    session.commit()
    attempted = []

    def fail(db, job_id):
        attempted.append(job_id)
        job = db.get(ProcessingJob, job_id)
        job.status = "failed"
        db.commit()
        return False

    monkeypatch.setattr(processor_module, "process_single_job", fail)
    assert process_pending_jobs(session, max_jobs=budget) == 0
    assert len(attempted) == (BATCH_SIZE if budget is None else budget)


def test_default_budget_does_not_drain_queue(session, monkeypatch):
    _, version = _make_version(session, title="bounded")
    for index in range(30):
        _make_job(session, version=version, stage="embedding", key=f"bound-{index}")
    session.commit()
    runner = _StubRunner()
    runner.install(monkeypatch)
    assert process_pending_jobs(session) == BATCH_SIZE
    assert len(runner.processed) == BATCH_SIZE


def test_single_job_budget_preserves_all_stage_opportunities(session, monkeypatch):
    _, version = _make_version(session, title="fairness")
    stages = list(STAGE_CYCLE) + ["future_stage"]
    for stage in stages:
        for index in range(20):
            _make_job(session, version=version, stage=stage, key=f"{stage}-{index}")
    session.commit()
    runner = _StubRunner()
    runner.install(monkeypatch)
    for _ in range(len(stages)):
        assert process_pending_jobs(session, max_jobs=1) == 1
    observed = {session.get(ProcessingJob, job_id).stage for job_id in runner.processed}
    # stored/parsing share two FIFO slots; all other lanes must get a turn.
    assert set(stages) - {"stored", "parsing"} <= observed
