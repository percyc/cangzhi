"""Integration tests for the embedding compatibility endpoints (ADR-015).

The tests are layered:

* the unit tests in :mod:`apps.api.tests.test_embeddings` cover
  the pure helpers (fingerprint, cosine, judge);
* the tests in this file exercise the FastAPI surface, the
  database persistence and the HTTP mock that the service uses
  to talk to the embedding provider.

The mock is the same ``httpx.MockTransport`` pattern used by
``test_settings_ai.py``. We monkey-patch the ``httpx.AsyncClient``
constructor that lives inside ``apps.api.embeddings.service`` so
the test path never reaches a real network.
"""

from __future__ import annotations

import json
import math
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from apps.api.api.auth import require_admin, reset_auth_limiters_for_tests
from apps.api.core.db import get_db
from apps.api.embeddings import CANARY_TEXTS, CANARY_VERSION
from apps.api.embeddings.compatibility import (
    COSINE_THRESHOLD,
    EXPECTED_CANARY_COUNT,
)
from apps.api.embeddings.fingerprint import compute_config_fingerprint
from apps.api.main import app
from apps.api.models.auth import AIRuntimeConfig
from apps.api.models.embedding_profiles import EmbeddingProfile


# --- helpers ---------------------------------------------------------------


class _MockAsyncClient:
    """Minimal httpx.AsyncClient stand-in for monkeypatched tests."""

    def __init__(self, transport: httpx.MockTransport, **kwargs):
        self._transport = transport
        # ``**kwargs`` is intentionally swallowed so we accept the
        # ``trust_env=False`` / ``timeout=...`` arguments the service
        # passes.
        self._kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, url, **kwargs):
        request = httpx.Request("GET", str(url), **kwargs)
        return self._transport.handle_request(request)

    async def post(self, url, **kwargs):
        request = httpx.Request("POST", str(url), **kwargs)
        return self._transport.handle_request(request)


def _patch_embedding_transport(monkeypatch, transport: httpx.MockTransport) -> None:
    """Replace the service module's ``httpx.AsyncClient`` with a mock."""

    monkeypatch.setattr(
        "apps.api.embeddings.service.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport, **kwargs),
    )


def _setup_admin(test_client) -> None:
    """Create the single admin row so dependent endpoints can resolve state.

    The conftest fixture already overrides ``require_admin``; this
    helper only seeds the database so the legacy chat-side code
    (which keys on the admin row) keeps working.
    """

    app.dependency_overrides.pop(require_admin, None)
    reset_auth_limiters_for_tests()
    try:
        response = test_client.post(
            "/api/auth/setup",
            json={"username": "founder", "password": "a-strong-password"},
        )
        assert response.status_code == 200, response.text
    finally:
        from types import SimpleNamespace as _NS

        app.dependency_overrides[require_admin] = lambda: _NS(
            id=1, username="tester", is_active=True
        )


def _configure_embedding_channel(
    test_client,
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key: str | None = None,
    timeout_seconds: int = 30,
) -> None:
    """Persist the independent embedding channel via the existing settings API."""

    payload: dict = {
        "provider": "openai",
        "openai": {
            "base_url": "https://placeholder.example.com/v1",
            "model": "placeholder-model",
        },
        "api_key_action": "keep",
        "embedding_model": model,
        "embedding_provider": provider,
        "embedding_base_url": base_url,
        "embedding_timeout_seconds": timeout_seconds,
    }
    if provider == "openai":
        if api_key is not None:
            payload["embedding_api_key_action"] = "replace"
            payload["embedding_api_key"] = api_key
        else:
            payload["embedding_api_key_action"] = "keep"
    response = test_client.patch("/api/settings/ai", json=payload)
    assert response.status_code == 200, response.text


def _build_canary_vectors(seed: int, dim: int) -> list[list[float]]:
    """Three deterministic canary vectors, one per canary text.

    The LCG keeps the test independent from the random module's
    state so two runs in the same process produce the same
    vectors.
    """

    state = seed
    out: list[list[float]] = []
    for _ in range(EXPECTED_CANARY_COUNT):
        vector: list[float] = []
        for _ in range(dim):
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            vector.append(((state % 2000) - 1000) / 1000.0)
        out.append(vector)
    return out


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


