from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.documents import Document, DocumentSourceType, DocumentVersion
from ..models.processing import ProcessingJob
from ..security import URLSecurityError, normalize_url
from .documents import build_document_response
from .schemas import DocumentResponse

router = APIRouter(prefix="/sources", tags=["sources"])


class URLCreate(BaseModel):
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def _clean(cls, value: str) -> str:
        candidate = (value or "").strip()
        if not candidate:
            raise ValueError("链接不能为空")
        return candidate


@router.post("/url", response_model=DocumentResponse, status_code=201)
async def create_url_source(
    data: URLCreate,
    db: AsyncSession = Depends(get_db),
):
    try:
        normalized = normalize_url(data.url)
    except URLSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    parsed = urlparse(normalized)
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="链接缺少主机名")

    document = Document(
        title=normalized[:1024],
        source_type=DocumentSourceType.url,
        source_url=normalized,
        meta={"original_url": data.url.strip()},
    )
    db.add(document)
    await db.flush()

    version = DocumentVersion(
        document_id=document.id,
        version_number=1,
        content_hash="",
        processing_status="created",
    )
    db.add(version)
    await db.flush()
    document.current_version_id = version.id
    db.add(
        ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage="parsing",
            status="created",
            idempotency_key=f"{version.id}:parsing:url-v1",
            config_version="url-v1",
        )
    )
    await db.commit()
    await db.refresh(document)
    return await build_document_response(db, document)
