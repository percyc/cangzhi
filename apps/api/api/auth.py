"""Authentication API: setup, login, logout, status.

The flow is intentionally small for the single-user M3-3
milestone:

* ``GET /api/auth/setup-status`` — unauthenticated. Returns whether
  an admin account already exists. Used by the front end to decide
  between ``/setup`` and ``/login``.
* ``POST /api/auth/setup`` — once-only. Creates the first admin
  account. Returns 409 if any admin already exists; the database
  also rejects a second row via a UNIQUE constraint on
  ``singleton_key`` so the rule is enforced even when the
  application-level check is bypassed.
* ``POST /api/auth/login`` — password check, sets a session cookie,
  rate-limited per client IP.
* ``POST /api/auth/logout`` — revokes the current session, clears
  the cookie.
* ``GET /api/auth/status`` — returns whether the caller is logged
  in and (when relevant) the username.

All endpoints respond in plain Chinese with stable error codes
that the front end can map to a non-technical message. The login
endpoint deliberately does not reveal whether the username or the
password was the wrong one.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.auth import Admin, AuthSession
from ..security.auth import (
    MAX_SESSIONS_PER_ADMIN,
    SESSION_COOKIE_NAME,
    SESSION_TTL,
    RateLimiter,
    SessionRecord,
    client_ip_from_headers,
    cookie_attributes,
    expires_at_from_now,
    extract_bearer_token,
    hash_session_token,
    is_secure_request,
    new_session_token,
    now_utc,
    user_agent_from_headers,
)
from ..security.passwords import hash_password, verify_password


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")
_MIN_PASSWORD_LEN = 8
_MAX_PASSWORD_LEN = 256

# Sentinel written into every row to enforce the single-account
# invariant at the database level. Kept in sync with the model.
_SINGLETON_KEY = "singleton"

_login_limiter = RateLimiter(max_attempts=5, window_seconds=60.0)
_setup_limiter = RateLimiter(max_attempts=3, window_seconds=60.0)
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32)).serialized


# --- Schemas --------------------------------------------------------------


class _CredentialsPayload(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=_MIN_PASSWORD_LEN, max_length=_MAX_PASSWORD_LEN)

    @field_validator("username")
    @classmethod
    def _username_format(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not _USERNAME_RE.fullmatch(cleaned):
            raise ValueError(
                "用户名只能包含字母、数字、下划线、点和短横线，长度 3-32"
            )
        return cleaned.lower()


# --- Helpers --------------------------------------------------------------


def _client_ip(request: Request) -> str:
    return client_ip_from_headers(request.headers)


def _user_agent(request: Request) -> str:
    return user_agent_from_headers(request.headers)


def _is_secure(request: Request) -> bool:
    return is_secure_request(request.headers) or request.url.scheme == "https"


def _set_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        **cookie_attributes(secure=secure),
    )


def _clear_session_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        samesite="lax",
        secure=secure,
        httponly=True,
    )


async def _has_admin(db: AsyncSession) -> bool:
    count = await db.scalar(select(func.count(Admin.id)))
    return bool(count and count > 0)


async def _create_session(
    db: AsyncSession,
    *,
    admin: Admin,
    token: str,
    user_agent: str,
    client_ip: str,
) -> AuthSession:
    session = AuthSession(
        admin_id=admin.id,
        token_hash=hash_session_token(token),
        user_agent=user_agent[:512] or None,
        ip_address=client_ip[:64] or None,
        last_seen_at=now_utc(),
        expires_at=expires_at_from_now(),
    )
    db.add(session)
    await db.flush()
    # Enforce a soft cap on concurrent sessions per admin so an old
    # stolen cookie cannot be used forever; we keep the N most
    # recently seen sessions and revoke the rest.
    sessions = list(
        (
            await db.execute(
                select(AuthSession)
                .where(AuthSession.admin_id == admin.id)
                .where(AuthSession.revoked_at.is_(None))
                .order_by(AuthSession.last_seen_at.desc(), AuthSession.id.desc())
            )
        )
        .scalars()
        .all()
    )
    if len(sessions) > MAX_SESSIONS_PER_ADMIN:
        for stale in sessions[MAX_SESSIONS_PER_ADMIN:]:
            stale.revoked_at = now_utc()
            db.add(stale)
    return session


def _public_session(session: AuthSession, admin: Admin) -> dict[str, Any]:
    expires_at = session.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return {
        "id": session.id,
        "admin_id": admin.id,
        "username": admin.username,
        "expires_at": expires_at.astimezone(timezone.utc).isoformat()
        if expires_at
        else None,
    }


# --- Endpoints ------------------------------------------------------------


@router.get("/setup-status", response_model=dict[str, Any])
async def setup_status(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Whether the first admin account has been created."""

    has_admin = await _has_admin(db)
    return {
        "setup_required": not has_admin,
        "has_admin": has_admin,
    }