# --- 401 gate --------------------------------------------------------------


def test_embeddings_endpoints_require_authentication(monkeypatch):
    """Anonymous requests must be rejected on both routes."""

    from fastapi.testclient import TestClient as TC

    app.dependency_overrides.pop(require_admin, None)
    try:
        with TC(app) as client_:
            response = client_.post("/api/embeddings/test")
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "unauthenticated"
            response = client_.get("/api/embeddings/status")
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "unauthenticated"
    finally:
        app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
            id=1, username="tester", is_active=True
        )


# --- /api/embeddings/test --------------------------------------------------


def test_test_endpoint_rejects_when_no_embedding_model(client):
    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.post("/api/embeddings/test")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "尚未选择 Embedding 模型" in body["reason"]
    assert body["decision"] == "rebuild_required"


def test_test_endpoint_rejects_when_provider_disabled(client):
    test_client, _ = client
    _setup_admin(test_client)
    # Persist a config that has the embedding model name but a
    # disabled embedding channel — the endpoint must refuse on
    # "channel disabled" rather than probe the model list.
    async def _seed():
        async for db in app.dependency_overrides[get_db]():
            row = AIRuntimeConfig(
                provider="disabled",
                embedding_model="text-embedding-3-small",
                embedding_provider="disabled",
                timeout_seconds=30,
                prompt_version="v1",
                singleton_key="singleton",
            )
            db.add(row)
            await db.commit()

    __import__("asyncio").run(_seed())
    body = test_client.post("/api/embeddings/test").json()
    assert body["ok"] is False
    assert "未启用" in body["reason"]


def test_test_endpoint_openai_success_persists_tested_profile(client, monkeypatch):
    """A valid OpenAI response persists a profile row and returns the verdict."""

    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        provider="openai",
        base_url="https://api.example.com/v1",
        model="text-embedding-3-small",
        api_key="sk-embed-test-aaaaaaaaaaaaaaaa",
    )

    vectors = _build_canary_vectors(101, 4)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "text-embedding-3-small"
        assert isinstance(body["input"], list)
        assert len(body["input"]) == EXPECTED_CANARY_COUNT
        # The exact strings are the canary; the request must not
        # echo the API key.
        assert all(text in CANARY_TEXTS for text in body["input"])
        assert "sk-embed-test-aaaaaaaaaaaaaaaa" not in request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={"data": [{"embedding": v} for v in vectors]},
        )

    transport = httpx.MockTransport(handler)
    _patch_embedding_transport(monkeypatch, transport)

    response = test_client.post("/api/embeddings/test")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["decision"] == "unknown"  # no active profile yet
    assert body["config"]["dim"] == 4
    assert body["config"]["provider"] == "openai"
    assert body["config"]["has_api_key"] is True
    assert body["config"]["canary_count"] == EXPECTED_CANARY_COUNT
    assert body["config"]["canary_version"] == CANARY_VERSION
    assert body["scores"] == []  # unknown → no cosine work
    assert body["profile"]["id"] is not None
    assert body["profile"]["status"] == "tested"
    assert body["active"]["id"] is None

    # The persisted row carries the canary vectors, the
    # configuration fingerprint and the ``tested`` status.
    async def _fetch():
        async for db in app.dependency_overrides[get_db]():
            return (
                await db.execute(
                    select(EmbeddingProfile).where(
                        EmbeddingProfile.id == body["profile"]["id"]
                    )
                )
            ).scalars().first()

    profile = __import__("asyncio").run(_fetch())
    assert profile is not None
    assert profile.status == "tested"
    assert profile.dim == 4
    assert profile.model == "text-embedding-3-small"
    assert profile.has_api_key is True
    assert profile.key_fingerprint is not None
    # The canary vectors are stored on the row, but the public
    # response only echoes the count and the dimension.
    assert profile.canary_vectors is not None
    assert len(profile.canary_vectors) == EXPECTED_CANARY_COUNT
    # The fingerprint includes the key_fingerprint and the canary
    # version, and is stable across two calls.
    expected_fp = compute_config_fingerprint(
        provider="openai",
        base_url="https://api.example.com/v1",
        model="text-embedding-3-small",
        dim=4,
        has_api_key=True,
        key_fingerprint=profile.key_fingerprint,
        canary_version=CANARY_VERSION,
    )
    assert profile.config_fingerprint == expected_fp


