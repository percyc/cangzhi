import hashlib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.documents import Document, DocumentSourceType, DocumentVersion
from ..models.processing import ProcessingJob
from .documents import build_document_response
from .schemas import DocumentResponse

router = APIRouter(prefix="/notes", tags=["notes"])


class NoteCreate(BaseModel):
    title: str = Field(default="", max_length=1024)
    content: str = Field(max_length=2_000_000)

    @field_validator("content")
    @classmethod
    def content_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("内容不能为空")
        return value


class NoteUpdate(NoteCreate):
    pass


def _note_title(title: str, content: str) -> str:
    if title.strip():
        return title.strip()
    first_line = next(
        (line.strip() for line in content.splitlines() if line.strip()),
        "未命名随手记",
    )
    return first_line[:100]


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _processing_job(document_id: int, version_id: int) -> ProcessingJob:
    return ProcessingJob(
        document_id=document_id,
        document_version_id=version_id,
        stage="stored",
        status="created",
        idempotency_key=f"{version_id}:stored:v1",
        config_version="m1-v1",
    )


@router.post("", response_model=DocumentResponse, status_code=201)
async def create_note(
    data: NoteCreate,
    db: AsyncSession = Depends(get_db),
):
    document = Document(
        title=_note_title(data.title, data.content),
        source_type=DocumentSourceType.note,
    )
    db.add(document)
    await db.flush()

    version = DocumentVersion(
        document_id=document.id,
        version_number=1,
        content_hash=_content_hash(data.content),
        raw_content=data.content,
        processing_status="created",
    )
    db.add(version)
    await db.flush()
    document.current_version_id = version.id
    db.add(_processing_job(document.id, version.id))
    await db.commit()
    await db.refresh(document)
    return await build_document_response(db, document)


@router.patch("/{document_id}", response_model=DocumentResponse)
async def update_note(
    document_id: int,
    data: NoteUpdate,
    db: AsyncSession = Depends(get_db),
):
    document = await db.get(Document, document_id)
    if (
        document is None
        or document.is_deleted
        or document.source_type != DocumentSourceType.note
    ):
        raise HTTPException(status_code=404, detail="随手记不存在")

    content_hash = _content_hash(data.content)
    title = _note_title(data.title, data.content)
    current = (
        await db.get(DocumentVersion, document.current_version_id)
        if document.current_version_id is not None
        else None
    )
    if (
        current is not None
        and current.content_hash == content_hash
        and document.title == title
    ):
        return await build_document_response(db, document)

    latest_number = await db.scalar(
        select(func.max(DocumentVersion.version_number)).where(
            DocumentVersion.document_id == document.id
        )
    )
    version = DocumentVersion(
        document_id=document.id,
        version_number=(latest_number or 0) + 1,
        content_hash=content_hash,
        raw_content=data.content,
        processing_status="created",
    )
    db.add(version)
    await db.flush()
    document.title = title
    document.current_version_id = version.id
    db.add(_processing_job(document.id, version.id))
    await db.commit()
    await db.refresh(document)
    return await build_document_response(db, document)
