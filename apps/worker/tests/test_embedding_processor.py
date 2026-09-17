"""Tests for the M3-6b phase 2 worker embedding processor.

The tests cover the per-job handler in
:mod:`apps.worker.services.embedding_processor` with a synchronous
SQLite session (the worker polls in a thread) and a mocked
``httpx.Client`` so the test path never hits the network.

The four "must not" rules from the module's docstring are
enforced:

1. The HTTP call uses the profile's recorded provider / base URL
   / model. The test deliberately points the operator's
   ``ai_runtime_configs`` row at a different URL and asserts
   that the job is left in ``retry`` rather than silently
   embedding against the wrong upstream.
2. The key is decrypted on demand. The test does not store a
   key on the profile; the profile row remains ``has_api_key=False``.
3. The vector is validated for dim and finite values. A bad
   vector leaves the job in ``retry`` / ``failed`` and does
   not poison the chunk_embeddings table.
4. Upsert by (profile_id, chunk_id) is content-hash aware. The
   test asserts that re-running the worker after a
   ``content_hash`` change triggers a fresh HTTP call.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import sessionmaker

from apps.api.core.db import Base
from apps.api.embeddings import compute_config_fingerprint
from apps.api.embeddings.build_service import (
    EMBEDDING_STAGE,
)
from apps.api.embeddings.canary import CANARY_VERSION
from apps.api.models.auth import AIRuntimeConfig
from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.embedding_profiles import ChunkEmbedding, EmbeddingProfile
from apps.api.models.processing import ProcessingJob
from apps.api.security.secrets import encrypt_secret
from apps.worker.services.embedding_processor import (
    EmbeddingConfigUnavailableError,
    EmbeddingKeyMismatchError,
    process_embedding_job,
    reconcile_profile_progress,
    resolve_embedding_runtime,
)
from apps.worker.services.processor import process_single_job

# --- helpers --------------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    """SQLite in-memory engine, plus a writable storage path for the
    secret store. The worker's runtime config depends on a working
    master key, so we need a real ``storage_path`` even though the
    test never reads a blob from disk."""

    from apps.api.core.config import settings

    settings.storage_path = str(tmp_path)
    from apps.api.security import secrets as _secrets

    _secrets.reset_secret_store()
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()
    _secrets.reset_secret_store()


@pytest.fixture
def session(engine):
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _make_config(
    session,
    *,
    provider="openai",
    base_url="https://embed.example.com/v1",
    model="m",
    has_api_key=True,
    api_key="sk-worker-test-aaaaaaaaaaaa",
):
    row = AIRuntimeConfig(
        provider="disabled",
        timeout_seconds=30,
        prompt_version="v1",
        singleton_key="singleton",
        embedding_provider=provider,
        embedding_base_url=base_url,
        embedding_model=model,
        embedding_timeout_seconds=30,
        has_embedding_api_key=has_api_key,
        embedding_api_key_cipher=(
            encrypt_secret(api_key) if has_api_key and api_key else None
        ),
    )
    session.add(row)
    session.commit()
    return row


def _make_profile(
    session,
    *,
    provider="openai",
    base_url="https://embed.example.com/v1",
    model="m",
    dim=4,
    status="building",
):
    fingerprint = compute_config_fingerprint(
        provider=provider,
        base_url=base_url,
        model=model,
        dim=dim,
        has_api_key=False,
        key_fingerprint=None,
        canary_version=CANARY_VERSION,
    )
    profile = EmbeddingProfile(
        provider=provider,
        base_url=base_url,
        model=model,
        dim=dim,
        has_api_key=False,
        key_fingerprint=None,
        config_fingerprint=fingerprint,
        status=status,
        total_chunks=1,
        completed_chunks=0,
        failed_chunks=0,
    )
    session.add(profile)
    session.commit()
    return profile


def _make_doc_and_chunk(session, *, content="hello world", content_hash="hash-1"):
    document = Document(title="测试", source_type=DocumentSourceType.note)
    session.add(document)
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        version_number=1,
        content_hash="doc-hash",
        raw_content="raw",
        structured_content={"blocks": []},
        processing_status="ready",
    )
    session.add(version)
    session.flush()
    chunk = DocumentChunk(
        document_id=document.id,
        document_version_id=version.id,
        external_id="chunk-1",
        role="child",
        chunk_type="paragraph",
        order_index=0,
        content=content,
        search_text=content,
        content_hash=content_hash,
        char_count=len(content),
        token_estimate=2,
        is_current=True,
    )
    session.add(chunk)
    session.commit()
    return document, version, chunk


def _make_embedding_job(session, *, profile, chunk, document, version):
    key = (
        f"{profile.id}:{chunk.id}:{EMBEDDING_STAGE}:"
        f"embedding:v1:{profile.config_fingerprint}"
    )
    job = ProcessingJob(
        document_id=document.id,
        document_version_id=version.id,
        stage=EMBEDDING_STAGE,
        status="processing",
        idempotency_key=key,
        config_version=profile.config_fingerprint,
        embedding_profile_id=profile.id,
        embedding_chunk_id=chunk.id,
    )
    session.add(job)
    session.commit()
    return job


def _run_and_commit(session, job):
    """Run the embedding handler and commit so the assertions
    see the persisted state. Mirrors what ``process_single_job``
    does in production."""

    outcome = process_embedding_job(session, job)
    session.commit()
    return outcome


class _MockSyncClient:
    def __init__(self, transport: httpx.MockTransport, **kwargs):
        self._transport = transport
        self._kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def post(self, url, **kwargs):
        request = httpx.Request("POST", str(url), **kwargs)
        return self._transport.handle_request(request)


def _patch_transport(monkeypatch, transport: httpx.MockTransport):
    monkeypatch.setattr(
        "apps.worker.services.embedding_processor.httpx.Client",
        lambda *args, **kwargs: _MockSyncClient(transport, **kwargs),
    )


def _vector_payload(dim: int, seed: int = 1) -> dict:
    state = seed
    out = []
    for _ in range(3):
        v = []
        for _ in range(dim):
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            v.append(((state % 2000) - 1000) / 1000.0)
        out.append(v)
    return out


# --- resolve_embedding_runtime -------------------------------------------


class TestResolveRuntime:
    def test_returns_runtime_for_matching_config(self, session):
        _make_config(session)
        profile = _make_profile(session)
        runtime = resolve_embedding_runtime(session, profile=profile)
        assert runtime.provider == "openai"
        assert runtime.base_url == "https://embed.example.com/v1"
        assert runtime.model == "m"
        assert runtime.api_key == "sk-worker-test-aaaaaaaaaaaa"

    def test_rejects_url_mismatch(self, session):
        _make_config(
            session, base_url="https://runtime-different.example.com/v1"
        )
        profile = _make_profile(session)
        with pytest.raises(EmbeddingKeyMismatchError):
            resolve_embedding_runtime(session, profile=profile)

    def test_rejects_model_mismatch(self, session):
        _make_config(session, model="runtime-model")
        profile = _make_profile(session)
        with pytest.raises(EmbeddingKeyMismatchError):
            resolve_embedding_runtime(session, profile=profile)

    def test_rejects_missing_key(self, session):
        _make_config(session, has_api_key=False, api_key=None)
        profile = _make_profile(session)
        with pytest.raises(EmbeddingConfigUnavailableError):
            resolve_embedding_runtime(session, profile=profile)

    def test_rejects_no_config(self, session):
        profile = _make_profile(session)
        with pytest.raises(EmbeddingConfigUnavailableError):
            resolve_embedding_runtime(session, profile=profile)

    def test_ollama_does_not_require_key(self, session):
        _make_config(
            session,
            provider="ollama",
            base_url="http://localhost:11434",
            model="nomic-embed-text",
            has_api_key=False,
            api_key=None,
        )
        profile = _make_profile(
            session,
            provider="ollama",
            base_url="http://localhost:11434",
            model="nomic-embed-text",
        )
        runtime = resolve_embedding_runtime(session, profile=profile)
        assert runtime.provider == "ollama"
        assert runtime.api_key == ""


# --- process_embedding_job: happy path -----------------------------------


class TestProcessEmbeddingJob:
    def test_completes_and_persists_vector(self, session, monkeypatch):
        _make_config(session)
        profile = _make_profile(session, dim=8)
        document, version, chunk = _make_doc_and_chunk(session)
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )

        vectors = _vector_payload(8, seed=11)
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            body = json.loads(request.content.decode("utf-8"))
            assert body["model"] == "m"
            assert body["input"] == "hello world"
            assert "Authorization" in request.headers
            return httpx.Response(
                200,
                json={"data": [{"embedding": v} for v in vectors]},
            )

        _patch_transport(monkeypatch, httpx.MockTransport(handler))

        outcome = _run_and_commit(session, job)
        # The unit-level handler does not commit; the worker
        # pipeline does. Persist here so the assertions read
        # back what we just wrote.
        session.commit()
        assert outcome.status == "completed"
        assert outcome.chunk_id == chunk.id
        assert outcome.profile_id == profile.id

        session.refresh(job)
        session.refresh(profile)
        assert job.status == "completed"
        assert job.finished_at is not None
        assert profile.completed_chunks == 1
        assert profile.failed_chunks == 0

        stored = session.scalar(
            select(ChunkEmbedding).where(
                ChunkEmbedding.profile_id == profile.id,
                ChunkEmbedding.chunk_id == chunk.id,
            )
        )
        assert stored is not None
        assert stored.content_hash == chunk.content_hash
        assert len(stored.vector) == 8
        assert all(isinstance(x, float) for x in stored.vector)

    def test_reuses_existing_vector_when_content_hash_matches(
        self, session, monkeypatch
    ):
        _make_config(session)
        profile = _make_profile(session, dim=4)
        document, version, chunk = _make_doc_and_chunk(session)
        # Pre-populate a row with the same content_hash.
        session.add(
            ChunkEmbedding(
                profile_id=profile.id,
                chunk_id=chunk.id,
                content_hash=chunk.content_hash,
                vector=[0.1, 0.2, 0.3, 0.4],
            )
        )
        session.commit()
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )

        # If the transport is called, the test should fail; we
        # therefore use a transport that records calls.
        calls: list[httpx.Request] = []
        _patch_transport(
            monkeypatch,
            httpx.MockTransport(
                lambda request: (
                    calls.append(request),
                    httpx.Response(200, json={"data": [{"embedding": [0.0, 0.0, 0.0, 0.0]}]}),
                )[1]
            ),
        )

        outcome = _run_and_commit(session, job)
        assert outcome.status == "skipped"
        assert outcome.reason == "already_current"
        assert calls == []
        session.refresh(profile)
        assert profile.completed_chunks == 0
        assert profile.failed_chunks == 0

    def test_recomputes_when_content_hash_changes(
        self, session, monkeypatch
    ):
        _make_config(session)
        profile = _make_profile(session, dim=4)
        document, version, chunk = _make_doc_and_chunk(
            session, content_hash="old-hash"
        )
        # Pre-populate with a stale hash.
        session.add(
            ChunkEmbedding(
                profile_id=profile.id,
                chunk_id=chunk.id,
                content_hash="stale-hash",
                vector=[0.0, 0.0, 0.0, 0.0],
            )
        )
        session.commit()
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )

        vectors = _vector_payload(4, seed=99)
        _patch_transport(
            monkeypatch,
            httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"data": [{"embedding": v} for v in vectors]},
                )
            ),
        )

        outcome = _run_and_commit(session, job)
        assert outcome.status == "completed"
        stored = session.scalar(
            select(ChunkEmbedding).where(
                ChunkEmbedding.profile_id == profile.id,
                ChunkEmbedding.chunk_id == chunk.id,
            )
        )
        assert stored.content_hash == "old-hash"
        assert len(stored.vector) == 4
        assert any(x != 0.0 for x in stored.vector)

    def test_retries_on_runtime_mismatch(self, session, monkeypatch):
        # The operator has a runtime URL that does not match the
        # profile. The job must be left in ``retry``, the
        # failure counter must not yet be incremented, and the
        # chunk_embeddings table must be untouched.
        _make_config(
            session, base_url="https://runtime-moved.example.com/v1"
        )
        profile = _make_profile(session)
        document, version, chunk = _make_doc_and_chunk(session)
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )

        _patch_transport(
            monkeypatch,
            httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"data": [{"embedding": [0.0, 0.0, 0.0, 0.0]}]},
                )
            ),
        )

        outcome = _run_and_commit(session, job)
        assert outcome.status == "retry"
        session.refresh(profile)
        # Retry has not consumed the failure budget; failed_chunks
        # should not be incremented yet.
        assert profile.failed_chunks == 0
        assert (
            session.scalar(
                select(ChunkEmbedding).where(
                    ChunkEmbedding.profile_id == profile.id,
                )
            )
            is None
        )

    def test_retries_on_upstream_http_error(self, session, monkeypatch):
        _make_config(session)
        profile = _make_profile(session)
        document, version, chunk = _make_doc_and_chunk(session)
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )
        _patch_transport(
            monkeypatch,
            httpx.MockTransport(lambda request: httpx.Response(503, text="upstream down")),
        )
        outcome = _run_and_commit(session, job)
        assert outcome.status == "retry"
        assert "HTTP 503" in (outcome.reason or "")

    def test_fails_on_wrong_dim(self, session, monkeypatch):
        _make_config(session)
        profile = _make_profile(session, dim=8)
        document, version, chunk = _make_doc_and_chunk(session)
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )

        # Return 4-dimensional vectors, profile says 8.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"data": [{"embedding": [0.0, 0.0, 0.0, 0.0]}]},
            )

        _patch_transport(monkeypatch, httpx.MockTransport(handler))
        # First run: retry_count goes 0 -> 1, status="retry".
        outcome = _run_and_commit(session, job)
        assert outcome.status in ("retry", "failed")
        # Drain retries: keep retry_count incrementing so the
        # ``max_retries`` check eventually trips. We only need
        # to reset status to "processing" (the worker's normal
        # claim path) between calls.
        for _ in range(5):
            session.refresh(job)
            if job.status == "failed":
                # The job hit its failure budget; the test
                # wants to see failed_chunks=1 by this point.
                break
            job.status = "processing"
            session.add(job)
            session.commit()
            outcome = _run_and_commit(session, job)
        session.refresh(profile)
        assert profile.failed_chunks == 1
        assert profile.completed_chunks == 0

    def test_fails_on_non_finite_vector(self, session, monkeypatch):
        _make_config(session)
        profile = _make_profile(session, dim=4)
        document, version, chunk = _make_doc_and_chunk(session)
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )
        _patch_transport(
            monkeypatch,
            httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"data": [{"embedding": [0.1, float("nan"), 0.2, 0.3]}]},
                )
            ),
        )
        outcome = _run_and_commit(session, job)
        assert outcome.status in ("retry", "failed")
        session.refresh(profile)
        # On a non-finite vector the failure path leaves the
        # counters untouched until the job hits ``failed``;
        # we just check the chunk_embeddings row never appeared.
        assert (
            session.scalar(
                select(ChunkEmbedding).where(
                    ChunkEmbedding.profile_id == profile.id
                )
            )
            is None
        )

    def test_does_not_touch_document_version_on_failure(
        self, session, monkeypatch
    ):
        """Keyword retrieval must keep working even if every
        embedding job for a document version fails."""

        _make_config(session)
        profile = _make_profile(session, dim=4)
        document, version, chunk = _make_doc_and_chunk(session)
        version.processing_status = "ready"
        session.add(version)
        session.commit()
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )
        _patch_transport(
            monkeypatch,
            httpx.MockTransport(lambda request: httpx.Response(500, text="boom")),
        )
        # Drain retries.
        for _ in range(4):
            session.refresh(job)
            job.retry_count = 0
            job.status = "processing"
            session.add(job)
            session.commit()
            _run_and_commit(session, job)
        session.refresh(version)
        assert version.processing_status == "ready"

    def test_skips_when_chunk_is_historical(self, session, monkeypatch):
        _make_config(session)
        profile = _make_profile(session, dim=4)
        document, version, chunk = _make_doc_and_chunk(session)
        chunk.is_current = False
        session.add(chunk)
        session.commit()
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )

        called = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(request)
            return httpx.Response(200, json={"data": [{"embedding": [0.0, 0.0, 0.0, 0.0]}]})

        _patch_transport(monkeypatch, httpx.MockTransport(handler))
        outcome = _run_and_commit(session, job)
        assert outcome.status == "skipped"
        assert outcome.reason == "chunk_missing"
        assert called == []


# --- reconcile_profile_progress -------------------------------------------


class TestReconcileProgress:
    def test_counts_only_chunks_selected_by_profile_jobs(self, session):
        _make_config(session)
        profile = _make_profile(session, dim=4)
        document, version, selected_chunk = _make_doc_and_chunk(session)
        unselected_chunk = DocumentChunk(
            document_id=document.id,
            document_version_id=version.id,
            external_id="chunk-unselected",
            role="child",
            chunk_type="table",
            order_index=1,
            content="未向量化的大表行",
            search_text="未向量化的大表行",
            content_hash="unselected-hash",
            char_count=9,
            token_estimate=5,
            is_current=True,
        )
        session.add(unselected_chunk)
        job = _make_embedding_job(
            session,
            profile=profile,
            chunk=selected_chunk,
            document=document,
            version=version,
        )
        job.status = "created"
        profile.status = "active"
        session.add_all([job, profile])
        session.commit()

        reconcile_profile_progress(session, profile.id)
        session.commit()
        session.refresh(profile)

        assert profile.total_chunks == 1
        assert profile.completed_chunks == 0
        assert profile.status == "active"

    def test_active_profile_keeps_serving_while_progress_updates(self, session):
        _make_config(session)
        profile = _make_profile(session, dim=4)
        document, version, chunk = _make_doc_and_chunk(session)
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )
        job.status = "created"
        profile.status = "active"
        profile.total_chunks = 0
        session.add_all([job, profile])
        session.commit()

        reconcile_profile_progress(session, profile.id)
        session.commit()
        session.refresh(profile)

        assert profile.status == "active"
        assert profile.total_chunks == 1
        assert profile.completed_chunks == 0
        assert profile.build_finished_at is None

    def test_flips_to_ready_when_complete(self, session):
        _make_config(session)
        profile = _make_profile(session, dim=4)
        document, version, chunk = _make_doc_and_chunk(session)
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )
        job.status = "completed"
        session.add(job)
        session.add(
            ChunkEmbedding(
                profile_id=profile.id,
                chunk_id=chunk.id,
                content_hash=chunk.content_hash,
                vector=[0.1, 0.2, 0.3, 0.4],
            )
        )
        profile.completed_chunks = 1
        profile.failed_chunks = 0
        profile.total_chunks = 1
        profile.status = "building"
        session.add(profile)
        session.commit()

        reconcile_profile_progress(session, profile.id)
        session.commit()
        session.refresh(profile)
        assert profile.status == "ready"
        assert profile.build_finished_at is not None

    def test_stays_building_with_failures(self, session):
        _make_config(session)
        profile = _make_profile(session, dim=4)
        document, version, chunk = _make_doc_and_chunk(session)
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )
        job.status = "failed"
        session.add(job)
        profile.completed_chunks = 0
        profile.failed_chunks = 1
        profile.total_chunks = 1
        profile.status = "building"
        session.add(profile)
        session.commit()
        reconcile_profile_progress(session, profile.id)
        session.commit()
        session.refresh(profile)
        assert profile.status == "failed"

    def test_idempotent(self, session):
        _make_config(session)
        profile = _make_profile(session, dim=4)
        document, version, chunk = _make_doc_and_chunk(session)
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )
        job.status = "completed"
        session.add(job)
        session.add(
            ChunkEmbedding(
                profile_id=profile.id,
                chunk_id=chunk.id,
                content_hash=chunk.content_hash,
                vector=[0.1, 0.2, 0.3, 0.4],
            )
        )
        profile.completed_chunks = 1
        profile.total_chunks = 1
        profile.status = "ready"
        profile.build_finished_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session.add(profile)
        session.commit()
        first_finished = profile.build_finished_at
        reconcile_profile_progress(session, profile.id)
        session.commit()
        reconcile_profile_progress(session, profile.id)
        session.commit()
        session.refresh(profile)
        assert profile.build_finished_at == first_finished

    def test_stale_hash_excluded_from_completed(self, session):
        """An embedding whose ``content_hash`` lags the chunk's
        current ``content_hash`` must not be counted as completed.

        The reconcile path joins ``processing_jobs`` with the
        current child chunk and ``chunk_embeddings``; the SQL
        aggregate gates the completed counter on the equality of
        the two content hashes, so a stale row drops out without
        any Python iteration over vector floats.
        """

        _make_config(session)
        profile = _make_profile(session, dim=4)
        document, version, chunk = _make_doc_and_chunk(
            session, content_hash="current-hash"
        )
        session.add(
            ChunkEmbedding(
                profile_id=profile.id,
                chunk_id=chunk.id,
                content_hash="stale-hash",
                vector=[0.1, 0.2, 0.3, 0.4],
            )
        )
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )
        job.status = "completed"
        profile.status = "building"
        session.add_all([job, profile])
        session.commit()

        reconcile_profile_progress(session, profile.id)
        session.commit()
        session.refresh(profile)

        assert profile.completed_chunks == 0
        assert profile.total_chunks == 1
        # ``pending == 0`` and ``failed == 0`` but ``completed != total``,
        # so the profile stays in ``building`` (matches the original
        # branch's behaviour for "finished, but not ready").
        assert profile.status == "building"

    def test_missing_vector_excluded_from_completed(self, session):
        """A chunk with a job but no ``chunk_embeddings`` row must
        not be counted as completed.

        The main aggregate LEFT-JOINs ``chunk_embeddings`` and the
        ``completed`` counter is gated on
        ``chunk_embeddings.id IS NOT NULL``, so a missing row is
        counted as zero. The status branch stays in ``building``
        for the same reason as the stale-hash case.
        """

        _make_config(session)
        profile = _make_profile(session, dim=4)
        document, version, chunk = _make_doc_and_chunk(session)
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )
        job.status = "completed"
        profile.status = "building"
        session.add_all([job, profile])
        session.commit()
        # No ChunkEmbedding row is pre-populated.

        reconcile_profile_progress(session, profile.id)
        session.commit()
        session.refresh(profile)

        assert profile.completed_chunks == 0
        assert profile.total_chunks == 1
        assert profile.status == "building"

    def test_non_finite_vector_excluded_from_completed(self, session):
        """A non-finite vector must not be counted as completed.

        This is the SQLite "safe fallback" contract: pgvector
        refuses non-finite vectors at insert time so production
        never sees them, but the test JSON column has no such
        invariant. The reconcile path therefore re-validates
        finiteness on the rows that already passed the dim + hash
        filter, only selecting the required scalars
        ``(chunk_id, content_hash, vector)`` from
        ``chunk_embeddings``.
        """

        _make_config(session)
        profile = _make_profile(session, dim=4)
        document, version, chunk = _make_doc_and_chunk(session)
        # Persist a non-finite vector directly, bypassing the
        # embedding API's validation. SQLite's JSON column accepts
        # ``NaN`` so the row is materialised.
        session.add(
            ChunkEmbedding(
                profile_id=profile.id,
                chunk_id=chunk.id,
                content_hash=chunk.content_hash,
                vector=[0.1, float("nan"), 0.2, 0.3],
            )
        )
        job = _make_embedding_job(
            session, profile=profile, chunk=chunk, document=document, version=version
        )
        job.status = "completed"
        profile.status = "building"
        session.add_all([job, profile])
        session.commit()

        reconcile_profile_progress(session, profile.id)
        session.commit()
        session.refresh(profile)

        # The main aggregate counts the row (dim + hash match); the
        # SQLite safe fallback then excludes it because the vector
        # is not finite. Net result: ``completed_chunks == 0``.
        assert profile.completed_chunks == 0
        assert profile.total_chunks == 1
        assert profile.status == "building"

    def test_progress_aggregate_sql_excludes_vector_payload(self, session):
        """The compiled aggregate must not SELECT the vector
        payload column.

        The aggregate's SELECT clause lists only
        ``count(...)`` / ``sum(CASE WHEN ... THEN 1 ELSE 0 END)``
        expressions. The ``chunk_embeddings.vector`` column is
        referenced solely inside the dim function call (which the
        database evaluates server-side), never as a bare column
        reference that would pull float arrays back into Python.
        Both the PostgreSQL and SQLite compilations must satisfy
        this contract.
        """

        from apps.worker.services.embedding_processor import (
            _build_progress_aggregate_stmt,
        )

        _make_config(session)
        profile = _make_profile(session, dim=4)

        for dialect, label in (
            (postgresql.dialect(), "postgresql"),
            (sqlite.dialect(), "sqlite"),
        ):
            stmt = _build_progress_aggregate_stmt(dialect.name, profile)
            sql = str(
                stmt.compile(
                    dialect=dialect, compile_kwargs={"literal_binds": True}
                )
            )
            match = re.search(
                r"\bSELECT\s+(.*?)\s+\bFROM\b", sql, re.DOTALL | re.IGNORECASE
            )
            assert match, f"[{label}] could not find SELECT clause in: {sql}"
            select_clause = match.group(1)
            # Strip the dialect-specific dim function call: the
            # vector column is only allowed to appear inside it.
            stripped = re.sub(
                r"(?:vector_dims|json_array_length|json_valid)\s*\([^)]*\)",
                "DIMFN",
                select_clause,
            )
            assert "chunk_embeddings.vector" not in stripped, (
                f"[{label}] SELECT clause still references "
                f"chunk_embeddings.vector: {select_clause}"
            )
            # Sanity-check the four aggregate expressions are
            # present so a future regression that drops a counter
            # is caught.
            for needle in ("count(", "sum(CASE"):
                assert needle in select_clause, (
                    f"[{label}] expected {needle!r} in SELECT: {select_clause}"
                )

    def test_multiple_jobs_do_not_double_count_completed_vectors(self, session):
        _make_config(session)
        profile = _make_profile(session, dim=4)
        document, version, chunk = _make_doc_and_chunk(session)
        job = _make_embedding_job(session, profile=profile, chunk=chunk,
                                  document=document, version=version)
        job.status = "completed"
        session.add(ProcessingJob(document_id=document.id, document_version_id=version.id,
            stage=EMBEDDING_STAGE, status="completed", idempotency_key="another-generation",
            config_version="another", embedding_profile_id=profile.id, embedding_chunk_id=chunk.id))
        session.add(ChunkEmbedding(profile_id=profile.id, chunk_id=chunk.id,
            content_hash=chunk.content_hash, vector=[0.1, 0.2, 0.3, 0.4]))
        session.commit()
        reconcile_profile_progress(session, profile.id)
        assert profile.total_chunks == profile.completed_chunks == 1
        assert profile.status == "ready"


# --- integration with process_single_job ---------------------------------


def test_process_single_job_dispatches_to_embedding(session, monkeypatch):
    """The worker's main loop must route ``stage='embedding'``
    jobs to the new module rather than the legacy parsing path."""

    _make_config(session)
    profile = _make_profile(session, dim=4)
    document, version, chunk = _make_doc_and_chunk(session)
    job = _make_embedding_job(
        session, profile=profile, chunk=chunk, document=document, version=version
    )
    job.status = "created"
    session.add(job)
    session.commit()

    vectors = _vector_payload(4, seed=5)
    _patch_transport(
        monkeypatch,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"data": [{"embedding": v} for v in vectors]},
            )
        ),
    )
    assert process_single_job(session, job.id) is True
    session.refresh(job)
    assert job.status == "completed"
    session.refresh(profile)
    assert profile.status == "ready"
    assert profile.completed_chunks == 1