@router.post("/setup", response_model=dict[str, Any])
async def setup(
    payload: _CredentialsPayload,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create the first admin account.

    Only succeeds when no admin exists. The check and the insert run
    inside the same transaction so a concurrent ``setup`` call can
    never create a second account; the unique constraints on
    ``admins.singleton_key`` and ``admins.username`` are the
    database-level safety net.
    """

    client_ip_value = _client_ip(request)
    if not _setup_limiter.hit(client_ip_value):
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "message": "尝试过于频繁，请稍后再试"},
        )
    if await _has_admin(db):
        raise HTTPException(
            status_code=409,
            detail={"code": "already_initialised", "message": "已经设置过管理员账户，请使用登录入口"},
        )
    hashed = hash_password(payload.password)
    admin = Admin(
        username=payload.username,
        password_hash=hashed.serialized,
        password_salt=hashed.salt.hex(),
        password_algo=hashed.algo,
        singleton_key=_SINGLETON_KEY,
        is_active=True,
    )
    db.add(admin)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "already_initialised", "message": "已经设置过管理员账户，请使用登录入口"},
        ) from None

    token = new_session_token()
    session_row = await _create_session(
        db,
        admin=admin,
        token=token,
        user_agent=_user_agent(request),
        client_ip=client_ip_value,
    )
    admin.last_login_at = now_utc()
    db.add(admin)
    await db.commit()

    _set_session_cookie(response, token, secure=_is_secure(request))
    logger.info("admin_setup username=%s", admin.username)
    return {
        "status": "ok",
        "admin": {
            "id": admin.id,
            "username": admin.username,
        },
        "session": _public_session(session_row, admin),
    }


@router.post("/login", response_model=dict[str, Any])
async def login(
    payload: _CredentialsPayload,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Authenticate as the existing admin and set the session cookie."""

    client_ip_value = _client_ip(request)
    limiter_key = f"{client_ip_value}:{payload.username}"
    if not _login_limiter.hit(limiter_key):
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "message": "尝试过于频繁，请稍后再试"},
        )

    admin = (
        await db.execute(select(Admin).where(Admin.username == payload.username))
    ).scalars().first()
    # Deliberately do not leak whether the username exists. Both
    # "no such user" and "wrong password" return the same message
    # and the same status code.
    candidate_hash = admin.password_hash if admin is not None else _DUMMY_PASSWORD_HASH
    password_valid = verify_password(payload.password, candidate_hash)
    if admin is None or not admin.is_active or not password_valid:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_credentials", "message": "用户名或密码不正确"},
        )

    token = new_session_token()
    session_row = await _create_session(
        db,
        admin=admin,
        token=token,
        user_agent=_user_agent(request),
        client_ip=client_ip_value,
    )
    admin.last_login_at = now_utc()
    db.add(admin)
    await db.commit()

    _set_session_cookie(response, token, secure=_is_secure(request))
    logger.info("admin_login username=%s", admin.username)
    return {
        "status": "ok",
        "admin": {
            "id": admin.id,
            "username": admin.username,
        },
        "session": _public_session(session_row, admin),
    }


