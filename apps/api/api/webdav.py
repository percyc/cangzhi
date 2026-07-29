from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.webdav import WebDAVEntry, WebDAVSource
from ..security.secrets import decrypt_secret, encrypt_secret
from ..security.url_safety import URLSecurityError
from ..services.webdav import WebDAVError, propfind, validate_webdav_url

router = APIRouter(prefix="/webdav", tags=["webdav"])
DEFAULT_EXTENSIONS = [".pdf", ".docx", ".md", ".markdown", ".txt"]


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1, max_length=1024)
    username: str = Field(default="", max_length=255)
    password: str = Field(default="", max_length=1024)
    root_path: str = Field(default="/", max_length=2048)
    recursive: bool = True
    trusted_private_network: bool = False
    include_extensions: list[str] = Field(
        default_factory=lambda: DEFAULT_EXTENSIONS.copy()
    )
    ignore_patterns: list[str] = Field(
        default_factory=lambda: [".*", "~$*", "*.tmp", "@eaDir"]
    )

    @field_validator("include_extensions")
    @classmethod
    def normalize_extensions(cls, value: list[str]) -> list[str]:
        result = []
        for item in value:
            extension = item.strip().lower()
            if extension and not extension.startswith("."):
                extension = "." + extension
            if extension and extension not in result:
                result.append(extension)
        return result


async def _source_or_404(db: AsyncSession, source_id: int) -> WebDAVSource:
    source = await db.get(WebDAVSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="WebDAV 知识源不存在")
    return source


@router.get("", response_model=list[dict[str, Any]])
async def list_sources(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(select(WebDAVSource).order_by(WebDAVSource.id.desc()))
    ).scalars()
    return [row.to_public_dict() for row in rows]


@router.post("", response_model=dict[str, Any], status_code=201)
async def create_source(payload: SourceCreate, db: AsyncSession = Depends(get_db)):
    try:
        base_url = validate_webdav_url(
            payload.base_url,
            trusted_private_network=payload.trusted_private_network,
        )
    except URLSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    source = WebDAVSource(
        name=payload.name.strip(),
        base_url=base_url,
        username=payload.username,
        password_cipher=encrypt_secret(payload.password),
        has_password=bool(payload.password),
        root_path="/" + payload.root_path.strip("/"),
        recursive=payload.recursive,
        trusted_private_network=payload.trusted_private_network,
        include_extensions=payload.include_extensions,
        ignore_patterns=payload.ignore_patterns,
        sync_status="idle",
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return source.to_public_dict()


@router.post("/{source_id}/test", response_model=dict[str, Any])
async def test_source(source_id: int, db: AsyncSession = Depends(get_db)):
    source = await _source_or_404(db, source_id)
    try:
        entries = await propfind(
            base_url=source.base_url,
            root_path=source.root_path,
            username=source.username,
            password=decrypt_secret(source.password_cipher),
            trusted_private_network=source.trusted_private_network,
        )
    except (WebDAVError, URLSecurityError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return {
        "ok": True,
        "message": "WebDAV 连接正常",
        "entry_count": len(entries),
    }


@router.post("/{source_id}/scan", response_model=dict[str, Any])
async def scan_source(source_id: int, db: AsyncSession = Depends(get_db)):
    source = await _source_or_404(db, source_id)
    source.sync_status = "scanning"
    source.last_error = None
    await db.commit()
    try:
        remote = await propfind(
            base_url=source.base_url,
            root_path=source.root_path,
            username=source.username,
            password=decrypt_secret(source.password_cipher),
            trusted_private_network=source.trusted_private_network,
        )
    except (WebDAVError, URLSecurityError) as exc:
        source.sync_status = "failed"
        source.last_error = str(exc)
        await db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from None

    now = datetime.now(timezone.utc)
    extensions = set(source.include_extensions or DEFAULT_EXTENSIONS)
    files = [
        entry
        for entry in remote
        if not entry.is_collection
        and PurePosixPath(entry.path).suffix.lower() in extensions
    ]
    existing = {
        entry.remote_path: entry
        for entry in (
            await db.execute(
                select(WebDAVEntry).where(WebDAVEntry.source_id == source.id)
            )
        ).scalars()
    }
    discovered = changed = unchanged = 0
    seen: set[str] = set()
    for remote_entry in files:
        seen.add(remote_entry.path)
        entry = existing.get(remote_entry.path)
        if entry is None:
            entry = WebDAVEntry(
                source_id=source.id,
                remote_path=remote_entry.path,
                state="discovered",
            )
            db.add(entry)
            discovered += 1
        elif (
            entry.etag != remote_entry.etag
            or entry.last_modified != remote_entry.last_modified
            or entry.file_size != remote_entry.size
        ):
            entry.state = "changed"
            changed += 1
        else:
            unchanged += 1
        entry.etag = remote_entry.etag
        entry.last_modified = remote_entry.last_modified
        entry.file_size = remote_entry.size
        entry.content_type = remote_entry.content_type
        entry.last_seen_at = now
    missing = 0
    for path, entry in existing.items():
        if path not in seen:
            entry.state = "missing"
            missing += 1
    source.sync_status = "idle"
    source.last_scan_at = now
    await db.commit()
    return {
        "ok": True,
        "discovered": discovered,
        "changed": changed,
        "unchanged": unchanged,
        "missing": missing,
        "eligible_files": len(files),
    }
