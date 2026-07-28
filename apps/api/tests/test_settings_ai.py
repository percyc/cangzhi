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