@router.post("/logout", response_model=dict[str, Any])
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Revoke the current session (if any) and clear the cookie."""

    token = _resolve_token(request)
    if token:
        token_hash = hash_session_token(token)
        row = (
            await db.execute(
                select(AuthSession).where(AuthSession.token_hash == token_hash)
            )
        ).scalars().first()
        if row is not None and row.revoked_at is None:
            row.revoked_at = now_utc()
            db.add(row)
            await db.commit()
    _clear_session_cookie(response, secure=_is_secure(request))
    return {"status": "ok"}


@router.get("/status", response_model=dict[str, Any])
async def status(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return whether the caller is logged in.

    ``admin`` is ``null`` when no valid session is presented. The
    response is intentionally public so the front end can use it to
    decide which page to render without needing a separate
    pre-flight request.
    """

    has_admin = await _has_admin(db)
    session_row, admin = await _resolve_session(db, request)
    if session_row is None or admin is None:
        return {
            "authenticated": False,
            "setup_required": not has_admin,
            "admin": None,
        }
    # Touch the session so the "last seen" timestamp stays fresh
    # and the cookie is refreshed by the same middleware.
    session_row.last_seen_at = now_utc()
    db.add(session_row)
    await db.commit()
    return {
        "authenticated": True,
        "setup_required": not has_admin,
        "admin": {
            "id": admin.id,
            "username": admin.username,
        },
        "session": {
            "id": session_row.id,
            "expires_at": _public_session(session_row, admin)["expires_at"],
        },
    }


# --- Session resolution ---------------------------------------------------


def _resolve_token(request: Request) -> str:
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME) or ""
    if cookie_token:
        return cookie_token.strip()
    header_token = extract_bearer_token(request.headers)
    return header_token.strip() if header_token else ""


async def _resolve_session(
    db: AsyncSession,
    request: Request,
) -> tuple[AuthSession | None, Admin | None]:
    """Look up the current session row and the associated admin.

    Returns ``(None, None)`` when no valid session is presented or
    the session has been revoked / expired. The function is
    intentionally forgiving so unauthenticated ``GET`` requests
    (e.g. ``/api/auth/status``) succeed.
    """

    token = _resolve_token(request)
    if not token:
        return None, None
    token_hash = hash_session_token(token)
    session_row = (
        await db.execute(
            select(AuthSession).where(AuthSession.token_hash == token_hash)
        )
    ).scalars().first()
    if session_row is None:
        return None, None
    if session_row.revoked_at is not None:
        return None, None
    expires_at = session_row.expires_at
    if expires_at is None:
        return None, None
    # SQLite drops the tzinfo on round-trip; normalise to UTC for
    # the comparison so both backends behave the same.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now_utc():
        return None, None
    admin = await db.get(Admin, session_row.admin_id)
    if admin is None or not admin.is_active:
        return None, None
    return session_row, admin


async def require_admin(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Admin:
    """FastAPI dependency that yields the active admin or 401s.

    The dependency does not touch the session row, so the auth
    status check can still be public. The route handlers that need
    a fresh ``last_seen_at`` timestamp can call
    :func:`_resolve_session` themselves.
    """

    session_row, admin = await _resolve_session(db, request)
    if admin is None or session_row is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthenticated", "message": "请先登录后再访问"},
        )
    return admin


def get_login_limiter() -> RateLimiter:
    return _login_limiter


def get_setup_limiter() -> RateLimiter:
    return _setup_limiter


def reset_auth_limiters_for_tests() -> None:
    _login_limiter.reset()
    _setup_limiter.reset()


def build_session_record(session_row: AuthSession, admin: Admin) -> SessionRecord:
    return SessionRecord(
        session_id=session_row.id,
        admin_id=admin.id,
        username=admin.username,
        expires_at=session_row.expires_at,
    )
