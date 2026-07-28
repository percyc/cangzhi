"""Tests that the Worker reads the AI runtime config from the
database on every job, not from the env-var based settings.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

from apps.api.ai import build_provider_from_session
from apps.api.models.auth import Admin, AIRuntimeConfig
from apps.api.security.passwords import hash_password
from apps.api.security.secrets import encrypt_secret


@pytest.fixture
def session():
    # Use a sync in-memory SQLite for the worker-style code path
    # because the worker polls jobs from a thread, not an event
    # loop.
    engine = create_engine("sqlite:///:memory:")
    from apps.api.core.db import Base

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_admin(session, username="worker-tester"):
    hashed = hash_password("a-strong-password")
    admin = Admin(
        username=username,
        password_hash=hashed.serialized,
        password_salt=hashed.salt.hex(),
        password_algo=hashed.algo,
        singleton_key="singleton",
    )
    session.add(admin)
    session.flush()
    return admin


def test_worker_reads_openai_runtime_config(session):
    _seed_admin(session)
    row = AIRuntimeConfig(
        provider="openai",
        openai_base_url="https://worker-db.example.com/v1",
        openai_model="worker-model",
        openai_api_key_cipher=encrypt_secret("sk-worker-db-key-aaaaaaaaa"),
        has_api_key=True,
        timeout_seconds=20,
        prompt_version="v1",
        singleton_key="singleton",
        updated_by=None,
    )
    session.add(row)
    session.commit()

    provider = build_provider_from_session(session)
    assert provider is not None
    assert provider._base_url == "https://worker-db.example.com/v1"
    assert provider._model == "worker-model"
    assert provider._api_key == "sk-worker-db-key-aaaaaaaaa"


def test_worker_reads_ollama_runtime_config(session):
    _seed_admin(session)
    row = AIRuntimeConfig(
        provider="ollama",
        ollama_base_url="http://worker-ollama:11434",
        ollama_model="worker-llama",
        timeout_seconds=20,
        prompt_version="v1",
        singleton_key="singleton",
    )
    session.add(row)
    session.commit()

    provider = build_provider_from_session(session)
    assert provider is not None
    assert provider._base_url == "http://worker-ollama:11434"
    assert provider._model == "worker-llama"


def test_worker_disabled_config_returns_none(session):
    _seed_admin(session)
    row = AIRuntimeConfig(
        provider="disabled",
        timeout_seconds=20,
        prompt_version="v1",
        singleton_key="singleton",
    )
    session.add(row)
    session.commit()
    assert build_provider_from_session(session) is None


def test_worker_falls_back_to_env_when_table_empty(session, monkeypatch):
    # No rows in the table; legacy env settings still drive the
    # provider. The fallback path is exercised via build_provider
    # which is reached when ``build_provider_from_session`` finds
    # no row.
    monkeypatch.setattr("apps.api.core.config.settings.ai_provider", "openai")
    monkeypatch.setattr("apps.api.core.config.settings.openai_base_url", "https://env-fallback.example.com/v1")
    monkeypatch.setattr("apps.api.core.config.settings.openai_model", "env-model")
    monkeypatch.setattr("apps.api.core.config.settings.openai_api_key", "sk-env-fallback-key")
    monkeypatch.setattr("apps.api.core.config.settings.ai_request_timeout_seconds", 15.0)
    provider = build_provider_from_session(session)
    assert provider is not None
    assert provider._base_url == "https://env-fallback.example.com/v1"
    assert provider._api_key == "sk-env-fallback-key"


def test_worker_runtime_config_overrides_env(session, monkeypatch):
    """When the database has a working config it takes precedence
    over the ``.env`` fallback even for the worker thread.
    """

    _seed_admin(session)
    row = AIRuntimeConfig(
        provider="openai",
        openai_base_url="https://db-wins.example.com/v1",
        openai_model="db-wins-model",
        openai_api_key_cipher=encrypt_secret("sk-db-wins-key-aaaaaaaaa"),
        has_api_key=True,
        timeout_seconds=20,
        prompt_version="v1",
        singleton_key="singleton",
    )
    session.add(row)
    session.commit()
    monkeypatch.setattr("apps.api.core.config.settings.ai_provider", "openai")
    monkeypatch.setattr("apps.api.core.config.settings.openai_base_url", "https://env-loses.example.com/v1")
    monkeypatch.setattr("apps.api.core.config.settings.openai_model", "env-loses-model")
    monkeypatch.setattr("apps.api.core.config.settings.openai_api_key", "sk-env-loses-key")
    provider = build_provider_from_session(session)
    assert provider is not None
    assert provider._base_url == "https://db-wins.example.com/v1"
    assert provider._api_key == "sk-db-wins-key-aaaaaaaaa"
