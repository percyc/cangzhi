"""Unit tests for the M3-6b phase 2 embedding build service.

These tests cover the state machine and the database bookkeeping
implemented in :mod:`apps.api.embeddings.build_service`. They
use a real in-memory SQLite database (via an async engine) and
exercise the same async ``AsyncSession`` the production code
relies on, so the test path is identical to the production path.

Each test uses ``asyncio.run`` to drive the async helpers; the
pattern matches the rest of the API test suite which has not
adopted ``pytest-asyncio`` markers.

The tests deliberately do not touch the network: the embedding
HTTP call lives in the worker, not the service, so the service
can be unit-tested entirely with SQLite.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import apps.api.models  # noqa: F401 - register all tables
from apps.api.core.db import Base
from apps.api.embeddings import compute_config_fingerprint
from apps.api.embeddings.build_service import (
    BUILDABLE_STATUSES,
    EMBEDDING_STAGE,
    RETRYABLE_STATUSES,
    ProfileNotActivatable,
    ProfileNotBuildable,
    ProfileNotFound,
    ProfileNotReady,
    _backfill_missing_jobs,
    activate_profile,
    enqueue_embedding_jobs_for_chunk,
    get_profile_summaries,
    retry_profile,
    rollback_profile,
    start_build,
)
from apps.api.embeddings.canary import CANARY_VERSION
from apps.api.models.auth import AIRuntimeConfig
from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.embedding_profiles import ChunkEmbedding, EmbeddingProfile
from apps.api.models.processing import ProcessingJob

# --- fixtures -------------------------------------------------------------


def _make_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite + aiosqlite under StaticPool can complain that the
    # same connection is used from two threads. We serialise the
    # event loop on a per-call basis with a lock so the
    # fixtures stay single-threaded.
    loop_lock = threading.Lock()

    async def _prepare():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_prepare())
    return engine, async_sessionmaker(engine, expire_on_commit=False), loop_lock


@pytest.fixture
def session_factory():
    engine, factory, _ = _make_session_factory()
    yield factory
    asyncio.run(engine.dispose())


def _run_async(coro):
    """Drive an async coroutine in a fresh event loop.

    The build-service functions are pure coroutines, so we can
    just call ``asyncio.run`` per test. This is exactly the
    pattern the rest of the API suite uses.
    """

    return asyncio.run(coro)


# --- helpers --------------------------------------------------------------


def _make_config(
    session: AsyncSession,
    *,
    provider: str = "openai",
    base_url: str = "https://embed.example.com/v1",
    model: str = "m",
) -> AIRuntimeConfig:
    row = AIRuntimeConfig(
        provider="disabled",
        timeout_seconds=30,
        prompt_version="v1",
        singleton_key="singleton",
        embedding_provider=provider,
        embedding_base_url=base_url,
        embedding_model=model,
        embedding_timeout_seconds=30,
    )
    session.add(row)
    return row


async def _commit(session: AsyncSession) -> None:
    await session.commit()


def _make_profile(
    *,
    provider: str = "openai",
    base_url: str = "https://embed.example.com/v1",
    model: str = "m",
    dim: int = 4,
    status: str = "tested",
) -> EmbeddingProfile:
    fingerprint = compute_config_fingerprint(
        provider=provider,
        base_url=base_url,
        model=model,
        dim=dim,
        has_api_key=False,
        key_fingerprint=None,
        canary_version=CANARY_VERSION,
    )
    return EmbeddingProfile(
        provider=provider,
        base_url=base_url,
        model=model,
        dim=dim,
        has_api_key=False,
        key_fingerprint=None,
        config_fingerprint=fingerprint,
        status=status,
    )


async def _seed_profile(session, **kwargs) -> EmbeddingProfile:
    profile = _make_profile(**kwargs)
    session.add(profile)
    await _commit(session)
    return (
        await session.execute(
            select(EmbeddingProfile).order_by(EmbeddingProfile.id.desc())
        )
    ).scalars().first()


async def _run_backfill(
    profile_id: int, session: AsyncSession
) -> tuple[int, int]:
    """Drive :func:`_backfill_missing_jobs` for tests that want to
    exercise the activation-time self-heal without going through
    the full :func:`activate_profile` wrapper."""

    profile = (
        await session.execute(
            select(EmbeddingProfile).where(EmbeddingProfile.id == profile_id)
        )
    ).scalars().first()
    return await _backfill_missing_jobs(session, profile)


async def _make_doc_and_chunks(
    session: AsyncSession,
    *,
    count: int = 3,
    content_hash: str | None = None,
) -> tuple[Document, DocumentVersion, list[DocumentChunk]]:
    document = Document(title="测试", source_type=DocumentSourceType.note)
    session.add(document)
    await session.flush()
    version = DocumentVersion(
        document_id=document.id,
        version_number=1,
        content_hash="doc-hash",
        raw_content="raw",
        structured_content={"blocks": []},
        processing_status="ready",
    )
    session.add(version)
    await session.flush()
    chunks: list[DocumentChunk] = []
    for index in range(count):
        chunk = DocumentChunk(
            document_id=document.id,
            document_version_id=version.id,
            external_id=f"chunk-{index}",
            role="child",
            chunk_type="paragraph",
            order_index=index,
            content=f"chunk {index}",
            search_text=f"chunk {index}",
            content_hash=(content_hash or f"hash-{index}"),
            char_count=10,
            token_estimate=2,
            is_current=True,
        )
        session.add(chunk)
        chunks.append(chunk)
    await session.commit()
    for chunk in chunks:
        await session.refresh(chunk)
    return document, version, chunks


# --- start_build ----------------------------------------------------------


class TestStartBuild:
    def test_rejects_unknown_profile(self, session_factory):
        async def _run():
            async with session_factory() as session:
                with pytest.raises(ProfileNotFound):
                    await start_build(session, 999)

        _run_async(_run())

    def test_rejects_active_profile(self, session_factory):
        async def _run():
            async with session_factory() as session:
                profile = await _seed_profile(session, status="active")
                with pytest.raises(ProfileNotBuildable):
                    await start_build(session, profile.id)

        _run_async(_run())

    def test_rejects_building_profile(self, session_factory):
        async def _run():
            async with session_factory() as session:
                profile = await _seed_profile(session, status="building")
                with pytest.raises(ProfileNotBuildable):
                    await start_build(session, profile.id)

        _run_async(_run())

    def test_creates_jobs_for_each_current_child_chunk(self, session_factory):
        async def _run():
            async with session_factory() as session:
                profile = await _seed_profile(session, status="tested")
                await _make_doc_and_chunks(session, count=3)
                result = await start_build(session, profile.id)
                assert result.status == "building"
                assert result.enqueued == 3
                assert result.total_chunks == 3
                assert result.completed_chunks == 0
                assert result.failed_chunks == 0
                assert result.finished_at is None
                await session.refresh(profile)
                assert profile.status == "building"
                assert profile.total_chunks == 3
                assert profile.build_started_at is not None
                jobs = list(
                    (
                        await session.execute(
                            select(ProcessingJob).where(
                                ProcessingJob.embedding_profile_id == profile.id,
                                ProcessingJob.stage == EMBEDDING_STAGE,
                            )
                        )
                    ).scalars().all()
                )
                assert len(jobs) == 3
                for job in jobs:
                    assert job.status == "created"
                    assert job.config_version == profile.config_fingerprint

        _run_async(_run())

    def test_empty_corpus_yields_ready_immediately(self, session_factory):
        async def _run():
            async with session_factory() as session:
                profile = await _seed_profile(session, status="tested")
                result = await start_build(session, profile.id)
                assert result.status == "ready"
                assert result.enqueued == 0
                assert result.total_chunks == 0
                assert result.finished_at is not None
                await session.refresh(profile)
                assert profile.status == "ready"
                assert profile.build_finished_at is not None

        _run_async(_run())

    def test_rebuild_is_idempotent_on_existing_jobs(self, session_factory):
        async def _run():
            async with session_factory() as session:
                profile = await _seed_profile(session, status="tested")
                await _make_doc_and_chunks(session, count=2)
                first = await start_build(session, profile.id)
                assert first.enqueued == 2
                await session.refresh(profile)
                profile.status = "tested"
                profile.build_finished_at = None
                session.add(profile)
                await _commit(session)
                second = await start_build(session, profile.id)
                assert second.enqueued == 0
                assert second.skipped == 2
                assert second.total_chunks == 2

        _run_async(_run())

    def test_ignores_non_current_and_parent_chunks(self, session_factory):
        async def _run():
            async with session_factory() as session:
                profile = await _seed_profile(session, status="tested")
                document, version, children = await _make_doc_and_chunks(
                    session, count=2
                )
                session.add(
                    DocumentChunk(
                        document_id=document.id,
                        document_version_id=version.id,
                        external_id="parent-1",
                        role="parent",
                        chunk_type="heading",
                        order_index=99,
                        content="intro",
                        search_text="intro",
                        content_hash="parent-hash",
                        char_count=5,
                        token_estimate=1,
                        is_current=True,
                    )
                )
                session.add(
                    DocumentChunk(
                        document_id=document.id,
                        document_version_id=version.id,
                        external_id="stale-1",
                        role="child",
                        chunk_type="paragraph",
                        order_index=100,
                        content="stale",
                        search_text="stale",
                        content_hash="stale-hash",
                        char_count=5,
                        token_estimate=1,
                        is_current=False,
                    )
                )
                await _commit(session)
                result = await start_build(session, profile.id)
                assert result.enqueued == 2

        _run_async(_run())

    def test_large_table_build_uses_version_sampling_strategy(self, session_factory):
        async def _run():
            async with session_factory() as session:
                profile = await _seed_profile(session, status="tested")
                _document, version, _children = await _make_doc_and_chunks(
                    session, count=10
                )
                version.meta = {
                    "embedding_strategy": {
                        "mode": "sampled",
                        "total_child_chunks": 10,
                        "selected_chunks": 3,
                        "structured_table_rows": 10_000,
                    }
                }
                session.add(version)
                await _commit(session)

                result = await start_build(session, profile.id)

                assert result.total_chunks == 3
                assert result.enqueued == 3
                jobs = list(
                    (
                        await session.execute(
                            select(ProcessingJob)
                            .where(ProcessingJob.embedding_profile_id == profile.id)
                            .order_by(ProcessingJob.embedding_chunk_id)
                        )
                    ).scalars()
                )
                assert len(jobs) == 3
                assert jobs[0].embedding_chunk_id != jobs[-1].embedding_chunk_id

        _run_async(_run())


# --- retry_profile --------------------------------------------------------


class TestRetry:
    def test_resets_only_failed_jobs(self, session_factory):
        async def _run():
            async with session_factory() as session:
                profile = await _seed_profile(session, status="tested")
                await _make_doc_and_chunks(session, count=3)
                await start_build(session, profile.id)
                await session.refresh(profile)
                jobs = list(
                    (
                        await session.execute(
                            select(ProcessingJob).where(
                                ProcessingJob.embedding_profile_id == profile.id,
                                ProcessingJob.stage == EMBEDDING_STAGE,
                            )
                        )
                    ).scalars().all()
                )
                jobs[0].status = "completed"
                jobs[1].status = "failed"
                jobs[1].last_error = "upstream 500"
                jobs[2].status = "failed"
                jobs[2].last_error = "upstream 500"
                session.add_all(jobs)
                profile.completed_chunks = 1
                profile.failed_chunks = 2
                session.add(profile)
                await _commit(session)
                result = await retry_profile(session, profile.id)
                assert result.enqueued == 2
                await session.refresh(profile)
                assert profile.status == "building"
                assert profile.failed_chunks == 0
                refreshed = list(
                    (
                        await session.execute(
                            select(ProcessingJob).where(
                                ProcessingJob.embedding_profile_id == profile.id
                            )
                        )
                    ).scalars().all()
                )
                statuses = sorted(job.status for job in refreshed)
                assert statuses == ["completed", "created", "created"]

        _run_async(_run())

    def test_rejects_ready_profile(self, session_factory):
        async def _run():
            async with session_factory() as session:
                profile = await _seed_profile(session, status="ready")
                with pytest.raises(ProfileNotBuildable):
                    await retry_profile(session, profile.id)

        _run_async(_run())

    def test_recovers_failed_profile(self, session_factory):
        async def _run():
            async with session_factory() as session:
                profile = await _seed_profile(session, status="failed")
                await _make_doc_and_chunks(session, count=1)
                await start_build(session, profile.id)
                await session.refresh(profile)
                profile.status = "failed"
                session.add(profile)
                await _commit(session)
                result = await retry_profile(session, profile.id)
                assert result.status == "building"

        _run_async(_run())


# --- activate / rollback --------------------------------------------------


class TestActivate:
    def test_activates_ready_profile(self, session_factory):
        async def _run():
            async with session_factory() as session:
                config = _make_config(session)
                profile = await _seed_profile(session, status="ready")
                result = await activate_profile(session, profile.id)
                assert result.active_profile_id == profile.id
                assert result.previous_profile_id is None
                await session.refresh(config)
                await session.refresh(profile)
                assert config.active_embedding_profile_id == profile.id
                assert profile.status == "active"
                assert profile.activated_at is not None

        _run_async(_run())

    def test_demotes_previous_active(self, session_factory):
        async def _run():
            async with session_factory() as session:
                config = _make_config(session)
                old = await _seed_profile(session, status="active")
                config.active_embedding_profile_id = old.id
                session.add(config)
                await _commit(session)
                new = _make_profile(
                    status="ready", base_url="https://new.example.com/v1"
                )
                session.add(new)
                await _commit(session)
                result = await activate_profile(session, new.id)
                assert result.active_profile_id == new.id
                assert result.previous_profile_id == old.id
                await session.refresh(old)
                await session.refresh(new)
                assert old.status == "retired"
                assert new.status == "active"

        _run_async(_run())

    def test_rejects_tested_profile(self, session_factory):
        async def _run():
            async with session_factory() as session:
                _make_config(session)
                profile = await _seed_profile(session, status="tested")
                with pytest.raises(ProfileNotActivatable):
                    await activate_profile(session, profile.id)

        _run_async(_run())

    def test_rejects_building_profile(self, session_factory):
        async def _run():
            async with session_factory() as session:
                _make_config(session)
                profile = await _seed_profile(session, status="building")
                with pytest.raises(ProfileNotActivatable):
                    await activate_profile(session, profile.id)

        _run_async(_run())

    def test_rejects_failed_profile(self, session_factory):
        async def _run():
            async with session_factory() as session:
                _make_config(session)
                profile = await _seed_profile(session, status="failed")
                with pytest.raises(ProfileNotActivatable):
                    await activate_profile(session, profile.id)

        _run_async(_run())

    def test_rejects_incomplete_profile(self, session_factory):
        async def _run():
            async with session_factory() as session:
                _make_config(session)
                profile = await _seed_profile(session, status="ready")
                await _make_doc_and_chunks(session, count=3)
                with pytest.raises(ProfileNotReady):
                    await activate_profile(session, profile.id)

        _run_async(_run())

    def test_rejects_stale_content_hash(self, session_factory):
        async def _run():
            async with session_factory() as session:
                _make_config(session)
                profile = await _seed_profile(session, status="ready")
                document, version, chunks = await _make_doc_and_chunks(
                    session, count=2
                )
                for chunk in chunks:
                    session.add(
                        ChunkEmbedding(
                            profile_id=profile.id,
                            chunk_id=chunk.id,
                            content_hash=chunk.content_hash,
                            vector=[0.1] * 4,
                        )
                    )
                await _commit(session)
                chunks[0].content_hash = "rotated-hash"
                session.add(chunks[0])
                await _commit(session)
                with pytest.raises(ProfileNotReady):
                    await activate_profile(session, profile.id)

        _run_async(_run())

    def test_idempotent_activate_same_profile(self, session_factory):
        async def _run():
            async with session_factory() as session:
                config = _make_config(session)
                profile = await _seed_profile(session, status="ready")
                first = await activate_profile(session, profile.id)
                assert first.previous_profile_id is None
                second = await activate_profile(session, profile.id)
                assert second.active_profile_id == profile.id
                assert second.previous_profile_id == profile.id
                await session.refresh(config)
                assert config.active_embedding_profile_id == profile.id

        _run_async(_run())

    def test_activate_backfills_missing_chunks_and_demotes(self, session_factory):
        async def _run():
            async with session_factory() as session:
                _make_config(session)
                profile = await _seed_profile(session, status="ready")
                await _make_doc_and_chunks(session, count=3)
                with pytest.raises(ProfileNotReady) as exc_info:
                    await activate_profile(session, profile.id)
                assert "已自动补建 3 个任务" in str(exc_info.value)
                await session.refresh(profile)
                assert profile.status == "building"
                assert profile.build_finished_at is None
                assert profile.total_chunks == 3
                assert profile.completed_chunks == 0
                jobs = list(
                    (
                        await session.execute(
                            select(ProcessingJob).where(
                                ProcessingJob.embedding_profile_id == profile.id,
                                ProcessingJob.stage == EMBEDDING_STAGE,
                            )
                        )
                    ).scalars().all()
                )
                assert len(jobs) == 3

        _run_async(_run())

    def test_activate_backfill_is_idempotent_across_calls(self, session_factory):
        async def _run():
            async with session_factory() as session:
                _make_config(session)
                profile = await _seed_profile(session, status="ready")
                await _make_doc_and_chunks(session, count=3)
                with pytest.raises(ProfileNotReady):
                    await activate_profile(session, profile.id)
                await session.refresh(profile)
                assert profile.status == "building"
                profile.status = "ready"
                profile.build_finished_at = None
                session.add(profile)
                await _commit(session)
                with pytest.raises(ProfileNotReady):
                    await activate_profile(session, profile.id)
                jobs = list(
                    (
                        await session.execute(
                            select(ProcessingJob).where(
                                ProcessingJob.embedding_profile_id == profile.id,
                                ProcessingJob.stage == EMBEDDING_STAGE,
                            )
                        )
                    ).scalars().all()
                )
                assert len(jobs) == 3
                assert profile.status == "ready"

        _run_async(_run())

    def test_activate_backfills_retired_profile(self, session_factory):
        async def _run():
            async with session_factory() as session:
                _make_config(session)
                profile = await _seed_profile(
                    session,
                    status="retired",
                    base_url="https://retired.example.com/v1",
                )
                await _make_doc_and_chunks(session, count=2)
                with pytest.raises(ProfileNotReady) as exc_info:
                    await activate_profile(session, profile.id)
                assert "已自动补建 2 个任务" in str(exc_info.value)
                await session.refresh(profile)
                assert profile.status == "building"
                jobs = list(
                    (
                        await session.execute(
                            select(ProcessingJob).where(
                                ProcessingJob.embedding_profile_id == profile.id,
                                ProcessingJob.stage == EMBEDDING_STAGE,
                            )
                        )
                    ).scalars().all()
                )
                assert len(jobs) == 2

        _run_async(_run())

    def test_activate_backfill_resets_completed_job_for_stale_hash(
        self, session_factory
    ):
        """A profile that previously completed a chunk whose content
        has since rotated must not stay permanently stuck: the old
        job is reset to ``created`` so the worker re-embeds it."""

        async def _run():
            async with session_factory() as session:
                _make_config(session)
                profile = await _seed_profile(session, status="ready")
                document, version, chunks = await _make_doc_and_chunks(
                    session, count=2
                )
                # Pre-populate embeddings + completed jobs for the
                # historical content hash, then rotate the hash
                # to simulate a chunk that changed after the build.
                for chunk in chunks:
                    session.add(
                        ChunkEmbedding(
                            profile_id=profile.id,
                            chunk_id=chunk.id,
                            content_hash=chunk.content_hash,
                            vector=[0.1] * 4,
                        )
                    )
                    key = (
                        f"{profile.id}:{chunk.id}:{EMBEDDING_STAGE}:"
                        f"embedding:v1:{profile.config_fingerprint}"
                    )
                    session.add(
                        ProcessingJob(
                            document_id=document.id,
                            document_version_id=version.id,
                            stage=EMBEDDING_STAGE,
                            status="completed",
                            idempotency_key=key,
                            config_version=profile.config_fingerprint,
                            embedding_profile_id=profile.id,
                            embedding_chunk_id=chunk.id,
                        )
                    )
                await _commit(session)
                for chunk in chunks:
                    chunk.content_hash = f"rotated-{chunk.id}"
                    session.add(chunk)
                await _commit(session)

                with pytest.raises(ProfileNotReady) as exc_info:
                    await activate_profile(session, profile.id)
                assert "已自动补建 2 个任务" in str(exc_info.value)

                await session.refresh(profile)
                assert profile.status == "building"
                jobs = list(
                    (
                        await session.execute(
                            select(ProcessingJob).where(
                                ProcessingJob.embedding_profile_id == profile.id,
                                ProcessingJob.stage == EMBEDDING_STAGE,
                            )
                        )
                    ).scalars().all()
                )
                assert len(jobs) == 2
                # The completed jobs were reset so the worker
                # re-runs them; the unique idempotency_key is
                # preserved, so no duplicate rows are inserted.
                statuses = sorted(job.status for job in jobs)
                assert statuses == ["created", "created"]
                for job in jobs:
                    assert job.retry_count == 0
                    assert job.last_error is None

        _run_async(_run())

    def test_activate_backfill_preserves_in_flight_jobs(self, session_factory):
        """A chunk whose job is still ``created`` / ``retry`` /
        ``processing`` must not be duplicated or reset by the
        backfill self-heal: another worker is already responsible
        for it."""

        async def _run():
            async with session_factory() as session:
                _make_config(session)
                profile = await _seed_profile(session, status="ready")
                document, version, chunks = await _make_doc_and_chunks(
                    session, count=3
                )
                in_flight_statuses = ["created", "retry", "processing"]
                for index, chunk in enumerate(chunks):
                    status = in_flight_statuses[index]
                    key = (
                        f"{profile.id}:{chunk.id}:{EMBEDDING_STAGE}:"
                        f"embedding:v1:{profile.config_fingerprint}"
                    )
                    session.add(
                        ProcessingJob(
                            document_id=document.id,
                            document_version_id=version.id,
                            stage=EMBEDDING_STAGE,
                            status=status,
                            idempotency_key=key,
                            config_version=profile.config_fingerprint,
                            embedding_profile_id=profile.id,
                            embedding_chunk_id=chunk.id,
                            retry_count=2 if status == "retry" else 0,
                        )
                    )
                await _commit(session)

                enqueued, skipped = await _run_backfill(profile.id, session)
                assert enqueued == 0
                assert skipped == 3
                jobs = list(
                    (
                        await session.execute(
                            select(ProcessingJob).where(
                                ProcessingJob.embedding_profile_id == profile.id,
                                ProcessingJob.stage == EMBEDDING_STAGE,
                            )
                        )
                    ).scalars().all()
                )
                # No duplicate rows; in-flight statuses are
                # preserved verbatim.
                assert len(jobs) == 3
                assert sorted(job.status for job in jobs) == sorted(in_flight_statuses)
                retry_job = next(
                    job for job in jobs if job.status == "retry"
                )
                assert retry_job.retry_count == 2

        _run_async(_run())

    def test_activate_backfill_resets_failed_job(self, session_factory):
        """A previously failed job whose chunk is still missing
        must be re-queued: the new content hash needs a real
        second chance, not a stale ``failed`` row."""

        async def _run():
            async with session_factory() as session:
                _make_config(session)
                profile = await _seed_profile(session, status="ready")
                document, version, chunks = await _make_doc_and_chunks(
                    session, count=1
                )
                chunk = chunks[0]
                key = (
                    f"{profile.id}:{chunk.id}:{EMBEDDING_STAGE}:"
                    f"embedding:v1:{profile.config_fingerprint}"
                )
                session.add(
                    ProcessingJob(
                        document_id=document.id,
                        document_version_id=version.id,
                        stage=EMBEDDING_STAGE,
                        status="failed",
                        idempotency_key=key,
                        config_version=profile.config_fingerprint,
                        embedding_profile_id=profile.id,
                        embedding_chunk_id=chunk.id,
                        retry_count=3,
                        last_error="upstream 503",
                    )
                )
                await _commit(session)

                enqueued, skipped = await _run_backfill(profile.id, session)
                assert enqueued == 1
                assert skipped == 0
                job = (
                    await session.execute(
                        select(ProcessingJob).where(
                            ProcessingJob.embedding_profile_id == profile.id,
                            ProcessingJob.stage == EMBEDDING_STAGE,
                        )
                    )
                ).scalars().first()
                assert job.status == "created"
                assert job.retry_count == 0
                assert job.last_error is None
                assert job.started_at is None
                assert job.finished_at is None

        _run_async(_run())

    def test_recompute_counters_preserves_failed_chunks(self, session_factory):
        """``_recompute_profile_counters`` must not unconditionally
        zero ``failed_chunks``; previous failures stay visible
        until the worker actually makes progress on them."""

        from apps.api.embeddings.build_service import _recompute_profile_counters

        async def _run():
            async with session_factory() as session:
                profile = await _seed_profile(session, status="building")
                profile.failed_chunks = 2
                profile.completed_chunks = 0
                profile.total_chunks = 0
                session.add(profile)
                _make_config(session)
                document, version, chunks = await _make_doc_and_chunks(
                    session, count=3
                )
                # Two failed jobs that the worker has not yet
                # recovered; one still missing entirely.
                for index, chunk in enumerate(chunks[:2]):
                    key = (
                        f"{profile.id}:{chunk.id}:{EMBEDDING_STAGE}:"
                        f"embedding:v1:{profile.config_fingerprint}"
                    )
                    session.add(
                        ProcessingJob(
                            document_id=document.id,
                            document_version_id=version.id,
                            stage=EMBEDDING_STAGE,
                            status="failed",
                            idempotency_key=key,
                            config_version=profile.config_fingerprint,
                            embedding_profile_id=profile.id,
                            embedding_chunk_id=chunk.id,
                            last_error="upstream timeout",
                        )
                    )
                await _commit(session)

                await _recompute_profile_counters(session, profile)
                await _commit(session)
                await session.refresh(profile)
                assert profile.total_chunks == 3
                assert profile.completed_chunks == 0
                # failed_chunks tracks the live job state, not
                # the old, pre-recompute value.
                assert profile.failed_chunks == 2

        _run_async(_run())

    def test_recompute_counters_counts_completed_only_for_matching_hash(
        self, session_factory
    ):
        """A stored embedding whose content_hash no longer matches
        the live chunk must not count as completed."""

        from apps.api.embeddings.build_service import _recompute_profile_counters

        async def _run():
            async with session_factory() as session:
                profile = await _seed_profile(session, status="building")
                profile.failed_chunks = 0
                profile.completed_chunks = 0
                profile.total_chunks = 0
                session.add(profile)
                _document, _version, chunks = await _make_doc_and_chunks(
                    session, count=2
                )
                # One matching stored vector, one stale.
                session.add(
                    ChunkEmbedding(
                        profile_id=profile.id,
                        chunk_id=chunks[0].id,
                        content_hash=chunks[0].content_hash,
                        vector=[0.1] * 4,
                    )
                )
                session.add(
                    ChunkEmbedding(
                        profile_id=profile.id,
                        chunk_id=chunks[1].id,
                        content_hash="old-hash",
                        vector=[0.1] * 4,
                    )
                )
                await _commit(session)

                await _recompute_profile_counters(session, profile)
                await _commit(session)
                await session.refresh(profile)
                assert profile.total_chunks == 2
                assert profile.completed_chunks == 1

        _run_async(_run())


class TestRollback:
    def test_rollback_to_retired_profile(self, session_factory):
        async def _run():
            async with session_factory() as session:
                config = _make_config(session)
                a = await _seed_profile(
                    session, status="retired", base_url="https://a.example.com/v1"
                )
                b = await _seed_profile(
                    session, status="active", base_url="https://b.example.com/v1"
                )
                config.active_embedding_profile_id = b.id
                session.add(config)
                await _commit(session)
                document, version, chunks = await _make_doc_and_chunks(
                    session, count=2
                )
                for chunk in chunks:
                    session.add(
                        ChunkEmbedding(
                            profile_id=a.id,
                            chunk_id=chunk.id,
                            content_hash=chunk.content_hash,
                            vector=[0.1] * 4,
                        )
                    )
                await _commit(session)
                result = await rollback_profile(session, a.id)
                assert result.active_profile_id == a.id
                assert result.previous_profile_id == b.id
                await session.refresh(a)
                await session.refresh(b)
                assert a.status == "active"
                assert b.status == "retired"

        _run_async(_run())

    def test_rollback_rejects_tested(self, session_factory):
        async def _run():
            async with session_factory() as session:
                _make_config(session)
                profile = await _seed_profile(session, status="tested")
                with pytest.raises(ProfileNotActivatable):
                    await rollback_profile(session, profile.id)

        _run_async(_run())

    def test_rollback_rejects_incomplete(self, session_factory):
        async def _run():
            async with session_factory() as session:
                _make_config(session)
                profile = await _seed_profile(
                    session, status="retired"
                )
                await _make_doc_and_chunks(session, count=2)
                with pytest.raises(ProfileNotReady):
                    await rollback_profile(session, profile.id)

        _run_async(_run())


# --- enqueue_embedding_jobs_for_chunk ------------------------------------


class TestAutoEnqueue:
    def test_no_profile_no_work(self, session_factory):
        async def _run():
            async with session_factory() as session:
                await _make_doc_and_chunks(session, count=1)
                chunk = (await session.execute(select(DocumentChunk).order_by(DocumentChunk.id))).scalars().first()
                assert await enqueue_embedding_jobs_for_chunk(session, chunk=chunk) == 0

        _run_async(_run())

    def test_enqueues_for_active_and_building(self, session_factory):
        async def _run():
            async with session_factory() as session:
                await _make_doc_and_chunks(session, count=1)
                active = _make_profile(
                    status="active", base_url="https://active.example.com/v1"
                )
                building = _make_profile(
                    status="building", base_url="https://building.example.com/v1"
                )
                tested = _make_profile(
                    status="tested", base_url="https://tested.example.com/v1"
                )
                failed = _make_profile(
                    status="failed", base_url="https://failed.example.com/v1"
                )
                session.add_all([active, building, tested, failed])
                await _commit(session)
                chunk = (await session.execute(select(DocumentChunk).order_by(DocumentChunk.id))).scalars().first()
                enqueued = await enqueue_embedding_jobs_for_chunk(session, chunk=chunk)
                assert enqueued == 2
                jobs = list(
                    (
                        await session.execute(
                            select(ProcessingJob).where(
                                ProcessingJob.embedding_profile_id.in_(
                                    [active.id, building.id]
                                )
                            )
                        )
                    ).scalars().all()
                )
                assert len(jobs) == 2
                assert {job.embedding_profile_id for job in jobs} == {
                    active.id,
                    building.id,
                }

        _run_async(_run())

    def test_idempotent(self, session_factory):
        async def _run():
            async with session_factory() as session:
                await _make_doc_and_chunks(session, count=1)
                await _seed_profile(session, status="active")
                chunk = (await session.execute(select(DocumentChunk).order_by(DocumentChunk.id))).scalars().first()
                first = await enqueue_embedding_jobs_for_chunk(session, chunk=chunk)
                second = await enqueue_embedding_jobs_for_chunk(session, chunk=chunk)
                assert first == 1
                assert second == 0

        _run_async(_run())

    def test_ready_profile_auto_enqueues_and_demotes_to_building(self, session_factory):
        async def _run():
            async with session_factory() as session:
                await _make_doc_and_chunks(session, count=3)
                profile = await _seed_profile(session, status="ready")
                profile.total_chunks = 3
                profile.completed_chunks = 3
                profile.failed_chunks = 0
                profile.build_started_at = None
                profile.build_finished_at = None
                session.add(profile)
                await _commit(session)
                chunk = (await session.execute(select(DocumentChunk).order_by(DocumentChunk.id))).scalars().first()
                enqueued = await enqueue_embedding_jobs_for_chunk(session, chunk=chunk)
                assert enqueued == 1
                await _commit(session)
                await session.refresh(profile)
                assert profile.status == "building"
                assert profile.build_finished_at is None
                assert profile.total_chunks == 3
                assert profile.completed_chunks == 0
                assert profile.failed_chunks == 0
                jobs = list(
                    (
                        await session.execute(
                            select(ProcessingJob).where(
                                ProcessingJob.embedding_profile_id == profile.id,
                                ProcessingJob.stage == EMBEDDING_STAGE,
                            )
                        )
                    ).scalars().all()
                )
                assert len(jobs) == 1

        _run_async(_run())

    def test_ready_profile_with_existing_job_is_not_demoted(self, session_factory):
        async def _run():
            async with session_factory() as session:
                await _make_doc_and_chunks(session, count=1)
                profile = await _seed_profile(session, status="ready")
                chunk = (await session.execute(select(DocumentChunk).order_by(DocumentChunk.id))).scalars().first()
                first = await enqueue_embedding_jobs_for_chunk(session, chunk=chunk)
                assert first == 1
                await _commit(session)
                await session.refresh(profile)
                assert profile.status == "building"
                profile.status = "ready"
                profile.build_finished_at = None
                session.add(profile)
                await _commit(session)
                second = await enqueue_embedding_jobs_for_chunk(session, chunk=chunk)
                assert second == 0
                await session.refresh(profile)
                assert profile.status == "ready"


# --- get_profile_summaries ------------------------------------------------


class TestProfileSummaries:
    def test_empty(self, session_factory):
        async def _run():
            async with session_factory() as session:
                assert await get_profile_summaries(session) == []

        _run_async(_run())

    def test_actions_per_state(self, session_factory):
        async def _run():
            async with session_factory() as session:
                config = _make_config(session)
                tested = await _seed_profile(session, status="tested")
                building = await _seed_profile(
                    session, status="building", base_url="https://building.example.com/v1"
                )
                ready = await _seed_profile(
                    session, status="ready", base_url="https://ready.example.com/v1"
                )
                active = await _seed_profile(
                    session, status="active", base_url="https://active.example.com/v1"
                )
                retired = await _seed_profile(
                    session, status="retired", base_url="https://retired.example.com/v1"
                )
                failed = await _seed_profile(
                    session, status="failed", base_url="https://failed.example.com/v1"
                )
                config.active_embedding_profile_id = active.id
                session.add(config)
                await _commit(session)
                summaries = {s.profile_id: s for s in await get_profile_summaries(session)}
                assert "build" in summaries[tested.id].available_actions
                assert "build" in summaries[failed.id].available_actions
                assert "retry" in summaries[building.id].available_actions
                assert "retry" in summaries[failed.id].available_actions
                assert "activate" in summaries[ready.id].available_actions
                assert "rollback" in summaries[retired.id].available_actions
                assert "activate" not in summaries[active.id].available_actions
                assert summaries[active.id].available_actions == ()
                assert summaries[active.id].is_active is True
                for other in (tested, building, ready, retired, failed):
                    assert summaries[other.id].is_active is False

        _run_async(_run())


# --- status machine sanity checks ----------------------------------------


def test_status_machine_constants_match_model():
    """The build service's status sets must agree with the
    model-level state machine. If a new status is added to the
    database, both sides need to be updated together."""

    assert set(BUILDABLE_STATUSES) == {"tested", "failed"}
    assert set(RETRYABLE_STATUSES) == {"building", "failed"}
