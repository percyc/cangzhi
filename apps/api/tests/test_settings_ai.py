"""Integration tests for the AI settings API.

The tests focus on the security contract: the page never sees the
key, the database stores only the ciphertext, the test connection
is sanitised, and a user-controlled runtime configuration takes
precedence over the legacy ``.env`` settings.
"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from apps.api.api.auth import require_admin, reset_auth_limiters_for_tests
from apps.api.core.config import settings as api_settings
from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.auth import Admin, AIRuntimeConfig
from apps.api.security.secrets import decrypt_secret


def _clear_auth_override():
    app.dependency_overrides.pop(require_admin, None)
    reset_auth_limiters_for_tests()


def _setup_admin(test_client):
    _clear_auth_override()
    response = test_client.post(
        "/api/auth/setup",
        json={"username": "founder", "password": "a-strong-password"},
    )
    assert response.status_code == 200
    return response


def _patch_env(monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setattr(api_settings, key, value)


# --- singleton enforcement ----------------------------------------------


def test_ai_config_table_is_singleton_via_database_constraints(client):
    """The DB rejects a second AI runtime config row even if the
    application pre-check is bypassed.

    The migration adds a UNIQUE constraint on ``singleton_key``;
    the model also exposes the same key. We pre-seed one row
    through the public API, then try to insert a second row
    directly. The commit must fail with an integrity error.
    """

    test_client, _ = client
    _setup_admin(test_client)
    # Seed the singleton row by reading the current (default) state.
    test_client.get("/api/settings/ai")

    async def _insert_second():
        async for db in app.dependency_overrides[get_db]():
            row = AIRuntimeConfig(
                provider="ollama",
                ollama_base_url="http://localhost:11434",
                ollama_model="llama3.1",
                timeout_seconds=30,
                prompt_version="v1",
                singleton_key="singleton",
            )
            db.add(row)
            try:
                await db.commit()
            except Exception as exc:  # noqa: BLE001
                return type(exc).__name__
            return None
        return None

    error_name = __import__("asyncio").run(_insert_second())
    assert error_name in {"IntegrityError", "UniqueViolationError"}

    async def _insert_different_key():
        async for db in app.dependency_overrides[get_db]():
            db.add(
                AIRuntimeConfig(
                    provider="disabled",
                    timeout_seconds=30,
                    prompt_version="v1",
                    singleton_key="another-row",
                )
            )
            try:
                await db.commit()
            except Exception as exc:  # noqa: BLE001
                return type(exc).__name__
            return None
        return None

    error_name = __import__("asyncio").run(_insert_different_key())
    assert error_name in {"IntegrityError", "UniqueViolationError"}


def test_admin_table_is_singleton_via_database_constraints(client):
    test_client, _ = client
    _setup_admin(test_client)

    async def _insert_second():
        async for db in app.dependency_overrides[get_db]():
            admin = Admin(
                username="intruder",
                password_hash="x" * 64,
                password_salt="0" * 32,
                password_algo="scrypt",
                singleton_key="singleton",
            )
            db.add(admin)
            try:
                await db.commit()
            except Exception as exc:  # noqa: BLE001
                return type(exc).__name__
            return None
        return None

    error_name = __import__("asyncio").run(_insert_second())
    assert error_name in {"IntegrityError", "UniqueViolationError"}

    async def _insert_different_key():
        async for db in app.dependency_overrides[get_db]():
            db.add(
                Admin(
                    username="another-intruder",
                    password_hash="x" * 64,
                    password_salt="0" * 32,
                    password_algo="scrypt",
                    singleton_key="another-row",
                )
            )
            try:
                await db.commit()
            except Exception as exc:  # noqa: BLE001
                return type(exc).__name__
            return None
        return None

    error_name = __import__("asyncio").run(_insert_different_key())
    assert error_name in {"IntegrityError", "UniqueViolationError"}


# --- read / update contract ---------------------------------------------


def test_get_settings_returns_default_when_table_is_empty(client):
    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.get("/api/settings/ai")
    assert response.status_code == 200
    config = response.json()["config"]
    assert config["provider"] == "disabled"
    assert config["has_api_key"] is False
    # The page must never see a key hint or any secret material.
    assert "api_key" not in config
    assert "openai_api_key" not in config
    assert "hint" not in config


def test_update_openai_config_encrypts_key_and_returns_only_flag(client):
    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
            "api_key_action": "replace",
            "api_key": "sk-test-very-long-secret-1234567890",
            "timeout_seconds": 45,
        },
    )
    assert response.status_code == 200
    config = response.json()["config"]
    assert config["provider"] == "openai"
    assert config["has_api_key"] is True
    assert "api_key" not in config
    assert "openai_api_key" not in config
    assert "hint" not in config
    assert config["timeout_seconds"] == 45
    # prompt_version is read-only compatibility metadata.
    assert config["prompt_version"] == "v1"

    # Confirm the database stores a ciphertext, not the plaintext.
    async def _fetch():
        async for db in app.dependency_overrides[get_db]():
            row = (await db.execute(select(AIRuntimeConfig))).scalars().first()
            return row
        return None

    row = __import__("asyncio").run(_fetch())
    assert row is not None
    assert row.openai_api_key_cipher != "sk-test-very-long-secret-1234567890"
    assert decrypt_secret(row.openai_api_key_cipher) == "sk-test-very-long-secret-1234567890"


def test_prompt_version_cannot_be_changed_by_client(client):
    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.patch(
        "/api/settings/ai",
        json={"provider": "disabled", "prompt_version": "v2"},
    )
    assert response.status_code == 422


def test_keep_action_does_not_overwrite_existing_key(client):
    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
            "api_key_action": "replace",
            "api_key": "sk-first-key-aaaaaaaaaaaaaaaa",
        },
    )
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
            "api_key_action": "keep",
        },
    )
    assert response.status_code == 200
    assert response.json()["config"]["has_api_key"] is True

    async def _fetch():
        async for db in app.dependency_overrides[get_db]():
            return (await db.execute(select(AIRuntimeConfig))).scalars().first()

    row = __import__("asyncio").run(_fetch())
    assert decrypt_secret(row.openai_api_key_cipher) == "sk-first-key-aaaaaaaaaaaaaaaa"


def test_clear_action_removes_key_and_flag(client):
    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
            "api_key_action": "replace",
            "api_key": "sk-second-key-bbbbbbbbbbbbbbbb",
        },
    )
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
            "api_key_action": "clear",
        },
    )
    assert response.status_code == 200
    config = response.json()["config"]
    assert config["has_api_key"] is False

    async def _fetch():
        async for db in app.dependency_overrides[get_db]():
            return (await db.execute(select(AIRuntimeConfig))).scalars().first()

    row = __import__("asyncio").run(_fetch())
    assert row.openai_api_key_cipher is None


def test_switching_away_from_openai_retains_key(client):
    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
            "api_key_action": "replace",
            "api_key": "sk-third-key-ccccccccccccccccc",
        },
    )
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "ollama",
            "ollama": {"base_url": "http://localhost:11434", "model": "llama3.1"},
            "api_key_action": "keep",
        },
    )
    assert response.status_code == 200
    config = response.json()["config"]
    assert config["provider"] == "ollama"
    assert config["has_api_key"] is True


# --- connection test ----------------------------------------------------


def test_connection_test_never_leaks_key_in_error_message(client, monkeypatch):
    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
            "api_key_action": "replace",
            "api_key": "sk-leaktest-zzzzzzzzzzzzzzzzzzz",
        },
    )

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            500,
            text=(
                "upstream failure sk-leaktest-zzzzzzzzzzzzzzzzzzz and "
                "Bearer sk-leaktest-zzzzzzzzzzzzzzzzzzz"
            ),
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )

    response = test_client.post("/api/settings/ai/test")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "sk-leaktest-zzzzzzzzzzzzzzzzzzz" not in body["message"]
    assert "HTTP 500" in body["message"]
    assert "upstream failure" not in body["message"]


class _MockAsyncClient:
    def __init__(self, transport):
        self._transport = transport

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


def test_connection_test_disabled_provider(client):
    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.post("/api/settings/ai/test")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "未启用" in body["message"]


def test_connection_test_rejects_html_200(client, monkeypatch):
    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://gateway.example.com", "model": "demo-model"},
            "api_key_action": "replace",
            "api_key": "sk-html-test-aaaaaaaaaaaaaaaa",
        },
    )

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>gateway home</html>")
    )
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )
    body = test_client.post("/api/settings/ai/test").json()
    assert body["ok"] is False
    assert "API 地址" in body["message"]


def test_connection_test_checks_configured_model(client, monkeypatch):
    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://gateway.example.com/v1", "model": "wanted"},
            "api_key_action": "replace",
            "api_key": "sk-model-test-aaaaaaaaaaaaaaa",
        },
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": "other"}]})
    )
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )
    body = test_client.post("/api/settings/ai/test").json()
    assert body["ok"] is False
    assert "模型" in body["message"]


# --- DB config overrides env -------------------------------------------


def test_db_openai_config_overrides_env_settings(client, monkeypatch):
    """When the database has a working config it takes precedence
    over the legacy ``.env`` settings.
    """

    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://db-config.example.com/v1", "model": "db-model"},
            "api_key_action": "replace",
            "api_key": "sk-db-config-key-aaaaaaaaaaaaa",
        },
    )
    # Set the env fallback to something completely different; the
    # database config should still win.
    _patch_env(
        monkeypatch,
        ai_provider="openai",
        openai_base_url="https://env-fallback.example.com/v1",
        openai_model="env-model",
        openai_api_key="sk-env-fallback-key-bbbbbbb",
    )

    from apps.api.ai import build_provider_from_db

    async def _run():
        async for db in app.dependency_overrides[get_db]():
            provider = await build_provider_from_db(db)
            return provider
        return None

    provider = __import__("asyncio").run(_run())
    assert provider is not None
    assert provider._base_url == "https://db-config.example.com/v1"
    assert provider._model == "db-model"
    assert provider._api_key == "sk-db-config-key-aaaaaaaaaaaaa"


def test_db_disabled_config_means_no_provider_even_if_env_set(client, monkeypatch):
    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={"provider": "disabled"},
    )
    _patch_env(
        monkeypatch,
        ai_provider="openai",
        openai_base_url="https://env-fallback.example.com/v1",
        openai_model="env-model",
        openai_api_key="sk-env-fallback-key-bbbbbbb",
    )

    from apps.api.ai import build_provider_from_db

    async def _run():
        async for db in app.dependency_overrides[get_db]():
            return await build_provider_from_db(db)
        return None

    provider = __import__("asyncio").run(_run())
    assert provider is None


def test_db_ollama_config_overrides_env_settings(client, monkeypatch):
    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "ollama",
            "ollama": {"base_url": "http://db-ollama:11434", "model": "db-llama"},
        },
    )
    _patch_env(
        monkeypatch,
        ai_provider="ollama",
        ollama_base_url="http://env-ollama:11434",
        ollama_model="env-llama",
    )

    from apps.api.ai import build_provider_from_db

    async def _run():
        async for db in app.dependency_overrides[get_db]():
            return await build_provider_from_db(db)
        return None

    provider = __import__("asyncio").run(_run())
    assert provider is not None
    assert provider._base_url == "http://db-ollama:11434"
    assert provider._model == "db-llama"


# --- 401 gate ---------------------------------------------------------


def test_settings_routes_require_authentication():
    """With ``require_admin`` not overridden, every settings route
    must reject anonymous traffic. The conftest auto-overrides
    the dependency; we pop the override to assert the real gate.
    """

    from fastapi.testclient import TestClient as TC

    # The conftest fixture auto-overrides ``require_admin``; we
    # re-implement a minimal test here to assert the gate works
    # end to end. The dependency override mechanism is the only
    # test bypass — there is no env-flag or middleware.
    app.dependency_overrides.pop(require_admin, None)
    try:
        with TC(app) as client:
            response = client.get("/api/settings/ai")
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "unauthenticated"
    finally:
        # Restore the override so the rest of the suite keeps
        # working.
        from types import SimpleNamespace

        app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
            id=1, username="tester", is_active=True
        )


# --- Model list endpoint ---------------------------------------------------


def test_models_endpoint_requires_authentication():
    """Anonymous requests must be rejected."""
    from fastapi.testclient import TestClient as TC

    app.dependency_overrides.pop(require_admin, None)
    try:
        with TC(app) as client:
            response = client.get("/api/settings/ai/models")
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "unauthenticated"
    finally:
        from types import SimpleNamespace

        app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
            id=1, username="tester", is_active=True
        )


def test_models_endpoint_rejects_disabled_provider(client):
    """Must return 400 when provider is disabled."""
    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.get("/api/settings/ai/models")
    assert response.status_code == 400
    assert "未启用" in response.json()["detail"]["message"]


def test_models_openai_requires_saved_api_key(client):
    """Must return 400 when API key is not saved."""
    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o"},
            "api_key_action": "clear",
        },
    )
    response = test_client.get("/api/settings/ai/models")
    assert response.status_code == 400
    assert "请先保存" in response.json()["detail"]["message"]


def test_openai_connection_can_be_saved_before_model_is_selected(client):
    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://api.example.com/v1", "model": ""},
            "api_key_action": "replace",
            "api_key": "sk-first-setup-without-model",
        },
    )
    assert response.status_code == 200
    assert response.json()["config"]["openai_model"] is None


def test_models_endpoint_handles_non_json_response(client, monkeypatch):
    """HTML responses must give clean error without leaking."""
    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://gateway.example.com", "model": "demo"},
            "api_key_action": "replace",
            "api_key": "sk-test-html",
        },
    )

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="<html><body>gateway</body></html>")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )

    response = test_client.get("/api/settings/ai/models")
    assert response.status_code == 502
    assert "不是模型列表" in response.json()["detail"]["message"]
    assert "sk-test-html" not in response.text


def test_models_endpoint_sorts_and_deduplicates(client, monkeypatch):
    """Response must be sorted naturally and deduplicated."""
    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://api.example.com/v1", "model": "gpt-4o"},
            "api_key_action": "replace",
            "api_key": "sk-test-models",
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-4o-mini"},
                    {"id": "gpt-4o"},
                    {"id": "gpt-3.5-turbo"},
                    {"id": "gpt-4o"},  # duplicate
                    {"id": "claude-3-opus"},
                    {"id": "model-10"},
                    {"id": "model-2"},
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )

    response = test_client.get("/api/settings/ai/models")
    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "openai"
    assert data["current_model"] == "gpt-4o"
    assert len(data["models"]) == 6
    ids = [m["id"] for m in data["models"]]
    # Natural sorted
    assert ids == [
        "claude-3-opus",
        "gpt-3.5-turbo",
        "gpt-4o",
        "gpt-4o-mini",
        "model-2",
        "model-10",
    ]


def test_models_endpoint_sanitizes_upstream_http_error(client, monkeypatch):
    test_client, _ = client
    _setup_admin(test_client)
    secret = "sk-upstream-secret-never-return"
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://api.example.com/v1", "model": "gpt-4o"},
            "api_key_action": "replace",
            "api_key": secret,
        },
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(401, text=f"invalid bearer {secret}")
    )
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )

    response = test_client.get("/api/settings/ai/models")
    assert response.status_code == 502
    assert "HTTP 401" in response.json()["detail"]["message"]
    assert secret not in response.text


def test_models_ollama_parses_tags(client, monkeypatch):
    """Ollama /api/tags response must be parsed correctly."""
    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "ollama",
            "ollama": {"base_url": "http://localhost:11434", "model": "llama3.1"},
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "llama3.1:latest"},
                    {"model": "mistral:7b-instruct"},
                    {"name": "llama3.1:latest"},  # duplicate
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )

    response = test_client.get("/api/settings/ai/models")
    assert response.status_code == 200
    data = response.json()
    assert len(data["models"]) == 2
    ids = [m["id"] for m in data["models"]]
    assert sorted(ids) == sorted(["llama3.1:latest", "mistral:7b-instruct"])


def test_models_caps_at_500_entries(client, monkeypatch):
    """Response must be capped at 500 models even if upstream returns more."""
    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://api.example.com/v1", "model": "test"},
            "api_key_action": "replace",
            "api_key": "sk-test-many",
        },
    )

    models_data = [{"id": f"model-{i:03d}"} for i in range(600)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": models_data})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )

    response = test_client.get("/api/settings/ai/models")
    assert response.status_code == 200
    data = response.json()
    assert len(data["models"]) == 500


# --- Embedding model field -------------------------------------------------


def test_embedding_model_is_default_null_in_public_dict(client):
    """The embedding_model field defaults to null and is exposed
    on the public response so the page can render an empty input.
    """

    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.get("/api/settings/ai")
    assert response.status_code == 200
    config = response.json()["config"]
    assert "embedding_model" in config
    assert config["embedding_model"] is None


def test_update_can_set_and_clear_embedding_model(client):
    """The PATCH endpoint persists the new field; an explicit empty
    string clears it so the feature can be turned off.
    """

    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {
                "base_url": "https://api.example.com/v1",
                "model": "gpt-4o-mini",
            },
            "api_key_action": "replace",
            "api_key": "sk-embed-persist-aaaaaaaaaaaaaa",
            "embedding_model": "text-embedding-3-small",
        },
    )
    assert response.status_code == 200
    assert response.json()["config"]["embedding_model"] == "text-embedding-3-small"

    # Persisted on disk
    async def _fetch():
        async for db in app.dependency_overrides[get_db]():
            return (await db.execute(select(AIRuntimeConfig))).scalars().first()

    row = __import__("asyncio").run(_fetch())
    assert row.embedding_model == "text-embedding-3-small"

    # Older clients that omit the new field must not clear it.
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {
                "base_url": "https://api.example.com/v1",
                "model": "gpt-4o-mini",
            },
            "api_key_action": "keep",
        },
    )
    assert response.json()["config"]["embedding_model"] == "text-embedding-3-small"

    # Empty string clears the field — embedding is off again.
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {
                "base_url": "https://api.example.com/v1",
                "model": "gpt-4o-mini",
            },
            "api_key_action": "keep",
            "embedding_model": "",
        },
    )
    assert response.status_code == 200
    assert response.json()["config"]["embedding_model"] is None


def test_update_rejects_overly_long_embedding_model(client):
    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {"base_url": "https://api.example.com/v1", "model": "x"},
            "api_key_action": "keep",
            "embedding_model": "a" * 300,
        },
    )
    assert response.status_code == 422


# --- Independent embedding channel (ADR-015 phase 1) ----------------------


def test_embedding_channel_fields_default_to_disabled_in_public_dict(client):
    """The independent embedding channel defaults to ``disabled``
    with no API key, no base URL, and a 30-second timeout. The
    public response must always expose these fields so the front-end
    can render the form without additional probes.
    """

    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.get("/api/settings/ai")
    assert response.status_code == 200
    config = response.json()["config"]
    assert config["embedding_provider"] == "disabled"
    assert config["embedding_base_url"] is None
    assert config["has_embedding_api_key"] is False
    assert config["embedding_timeout_seconds"] == 30


def test_update_can_set_and_clear_embedding_channel_openai(client):
    """The PATCH endpoint accepts an independent ``embedding_*``
    group. Setting ``embedding_provider`` to ``openai`` requires a
    matching base URL, and the response mirrors every field back
    so the front-end can re-render without an extra GET.
    """

    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
            },
            "api_key_action": "replace",
            "api_key": "sk-chat-aaaaaaaaaaaaaaaaaaaaa",
            "embedding_model": "text-embedding-3-small",
            "embedding_provider": "openai",
            "embedding_base_url": "https://embed.example.com/v1",
            "embedding_api_key_action": "replace",
            "embedding_api_key": "sk-embed-aaaaaaaaaaaaaaaaaaaa",
            "embedding_timeout_seconds": 45,
        },
    )
    assert response.status_code == 200, response.text
    config = response.json()["config"]
    assert config["embedding_provider"] == "openai"
    assert config["embedding_base_url"] == "https://embed.example.com/v1"
    assert config["has_embedding_api_key"] is True
    assert config["embedding_timeout_seconds"] == 45

    # Persisted on disk and uses the embedding-side cipher, not the
    # chat-side one.
    async def _fetch():
        async for db in app.dependency_overrides[get_db]():
            return (await db.execute(select(AIRuntimeConfig))).scalars().first()

    row = __import__("asyncio").run(_fetch())
    assert row.embedding_provider == "openai"
    assert row.embedding_base_url == "https://embed.example.com/v1"
    assert row.has_embedding_api_key is True
    assert row.embedding_timeout_seconds == 45
    # The chat-side cipher is unrelated to the embedding-side cipher
    # even when both happen to be OpenAI-shaped.
    assert row.embedding_api_key_cipher != row.openai_api_key_cipher
    assert decrypt_secret(row.embedding_api_key_cipher) == (
        "sk-embed-aaaaaaaaaaaaaaaaaaaa"
    )

    # Switching the provider to ``ollama`` requires a fresh base URL
    # but re-uses the same model name and keeps the timeout.
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
            },
            "api_key_action": "keep",
            "embedding_provider": "ollama",
            "embedding_base_url": "http://localhost:11434",
        },
    )
    assert response.status_code == 200
    config = response.json()["config"]
    assert config["embedding_provider"] == "ollama"
    assert config["embedding_base_url"] == "http://localhost:11434"
    assert config["has_embedding_api_key"] is True  # cipher is kept for UX
    assert config["embedding_timeout_seconds"] == 45

    # Disabling the channel turns the provider flag off and leaves
    # the rest of the row alone.
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
            },
            "api_key_action": "keep",
            "embedding_provider": "disabled",
            "embedding_api_key_action": "clear",
        },
    )
    assert response.status_code == 200
    config = response.json()["config"]
    assert config["embedding_provider"] == "disabled"
    assert config["has_embedding_api_key"] is False


def test_update_rejects_embedding_provider_without_base_url(client):
    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "disabled",
            "embedding_provider": "openai",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "embedding_openai_missing"

    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "disabled",
            "embedding_provider": "ollama",
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "embedding_ollama_missing"


def test_update_rejects_invalid_embedding_provider(client):
    test_client, _ = client
    _setup_admin(test_client)
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "disabled",
            "embedding_provider": "unknown",
            "embedding_base_url": "https://embed.example.com/v1",
        },
    )
    assert response.status_code == 422


def test_update_keeps_embedding_channel_when_not_specified(client):
    """Older clients that omit the new fields must not wipe the
    embedding channel. A PATCH without ``embedding_provider`` must
    leave every ``embedding_*`` column at its previous value.
    """

    test_client, _ = client
    _setup_admin(test_client)
    test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
            },
            "api_key_action": "replace",
            "api_key": "sk-chat-keep-zzzzzzzzzzzzzzzz",
            "embedding_provider": "openai",
            "embedding_base_url": "https://embed.example.com/v1",
            "embedding_api_key_action": "replace",
            "embedding_api_key": "sk-embed-keep-zzzzzzzzzzzzzz",
            "embedding_timeout_seconds": 90,
        },
    )
    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
            },
            "api_key_action": "keep",
        },
    )
    config = response.json()["config"]
    assert config["embedding_provider"] == "openai"
    assert config["embedding_base_url"] == "https://embed.example.com/v1"
    assert config["has_embedding_api_key"] is True
    assert config["embedding_timeout_seconds"] == 90


# --- Embedding connection test --------------------------------------------


def _configure_embedding_channel(
    test_client,
    *,
    embedding_provider: str,
    embedding_base_url: str,
    embedding_model: str,
    embedding_api_key: str | None = None,
    embedding_timeout_seconds: int = 30,
) -> None:
    """Persist the independent embedding channel used by the probe.

    The chat-side fields are kept as a placeholder so the PATCH
    payload remains valid. The embedding test endpoint only reads
    the ``embedding_*`` columns, so the chat-side value never
    affects the probe.
    """

    payload: dict = {
        "provider": "openai",
        "openai": {
            "base_url": "https://placeholder.example.com/v1",
            "model": "placeholder-model",
        },
        "api_key_action": "keep",
        "embedding_model": embedding_model,
        "embedding_provider": embedding_provider,
        "embedding_base_url": embedding_base_url,
        "embedding_timeout_seconds": embedding_timeout_seconds,
    }
    if embedding_provider == "openai":
        if embedding_api_key is not None:
            payload["embedding_api_key_action"] = "replace"
            payload["embedding_api_key"] = embedding_api_key
        else:
            payload["embedding_api_key_action"] = "keep"
    response = test_client.patch("/api/settings/ai", json=payload)
    assert response.status_code == 200, response.text


def test_embedding_test_requires_authentication():
    """Anonymous requests must be rejected, matching the rest of the
    settings routes.
    """

    from fastapi.testclient import TestClient as TC

    app.dependency_overrides.pop(require_admin, None)
    try:
        with TC(app) as client_:
            response = client_.post("/api/settings/ai/embedding/test")
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "unauthenticated"
    finally:
        from types import SimpleNamespace

        app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
            id=1, username="tester", is_active=True
        )


def test_embedding_test_rejects_when_model_not_set(client):
    """A missing embedding model must be reported as off, not as a
    successful probe — keeps the FTS path safe.
    """

    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        embedding_provider="openai",
        embedding_base_url="https://api.example.com/v1",
        embedding_model="",
        embedding_api_key="sk-embed-empty-zzzzzzzzzzzzzzz",
    )
    response = test_client.post("/api/settings/ai/embedding/test")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "Embedding" in body["message"]


def test_embedding_test_rejects_when_provider_disabled(client):
    test_client, _ = client
    _setup_admin(test_client)
    # Manually persist a config with an embedding model but a
    # disabled embedding channel — the test endpoint must refuse on
    # "embedding channel disabled" rather than probe the model list.
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
    body = test_client.post("/api/settings/ai/embedding/test").json()
    assert body["ok"] is False
    assert "未启用" in body["message"]


def test_embedding_test_openai_success_and_dimension_report(client, monkeypatch):
    """A valid OpenAI /v1/embeddings response yields ok=true with
    the reported dimensionality.
    """

    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        embedding_provider="openai",
        embedding_base_url="https://api.example.com/v1",
        embedding_model="text-embedding-3-small",
        embedding_api_key="sk-embed-ok-aaaaaaaaaaaaaaaa",
    )

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.1, 0.2, 0.3, 0.4]},
                ],
                "model": "text-embedding-3-small",
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )

    response = test_client.post("/api/settings/ai/embedding/test")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "4" in body["message"]

    # The probe must hit /embeddings, send a tiny input and include
    # the configured model name. Any secret must never appear in
    # the request.
    assert captured, "expected at least one HTTP request"
    request = captured[0]
    assert request.url.path.endswith("/embeddings")
    body_text = request.content.decode("utf-8")
    payload = json.loads(body_text)
    assert payload["model"] == "text-embedding-3-small"
    assert payload["input"]  # a short, non-empty string
    assert len(payload["input"]) < 32
    assert "sk-embed-ok-aaaaaaaaaaaaaaaa" not in body_text


def test_embedding_test_never_leaks_key_in_error_message(client, monkeypatch):
    """A 500 from the upstream that embeds the API key must not
    surface the key in the public response.
    """

    test_client, _ = client
    _setup_admin(test_client)
    secret = "sk-embed-leak-zzzzzzzzzzzzzzzzz"
    _configure_embedding_channel(
        test_client,
        embedding_provider="openai",
        embedding_base_url="https://api.example.com/v1",
        embedding_model="text-embedding-3-small",
        embedding_api_key=secret,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            text=f"upstream failure {secret} Bearer {secret}",
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )

    response = test_client.post("/api/settings/ai/embedding/test")
    body = response.json()
    assert body["ok"] is False
    assert secret not in body["message"]
    assert secret not in response.text
    assert "HTTP 500" in body["message"]


def test_embedding_test_rejects_empty_or_invalid_vectors(client, monkeypatch):
    """Vectors that are empty, missing, or contain NaN/Inf must be
    rejected. The error message must not leak the API key.
    """

    test_client, _ = client
    _setup_admin(test_client)
    secret = "sk-embed-vector-aaaaaaaaaaaaaaa"
    _configure_embedding_channel(
        test_client,
        embedding_provider="openai",
        embedding_base_url="https://api.example.com/v1",
        embedding_model="text-embedding-3-small",
        embedding_api_key=secret,
    )

    # ``NaN`` and ``Infinity`` are not JSON-compliant under
    # ``json.dumps``; we craft the raw response body ourselves so
    # the server still sees a 200 with the malformed payload. The
    # ``hint`` is a substring we expect to find in the user-facing
    # error so each case asserts a different failure surface.
    cases = [
        (b'{"data": []}', "缺少 data"),
        (b'{"data": [{}]}', "不是数组"),
        (b'{"data": [{"embedding": []}]}', "不是数组"),
        (b'{"data": [{"embedding": [1e9999, 0.1]}]}', "NaN"),
        (b'{"data": [{"embedding": [0.1, 1e9999]}]}', "NaN"),
        (b'{"data": [{"embedding": ["x", 0.1]}]}', "非数值"),
    ]
    for payload, hint in cases:
        transport = httpx.MockTransport(
            lambda request, body=payload: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=body,
            )
        )
        monkeypatch.setattr(
            "apps.api.api.settings_ai.httpx.AsyncClient",
            lambda *args, **kwargs: _MockAsyncClient(transport),
        )
        response = test_client.post("/api/settings/ai/embedding/test")
        body = response.json()
        assert body["ok"] is False, f"expected rejection for {hint}"
        assert hint in body["message"], (
            f"missing hint {hint!r} in {body['message']!r}"
        )
        assert secret not in body["message"]
        assert secret not in response.text


def test_embedding_test_rejects_non_json_response(client, monkeypatch):
    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        embedding_provider="openai",
        embedding_base_url="https://api.example.com/v1",
        embedding_model="text-embedding-3-small",
        embedding_api_key="sk-embed-html-aaaaaaaaaaaaaaaa",
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>homepage</html>")
    )
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )
    body = test_client.post("/api/settings/ai/embedding/test").json()
    assert body["ok"] is False
    assert "JSON" in body["message"]


def test_embedding_test_ollama_success(client, monkeypatch):
    """A valid Ollama /api/embed response yields ok=true."""

    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        embedding_provider="ollama",
        embedding_base_url="http://localhost:11434",
        embedding_model="nomic-embed-text",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/api/embed")
        return httpx.Response(
            200,
            json={"model": "nomic-embed-text", "embeddings": [[0.5, -0.5, 1.0]]},
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )

    body = test_client.post("/api/settings/ai/embedding/test").json()
    assert body["ok"] is True
    assert "3" in body["message"]


def test_embedding_test_ollama_falls_back_to_legacy_endpoint(client, monkeypatch):
    """If the modern /api/embed returns 404 the probe falls back to
    the legacy /api/embeddings endpoint and validates that one.
    """

    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        embedding_provider="ollama",
        embedding_base_url="http://localhost:11434",
        embedding_model="nomic-embed-text",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/embed"):
            return httpx.Response(404, text="not found")
        assert request.url.path.endswith("/api/embeddings")
        return httpx.Response(
            200,
            json={"embedding": [0.1, 0.2, 0.3, 0.4, 0.5]},
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )

    body = test_client.post("/api/settings/ai/embedding/test").json()
    assert body["ok"] is True
    assert "5" in body["message"]


def test_embedding_test_ollama_rejects_invalid_vector(client, monkeypatch):
    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        embedding_provider="ollama",
        embedding_base_url="http://localhost:11434",
        embedding_model="nomic-embed-text",
    )
    # 1e9999 overflows to ``inf`` when parsed as JSON, exercising
    # the NaN/Inf check in ``_validate_embedding_vector``.
    body_bytes = b'{"embeddings": [[1e9999, 0.1]]}'
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=body_bytes,
        )
    )
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )
    body = test_client.post("/api/settings/ai/embedding/test").json()
    assert body["ok"] is False
    assert "非法" in body["message"] or "NaN" in body["message"]


def test_embedding_test_openai_connection_failure_is_sanitized(client, monkeypatch):
    """A network failure must surface only the exception class, not
    a verbose traceback or any embedded credentials.
    """

    test_client, _ = client
    _setup_admin(test_client)
    _configure_embedding_channel(
        test_client,
        embedding_provider="openai",
        embedding_base_url="https://api.example.com/v1",
        embedding_model="text-embedding-3-small",
        embedding_api_key="sk-embed-netfail-aaaaaaaaaaaaa",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sk-embed-netfail-aaaaaaaaaaaaa unreachable")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "apps.api.api.settings_ai.httpx.AsyncClient",
        lambda *args, **kwargs: _MockAsyncClient(transport),
    )
    body = test_client.post("/api/settings/ai/embedding/test").json()
    assert body["ok"] is False
    assert "sk-embed-netfail-aaaaaaaaaaaaa" not in body["message"]
    assert "无法连接" in body["message"]