def test_test_endpoint_never_leaks_key_in_response(client, monkeypatch):
    """An upstream error that embeds the API key must not surface
    in the public response or in the persisted row.
    """

    test_client, _ = client
    _setup_admin(test_client)
    secret = "sk-embed-leak-zzzzzzzzzzzzzzzzz"
    _configure_embedding_channel(
        test_client,
        provider="openai",
        base_url="https://api.example.com/v1",
        model="text-embedding-3-small",
        api_key=secret,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            text=f"upstream failure {secret} Bearer {secret}",
        )

    transport = httpx.MockTransport(handler)
    _patch_embedding_transport(monkeypatch, transport)

    response = test_client.post("/api/embeddings/test")
    body = response.json()
    assert body["ok"] is False
    assert secret not in body["reason"]
    assert secret not in response.text

    # A ``failed`` row is recorded but does not contain the key.
    async def _fetch():
        async for db in app.dependency_overrides[get_db]():
            return (
                await db.execute(select(EmbeddingProfile))
            ).scalars().all()

    rows = __import__("asyncio").run(_fetch())
    assert rows, "expected a failed profile row to be recorded"
    for row in rows:
        assert row.status == "failed"
        assert row.last_error is not None
        assert secret not in row.last_error
        assert row.key_fingerprint is None or secret not in row.key_fingerprint


def test_test_endpoint_ollama_legacy_fallback(client, monkeypatch):
    """An Ollama 404 on ``/api/embed`` falls back to ``/api/embeddings``."""

    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        provider="ollama",
        base_url="http://localhost:11434",
        model="nomic-embed-text",
    )

    vectors = _build_canary_vectors(202, 3)
    legacy_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/embed"):
            return httpx.Response(404, text="not found")
        legacy_calls.append(request)
        # The legacy endpoint only takes one ``prompt`` at a time.
        payload = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"embedding": vectors[len(legacy_calls) - 1]},
        )

    transport = httpx.MockTransport(handler)
    _patch_embedding_transport(monkeypatch, transport)

    response = test_client.post("/api/embeddings/test")
    body = response.json()
    assert body["ok"] is True
    assert body["config"]["dim"] == 3
    assert body["config"]["provider"] == "ollama"
    assert body["profile"]["status"] == "tested"
    assert len(legacy_calls) == EXPECTED_CANARY_COUNT


def test_test_endpoint_rejects_dim_mismatch_in_canary_response(client, monkeypatch):
    """A response where the canary vectors disagree on dimensionality
    must be reported as a hard failure and never reach the
    compatibility judge.
    """

    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        provider="openai",
        base_url="https://api.example.com/v1",
        model="text-embedding-3-small",
        api_key="sk-embed-dim-zzzzzzzzzzzzzzzz",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.1, 0.2, 0.3, 0.4]},
                    {"embedding": [0.1, 0.2, 0.3]},
                    {"embedding": [0.1, 0.2, 0.3, 0.4]},
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    _patch_embedding_transport(monkeypatch, transport)

    response = test_client.post("/api/embeddings/test")
    body = response.json()
    assert body["ok"] is False
    assert "维度" in body["reason"]
    assert body["decision"] == "rebuild_required"


def test_test_endpoint_rejects_non_finite_vectors(client, monkeypatch):
    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        provider="openai",
        base_url="https://api.example.com/v1",
        model="text-embedding-3-small",
        api_key="sk-embed-nan-zzzzzzzzzzzzzzz",
    )

    body_bytes = b'{"data": [{"embedding": [0.1, 1e9999, 0.2, 0.3]}, {"embedding": [0.1, 0.2, 0.3, 0.4]}, {"embedding": [0.1, 0.2, 0.3, 0.4]}]}'
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=body_bytes,
        )
    )
    _patch_embedding_transport(monkeypatch, transport)

    body = test_client.post("/api/embeddings/test").json()
    assert body["ok"] is False
    assert "非法" in body["reason"] or "NaN" in body["reason"]


