"""Authentication shared by versioned REST and MCP adapters."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.auth import _resolve_session
from ..core.db import get_db
from ..models.auth import Admin, PersonalAccessToken
from ..models.exploration_grants import ExplorationGrant
from ..services.scope_keys import DocumentSelection
from .auth import extract_bearer_token, now_utc

PAT_PREFIX = "cz_pat_"
EXPLORATION_GRANT_PREFIX = "cz_eg_"
PAT_TOKEN_BYTES = 32
PAT_SCOPES = frozenset(
    {
        "knowledge:read",
        "knowledge:search",
        "knowledge:ask",
        "documents:write",
    }
)


@dataclass(frozen=True)
class APIIdentity:
    """Authenticated caller with unambiguous audit identifiers.

    ``bound_workspace_id`` is the workspace a PAT was created with; routes
    must reject any request that names a different workspace.
    """

    admin: Admin
    auth_method: Literal["cookie", "pat", "exploration"]
    scopes: frozenset[str]
    pat_id: int | None = None
    session_id: int | None = None
    bound_workspace_id: int | None = None
    exploration_grant_id: int | None = None
    exploration_boundary: DocumentSelection | None = None

    @property
    def admin_id(self) -> int:
        return self.admin.id

    def has_scope(self, scope: str) -> bool:
        return self.auth_method == "cookie" or scope in self.scopes


def generate_pat_token() -> str:
    return f"{PAT_PREFIX}{secrets.token_urlsafe(PAT_TOKEN_BYTES)}"


def generate_exploration_grant_token() -> str:
    return f"{EXPLORATION_GRANT_PREFIX}{secrets.token_urlsafe(PAT_TOKEN_BYTES)}"


def hash_pat_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def extract_token_prefix(token: str) -> str:
    """Return the safe display prefix, including the PAT namespace."""

    prefix = (
        EXPLORATION_GRANT_PREFIX
        if token.startswith(EXPLORATION_GRANT_PREFIX)
        else PAT_PREFIX
    )
    random_part = token.removeprefix(prefix)
    return f"{prefix}{random_part[:8]}"


async def resolve_identity(
    request: Request,
    db: AsyncSession,
) -> APIIdentity | None:
    """Resolve an explicit PAT first, otherwise fall back to a session cookie.

    If an Authorization header is present but invalid, it never silently falls
    back to a Cookie. This prevents a malformed automation credential from
    appearing to work merely because the browser also happens to be logged in.
    """

    cached = getattr(request.state, "cangzhi_api_identity", None)
    if isinstance(cached, APIIdentity):
        return cached

    bearer_token = extract_bearer_token(request.headers)
    if bearer_token and bearer_token.startswith(PAT_PREFIX):
        pat = (
            await db.execute(
                select(PersonalAccessToken).where(
                    PersonalAccessToken.token_hash
                    == hash_pat_token(bearer_token)
                )
            )
        ).scalar_one_or_none()
        if pat is None or not pat.is_valid:
            return None
        admin = await db.get(Admin, pat.admin_id)
        if admin is None or not admin.is_active:
            return None
        pat.last_used_at = now_utc()
        db.add(pat)
        # Read-only requests would otherwise never commit this audit timestamp.
        await db.commit()
        identity = APIIdentity(
            admin=admin,
            auth_method="pat",
            scopes=frozenset(pat.scopes or []),
            pat_id=pat.id,
            bound_workspace_id=pat.workspace_id,
        )
        request.state.cangzhi_api_identity = identity
        return identity

    if bearer_token and bearer_token.startswith(EXPLORATION_GRANT_PREFIX):
        grant = (
            await db.execute(
                select(ExplorationGrant).where(
                    ExplorationGrant.token_hash == hash_pat_token(bearer_token)
                )
            )
        ).scalar_one_or_none()
        if grant is None or not grant.is_valid:
            return None
        creator_pat = (
            await db.get(PersonalAccessToken, grant.created_by_pat_id)
            if grant.created_by_pat_id is not None
            else None
        )
        if creator_pat is None or not creator_pat.is_valid:
            return None
        admin = await db.get(Admin, grant.admin_id)
        if admin is None or not admin.is_active:
            return None
        boundary = DocumentSelection.coerce(
            grant.scope_keys or [], grant.document_ids or []
        )
        if boundary.is_empty:
            return None
        grant.last_used_at = now_utc()
        db.add(grant)
        await db.commit()
        identity = APIIdentity(
            admin=admin,
            auth_method="exploration",
            scopes=frozenset(grant.scopes or []),
            bound_workspace_id=grant.workspace_id,
            exploration_grant_id=grant.id,
            exploration_boundary=boundary,
        )
        request.state.cangzhi_api_identity = identity
        return identity

    if bearer_token:
        return None

    session, admin = await _resolve_session(db, request)
    if session is None or admin is None:
        return None
    identity = APIIdentity(
        admin=admin,
        auth_method="cookie",
        scopes=PAT_SCOPES,
        session_id=session.id,
    )
    request.state.cangzhi_api_identity = identity
    return identity


def require_api_identity(
    *required_scopes: str,
    allow_exploration: bool = False,
):
    """Build a FastAPI dependency requiring every named PAT scope."""

    unknown = set(required_scopes) - PAT_SCOPES
    if unknown:
        raise ValueError(f"Unknown API scopes: {', '.join(sorted(unknown))}")

    async def dependency(
        request: Request,
        db: AsyncSession = Depends(get_db),  # noqa: B008
    ) -> APIIdentity:
        identity = await resolve_identity(request, db)
        if identity is None:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "unauthenticated",
                    "message": "缺少有效身份凭证",
                },
            )
        if identity.auth_method == "exploration" and not allow_exploration:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "credential_not_allowed",
                    "message": "探索凭证仅可用于 MCP",
                },
            )
        missing = [
            scope for scope in required_scopes if not identity.has_scope(scope)
        ]
        if missing:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "insufficient_scope",
                    "message": "令牌缺少所需权限",
                    "required_scopes": missing,
                },
            )
        return identity

    return dependency
