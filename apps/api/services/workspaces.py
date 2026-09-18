"""Workspace management: resolution, CRUD, and archiving.

The services are deliberately thin and DB-focused; HTTP concerns
(header/cookie/query parsing, error mapping) live in the router.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Query, Request
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, with_loader_criteria

from ..core.db import get_db
from ..models.ask_history import AskConversation
from ..models.database_source import DatabaseSnapshot, DatabaseSource
from ..models.document_scope_keys import DocumentScopeKey
from ..models.documents import Document
from ..models.exploration_grants import ExplorationGrant
from ..models.enhancement import EnhancementRun
from ..models.knowledge_scopes import KnowledgeScope
from ..models.taxonomy import DEFAULT_CATEGORY_SLUGS, Category, Tag
from ..models.webdav import ExternalItemExclusion, WebDAVSource
from ..models.workspaces import (
    DEFAULT_WORKSPACE_SLUG,
    Workspace,
    WorkspaceError,
)

WORKSPACE_HEADER = "X-Cangzhi-Workspace"
WORKSPACE_COOKIE = "cangzhi_workspace"
WORKSPACE_QUERY = "workspace"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SCOPED_MODELS = (
    Document,
    Category,
    Tag,
    KnowledgeScope,
    WebDAVSource,
    ExternalItemExclusion,
    AskConversation,
    DatabaseSource,
    DatabaseSnapshot,
    DocumentScopeKey,
    ExplorationGrant,
    EnhancementRun,
)


@event.listens_for(Session, "do_orm_execute")
def _apply_workspace_filter(execute_state) -> None:
    """Apply the request workspace to every ORM read/update/delete."""

    workspace_id = execute_state.session.info.get("cangzhi_workspace_id")
    if workspace_id is None or execute_state.execution_options.get(
        "include_all_workspaces"
    ):
        return
    if not (
        execute_state.is_select
        or execute_state.is_update
        or execute_state.is_delete
    ):
        return
    statement = execute_state.statement
    for model in _SCOPED_MODELS:
        statement = statement.options(
            with_loader_criteria(
                model,
                lambda cls: cls.workspace_id == workspace_id,
                include_aliases=True,
            )
        )
    execute_state.statement = statement


@event.listens_for(Session, "before_flush")
def _assign_workspace_to_new_rows(session, _flush_context, _instances) -> None:
    workspace_id = session.info.get("cangzhi_workspace_id")
    if workspace_id is None:
        return
    for row in session.new:
        if isinstance(row, _SCOPED_MODELS) and row.workspace_id is None:
            row.workspace_id = workspace_id


def validate_slug(value: str) -> str:
    cleaned = (value or "").strip().lower()
    if not _SLUG_RE.fullmatch(cleaned):
        raise WorkspaceError(
            "invalid_slug",
            "slug 只能包含小写字母、数字和短横线，且必须以字母或数字开头",
        )
    return cleaned


def validate_name(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise WorkspaceError("invalid_name", "工作空间名称不能为空")
    return cleaned


async def get_workspace(db: AsyncSession, slug: str) -> Workspace | None:
    return (
        await db.execute(select(Workspace).where(Workspace.slug == slug))
    ).scalar_one_or_none()


async def list_workspaces(db: AsyncSession) -> list[Workspace]:
    return list(
        (
            await db.execute(
                select(Workspace).order_by(Workspace.is_default.desc(), Workspace.slug)
            )
        )
        .scalars()
        .all()
    )


def _category_has_workspace_id() -> bool:
    return hasattr(Category, "workspace_id")


async def create_workspace(
    db: AsyncSession,
    *,
    slug: str,
    name: str,
    description: str | None,
    created_by_admin_id: int | None,
) -> Workspace:
    slug = validate_slug(slug)
    if await get_workspace(db, slug) is not None:
        raise WorkspaceError("slug_exists", "工作区 slug 已存在")
    workspace = Workspace(
        slug=slug,
        name=validate_name(name),
        description=description,
        is_default=False,
        status="active",
        settings={},
        created_by_admin_id=created_by_admin_id,
    )
    db.add(workspace)
    await db.flush()
    # Seed the canonical category set for the new workspace. The
    # ``workspace_id`` column lands on ``Category`` in a later
    # migration; until then the column is absent so we skip seeding
    # to avoid colliding with the globally-unique default slugs.
    if _category_has_workspace_id():
        for cat_slug, cat_name in DEFAULT_CATEGORY_SLUGS:
            db.add(
                Category(
                    slug=cat_slug,
                    name=cat_name,
                    workspace_id=workspace.id,
                )
            )
    await db.commit()
    await db.refresh(workspace)
    return workspace


async def update_workspace(
    db: AsyncSession,
    workspace: Workspace,
    *,
    name: str | None,
    description: str | None,
    settings: dict | None,
) -> Workspace:
    if settings is not None:
        workspace = (await db.execute(select(Workspace).where(Workspace.id == workspace.id)
                     .with_for_update().execution_options(populate_existing=True))).scalar_one()
        reserved = "knowledge_enhancement"
        existing = (workspace.settings or {}).get(reserved)
        if reserved in settings and settings[reserved] != existing:
            raise WorkspaceError("dedicated_enhancement_settings", "请通过知识增强设置修改增强配置")
        settings = dict(settings)
        if existing is not None:
            settings[reserved] = existing
    if name is not None:
        workspace.name = validate_name(name)
    if description is not None:
        workspace.description = description
    if settings is not None:
        workspace.settings = settings
    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)
    return workspace


async def archive_workspace(db: AsyncSession, workspace: Workspace) -> Workspace:
    if workspace.is_default:
        raise WorkspaceError("cannot_archive_default", "默认工作区不能归档")
    workspace.status = "archived"
    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)
    return workspace


async def restore_workspace(db: AsyncSession, workspace: Workspace) -> Workspace:
    workspace.status = "active"
    db.add(workspace)
    await db.commit()
    await db.refresh(workspace)
    return workspace


def _resolve_requested_slug(request: Request) -> str:
    header = request.headers.get(WORKSPACE_HEADER)
    if header:
        return header.strip()
    query = request.query_params.get(WORKSPACE_QUERY)
    if query:
        return query.strip()
    cookie = request.cookies.get(WORKSPACE_COOKIE)
    if cookie:
        return cookie.strip()
    return DEFAULT_WORKSPACE_SLUG


async def get_current_workspace(
    request: Request,
    db: AsyncSession,
) -> Workspace:
    """Resolve the active workspace with header > query > cookie > default.

    An explicitly requested but unknown slug is a 404; an archived
    workspace that was explicitly requested is a 409.
    """
    slug = _resolve_requested_slug(request)
    workspace = await get_workspace(db, slug)
    if workspace is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "workspace_not_found", "message": "工作区不存在"},
        )
    if workspace.status == "archived":
        raise HTTPException(
            status_code=409,
            detail={"code": "workspace_archived", "message": "工作区已归档"},
        )
    return workspace


async def require_workspace_context(
    request: Request,
    db: AsyncSession = Depends(get_db),  # noqa: B008
    workspace_header: Annotated[
        str | None,
        Header(
            alias=WORKSPACE_HEADER,
            description="工作空间 slug；未提供时使用默认空间",
        ),
    ] = None,
    workspace_query: Annotated[
        str | None,
        Query(alias=WORKSPACE_QUERY, description="工作空间 slug（请求头优先）"),
    ] = None,
    workspace_cookie: Annotated[
        str | None,
        Cookie(alias=WORKSPACE_COOKIE),
    ] = None,
) -> Workspace:
    """Resolve the workspace and bind it to the request's shared DB session."""

    requested_slug = (
        (workspace_header or "").strip()
        or (workspace_query or "").strip()
        or (workspace_cookie or "").strip()
        or DEFAULT_WORKSPACE_SLUG
    )
    # A PAT may be pinned to one workspace. Resolve it here because router-level
    # workspace dependencies run before endpoint identity dependencies.
    from ..security.api_auth import resolve_identity

    identity = await resolve_identity(request, db)
    if identity is not None and identity.bound_workspace_id is not None:
        workspace = await db.get(Workspace, identity.bound_workspace_id)
        explicit_slug = (workspace_header or "").strip() or (
            workspace_query or ""
        ).strip()
        if workspace is not None and explicit_slug and explicit_slug != workspace.slug:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "workspace_token_mismatch",
                    "message": "该令牌已绑定其他工作空间",
                },
            )
    else:
        workspace = await get_workspace(db, requested_slug)
    if workspace is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "workspace_not_found", "message": "工作区不存在"},
        )
    if workspace.status == "archived":
        raise HTTPException(
            status_code=409,
            detail={"code": "workspace_archived", "message": "工作区已归档"},
        )
    bind_workspace_context(db.sync_session, workspace.id, workspace.slug)
    return workspace


def bind_workspace_context(
    session: Session,
    workspace_id: int,
    workspace_slug: str | None = None,
) -> None:
    """Bind one workspace to a synchronous ORM session.

    Request sessions use this through ``require_workspace_context``. Background
    workers call it after loading the job's document so reused sessions cannot
    leak taxonomy reads or writes between workspaces.
    """

    session.info["cangzhi_workspace_id"] = workspace_id
    if workspace_slug is None:
        session.info.pop("cangzhi_workspace_slug", None)
    else:
        session.info["cangzhi_workspace_slug"] = workspace_slug


def clear_workspace_context(session: Session) -> None:
    """Remove a previously bound workspace before resolving a background job."""

    session.info.pop("cangzhi_workspace_id", None)
    session.info.pop("cangzhi_workspace_slug", None)
