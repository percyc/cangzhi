"""API integration tests for the M3-6b phase 2 lifecycle endpoints.

The tests exercise the FastAPI surface of:

* ``POST /api/embeddings/profiles/{id}/build``
* ``POST /api/embeddings/profiles/{id}/retry``
* ``POST /api/embeddings/profiles/{id}/activate``
* ``POST /api/embeddings/profiles/{id}/rollback``
* ``GET  /api/embeddings/status`` (the expanded payload)

The tests use the in-memory SQLite + TestClient fixture from
:mod:`apps.api.tests.conftest`. The profile rows are created
directly in the database (the ``/test`` endpoint is already
covered by the phase 1 tests; this file focuses on phase 2).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apps.api.api.auth import require_admin, reset_auth_limiters_for_tests
from apps.api.core.db import get_db
from apps.api.embeddings import compute_config_fingerprint
from apps.api.embeddings.build_service import EMBEDDING_STAGE
from apps.api.embeddings.canary import CANARY_VERSION
from apps.api.main import app
from apps.api.models.auth import AIRuntimeConfig
from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.embedding_profiles import ChunkEmbedding, EmbeddingProfile
from apps.api.models.processing import ProcessingJob


# --- helpers --------------------------------------------------------------


def _seed_profile(
    *,
    provider="openai",
    base_url="https://embed.example.com/v1",
    model="m",
    dim=4,
    status="tested",
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


def _seed_chunk(
    *, document_id, version_id, content_hash="hash-1", role="child"
):
    return DocumentChunk(
        document_id=document_id,
        document_version_id=version_id,
        external_id=f"chunk-{content_hash}",
        role=role,
        chunk_type="paragraph",
        order_index=0,
        content="hello world",
        search_text="hello world",
        content_hash=content_hash,
        char_count=11,
        token_estimate=3,
        is_current=True,
    )


def _setup_admin(test_client) -> None:
    app.dependency_overrides.pop(require_admin, None)
    reset_auth_limiters_for_tests()
    try:
        response = test_client.post(
            "/api/auth/setup",
            json={"username": "founder", "password": "a-strong-password"},
        )
        assert response.status_code == 200, response.text
    finally:
        app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
            id=1, username="tester", is_active=True
        )


async def _insert(profile: EmbeddingProfile | None = None, *more):
    async for db in app.dependency_overrides[get_db]():
        if profile is not None:
            db.add(profile)
        for item in more:
            db.add(item)
        await db.commit()
        if profile is not None:
            await db.refresh(profile)
        for item in more:
            await db.refresh(item)


def _seed(test_client, profile: EmbeddingProfile, *more) -> None:
    asyncio.run(_insert(profile, *more))


def _fetch_profile(profile_id: int) -> EmbeddingProfile:
    async def _run():
        async for db in app.dependency_overrides[get_db]():
            return (
                await db.execute(
                    select(EmbeddingProfile).where(EmbeddingProfile.id == profile_id)
                )
            ).scalars().first()

    return asyncio.run(_run())


def _fetch_jobs(profile_id: int) -> list[ProcessingJob]:
    async def _run():
        async for db in app.dependency_overrides[get_db]():
            return list(
                (
                    await db.execute(
                        select(ProcessingJob).where(
                            ProcessingJob.embedding_profile_id == profile_id
                        )
                    )
                ).scalars().all()
            )

    return asyncio.run(_run())


def _fetch_config() -> AIRuntimeConfig:
    async def _run():
        async for db in app.dependency_overrides[get_db]():
            return (
                await db.execute(
                    select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc())
                )
            ).scalars().first()

    return asyncio.run(_run())


def _set_active(profile_id: int) -> None:
    """Point ``ai_runtime_configs.active_embedding_profile_id``
    at ``profile_id``. Used by tests that need to set up the
    "previous active" pointer before calling ``/activate``."""

    async def _run():
        async for db in app.dependency_overrides[get_db]():
            config = (
                await db.execute(
                    select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc())
                )
            ).scalars().first()
            config.active_embedding_profile_id = profile_id
            db.add(config)
            await db.commit()

    asyncio.run(_run())


# --- 401 gate -------------------------------------------------------------


def test_lifecycle_endpoints_require_authentication(monkeypatch):
    from fastapi.testclient import TestClient as TC

    app.dependency_overrides.pop(require_admin, None)
    try:
        with TC(app) as client_:
            for path in (
                "/api/embeddings/profiles/1/build",
                "/api/embeddings/profiles/1/retry",
                "/api/embeddings/profiles/1/activate",
                "/api/embeddings/profiles/1/rollback",
            ):
                response = client_.post(path)
                assert response.status_code == 401, path
                assert response.json()["detail"]["code"] == "unauthenticated", path
    finally:
        app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
            id=1, username="tester", is_active=True
        )


# --- /status returns the expanded payload --------------------------------


def test_status_endpoint_lists_profiles_with_actions(client):
    test_client, _ = client
    _setup_admin(test_client)
    profile = _seed_profile(status="tested")
    _seed(test_client, profile)

    body = test_client.get("/api/embeddings/status").json()
    assert "profiles" in body
    assert "status_legend" in body
    assert set(body["status_legend"]["buildable"]) == {"tested", "failed"}
    assert set(body["status_legend"]["retryable"]) == {"building", "failed"}
    assert set(body["status_legend"]["activatable"]) >= {"ready", "retired"}
    assert len(body["profiles"]) == 1
    summary = body["profiles"][0]
    assert summary["id"] == profile.id
    assert summary["status"] == "tested"
    assert "build" in summary["available_actions"]


# --- /build ---------------------------------------------------------------


def test_build_endpoint_enqueues_jobs_for_each_chunk(client):
    test_client, _ = client
    _setup_admin(test_client)
    profile = _seed_profile(status="tested")
    document = Document(title="测试", source_type=DocumentSourceType.note)
    version = DocumentVersion(
        document_id=1,
        version_number=1,
        content_hash="doc-hash",
        raw_content="raw",
        structured_content={"blocks": []},
        processing_status="ready",
    )
    chunks = [
        _seed_chunk(document_id=1, version_id=1, content_hash=f"hash-{i}")
        for i in range(3)
    ]
    _seed(test_client, profile, document, version, *chunks)
    profile_id = profile.id
    # Resolve the foreign keys: we used placeholder ids; the
    # actual ids are populated by SQLAlchemy after the commit.
    fetched = _fetch_profile(profile_id)
    assert fetched.id == profile_id

    response = test_client.post(f"/api/embeddings/profiles/{profile_id}/build")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "building"
    assert body["enqueued"] == 3
    assert body["total_chunks"] == 3

    jobs = _fetch_jobs(profile_id)
    assert len(jobs) == 3
    for job in jobs:
        assert job.stage == EMBEDDING_STAGE
        assert job.embedding_profile_id == profile_id
        assert job.embedding_chunk_id is not None


def test_build_endpoint_rejects_already_building(client):
    test_client, _ = client
    _setup_admin(test_client)
    profile = _seed_profile(status="building")
    _seed(test_client, profile)
    response = test_client.post(f"/api/embeddings/profiles/{profile.id}/build")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "profile_not_buildable"


def test_build_endpoint_rejects_unknown_profile(client):
    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.post("/api/embeddings/profiles/999/build")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "profile_not_found"


def test_build_empty_corpus_yields_ready(client):
    test_client, _ = client
    _setup_admin(test_client)
    profile = _seed_profile(status="tested")
    _seed(test_client, profile)
    response = test_client.post(f"/api/embeddings/profiles/{profile.id}/build")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["total_chunks"] == 0
    assert body["build_finished_at"] is not None


# --- /retry ---------------------------------------------------------------


def test_retry_endpoint_resets_failed_jobs(client):
    test_client, _ = client
    _setup_admin(test_client)
    profile = _seed_profile(status="tested")
    document = Document(title="测试", source_type=DocumentSourceType.note)
    version = DocumentVersion(
        document_id=1,
        version_number=1,
        content_hash="doc-hash",
        raw_content="raw",
        structured_content={"blocks": []},
        processing_status="ready",
    )
    chunks = [
        _seed_chunk(document_id=1, version_id=1, content_hash=f"hash-{i}")
        for i in range(2)
    ]
    _seed(test_client, profile, document, version, *chunks)
    profile_id = profile.id
    test_client.post(f"/api/embeddings/profiles/{profile_id}/build")
    jobs = _fetch_jobs(profile_id)
    for job in jobs:
        job.status = "failed"
        job.last_error = "upstream 500"
    # Persist the failure status.
    async def _commit_failures():
        async for db in app.dependency_overrides[get_db]():
            for job in jobs:
                db.add(job)
            # Update the profile's failed_chunks to match.
            profile_row = (
                await db.execute(
                    select(EmbeddingProfile).where(EmbeddingProfile.id == profile_id)
                )
            ).scalars().first()
            profile_row.failed_chunks = 2
            db.add(profile_row)
            await db.commit()

    asyncio.run(_commit_failures())

    response = test_client.post(f"/api/embeddings/profiles/{profile_id}/retry")
    assert response.status_code == 200
    body = response.json()
    assert body["enqueued"] == 2
    refreshed = _fetch_jobs(profile_id)
    statuses = sorted(job.status for job in refreshed)
    assert statuses == ["created", "created"]


def test_retry_endpoint_rejects_ready(client):
    test_client, _ = client
    _setup_admin(test_client)
    profile = _seed_profile(status="ready")
    _seed(test_client, profile)
    response = test_client.post(f"/api/embeddings/profiles/{profile.id}/retry")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "profile_not_buildable"


# --- /activate ------------------------------------------------------------


def test_activate_endpoint_swaps_active_pointer(client):
    test_client, _ = client
    _setup_admin(test_client)
    # Configure a runtime so the activate endpoint has a row to lock.
    config = AIRuntimeConfig(
        provider="disabled",
        timeout_seconds=30,
        prompt_version="v1",
        singleton_key="singleton",
        embedding_provider="openai",
        embedding_base_url="https://embed.example.com/v1",
        embedding_model="m",
        embedding_timeout_seconds=30,
    )
    profile = _seed_profile(status="ready")
    # Empty corpus means ready is allowed.
    _seed(test_client, config, profile)
    response = test_client.post(f"/api/embeddings/profiles/{profile.id}/activate")
    assert response.status_code == 200
    body = response.json()
    assert body["active_profile_id"] == profile.id

    config_row = _fetch_config()
    assert config_row.active_embedding_profile_id == profile.id


def test_activate_endpoint_demotes_previous(client):
    test_client, _ = client
    _setup_admin(test_client)
    config = AIRuntimeConfig(
        provider="disabled",
        timeout_seconds=30,
        prompt_version="v1",
        singleton_key="singleton",
        embedding_provider="openai",
        embedding_base_url="https://embed.example.com/v1",
        embedding_model="m",
        embedding_timeout_seconds=30,
    )
    old = _seed_profile(
        status="active", base_url="https://old.example.com/v1"
    )
    new = _seed_profile(
        status="ready", base_url="https://new.example.com/v1"
    )
    _seed(test_client, config, old, new)
    # Make ``old`` the previously active profile before we
    # promote ``new`` to active.
    _set_active(old.id)
    old_id = old.id
    new_id = new.id
    response = test_client.post(f"/api/embeddings/profiles/{new_id}/activate")
    assert response.status_code == 200
    assert response.json()["active_profile_id"] == new_id
    assert response.json()["previous_profile_id"] == old_id

    old_refreshed = _fetch_profile(old_id)
    new_refreshed = _fetch_profile(new_id)
    assert old_refreshed.status == "retired"
    assert new_refreshed.status == "active"


def test_activate_endpoint_rejects_tested(client):
    test_client, _ = client
    _setup_admin(test_client)
    config = AIRuntimeConfig(
        provider="disabled",
        timeout_seconds=30,
        prompt_version="v1",
        singleton_key="singleton",
        embedding_provider="openai",
        embedding_base_url="https://embed.example.com/v1",
        embedding_model="m",
        embedding_timeout_seconds=30,
    )
    profile = _seed_profile(status="tested")
    _seed(test_client, config, profile)
    response = test_client.post(f"/api/embeddings/profiles/{profile.id}/activate")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "profile_not_activatable"


def test_activate_endpoint_rejects_incomplete(client):
    """A ready profile that hasn't covered the corpus is
    refused, even though the operator's UI says ``ready``."""

    test_client, _ = client
    _setup_admin(test_client)
    config = AIRuntimeConfig(
        provider="disabled",
        timeout_seconds=30,
        prompt_version="v1",
        singleton_key="singleton",
        embedding_provider="openai",
        embedding_base_url="https://embed.example.com/v1",
        embedding_model="m",
        embedding_timeout_seconds=30,
    )
    profile = _seed_profile(status="ready")
    document = Document(title="测试", source_type=DocumentSourceType.note)
    version = DocumentVersion(
        document_id=1,
        version_number=1,
        content_hash="doc-hash",
        raw_content="raw",
        structured_content={"blocks": []},
        processing_status="ready",
    )
    chunk = _seed_chunk(document_id=1, version_id=1)
    _seed(test_client, config, profile, document, version, chunk)
    response = test_client.post(f"/api/embeddings/profiles/{profile.id}/activate")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "profile_not_ready"


