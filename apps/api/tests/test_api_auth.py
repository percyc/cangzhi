"""Tests for personal access tokens used by external knowledge clients."""

from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from apps.api.core.db import Base
from apps.api.models.auth import Admin, AuthSession, PersonalAccessToken
from apps.api.security.api_auth import (
    PAT_PREFIX,
    extract_token_prefix,
    generate_pat_token,
    hash_pat_token,
    require_api_identity,
    resolve_identity,
)
from apps.api.security.auth import (
    SESSION_COOKIE_NAME,
    hash_session_token,
    new_session_token,
    now_utc,
)


@pytest.fixture
def auth_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    import asyncio

    asyncio.run(prepare())
    yield sessions
    asyncio.run(engine.dispose())


def _request(
    *,
    bearer: str | None = None,
    cookie: str | None = None,
    authorization: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if bearer is not None:
        authorization = f"Bearer {bearer}"
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if cookie is not None:
        headers.append(
            (
                b"cookie",
                f"{SESSION_COOKIE_NAME}={cookie}".encode(),
            )
        )
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/test",
            "headers": headers,
            "query_string": b"",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1),
            "scheme": "http",
        }
    )


async def _create_admin(session, *, active: bool = True) -> Admin:
    admin = Admin(
        username="owner",
        password_hash="hash",
        password_salt="salt",
        is_active=active,
    )
    session.add(admin)
    await session.flush()
    return admin


async def _create_pat(
    session,
    admin: Admin,
    *,
    scopes: list[str] | None = None,
    expired: bool = False,
    revoked: bool = False,
) -> tuple[str, PersonalAccessToken]:
    token = generate_pat_token()
    row = PersonalAccessToken(
        admin_id=admin.id,
        name="Agent",
        token_hash=hash_pat_token(token),
        token_prefix=extract_token_prefix(token),
        scopes=scopes or ["knowledge:read"],
        expires_at=now_utc() - timedelta(minutes=1) if expired else None,
        revoked_at=now_utc() if revoked else None,
    )
    session.add(row)
    await session.commit()
    return token, row


def test_pat_material_is_namespaced_hashed_and_safe_to_display():
    first = generate_pat_token()
    second = generate_pat_token()

    assert first.startswith(PAT_PREFIX)
    assert first != second
    assert len(hash_pat_token(first)) == 64
    assert hash_pat_token(first) != first
    assert extract_token_prefix(first) == first[:15]

    row = PersonalAccessToken(
        admin_id=1,
        name="CLI",
        token_hash=hash_pat_token(first),
        token_prefix=extract_token_prefix(first),
        scopes=["knowledge:read"],
    )
    assert "token_hash" not in row.to_public_dict()


@pytest.mark.asyncio
async def test_valid_pat_resolves_identity_and_updates_audit_time(auth_db):
    async with auth_db() as session:
        admin = await _create_admin(session)
        token, pat = await _create_pat(
            session,
            admin,
            scopes=["knowledge:read", "knowledge:search"],
        )

        identity = await resolve_identity(_request(bearer=token), session)
        assert identity is not None
        assert identity.auth_method == "pat"
        assert identity.admin_id == admin.id
        assert identity.pat_id == pat.id
        assert identity.scopes == frozenset(
            {"knowledge:read", "knowledge:search"}
        )

        await session.refresh(pat)
        assert pat.last_used_at is not None


@pytest.mark.asyncio
async def test_explicit_authorization_never_falls_back_to_cookie(auth_db):
    async with auth_db() as session:
        admin = await _create_admin(session)
        valid_pat, _ = await _create_pat(session, admin)
        session_token = new_session_token()
        auth_session = AuthSession(
            admin_id=admin.id,
            token_hash=hash_session_token(session_token),
            last_seen_at=now_utc(),
            expires_at=now_utc() + timedelta(hours=1),
        )
        session.add(auth_session)
        await session.commit()

        invalid = await resolve_identity(
            _request(bearer=f"{PAT_PREFIX}invalid", cookie=session_token),
            session,
        )
        assert invalid is None

        valid = await resolve_identity(
            _request(bearer=valid_pat, cookie=session_token),
            session,
        )
        assert valid is not None
        assert valid.auth_method == "pat"

        old_session_as_bearer = await resolve_identity(
            _request(bearer=session_token),
            session,
        )
        assert old_session_as_bearer is None


@pytest.mark.asyncio
async def test_cookie_identity_keeps_full_owner_access(auth_db):
    async with auth_db() as session:
        admin = await _create_admin(session)
        session_token = new_session_token()
        auth_session = AuthSession(
            admin_id=admin.id,
            token_hash=hash_session_token(session_token),
            last_seen_at=now_utc(),
            expires_at=now_utc() + timedelta(hours=1),
        )
        session.add(auth_session)
        await session.commit()

        identity = await resolve_identity(
            _request(cookie=session_token),
            session,
        )
        assert identity is not None
        assert identity.auth_method == "cookie"
        assert identity.session_id == auth_session.id
        assert identity.has_scope("knowledge:ask")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expired", "revoked", "active"),
    [(True, False, True), (False, True, True), (False, False, False)],
)
async def test_unusable_pat_is_rejected(
    auth_db,
    expired: bool,
    revoked: bool,
    active: bool,
):
    async with auth_db() as session:
        admin = await _create_admin(session, active=active)
        token, _ = await _create_pat(
            session,
            admin,
            expired=expired,
            revoked=revoked,
        )
        assert await resolve_identity(_request(bearer=token), session) is None


@pytest.mark.asyncio
async def test_required_scope_returns_stable_forbidden_error(auth_db):
    async with auth_db() as session:
        admin = await _create_admin(session)
        token, _ = await _create_pat(
            session,
            admin,
            scopes=["knowledge:read"],
        )
        dependency = require_api_identity("knowledge:ask")

        with pytest.raises(HTTPException) as error:
            await dependency(_request(bearer=token), session)
        assert error.value.status_code == 403
        assert error.value.detail["code"] == "insufficient_scope"
        assert error.value.detail["required_scopes"] == ["knowledge:ask"]


def test_unknown_scope_is_rejected_during_route_definition():
    with pytest.raises(ValueError, match="Unknown API scopes"):
        require_api_identity("knowledge:delete")