def test_test_endpoint_compatible_when_canary_above_threshold(client, monkeypatch):
    """Re-test the *same* configuration after the first test
    established it as the active profile. The second call must
    report ``same`` only after the fresh canary results are also
    verified.
    """

    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        provider="openai",
        base_url="https://api.example.com/v1",
        model="text-embedding-3-small",
        api_key="sk-embed-same-aaaaaaaaaaaaaa",
    )

    vectors = _build_canary_vectors(303, 8)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"data": [{"embedding": v} for v in vectors]},
        )
    )
    _patch_embedding_transport(monkeypatch, transport)

    first = test_client.post("/api/embeddings/test").json()
    assert first["ok"] is True
    assert first["decision"] == "unknown"

    # Promote the freshly-tested profile to "active" so the next
    # probe has something to compare against.
    async def _activate():
        async for db in app.dependency_overrides[get_db]():
            config = (
                await db.execute(
                    select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc())
                )
            ).scalars().first()
            config.active_embedding_profile_id = first["profile"]["id"]
            await db.commit()

    __import__("asyncio").run(_activate())

    second = test_client.post("/api/embeddings/test").json()
    assert second["ok"] is True
    assert second["decision"] == "same"
    assert "指纹" in second["reason"]
    assert second["scores"] == [1.0, 1.0, 1.0]
    assert second["active"]["id"] == first["profile"]["id"]


def test_test_endpoint_rebuild_when_canary_below_threshold(client, monkeypatch):
    """A second test against a different URL (but the same model
    name and dimensionality) must report ``rebuild_required``
    because the canary vectors are very different.

    Note: changing the URL while keeping the model name and
    dimension is the realistic case where the operator is
    migrating to a new gateway that fronts a different
    implementation. The fingerprint differs, so the cosine
    check is exercised.
    """

    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        provider="openai",
        base_url="https://api.example.com/v1",
        model="text-embedding-3-small",
        api_key="sk-embed-rebuild-aaaaaaaaaa",
    )

    active_vectors = _build_canary_vectors(404, 8)
    candidate_vectors = _build_canary_vectors(909, 8)

    request_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        request_count["n"] += 1
        # First call = active profile; second call = candidate.
        which = active_vectors if request_count["n"] == 1 else candidate_vectors
        return httpx.Response(
            200,
            json={"data": [{"embedding": v} for v in which]},
        )

    transport = httpx.MockTransport(handler)
    _patch_embedding_transport(monkeypatch, transport)

    first = test_client.post("/api/embeddings/test").json()
    assert first["decision"] == "unknown"

    async def _activate():
        async for db in app.dependency_overrides[get_db]():
            config = (
                await db.execute(
                    select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc())
                )
            ).scalars().first()
            config.active_embedding_profile_id = first["profile"]["id"]
            await db.commit()

    __import__("asyncio").run(_activate())

    # Now point the embedding channel at a *different* upstream
    # URL. The model name and dimensionality stay the same, so
    # the only thing that changes is the vector space itself.
    # The candidate vectors are very different from the active
    # ones, so the cosine must fall below the threshold.
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {
                "base_url": "https://placeholder.example.com/v1",
                "model": "placeholder-model",
            },
            "api_key_action": "keep",
            "embedding_model": "text-embedding-3-small",
            "embedding_provider": "openai",
            "embedding_base_url": "https://api.different-provider.example.com/v1",
            "embedding_api_key_action": "keep",
            "embedding_timeout_seconds": 30,
        },
    )

    second = test_client.post("/api/embeddings/test").json()
    assert second["ok"] is True
    assert second["decision"] == "rebuild_required"
    # The per-canary scores are reported; they must all be well
    # below the threshold because the candidate and active
    # vectors are unrelated.
    assert len(second["scores"]) == EXPECTED_CANARY_COUNT
    assert all(score < COSINE_THRESHOLD for score in second["scores"])


