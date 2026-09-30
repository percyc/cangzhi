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
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.api.auth import require_admin, reset_auth_limiters_for_tests
from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.auth import AuthSession
from apps.api.security.auth import hash_session_token


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
    assert 43198 <= int(response.headers["set-cookie"].split("Max-Age=")[1].split(";")[0]) <= 43200
    assert "set-cookie" not in test_client.get("/api/auth/status").headers


def test_remembered_login_renews_until_absolute_deadline(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    test_client.cookies.clear()
    login_response = test_client.post(
        "/api/auth/login",
        json={"username": "founder", "password": "a-strong-password", "remember": True},
    )
    assert login_response.status_code == 200
    assert 604798 <= int(login_response.headers["set-cookie"].split("Max-Age=")[1].split(";")[0]) <= 604800
    token = test_client.cookies.get("cangzhi_session")

    async def _move_near_deadline():
        async for db in app.dependency_overrides[get_db]():
            row = (await db.execute(select(AuthSession).where(AuthSession.token_hash == hash_session_token(token)))).scalar_one()
            assert row.remembered is True
            row.created_at = datetime.now(tz=timezone.utc) - timedelta(days=29)
            row.expires_at = datetime.now(tz=timezone.utc) + timedelta(hours=1)
            await db.commit()
            return

    asyncio.run(_move_near_deadline())
    status_response = test_client.get("/api/auth/status")
    assert status_response.json()["authenticated"] is True
    seconds_left = (datetime.fromisoformat(status_response.json()["session"]["expires_at"]) - datetime.now(tz=timezone.utc)).total_seconds()
    assert 86390 <= seconds_left <= 86410
    assert 86388 <= int(status_response.headers["set-cookie"].split("Max-Age=")[1].split(";")[0]) <= 86410
    assert test_client.post("/api/auth/logout").status_code == 200
    assert test_client.get("/api/auth/status").json()["authenticated"] is False


def test_remembered_session_expires_after_idle_deadline(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    test_client.cookies.clear()
    response = test_client.post(
        "/api/auth/login",
        json={"username": "founder", "password": "a-strong-password", "remember": True},
    )
    assert response.status_code == 200
    token = test_client.cookies.get("cangzhi_session")

    async def _expire():
        async for db in app.dependency_overrides[get_db]():
            row = (await db.execute(select(AuthSession).where(AuthSession.token_hash == hash_session_token(token)))).scalar_one()
            row.expires_at = datetime.now(tz=timezone.utc) - timedelta(seconds=1)
            await db.commit()
            return

    asyncio.run(_expire())
    assert test_client.get("/api/auth/status").json()["authenticated"] is False


def test_remembered_session_cannot_pass_absolute_deadline(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    test_client.cookies.clear()
    response = test_client.post(
        "/api/auth/login",
        json={"username": "founder", "password": "a-strong-password", "remember": True},
    )
    assert response.status_code == 200
    token = test_client.cookies.get("cangzhi_session")

    async def _past_hard_limit():
        async for db in app.dependency_overrides[get_db]():
            row = (await db.execute(select(AuthSession).where(AuthSession.token_hash == hash_session_token(token)))).scalar_one()
            row.created_at = datetime.now(tz=timezone.utc) - timedelta(days=31)
            row.expires_at = datetime.now(tz=timezone.utc) + timedelta(hours=1)
            await db.commit()
            return

    asyncio.run(_past_hard_limit())
    assert test_client.get("/api/auth/status").json()["authenticated"] is False


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


def test_account_requires_login_and_rename_requires_current_password(client):
    test_client, _ = client
    _clear_auth_override()
    assert test_client.get("/api/auth/account").status_code == 401
    _setup(test_client, "founder", "a-strong-password")
    profile = test_client.get("/api/auth/account").json()["admin"]
    assert profile["username"] == "founder"
    assert "password_hash" not in profile
    bad = test_client.patch(
        "/api/auth/account",
        json={"username": "renamed", "current_password": "wrong-password"},
    )
    assert bad.status_code == 401
    assert bad.json()["detail"]["code"] == "invalid_password"
    invalid = test_client.patch(
        "/api/auth/account",
        json={"username": "bad name", "current_password": "a-strong-password"},
    )
    assert invalid.status_code == 422
    changed = test_client.patch(
        "/api/auth/account",
        json={"username": "Renamed", "current_password": "a-strong-password"},
    )
    assert changed.status_code == 200
    assert changed.json()["admin"]["username"] == "renamed"
    test_client.cookies.clear()
    assert _login(test_client, "founder", "a-strong-password").status_code == 401
    assert _login(test_client, "renamed", "a-strong-password").status_code == 200


def test_password_change_rotates_current_cookie_and_revokes_other_sessions(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    old_device_token = test_client.cookies.get("cangzhi_session")
    test_client.cookies.clear()
    assert _login(test_client, "founder", "a-strong-password").status_code == 200
    current_token = test_client.cookies.get("cangzhi_session")
    sessions = test_client.get("/api/auth/sessions").json()["items"]
    assert len(sessions) == 2
    assert sum(item["current"] for item in sessions) == 1
    assert all("token_hash" not in item for item in sessions)

    bad = test_client.post(
        "/api/auth/change-password",
        json={"current_password": "wrong-password", "new_password": "new-strong-password"},
    )
    assert bad.status_code == 401
    assert test_client.cookies.get("cangzhi_session") == current_token
    same = test_client.post(
        "/api/auth/change-password",
        json={"current_password": "a-strong-password", "new_password": "a-strong-password"},
    )
    assert same.status_code == 400

    changed = test_client.post(
        "/api/auth/change-password",
        json={"current_password": "a-strong-password", "new_password": "new-strong-password"},
    )
    assert changed.status_code == 200
    assert changed.json()["other_sessions_revoked"] == 1
    new_token = test_client.cookies.get("cangzhi_session")
    assert new_token != current_token
    assert test_client.get("/api/auth/status").json()["authenticated"] is True
    test_client.cookies.clear()
    for token in (old_device_token, current_token):
        response = test_client.get("/api/auth/account", headers={"authorization": f"Bearer {token}"})
        assert response.status_code == 401
    assert _login(test_client, "founder", "a-strong-password").status_code == 401
    assert _login(test_client, "founder", "new-strong-password").status_code == 200


def test_password_change_preserves_remember_choice_and_rotates_deadline(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    test_client.cookies.clear()
    login_response = test_client.post(
        "/api/auth/login",
        json={"username": "founder", "password": "a-strong-password", "remember": True},
    )
    assert login_response.status_code == 200
    old_token = test_client.cookies.get("cangzhi_session")
    changed = test_client.post(
        "/api/auth/change-password",
        json={"current_password": "a-strong-password", "new_password": "new-strong-password"},
    )
    assert changed.status_code == 200
    assert test_client.cookies.get("cangzhi_session") != old_token
    assert 604798 <= int(changed.headers["set-cookie"].split("Max-Age=")[1].split(";")[0]) <= 604800
    assert test_client.get("/api/auth/status").json()["authenticated"] is True


def test_revoke_other_sessions_keeps_current_session(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    first_token = test_client.cookies.get("cangzhi_session")
    test_client.cookies.clear()
    _login(test_client, "founder", "a-strong-password")
    result = test_client.post("/api/auth/sessions/revoke-others")
    assert result.status_code == 200
    assert result.json()["revoked"] == 1
    assert len(test_client.get("/api/auth/sessions").json()["items"]) == 1
    test_client.cookies.clear()
    assert test_client.get("/api/auth/account", headers={"authorization": f"Bearer {first_token}"}).status_code == 401


def test_account_password_verification_is_rate_limited(client):
    test_client, _ = client
    _clear_auth_override()
    _setup(test_client, "founder", "a-strong-password")
    for _ in range(5):
        response = test_client.patch(
            "/api/auth/account",
            json={"username": "renamed", "current_password": "wrong-password"},
        )
        assert response.status_code == 401
    blocked = test_client.post(
        "/api/auth/change-password",
        json={"current_password": "a-strong-password", "new_password": "new-strong-password"},
    )
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "rate_limited"
