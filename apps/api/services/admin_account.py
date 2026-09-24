"""Administrator profile and browser-session lifecycle operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.auth import Admin, AuthSession
from ..security.auth import expires_at_from_now, hash_session_token, new_session_token, now_utc
from ..security.passwords import hash_password


async def rename_admin(db: AsyncSession, admin: Admin, username: str) -> None:
    admin.username = username
    await db.commit()


async def active_sessions(db: AsyncSession, admin_id: int) -> list[AuthSession]:
    return list(
        (
            await db.execute(
                select(AuthSession)
                .where(AuthSession.admin_id == admin_id)
                .where(AuthSession.revoked_at.is_(None))
                .where(AuthSession.expires_at > now_utc())
                .order_by(AuthSession.last_seen_at.desc(), AuthSession.id.desc())
            )
        ).scalars().all()
    )


async def revoke_other_sessions(db: AsyncSession, admin_id: int, current_session_id: int) -> int:
    rows = (
        await db.execute(
            select(AuthSession)
            .where(AuthSession.admin_id == admin_id)
            .where(AuthSession.id != current_session_id)
            .where(AuthSession.revoked_at.is_(None))
        )
    ).scalars().all()
    for row in rows:
        row.revoked_at = now_utc()
    await db.commit()
    return len(rows)


async def change_admin_password(
    db: AsyncSession,
    admin: Admin,
    current_session: AuthSession,
    new_password: str,
) -> tuple[str, int]:
    """Rotate the current cookie and invalidate every other browser session."""

    hashed = hash_password(new_password)
    admin.password_hash = hashed.serialized
    admin.password_salt = hashed.salt.hex()
    admin.password_algo = hashed.algo
    rows = (
        await db.execute(
            select(AuthSession)
            .where(AuthSession.admin_id == admin.id)
            .where(AuthSession.revoked_at.is_(None))
        )
    ).scalars().all()
    revoked = 0
    for row in rows:
        if row.id != current_session.id:
            row.revoked_at = now_utc()
            revoked += 1
    token = new_session_token()
    current_session.token_hash = hash_session_token(token)
    current_session.last_seen_at = now_utc()
    current_session.expires_at = expires_at_from_now()
    await db.commit()
    return token, revoked
