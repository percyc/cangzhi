"""Channel-local setup edits and destination-bound saved credentials.

All credentials and connection addresses here are synthetic. The API fixture
uses an in-memory database; model discovery is stubbed before every request.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from apps.api.api import settings_ai
from apps.api.api.auth import require_admin
from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.auth import AIRuntimeConfig
from apps.api.models.embedding_profiles import EmbeddingProfile
from apps.api.models.processing import ProcessingJob
from apps.api.security import secrets
from apps.api.services.settings_connections import (
    ConnectionUpdateError,
    apply_embedding_connection,
)


CHAT_KEY = "synthetic-chat-key-never-sent"
EMBEDDING_KEY = "synthetic-embedding-key-never-sent"
OCR_KEY = "synthetic-ocr-key-never-sent"
CHAT_URL = "https://chat.example.test/v1"
EMBEDDING_URL = "https://embedding.example.test/v1"
OCR_URL = "https://ocr.example.test/v1"


@pytest.fixture(autouse=True)
def isolated_secrets_and_no_network(monkeypatch, tmp_path):
    monkeypatch.delenv("CANGZHI_SECRET_KEY", raising=False)
    store = secrets.SecretStore(tmp_path / "connection-test-secrets")
    monkeypatch.setattr(secrets, "get_secret_store", lambda: store)

    async def unexpected_discovery(**kwargs):
        raise AssertionError("A model request must be explicitly stubbed")

    monkeypatch.setattr(settings_ai, "_request_model_ids", unexpected_discovery)


def _seed_config(**overrides):
    async def seed():
        async for db in app.dependency_overrides[get_db]():
            values = {
                "provider": "openai",
                "openai_base_url": CHAT_URL,
                "openai_model": "chat-only-model",
                "openai_api_key_cipher": secrets.encrypt_secret(CHAT_KEY),
                "has_api_key": True,
                "embedding_provider": "openai",
                "embedding_base_url": EMBEDDING_URL,
                "embedding_model": "embedding-only-model",
                "embedding_api_key_cipher": secrets.encrypt_secret(EMBEDDING_KEY),
                "has_embedding_api_key": True,
                "embedding_timeout_seconds": 30,
                "ocr_provider": "openai",
                "ocr_base_url": OCR_URL,
                "ocr_model": "vision-only-model",
                "ocr_api_key_cipher": secrets.encrypt_secret(OCR_KEY),
                "has_ocr_api_key": True,
            }
            values.update(overrides)
            row = AIRuntimeConfig(**values)
            db.add(row)
            await db.commit()
            return row.id
        raise AssertionError("Missing isolated database")

    return asyncio.run(seed())


def _read_state():
    async def read():
        async for db in app.dependency_overrides[get_db]():
            row = (await db.execute(select(AIRuntimeConfig))).scalars().one()
            return {
                "config": {column.name: getattr(row, column.name) for column in row.__table__.columns},
                "profiles": await db.scalar(select(func.count()).select_from(EmbeddingProfile)),
                "jobs": await db.scalar(select(func.count()).select_from(ProcessingJob)),
            }
        raise AssertionError("Missing isolated database")

    return asyncio.run(read())


def _assert_no_secrets(response):
    assert "cipher" not in response.text
    for secret in (CHAT_KEY, EMBEDDING_KEY, OCR_KEY):
        assert secret not in response.text


def test_embedding_patch_requires_authentication(client):
    test_client, _ = client
    app.dependency_overrides.pop(require_admin, None)
    response = test_client.patch("/api/settings/ai/embedding", json={"provider": "disabled"})
    assert response.status_code == 401


@pytest.mark.parametrize("unexpected", ["chat_model", "active_embedding_profile_id", "enqueue_history"])
def test_embedding_patch_rejects_extra_fields(client, unexpected):
    test_client, _ = client
    response = test_client.patch(
        "/api/settings/ai/embedding",
        json={"provider": "disabled", unexpected: "not-allowed"},
    )
    assert response.status_code == 422


def test_embedding_patch_is_channel_local_and_does_not_schedule_work(client, monkeypatch):
    test_client, _ = client
    _seed_config()
    before = _read_state()

    async def forbidden_queue(*args, **kwargs):
        raise AssertionError("Saving embedding must not reschedule understanding")

    monkeypatch.setattr(settings_ai, "_enqueue_unclassified_documents", forbidden_queue)
    response = test_client.patch(
        "/api/settings/ai/embedding",
        json={
            "provider": "openai",
            "base_url": EMBEDDING_URL,
            "model": "replacement-embedding-model",
            "timeout_seconds": 45,
        },
    )
    assert response.status_code == 200
    _assert_no_secrets(response)
    after = _read_state()
    changed = {
        "embedding_model", "embedding_timeout_seconds", "updated_by", "updated_at",
    }
    for name, previous in before["config"].items():
        if name not in changed:
            assert after["config"][name] == previous, name
    assert after["config"]["embedding_model"] == "replacement-embedding-model"
    assert after["config"]["embedding_timeout_seconds"] == 45
    assert after["profiles"] == before["profiles"] == 0
    assert after["jobs"] == before["jobs"] == 0


def test_copy_chat_connection_keeps_independent_model_and_does_not_return_key(client):
    test_client, _ = client
    _seed_config()
    response = test_client.patch(
        "/api/settings/ai/embedding",
        json={"provider": "openai", "reuse_chat": True, "model": "explicit-embedding-model"},
    )
    assert response.status_code == 200
    public = response.json()["config"]
    assert public["embedding_base_url"] == CHAT_URL
    assert public["embedding_model"] == "explicit-embedding-model"
    assert public["openai_model"] == "chat-only-model"
    assert public["has_embedding_api_key"] is True
    _assert_no_secrets(response)
    state = _read_state()
    assert secrets.decrypt_secret(state["config"]["embedding_api_key_cipher"]) == CHAT_KEY
    assert secrets.decrypt_secret(state["config"]["ocr_api_key_cipher"]) == OCR_KEY
    assert state["profiles"] == state["jobs"] == 0


def test_copy_keeps_existing_active_profile_and_never_builds_or_activates(client):
    test_client, _ = client
    _seed_config()

    async def attach_active_profile():
        async for db in app.dependency_overrides[get_db]():
            profile = EmbeddingProfile(
                provider="openai", base_url=EMBEDDING_URL,
                model="embedding-only-model", dim=3, has_api_key=True,
                config_fingerprint="synthetic-active-profile", status="active",
            )
            db.add(profile)
            await db.flush()
            row = (await db.execute(select(AIRuntimeConfig))).scalars().one()
            row.active_embedding_profile_id = profile.id
            await db.commit()
            return profile.id
        raise AssertionError("Missing isolated database")

    profile_id = asyncio.run(attach_active_profile())
    response = test_client.patch(
        "/api/settings/ai/embedding",
        json={"provider": "openai", "reuse_chat": True, "model": "new-candidate-model"},
    )
    assert response.status_code == 200
    after = _read_state()
    assert after["config"]["active_embedding_profile_id"] == profile_id
    assert after["profiles"] == 1
    assert after["jobs"] == 0

    async def active_unchanged():
        async for db in app.dependency_overrides[get_db]():
            profile = await db.get(EmbeddingProfile, profile_id)
            assert profile.status == "active"
            assert profile.model == "embedding-only-model"
            assert profile.base_url == EMBEDDING_URL
            assert profile.build_started_at is None
            return
        raise AssertionError("Missing isolated database")

    asyncio.run(active_unchanged())


def test_copy_is_snapshot_not_live_link_to_chat(client):
    test_client, _ = client
    _seed_config()
    copied = test_client.patch(
        "/api/settings/ai/embedding", json={"provider": "openai", "reuse_chat": True},
    )
    assert copied.status_code == 200
    before = _read_state()["config"]
    changed = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://changed-chat.example.test/v1", "model": "new-chat"},
            "api_key_action": "replace",
            "api_key": "synthetic-replacement-chat-key",
        },
    )
    assert changed.status_code == 200
    after = _read_state()["config"]
    for field in ("embedding_provider", "embedding_base_url", "embedding_model", "embedding_api_key_cipher"):
        assert after[field] == before[field]
    assert after["embedding_model"] == "embedding-only-model"
    assert secrets.decrypt_secret(after["embedding_api_key_cipher"]) == CHAT_KEY


@pytest.mark.parametrize("overrides", [
    {"provider": "disabled"},
    {"provider": "ollama"},
    {"openai_base_url": None},
    {"has_api_key": False},
    {"openai_api_key_cipher": None},
])
def test_copy_requires_saved_enabled_chat_connection_and_leaves_state_intact(client, overrides):
    test_client, _ = client
    _seed_config(**overrides)
    before = _read_state()
    response = test_client.patch(
        "/api/settings/ai/embedding", json={"provider": "openai", "reuse_chat": True},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "reuse_requires_chat"
    _assert_no_secrets(response)
    assert _read_state() == before


@pytest.mark.parametrize("extra", [
    {"provider": "disabled"},
    {"provider": "ollama"},
    {"api_key_action": "replace", "api_key": "conflicting-synthetic-key"},
    {"api_key_action": "clear"},
    {"api_key": "conflicting-synthetic-key"},
])
def test_copy_rejects_conflicting_parameters_without_mutation(client, extra):
    test_client, _ = client
    _seed_config()
    before = _read_state()
    response = test_client.patch(
        "/api/settings/ai/embedding",
        json={"provider": "openai", "reuse_chat": True, **extra},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "reuse_conflict"
    assert "conflicting-synthetic-key" not in response.text
    assert _read_state() == before


def test_copy_corrupt_cipher_returns_safe_error_and_preserves_existing_connection(client):
    test_client, _ = client
    corrupt = "synthetic-not-a-valid-fernet-token"
    _seed_config(openai_api_key_cipher=corrupt)
    before = _read_state()
    response = test_client.patch(
        "/api/settings/ai/embedding",
        json={"provider": "openai", "reuse_chat": True, "model": "must-not-save"},
    )
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "reuse_key_unavailable"
    assert corrupt not in response.text
    _assert_no_secrets(response)
    assert _read_state() == before


def test_service_validates_before_mutating_when_decryption_fails():
    row = AIRuntimeConfig(
        provider="openai", openai_base_url=CHAT_URL, has_api_key=True,
        openai_api_key_cipher="synthetic-invalid-cipher",
        embedding_provider="disabled", embedding_model="old-model",
        embedding_base_url=EMBEDDING_URL, embedding_timeout_seconds=30,
    )
    before = {name: value for name, value in vars(row).items() if not name.startswith("_")}
    with pytest.raises(ConnectionUpdateError) as exc:
        apply_embedding_connection(row, {
            "provider": "openai", "reuse_chat": True,
            "model": "must-not-save", "timeout_seconds": 40,
        })
    assert exc.value.status == 500
    assert exc.value.code == "reuse_key_unavailable"
    assert {name: value for name, value in vars(row).items() if not name.startswith("_")} == before


def test_embedding_new_destination_cannot_silently_reuse_existing_key(client):
    test_client, _ = client
    _seed_config()
    before = _read_state()
    response = test_client.patch(
        "/api/settings/ai/embedding",
        json={"provider": "openai", "base_url": "https://changed.example.test/v1", "api_key_action": "keep"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "embedding_key_reconfirm"
    _assert_no_secrets(response)
    assert _read_state() == before


def test_embedding_new_destination_accepts_explicit_new_key(client):
    test_client, _ = client
    _seed_config()
    new_key = "synthetic-new-destination-key"
    response = test_client.patch(
        "/api/settings/ai/embedding",
        json={
            "provider": "openai", "base_url": "https://changed.example.test/v1",
            "api_key_action": "replace", "api_key": new_key,
        },
    )
    assert response.status_code == 200
    assert new_key not in response.text
    row = _read_state()["config"]
    assert row["embedding_base_url"] == "https://changed.example.test/v1"
    assert secrets.decrypt_secret(row["embedding_api_key_cipher"]) == new_key


def test_embedding_same_destination_allows_trailing_slash_with_saved_key(client):
    test_client, _ = client
    _seed_config()
    before = _read_state()["config"]
    response = test_client.patch(
        "/api/settings/ai/embedding",
        json={"provider": "openai", "base_url": f"{EMBEDDING_URL}/", "api_key_action": "keep"},
    )
    assert response.status_code == 200
    after = _read_state()["config"]
    assert after["embedding_base_url"] == EMBEDDING_URL
    assert after["embedding_api_key_cipher"] == before["embedding_api_key_cipher"]


@pytest.mark.parametrize("channel", ["chat", "embedding", "ocr"])
def test_discovery_saved_key_is_bound_to_its_saved_destination(client, channel):
    test_client, _ = client
    _seed_config()
    before = _read_state()
    response = test_client.post(
        "/api/settings/ai/models/discover",
        json={
            "channel": channel, "provider": "openai",
            "base_url": "https://different.example.test/v1", "use_saved_api_key": True,
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "saved_key_url_mismatch"
    _assert_no_secrets(response)
    assert _read_state() == before


@pytest.mark.parametrize("channel,url,key", [
    ("chat", CHAT_URL, CHAT_KEY),
    ("embedding", EMBEDDING_URL, EMBEDDING_KEY),
    ("ocr", OCR_URL, OCR_KEY),
])
def test_discovery_same_destination_allows_trailing_slash(client, monkeypatch, channel, url, key):
    test_client, _ = client
    _seed_config()
    before = _read_state()
    calls = []

    async def discovery(**kwargs):
        calls.append(kwargs)
        return ["synthetic-model"]

    monkeypatch.setattr(settings_ai, "_request_model_ids", discovery)
    response = test_client.post(
        "/api/settings/ai/models/discover",
        json={"channel": channel, "provider": "openai", "base_url": f"{url}/", "use_saved_api_key": True},
    )
    assert response.status_code == 200
    assert response.json()["models"] == [{"id": "synthetic-model"}]
    assert len(calls) == 1
    assert calls[0]["url"] == f"{url}/models"
    assert calls[0]["headers"]["Authorization"] == f"Bearer {key}"
    _assert_no_secrets(response)
    assert _read_state() == before


def test_discovery_new_destination_accepts_explicit_key_without_persisting(client, monkeypatch):
    test_client, _ = client
    _seed_config()
    before = _read_state()
    new_key = "synthetic-one-request-key"
    calls = []

    async def discovery(**kwargs):
        calls.append(kwargs)
        return ["new-model"]

    monkeypatch.setattr(settings_ai, "_request_model_ids", discovery)
    response = test_client.post(
        "/api/settings/ai/models/discover",
        json={
            "channel": "embedding", "provider": "openai",
            "base_url": "https://different.example.test/v1", "api_key": new_key,
            "use_saved_api_key": True,
        },
    )
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["url"] == "https://different.example.test/v1/models"
    assert calls[0]["headers"]["Authorization"] == f"Bearer {new_key}"
    assert new_key not in response.text
    assert _read_state() == before


@pytest.mark.parametrize("saved_provider", ["disabled", "ollama"])
def test_discovery_cannot_rebind_saved_key_via_non_openai_channel(client, saved_provider):
    test_client, _ = client
    _seed_config()
    new_url = "https://different.example.test/v1"
    edited = test_client.patch(
        "/api/settings/ai/embedding",
        json={"provider": saved_provider, "base_url": new_url},
    )
    assert edited.status_code == 200
    before = _read_state()
    response = test_client.post(
        "/api/settings/ai/models/discover",
        json={
            "channel": "embedding", "provider": "openai",
            "base_url": new_url, "use_saved_api_key": True,
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "saved_key_provider_mismatch"
    assert _read_state() == before


def test_ocr_copy_accepts_ui_payload_without_base_url_and_preserves_other_channels(client, monkeypatch):
    test_client, _ = client
    _seed_config()
    before = _read_state()

    async def forbidden_queue(*args, **kwargs):
        raise AssertionError("Saving OCR must not reschedule understanding")

    monkeypatch.setattr(settings_ai, "_enqueue_unclassified_documents", forbidden_queue)
    response = test_client.patch(
        "/api/settings/ai/ocr",
        json={
            "provider": "openai", "model": "independent-vision-model", "reuse_chat": True,
            "api_key_action": "keep", "timeout_seconds": 45,
            "confidence_threshold": 600, "min_chars": 8, "max_external_pages": 20,
        },
    )
    assert response.status_code == 200
    _assert_no_secrets(response)
    after = _read_state()
    assert after["config"]["ocr_provider"] == "openai"
    assert after["config"]["ocr_base_url"] == CHAT_URL
    assert after["config"]["ocr_model"] == "independent-vision-model"
    assert secrets.decrypt_secret(after["config"]["ocr_api_key_cipher"]) == CHAT_KEY
    for field, previous in before["config"].items():
        if not field.startswith("ocr_") and field not in {"updated_by", "updated_at"}:
            assert after["config"][field] == previous, field
    assert after["jobs"] == after["profiles"] == 0


@pytest.mark.parametrize("chat_provider", ["disabled", "ollama"])
def test_ocr_copy_rejects_inactive_saved_chat_even_with_leftover_openai_credentials(client, chat_provider):
    test_client, _ = client
    _seed_config(provider=chat_provider)
    before = _read_state()
    response = test_client.patch(
        "/api/settings/ai/ocr",
        json={"provider": "openai", "reuse_chat": True, "model": "not-saved"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "ocr_reuse_requires_chat"
    assert _read_state() == before


@pytest.mark.parametrize("extra", [
    {"provider": "disabled"},
    {"api_key_action": "clear"},
    {"api_key_action": "replace", "api_key": "conflicting-synthetic-key"},
])
def test_ocr_copy_rejects_conflicting_parameters_without_mutation(client, extra):
    test_client, _ = client
    _seed_config()
    before = _read_state()
    response = test_client.patch(
        "/api/settings/ai/ocr",
        json={"provider": "openai", "reuse_chat": True, **extra},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "ocr_reuse_conflict"
    assert "conflicting-synthetic-key" not in response.text
    assert _read_state() == before


def test_ocr_copy_corrupt_cipher_is_safe_and_rolls_back_partial_fields(client):
    test_client, _ = client
    corrupt = "synthetic-corrupt-chat-cipher"
    _seed_config(openai_api_key_cipher=corrupt)
    before = _read_state()
    response = test_client.patch(
        "/api/settings/ai/ocr",
        json={"provider": "openai", "reuse_chat": True, "model": "not-saved", "timeout_seconds": 45},
    )
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "ocr_reuse_decrypt_failed"
    assert corrupt not in response.text
    _assert_no_secrets(response)
    assert _read_state() == before