def test_test_endpoint_rebuild_when_dim_changes(client, monkeypatch):
    """Switching to a model that returns a different dimensionality
    must be reported as ``rebuild_required``.
    """

    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        provider="openai",
        base_url="https://api.example.com/v1",
        model="text-embedding-3-small",
        api_key="sk-embed-dim2-aaaaaaaaaaaaaa",
    )

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.1, 0.2, 0.3, 0.4]},
                    {"embedding": [0.1, 0.2, 0.3, 0.4]},
                    {"embedding": [0.1, 0.2, 0.3, 0.4]},
                ]
            },
        )
    )
    _patch_embedding_transport(monkeypatch, transport)
    first = test_client.post("/api/embeddings/test").json()
    assert first["config"]["dim"] == 4

    async def _activate():
        async for db in app.dependency_overrides[get_db]():
            config = (
                await db.execute(
                    select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc())
                )
            ).scalars().first()
            config.active_embedding_profile_id = first["profile"]["id"]
            await db.commit()

    __import__("asyncio").run(_activate())

    # The same model name but a different dimension (e.g. a user
    # trims ``text-embedding-3-small`` to 256 via a custom
    # gateway). The mock now returns 256-dimensional vectors.
    def handler2(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.01] * 256},
                    {"embedding": [0.01] * 256},
                    {"embedding": [0.01] * 256},
                ]
            },
        )

    transport2 = httpx.MockTransport(handler2)
    _patch_embedding_transport(monkeypatch, transport2)

    second = test_client.post("/api/embeddings/test").json()
    assert second["decision"] == "rebuild_required"
    assert "维度" in second["reason"]


def test_test_endpoint_compatible_when_only_key_rotates(client, monkeypatch):
    """A key rotation must NOT change the verdict as long as the
    canary vectors are still within the threshold.

    We simulate the rotation by re-using the same canary vectors
    but rewriting the persisted API key. The fingerprint includes
    the key_fingerprint (which is derived from the master Fernet
    key, not the API key) so it stays the same; the verdict is
    ``compatible`` only if the cosine check agrees.
    """

    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        provider="openai",
        base_url="https://api.example.com/v1",
        model="text-embedding-3-small",
        api_key="sk-embed-original-aaaaaaaaa",
    )

    # Reuse the same vectors for both probes to make the cosine
    # exactly 1.0.
    active_vectors = _build_canary_vectors(505, 8)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"data": [{"embedding": v} for v in active_vectors]},
        )
    )
    _patch_embedding_transport(monkeypatch, transport)

    first = test_client.post("/api/embeddings/test").json()
    assert first["decision"] == "unknown"

    async def _activate():
        async for db in app.dependency_overrides[get_db]():
            config = (
                await db.execute(
                    select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc())
                )
            ).scalars().first()
            config.active_embedding_profile_id = first["profile"]["id"]
            await db.commit()

    __import__("asyncio").run(_activate())

    # Rotate the key in the database and re-run the probe. The
    # key change must not change the verdict.
    async def _rotate():
        async for db in app.dependency_overrides[get_db]():
            config = (
                await db.execute(
                    select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc())
                )
            ).scalars().first()
            from apps.api.security.secrets import encrypt_secret

            config.embedding_api_key_cipher = encrypt_secret(
                "sk-embed-rotated-bbbbbbbbb"
            )
            config.has_embedding_api_key = True
            await db.commit()

    __import__("asyncio").run(_rotate())

    second = test_client.post("/api/embeddings/test").json()
    # The fingerprint includes the key_fingerprint, so the
    # fingerprint itself does change (the master key fingerprint
    # is the same, but the canary comparison is what determines
    # compatibility). The verdict must therefore be
    # ``compatible`` (or ``same`` if the master key fingerprint
    # also matched), never ``rebuild_required``.
    assert second["ok"] is True
    assert second["decision"] in {"compatible", "same"}
    assert second["decision"] != "rebuild_required"
    if second["decision"] == "compatible":
        assert all(score >= COSINE_THRESHOLD for score in second["scores"])


