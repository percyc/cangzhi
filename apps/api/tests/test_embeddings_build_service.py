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
