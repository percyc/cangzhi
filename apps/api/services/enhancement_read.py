"""Read-only enhancement exploration service for the third batch contract (ADR-024).

Public REST, MCP, CLI and Skill share this module so authorization,
pagination and fingerprint validation stay in one place. Reads never
invoke models, never enqueue jobs, never commit and never mutate
source snapshots. The list function loads no snapshot body when the
candidate set is empty; the read function validates the run-id first
to avoid leaking the existence of runs owned by other workspaces.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, undefer

from ..models.documents import Document, DocumentVersion
from ..models.enhancement import EnhancementRun, EnhancementWindow
from ..models.workspaces import Workspace
from . import knowledge_enhancement as domain
from .document_map import build_document_map
from .knowledge_read import KnowledgeReadError
from .scope_keys import DocumentSelection, candidate_condition

MAX_LIST_LIMIT = 50
DEFAULT_LIST_LIMIT = 20
MAX_READ_LIMIT = 20
DEFAULT_READ_LIMIT = 5
MIN_READ_LIMIT = 1
MAX_OFFSET = 10**6
ALLOWED_VIEWS = frozenset({"summary", "entities", "relations", "events", "evidence"})

__all__ = [
    "list_document_enhancements",
    "read_enhancement",
    "MAX_LIST_LIMIT",
    "DEFAULT_LIST_LIMIT",
    "MAX_READ_LIMIT",
    "DEFAULT_READ_LIMIT",
    "MIN_READ_LIMIT",
    "ALLOWED_VIEWS",
]


def _coerce_positive_id(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise KnowledgeReadError("invalid_arguments", f"{name} 必须是正整数")
    return value


def _coerce_pagination_int(name: str, value, *, min_value: int, max_value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KnowledgeReadError("invalid_arguments", f"{name} 必须是整数")
    if value < min_value or value > max_value:
        raise KnowledgeReadError(
            "invalid_arguments", f"{name} 必须在 {min_value}..{max_value} 范围内"
        )
    return value


def _resolve_view(view) -> str:
    if not isinstance(view, str) or view not in ALLOWED_VIEWS:
        raise KnowledgeReadError("invalid_arguments", "view 取值无效")
    return view


async def _require_workspace(db: AsyncSession) -> Workspace:
    info = db.info if db.info is not None else {}
    workspace_id = info.get("cangzhi_workspace_id")
    if not isinstance(workspace_id, int) or isinstance(workspace_id, bool):
        raise KnowledgeReadError("document_not_found", "知识文档不存在")
    space = await db.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id, Workspace.status == "active"
        )
    )
    if space is None:
        # Do not leak whether the workspace exists or is archived.
        raise KnowledgeReadError("document_not_found", "知识文档不存在")
    return space


async def _current_document(
    db: AsyncSession,
    document_id: int,
    *,
    document_selection: DocumentSelection | None,
    document_boundary: DocumentSelection | None,
):
    space = await _require_workspace(db)
    conditions = [
        Document.id == document_id,
        Document.workspace_id == space.id,
        Document.is_deleted.is_(False),
    ]
    selection_condition = candidate_condition(document_selection)
    if selection_condition is not None:
        conditions.append(selection_condition)
    boundary_condition = candidate_condition(document_boundary)
    if boundary_condition is not None:
        conditions.append(boundary_condition)
    row = (
        await db.execute(
            select(Document, DocumentVersion)
            .options(defer(DocumentVersion.raw_content), defer(DocumentVersion.structured_content))
            .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
            .where(*conditions)
        )
    ).one_or_none()
    if row is None:
        raise KnowledgeReadError("document_not_found", "知识文档不存在")
    return row


def _current_fingerprint(version: DocumentVersion) -> str | None:
    payload = version.structured_content
    if not payload:
        return None
    try:
        return build_document_map(payload, version_id=version.id)["source_fingerprint"]
    except (TypeError, ValueError):
        return None


def _empty_list_payload(
    document: Document, version: DocumentVersion, offset: int, limit: int
) -> dict:
    return {
        "document_id": document.id,
        "document_version_id": version.id,
        "items": [],
        "total": 0,
        "offset": offset,
        "limit": limit,
        "next_offset": None,
    }


async def _summary_via_domain(db: AsyncSession, run: EnhancementRun) -> dict:
    return await db.run_sync(domain.run_summary, run)


async def list_document_enhancements(
    db: AsyncSession,
    document_id: int,
    *,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    document_selection: DocumentSelection | None = None,
    document_boundary: DocumentSelection | None = None,
) -> dict:
    """List runs for the current document version, filtered by source identity.

    Stale runs (older version or different structure) are excluded. The
    source fingerprint is computed once per request; without matching
    runs the function never reads the full snapshots.
    """
    _coerce_positive_id("document_id", document_id)
    offset = _coerce_pagination_int("offset", offset, min_value=0, max_value=MAX_OFFSET)
    limit = _coerce_pagination_int(
        "limit", limit, min_value=1, max_value=MAX_LIST_LIMIT
    )

    document, version = await _current_document(
        db,
        document_id,
        document_selection=document_selection,
        document_boundary=document_boundary,
    )
    candidates = await db.scalar(select(EnhancementRun.id).where(
        EnhancementRun.workspace_id == document.workspace_id,
        EnhancementRun.document_id == document.id,
        EnhancementRun.document_version_id == version.id,
        EnhancementRun.status != "stale",
    ).limit(1))
    if candidates is None:
        return _empty_list_payload(document, version, offset, limit)
    await db.refresh(version, attribute_names=["structured_content"])
    fingerprint = _current_fingerprint(version)
    if fingerprint is None:
        return _empty_list_payload(document, version, offset, limit)

    # Cheap count first: no row body is read when there are no candidates.
    total = int(
        await db.scalar(
            select(func.count(EnhancementRun.id)).where(
                EnhancementRun.document_id == document.id,
                EnhancementRun.document_version_id == version.id,
                EnhancementRun.source_fingerprint == fingerprint,
                EnhancementRun.status != "stale",
            )
        )
        or 0
    )
    if total == 0:
        return _empty_list_payload(document, version, offset, limit)

    # We need attached EnhancementRun instances for the domain run_summary.
    # Defer the heavy source_snapshot column so the list never loads it.
    rows = (
        await db.scalars(
            select(EnhancementRun)
            .options(defer(EnhancementRun.source_snapshot))
            .where(
                EnhancementRun.document_id == document.id,
                EnhancementRun.document_version_id == version.id,
                EnhancementRun.source_fingerprint == fingerprint,
                EnhancementRun.status != "stale",
            )
            .order_by(EnhancementRun.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()

    items = []
    for run in rows:
        items.append(await _summary_via_domain(db, run))
    next_offset = offset + len(items) if offset + len(items) < total else None
    return {
        "document_id": document.id,
        "document_version_id": version.id,
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset,
    }


async def _authenticate_run(
    db: AsyncSession,
    run_id: int,
    *,
    document_selection: DocumentSelection | None,
    document_boundary: DocumentSelection | None,
):
    space = await _require_workspace(db)
    # First, confirm the run belongs to the active workspace without
    # leaking the existence of runs owned by other workspaces.
    run = await db.scalar(
        select(EnhancementRun)
        .options(defer(EnhancementRun.source_snapshot))
        .where(
            EnhancementRun.id == run_id, EnhancementRun.workspace_id == space.id
        )
    )
    if run is None:
        raise KnowledgeReadError("enhancement_not_found", "增强记录不存在")
    try:
        document, version = await _current_document(
            db,
            run.document_id,
            document_selection=document_selection,
            document_boundary=document_boundary,
        )
    except KnowledgeReadError as exc:
        if exc.code == "document_not_found":
            # The run exists but the document is outside the caller's
            # selection/boundary, deleted or otherwise unreachable. Do
            # not distinguish this from "run not found" to avoid leaking
            # the document's existence.
            raise KnowledgeReadError("enhancement_not_found", "增强记录不存在") from None
        raise
    if run.status == "stale" or run.document_version_id != document.current_version_id:
        raise KnowledgeReadError("enhancement_stale", "增强记录已过期")
    await db.refresh(version, attribute_names=["structured_content"])
    fingerprint = _current_fingerprint(version)
    if fingerprint is None or run.source_fingerprint != fingerprint:
        raise KnowledgeReadError("enhancement_stale", "增强记录已过期")
    return run, document, version


def _paginate(items: list, offset: int, limit: int) -> tuple[list, int, int | None]:
    total = len(items)
    sliced = items[offset:offset + limit]
    next_offset = offset + len(sliced) if offset + len(sliced) < total else None
    return sliced, total, next_offset


async def read_enhancement(
    db: AsyncSession,
    run_id: int,
    *,
    window_index: int = 0,
    view: str = "summary",
    offset: int = 0,
    limit: int = DEFAULT_READ_LIMIT,
    document_selection: DocumentSelection | None = None,
    document_boundary: DocumentSelection | None = None,
) -> dict:
    """Return one window's view of an enhancement run, paginated.

    The function never invokes a model, never enqueues a job and never
    commits. Stale runs return ``enhancement_stale``; missing runs
    return ``enhancement_not_found``; document/boundary misses are
    indistinguishable from missing runs to avoid metadata leakage.
    """
    _coerce_positive_id("run_id", run_id)
    window_index = _coerce_pagination_int(
        "window_index", window_index, min_value=0, max_value=MAX_OFFSET
    )
    view = _resolve_view(view)
    offset = _coerce_pagination_int("offset", offset, min_value=0, max_value=MAX_OFFSET)
    limit = _coerce_pagination_int(
        "limit", limit, min_value=MIN_READ_LIMIT, max_value=MAX_READ_LIMIT
    )

    run, _document, _version = await _authenticate_run(
        db,
        run_id,
        document_selection=document_selection,
        document_boundary=document_boundary,
    )

    window = await db.scalar(
        select(EnhancementWindow).where(
            EnhancementWindow.run_id == run.id,
            EnhancementWindow.ordinal == window_index,
        )
    )
    if window is None:
        raise KnowledgeReadError("enhancement_not_found", "增强窗口不存在")

    run_summary = await _summary_via_domain(db, run)
    next_window_index = (
        window_index + 1 if window_index + 1 < run.total_windows else None
    )
    base = {
        "run": run_summary,
        "window_index": window_index,
        "window_status": window.status,
        "view": view,
        "items": [],
        "total": 0,
        "offset": offset,
        "limit": limit,
        "next_offset": None,
        "next_window_index": next_window_index,
        "evidence_status": "model_extracted_unverified",
        "read_only": True,
        "model_calls": 0,
    }
    if window.status != "completed" or not window.result:
        return base

    result = window.result or {}
    if view == "summary":
        summary_data = result.get("summary") or {}
        items = [summary_data] if summary_data.get("text") else []
        sliced, total, next_offset = _paginate(items, offset, limit)
        return {
            **base,
            "items": sliced,
            "total": total,
            "next_offset": next_offset,
        }
    if view == "entities":
        items = list(result.get("entities") or [])
        sliced, total, next_offset = _paginate(items, offset, limit)
        return {**base, "items": sliced, "total": total, "next_offset": next_offset}
    if view == "relations":
        items = list(result.get("relations") or [])
        sliced, total, next_offset = _paginate(items, offset, limit)
        return {**base, "items": sliced, "total": total, "next_offset": next_offset}
    if view == "events":
        items = list(result.get("events") or [])
        sliced, total, next_offset = _paginate(items, offset, limit)
        return {**base, "items": sliced, "total": total, "next_offset": next_offset}
    if view == "evidence":
        # source_segments reads run.source_snapshot; reload the run
        # with the deferred column un-deferred and populate existing.
        full_run = await db.get(
            EnhancementRun,
            run.id,
            options=[undefer(EnhancementRun.source_snapshot)],
            populate_existing=True,
        )
        if full_run is None:
            raise KnowledgeReadError("enhancement_not_found", "增强记录不存在")
        segments = domain.source_segments(full_run, window)
        sliced, total, next_offset = _paginate(segments, offset, limit)
        return {**base, "items": sliced, "total": total, "next_offset": next_offset}
    # _resolve_view already validated; this branch is unreachable.
    return base