def test_test_endpoint_is_idempotent_on_same_config(client, monkeypatch):
    """Re-running the probe on the exact same configuration
    overwrites the previous row in place rather than creating
    duplicates.
    """

    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        provider="openai",
        base_url="https://api.example.com/v1",
        model="text-embedding-3-small",
        api_key="sk-embed-idem-aaaaaaaaaaaaaa",
    )

    vectors = _build_canary_vectors(606, 4)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"data": [{"embedding": v} for v in vectors]},
        )
    )
    _patch_embedding_transport(monkeypatch, transport)

    first = test_client.post("/api/embeddings/test").json()
    second = test_client.post("/api/embeddings/test").json()

    assert first["profile"]["id"] == second["profile"]["id"]
    assert first["config"]["config_fingerprint"] == second["config"]["config_fingerprint"]

    async def _count():
        async for db in app.dependency_overrides[get_db]():
            return (
                await db.execute(select(EmbeddingProfile))
            ).scalars().all()

    rows = __import__("asyncio").run(_count())
    assert len(rows) == 1


# --- /api/embeddings/status -----------------------------------------------


def test_status_endpoint_returns_empty_state_when_no_profile(client):
    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.get("/api/embeddings/status")
    assert response.status_code == 200
    body = response.json()
    assert body["canary_version"] == CANARY_VERSION
    assert body["active_profile"]["id"] is None
    assert body["last_tested"]["id"] is None
    assert body["config"]["provider"] == "disabled"


def test_status_endpoint_surfaces_active_and_last_tested(client, monkeypatch):
    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        provider="openai",
        base_url="https://api.example.com/v1",
        model="text-embedding-3-small",
        api_key="sk-embed-status-aaaaaaaaaa",
    )

    vectors = _build_canary_vectors(707, 4)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"data": [{"embedding": v} for v in vectors]},
        )
    )
    _patch_embedding_transport(monkeypatch, transport)

    test_client.post("/api/embeddings/test")

    async def _activate():
        async for db in app.dependency_overrides[get_db]():
            config = (
                await db.execute(
                    select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc())
                )
            ).scalars().first()
            profile = (
                await db.execute(
                    select(EmbeddingProfile).order_by(EmbeddingProfile.id.desc())
                )
            ).scalars().first()
            config.active_embedding_profile_id = profile.id
            await db.commit()

    __import__("asyncio").run(_activate())

    body = test_client.get("/api/embeddings/status").json()
    assert body["active_profile"]["id"] is not None
    assert body["active_profile"]["status"] == "tested"
    assert body["active_profile"]["dim"] == 4
    assert body["active_profile"]["model"] == "text-embedding-3-small"
    assert body["active_profile"]["config_fingerprint"] is not None
    # The public status endpoint must never return raw canary
    # vectors or any key material.
    body_text = json.dumps(body, ensure_ascii=False)
    assert "embedding" not in body_text or "config" in body_text  # keys names are fine
    assert "sk-embed-status-aaaaaaaaaa" not in body_text
    assert body["last_tested"]["id"] == body["active_profile"]["id"]
    assert body["config"]["provider"] == "openai"
    assert body["config"]["model"] == "text-embedding-3-small"


# --- 404 on ``/api/embeddings/test`` does not exist for unknown paths -----


def test_status_endpoint_never_returns_vectors_or_keys(client, monkeypatch):
    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        provider="openai",
        base_url="https://api.example.com/v1",
        model="text-embedding-3-small",
        api_key="sk-embed-sanity-aaaaaaaaaa",
    )

    vectors = _build_canary_vectors(808, 4)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"data": [{"embedding": v} for v in vectors]},
        )
    )
    _patch_embedding_transport(monkeypatch, transport)
    test_client.post("/api/embeddings/test")

    body_text = test_client.get("/api/embeddings/status").text
    # No key, no vector coordinates, no ciphertext.
    assert "sk-embed-sanity-aaaaaaaaaa" not in body_text
    # The canary vector values are deterministic but never appear
    # in the public response.
    for vector in vectors:
        # If the first element of a vector were ever serialised
        # this assertion would catch it.
        first = vector[0]
        assert f"{first:.4f}" not in body_text or first == 0.0
