"""Cookie-authenticated management for personal API access tokens."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.auth import require_admin
from ..core.db import get_db
from ..models.auth import Admin, PersonalAccessToken
from ..models.workspaces import Workspace
from ..security.api_auth import (
    PAT_SCOPES,
    extract_token_prefix,
    generate_pat_token,
    hash_pat_token,
)
from ..security.auth import now_utc

router = APIRouter(prefix="/access-tokens", tags=["access-tokens"])
MAX_ACTIVE_TOKENS = 20


class CreateAccessTokenPayload(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(default_factory=lambda: sorted(PAT_SCOPES))
    expires_at: datetime | None = None
    workspace_id: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: list[str]) -> list[str]:
        normalized = sorted(set(value))
        unknown = set(normalized) - PAT_SCOPES
        if unknown:
            raise ValueError(f"不支持的权限：{', '.join(sorted(unknown))}")
        if not normalized:
            raise ValueError("至少选择一个权限")
        return normalized

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if value <= now_utc():
            raise ValueError("过期时间必须晚于当前时间")
        return value


@router.get("", response_model=dict[str, Any])
async def list_access_tokens(
    admin: Admin = Depends(require_admin),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    rows = (
        (
            await db.execute(
                select(PersonalAccessToken)
                .where(PersonalAccessToken.admin_id == admin.id)
                .order_by(PersonalAccessToken.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [row.to_public_dict() for row in rows],
        "available_scopes": sorted(PAT_SCOPES),
    }


@router.post("", response_model=dict[str, Any], status_code=201)
async def create_access_token(
    payload: CreateAccessTokenPayload,
    admin: Admin = Depends(require_admin),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    if payload.workspace_id is not None:
        workspace = await db.get(Workspace, payload.workspace_id)
        if workspace is None or workspace.status != "active":
            raise HTTPException(status_code=400, detail="绑定的工作空间不存在或不可用")
    active_count = (
        await db.execute(
            select(func.count(PersonalAccessToken.id)).where(
                PersonalAccessToken.admin_id == admin.id,
                PersonalAccessToken.revoked_at.is_(None),
            )
        )
    ).scalar_one()
    if active_count >= MAX_ACTIVE_TOKENS:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "token_limit_reached",
                "message": f"最多保留 {MAX_ACTIVE_TOKENS} 个有效令牌",
            },
        )

    plaintext = generate_pat_token()
    row = PersonalAccessToken(
        admin_id=admin.id,
        name=payload.name,
        token_hash=hash_pat_token(plaintext),
        token_prefix=extract_token_prefix(plaintext),
        scopes=payload.scopes,
        expires_at=payload.expires_at,
        workspace_id=payload.workspace_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {
        "token": plaintext,
        "item": row.to_public_dict(),
        "notice": "令牌只显示这一次，请立即保存。",
        "integration": {
            # The browser supplies its public origin. A reverse proxy may
            # otherwise make request.base_url point at an internal hostname.
            "api_path": "/api/v1",
            "mcp_path": "/api/mcp",
            "authorization_header": f"Bearer {plaintext}",
        },
    }


@router.post("/{token_id}/revoke", response_model=dict[str, Any])
async def revoke_access_token(
    token_id: int,
    admin: Admin = Depends(require_admin),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    row = (
        await db.execute(
            select(PersonalAccessToken).where(
                PersonalAccessToken.id == token_id,
                PersonalAccessToken.admin_id == admin.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "token_not_found", "message": "访问令牌不存在"},
        )
    if row.revoked_at is None:
        row.revoked_at = now_utc()
        await db.commit()
        await db.refresh(row)
    return {"item": row.to_public_dict()}


@router.delete("/{token_id}", status_code=204)
async def delete_access_token(
    token_id: int,
    admin: Admin = Depends(require_admin),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Response:
    """Permanently remove a revoked token record.

    Active credentials must be revoked first so an accidental delete can never
    be mistaken for a harmless list cleanup.
    """

    row = (
        await db.execute(
            select(PersonalAccessToken).where(
                PersonalAccessToken.id == token_id,
                PersonalAccessToken.admin_id == admin.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "token_not_found", "message": "访问令牌不存在"},
        )
    if row.revoked_at is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "token_must_be_revoked",
                "message": "请先撤销令牌，再永久删除记录",
            },
        )
    await db.delete(row)
    await db.commit()
    return Response(status_code=204)