# --- /rollback ------------------------------------------------------------


def test_rollback_endpoint_demotes_current_active(client):
    test_client, _ = client
    _setup_admin(test_client)
    config = AIRuntimeConfig(
        provider="disabled",
        timeout_seconds=30,
        prompt_version="v1",
        singleton_key="singleton",
        embedding_provider="openai",
        embedding_base_url="https://embed.example.com/v1",
        embedding_model="m",
        embedding_timeout_seconds=30,
    )
    # The "current" active profile is B, we want to roll back
    # to A (which is retired).
    a = _seed_profile(
        status="retired", base_url="https://a.example.com/v1"
    )
    b = _seed_profile(
        status="active", base_url="https://b.example.com/v1"
    )
    _seed(test_client, config, a, b)
    # Make b the active pointer before rolling back to a.
    _set_active(b.id)

    response = test_client.post(f"/api/embeddings/profiles/{a.id}/rollback")
    assert response.status_code == 200
    body = response.json()
    assert body["active_profile_id"] == a.id
    assert body["previous_profile_id"] == b.id

    a_fetched = _fetch_profile(a.id)
    b_fetched = _fetch_profile(b.id)
    assert a_fetched.status == "active"
    assert b_fetched.status == "retired"


def test_rollback_endpoint_rejects_tested(client):
    test_client, _ = client
    _setup_admin(test_client)
    config = AIRuntimeConfig(
        provider="disabled",
        timeout_seconds=30,
        prompt_version="v1",
        singleton_key="singleton",
        embedding_provider="openai",
        embedding_base_url="https://embed.example.com/v1",
        embedding_model="m",
        embedding_timeout_seconds=30,
    )
    profile = _seed_profile(status="tested")
    _seed(test_client, config, profile)
    response = test_client.post(f"/api/embeddings/profiles/{profile.id}/rollback")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "profile_not_activatable"
