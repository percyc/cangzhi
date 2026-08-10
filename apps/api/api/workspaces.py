"""Workspace management API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.auth import Admin
from ..models.workspaces import Workspace, WorkspaceError
from ..services.workspaces import (
    archive_workspace as _archive_workspace,
)
from ..services.workspaces import (
    create_workspace as _create_workspace,
)
from ..services.workspaces import (
    get_current_workspace as _get_current_workspace,
)
from ..services.workspaces import (
    get_workspace as _get_workspace,
)
from ..services.workspaces import (
    list_workspaces as _list_workspaces,
)
from ..services.workspaces import restore_workspace as _restore_workspace
from ..services.workspaces import (
    update_workspace as _update_workspace,
)
from .auth import require_admin

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


async def _current_workspace_dependency(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Workspace:
    return await _get_current_workspace(request, db)


class WorkspaceCreate(BaseModel):
    slug: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=255)]
    description: str | None = Field(default=None, max_length=2000)


class WorkspaceUpdate(BaseModel):
    name: Annotated[str | None, Field(default=None, min_length=1, max_length=255)]
    description: str | None = Field(default=None, max_length=2000)
    settings: dict | None = None


@router.get("", response_model=list[dict])
async def list_workspaces(db: AsyncSession = Depends(get_db)):
    workspaces = await _list_workspaces(db)
    return [workspace.to_public_dict() for workspace in workspaces]


@router.get("/current", response_model=dict)
async def get_current_workspace(
    workspace: Workspace = Depends(_current_workspace_dependency),
):
    return workspace.to_public_dict()


@router.post("", response_model=dict, status_code=201)
async def create_workspace(
    data: WorkspaceCreate,
    admin: Admin = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        workspace = await _create_workspace(
            db,
            slug=data.slug,
            name=data.name,
            description=data.description,
            created_by_admin_id=getattr(admin, "id", None),
        )
    except WorkspaceError as exc:
        raise HTTPException(
            status_code=409 if exc.code == "slug_exists" else 400,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    return workspace.to_public_dict()


@router.get("/{slug}", response_model=dict)
async def get_workspace(slug: str, db: AsyncSession = Depends(get_db)):
    workspace = await _get_workspace(db, slug)
    if workspace is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "workspace_not_found", "message": "工作区不存在"},
        )
    return workspace.to_public_dict()


@router.patch("/{slug}", response_model=dict)
async def update_workspace(
    slug: str,
    data: WorkspaceUpdate,
    db: AsyncSession = Depends(get_db),
):
    workspace = await _get_workspace(db, slug)
    if workspace is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "workspace_not_found", "message": "工作区不存在"},
        )
    try:
        updated = await _update_workspace(
            db,
            workspace,
            name=data.name,
            description=data.description,
            settings=data.settings,
        )
    except WorkspaceError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    return updated.to_public_dict()


@router.post("/{slug}/archive", response_model=dict)
async def archive_workspace(slug: str, db: AsyncSession = Depends(get_db)):
    workspace = await _get_workspace(db, slug)
    if workspace is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "workspace_not_found", "message": "工作区不存在"},
        )
    try:
        archived = await _archive_workspace(db, workspace)
    except WorkspaceError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    return archived.to_public_dict()


@router.post("/{slug}/restore", response_model=dict)
async def restore_workspace(slug: str, db: AsyncSession = Depends(get_db)):
    workspace = await _get_workspace(db, slug)
    if workspace is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "workspace_not_found", "message": "工作区不存在"},
        )
    restored = await _restore_workspace(db, workspace)
    return restored.to_public_dict()
