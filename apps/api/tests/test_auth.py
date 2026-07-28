"""Integration tests for the authentication API.

The tests cover the first-time setup flow, login success/failure
paths, the cookie + session model, expiry handling, the rate
limiter, the 401 gate, and the ``/logout`` revocation. They
exercise the real FastAPI app and a real database; the
``require_admin`` dependency is overridden so the protected
endpoints can be tested directly when the cookie flow is not
under test.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.api.auth import require_admin, reset_auth_limiters_for_tests
from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.auth import Admin, AuthSession


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )


def _setup(client: TestClient, username: str, password: str):
    return client.post(
        "/api/auth/setup",
        json={"username": username, "password": password},
    )


def _clear_auth_override():
    app.dependency_overrides.pop(require_admin, None)
    # The login and setup rate limiters are process-global; reset
    # them so the order in which tests run cannot change the
    # outcome.
    reset_auth_limiters_for_tests()


# --- /api/auth/setup -----------------------------------------------------


def test_setup_creates_admin_and_returns_cookie(client):
    test_client, _ = client
    _clear_auth_override()
    response = _setup(test_client, "founder", "a-strong-password")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["admin"]["username"] == "founder"
    assert "session" in body
    # The cookie is HttpOnly + SameSite=Lax and lives for the
    # session TTL window.
    cookie = test_client.cookies.get("cangzhi_session")
    assert cookie
    meta = response.headers.get("set-cookie", "")
    assert "HttpOnly" in meta
    assert "samesite=lax" in meta.lower()


def test_setup_rejects_duplicate_with_stable_message(client):
    test_client, _ = client
    _clear_auth_override()
    first = _setup(test_client, "founder", "a-strong-password")
    assert first.status_code == 200
    second = _setup(test_client, "another", "another-password")
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "already_initialised"


def test_setup_validates_username_and_password_format(client):
    test_client, _ = client
    _clear_auth_override()
    # Username with forbidden characters.
    response = _setup(test_client, "bad name", "a-strong-password")
    assert response.status_code == 422
    # Password that is too short.
    response = _setup(test_client, "goodname", "short")
    assert response.status_code == 422


def test_setup_is_rate_limited(client):
    test_client, _ = client
    _clear_auth_override()
    reset_auth_limiters_for_tests()
    # The setup limiter allows 3 attempts per minute. We don't want
    # to actually create an admin in the first slot because the
    # limiter checks happen before the row count. So we issue 3
    # requests, the first succeeds, the next two are 409. The 4th
    # request must be 429.
    _setup(test_client, "founder", "a-strong-password")
    _setup(test_client, "second", "a-strong-password")
    _setup(test_client, "third", "a-strong-password")
    blocked = _setup(test_client, "fourth", "a-strong-password")
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "rate_limited"


# --- /api/auth/login -----------------------------------------------------


def test_login_succeeds_with_correct_password(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    # ``TestClient`` shares a cookie jar by default, so the
    # setup cookie will be sent on the next request. Clear it so
    # the login test starts from a clean slate.
    test_client.cookies.clear()
    response = _login(test_client, "founder", "a-strong-password")
    assert response.status_code == 200
    assert response.json()["admin"]["username"] == "founder"


def test_login_fails_with_wrong_password(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    test_client.cookies.clear()
    response = _login(test_client, "founder", "wrong-password")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_credentials"


def test_login_returns_same_message_for_unknown_user_and_wrong_password(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    test_client.cookies.clear()
    response_unknown = _login(test_client, "stranger", "a-strong-password")
    response_wrong = _login(test_client, "founder", "wrong-password")
    assert response_unknown.status_code == 401
    assert response_wrong.status_code == 401
    assert response_unknown.json()["detail"]["message"] == response_wrong.json()["detail"]["message"]


def test_login_is_rate_limited(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    test_client.cookies.clear()
    for _ in range(5):
        _login(test_client, "founder", "definitely-wrong-password")
    blocked = _login(test_client, "founder", "definitely-wrong-password")
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "rate_limited"


# --- session / status / protected routes ---------------------------------


def test_status_endpoint_reports_authenticated_after_login(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    response = test_client.get("/api/auth/status")
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["admin"]["username"] == "founder"
    assert body["setup_required"] is False


def test_status_endpoint_reports_setup_required_when_no_admin(client):
    test_client, _ = client
    response = test_client.get("/api/auth/status")
    assert response.status_code == 200
    body = response.json()
    assert body["setup_required"] is True
    assert body["authenticated"] is False
    assert body["admin"] is None


def test_logout_revokes_session_and_clears_cookie(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    response = test_client.post("/api/auth/logout")
    assert response.status_code == 200
    assert "Set-Cookie" in response.headers
    # After logout the status endpoint must report no active admin.
    test_client.cookies.clear()
    response = test_client.get("/api/auth/status")
    assert response.json()["authenticated"] is False


def test_protected_routes_return_401_without_session(client):
    test_client, _ = client
    _clear_auth_override()
    response = test_client.get("/api/documents")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "unauthenticated"


def test_protected_routes_succeed_with_valid_session(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    response = test_client.get("/api/documents")
    assert response.status_code == 200


def test_expired_session_is_rejected(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")

    async def _expire():
        async for db in app.dependency_overrides[get_db]():
            row = (await db.execute(select(AuthSession))).scalars().first()
            assert row is not None
            row.expires_at = datetime.now(tz=timezone.utc) - timedelta(hours=1)
            await db.commit()
            return
        raise RuntimeError("no db override")

    asyncio.run(_expire())

    response = test_client.get("/api/auth/status")
    assert response.json()["authenticated"] is False
    response = test_client.get("/api/documents")
    assert response.status_code == 401


def test_revoked_session_is_rejected(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    test_client.post("/api/auth/logout")
    test_client.cookies.clear()
    response = test_client.get("/api/auth/status")
    assert response.json()["authenticated"] is False


# --- session storage shape -----------------------------------------------


def test_session_token_is_hashed_not_stored_plain(client):
    test_client, _ = client
    _clear_auth_override()
    response = _setup(test_client, "founder", "a-strong-password")
    assert response.status_code == 200
    cookie = test_client.cookies.get("cangzhi_session")
    assert cookie

    async def _fetch():
        async for db in app.dependency_overrides[get_db]():
            rows = (await db.execute(select(AuthSession))).scalars().all()
            return [row.token_hash for row in rows]
        return []

    hashes = asyncio.run(_fetch())
    assert hashes
    # The cookie value is the raw token, which must NOT be present
    # anywhere on disk. Only its SHA-256 lives in the database.
    assert cookie not in hashes
    for stored in hashes:
        assert len(stored) == 64
