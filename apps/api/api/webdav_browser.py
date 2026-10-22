"""Administrator-only inventory browsing; no remote network requests."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.webdav import WebDAVSource
from ..services.webdav_browser import browse_entries

router = APIRouter(prefix="/webdav", tags=["webdav"])


@router.get("/{source_id}/browse")
async def browse_source(
    source_id: int,
    directory: str | None = Query(None, max_length=2048),
    q: str = Query("", max_length=200),
    state: Literal["discovered", "changed", "failed", "synced", "ignored",
                   "suspected_missing", "missing"] | None = None,
    recursive: bool = False,
    page: int = Query(1, ge=1, le=100000),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(WebDAVSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="WebDAV 知识源不存在")
    try:
        return await browse_entries(db, source, directory=directory, q=q, state=state,
                                    recursive=recursive, page=page, page_size=page_size)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
