"""Admin API for read-only database sources and their imported snapshots.

Endpoints enforce the workspace filter through ``require_workspace_context``
and the ``DatabaseSource`` / ``DatabaseSnapshot`` row-level scope applied in
``apps.api.services.workspaces``. Passwords are encrypted at rest via
``encrypt_secret``; the API never returns the cipher to the client and the
``password`` write field is optional so a missing body means "keep the
existing password" rather than "drop the password".
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.database_source import DatabaseSnapshot, DatabaseSource
from ..models.documents import Document
from ..security.secrets import encrypt_secret
from ..services.database_source import (
    DatabaseImportResult,
    DatabaseSourceError,
    fetch_database_catalog,
    import_database_table,
    list_database_schemas,
    purge_empty_database_snapshots,
    test_database_connection,
    validate_database_host,
    validate_identifier,
    validate_ssl_mode,
)
from ..services.dataset_freshness import validate_freshness_policy
from ..services.document_lifecycle import (
    trash_documents as trash_document_records,
)
from ..storage import get_storage
from ..storage.base import BlobStorage

router = APIRouter(prefix="/database-sources", tags=["database-sources"])
DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
StorageDependency = Annotated[BlobStorage, Depends(get_storage)]

PASSWORD_KEEP = "keep"
PASSWORD_REPLACE = "replace"
PASSWORD_CLEAR = "clear"
PASSWORD_ACTIONS = {PASSWORD_KEEP, PASSWORD_REPLACE, PASSWORD_CLEAR}

DOCUMENT_ACTION_KEEP = "keep"
DOCUMENT_ACTION_TRASH = "trash"
DOCUMENT_ACTIONS = {DOCUMENT_ACTION_KEEP, DOCUMENT_ACTION_TRASH}


# --------------------------------------------------------------------------
# Request / response models
# --------------------------------------------------------------------------


class DatabaseSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    engine: Literal["postgresql", "mysql"]
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    database_name: str = Field(min_length=1, max_length=255)
    username: str = Field(default="", max_length=255)
    password: str = Field(default="", max_length=1024)
    ssl_mode: str | None = Field(default=None, max_length=32)
    trusted_private_network: bool = False
    freshness_mode: Literal["manual", "background", "strict"] = "background"
    freshness_interval_minutes: int = Field(default=1440, ge=5, le=43_200)
    semantic_refresh_mode: Literal["smart", "full"] = "smart"

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("名称不能为空")
        return cleaned

    @field_validator("host")
    @classmethod
    def _clean_host(cls, value: str) -> str:
        return (value or "").strip()

    @field_validator("database_name", "username")
    @classmethod
    def _clean_str(cls, value: str) -> str:
        return (value or "").strip()


class DatabaseSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database_name: str | None = Field(default=None, min_length=1, max_length=255)
    username: str | None = Field(default=None, max_length=255)
    password_action: Literal["keep", "replace", "clear"] = "keep"
    password: str = Field(default="", max_length=1024)
    ssl_mode: str | None = Field(default=None, max_length=32)
    trusted_private_network: bool | None = None
    is_enabled: bool | None = None
    freshness_mode: Literal["manual", "background", "strict"] | None = None
    freshness_interval_minutes: int | None = Field(default=None, ge=5, le=43_200)
    semantic_refresh_mode: Literal["smart", "full"] | None = None

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("名称不能为空")
        return cleaned

    @field_validator("host")
    @classmethod
    def _clean_host(cls, value: str | None) -> str | None:
        return None if value is None else value.strip()


class ImportTableRequest(BaseModel):
    schema_name: str = Field(min_length=1, max_length=255)
    table: str = Field(min_length=1, max_length=255)

    @field_validator("schema_name", "table")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("标识符不能为空")
        return cleaned


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _password_action(payload: DatabaseSourceUpdate) -> str:
    if payload.password_action not in PASSWORD_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_password_action",
                "message": "密码操作必须是 keep / replace / clear 之一",
            },
        )
    if payload.password_action == PASSWORD_REPLACE and not payload.password:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "password_required",
                "message": "替换密码时必须提供新密码",
            },
        )
    if payload.password_action == PASSWORD_KEEP and payload.password:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "password_action_mismatch",
                "message": "未选择替换密码时不能提交新密码",
            },
        )
    return payload.password_action


def _normalize_host(host: str, port: int, *, trusted_private_network: bool) -> str:
    try:
        return validate_database_host(
            host,
            port=port,
            trusted_private_network=trusted_private_network,
            resolve=False,
        )
    except DatabaseSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _enrich_dict(source: DatabaseSource) -> dict[str, Any]:
    return source.to_public_dict()


async def _snapshot_counts(db: AsyncSession, source_id: int) -> dict[str, int]:
    row = (
        await db.execute(
            select(
                func.count(DatabaseSnapshot.id),
                func.count(DatabaseSnapshot.id).filter(
                    DatabaseSnapshot.row_count == 0
                ),
            ).where(DatabaseSnapshot.source_id == source_id)
        )
    ).one()
    return {
        "total": int(row[0] or 0),
        "empty": int(row[1] or 0),
    }


@router.delete("/{source_id}/snapshots/empty", response_model=dict[str, Any])
async def delete_empty_snapshots(
    source_id: int,
    db: DatabaseSession,
    storage: StorageDependency,
):
    source = await _load_source_or_404(db, source_id)
    result = await purge_empty_database_snapshots(
        db,
        source,
        storage_root=_storage_root_for(storage),
    )
    return {
        "ok": True,
        "affected": result.affected,
        "deleted_artifacts": result.deleted_artifacts,
        "cleanup_warnings": list(result.cleanup_warnings),
    }


async def _load_source_or_404(db: AsyncSession, source_id: int) -> DatabaseSource:
    source = await db.get(DatabaseSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="数据库连接器不存在")
    return source


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@router.get("", response_model=list[dict[str, Any]])
async def list_sources(db: DatabaseSession):
    rows = list(
        (await db.execute(select(DatabaseSource).order_by(DatabaseSource.id.desc())))
        .scalars()
        .all()
    )
    output: list[dict[str, Any]] = []
    for row in rows:
        payload = _enrich_dict(row)
        payload["snapshot_counts"] = await _snapshot_counts(db, row.id)
        output.append(payload)
    return output


@router.post("", response_model=dict[str, Any], status_code=201)
async def create_source(payload: DatabaseSourceCreate, db: DatabaseSession):
    trusted = bool(payload.trusted_private_network)
    host = _normalize_host(payload.host, payload.port, trusted_private_network=trusted)
    database_name = validate_identifier(payload.database_name, "数据库名")
    try:
        ssl_mode = validate_ssl_mode(
            payload.engine,
            payload.ssl_mode
            or ("prefer" if payload.engine == "postgresql" else "required"),
        )
    except DatabaseSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    username = payload.username or ""
    has_password = bool(payload.password)
    try:
        freshness_mode, freshness_interval = validate_freshness_policy(
            payload.freshness_mode, payload.freshness_interval_minutes
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    source = DatabaseSource(
        name=payload.name,
        engine=payload.engine,
        host=host,
        port=payload.port,
        database_name=database_name,
        username=username,
        password_cipher=encrypt_secret(payload.password) if has_password else "",
        has_password=has_password,
        ssl_mode=ssl_mode,
        trusted_private_network=trusted,
        is_enabled=True,
        status="idle",
        freshness_mode=freshness_mode,
        freshness_interval_minutes=freshness_interval,
        semantic_refresh_mode=payload.semantic_refresh_mode,
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    body = _enrich_dict(source)
    body["snapshot_counts"] = await _snapshot_counts(db, source.id)
    return body


@router.get("/{source_id}", response_model=dict[str, Any])
async def get_source(source_id: int, db: DatabaseSession):
    source = await _load_source_or_404(db, source_id)
    body = _enrich_dict(source)
    body["snapshot_counts"] = await _snapshot_counts(db, source.id)
    return body


@router.patch("/{source_id}", response_model=dict[str, Any])
async def update_source(
    source_id: int,
    payload: DatabaseSourceUpdate,
    db: DatabaseSession,
):
    source = await _load_source_or_404(db, source_id)
    action = _password_action(payload)
    requested_database = (
        validate_identifier(payload.database_name, "数据库名")
        if payload.database_name is not None
        else None
    )

    if (
        requested_database is not None
        and requested_database != source.database_name
        and (await _snapshot_counts(db, source.id))["total"] > 0
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "database_scope_has_snapshots",
                "message": (
                    "该连接器已有导入表，不能直接改为另一个数据库。"
                    "请新建连接器；如不再需要旧连接，可删除连接器并选择保留已导入资料。"
                ),
            },
        )

    if (
        payload.host is not None
        or payload.port is not None
        or payload.trusted_private_network is not None
    ):
        host = payload.host if payload.host is not None else source.host
        port = payload.port if payload.port is not None else source.port
        trusted = (
            payload.trusted_private_network
            if payload.trusted_private_network is not None
            else source.trusted_private_network
        )
        source.host = _normalize_host(host, port, trusted_private_network=trusted)
        source.port = port
        source.trusted_private_network = trusted

    if payload.port is not None:
        source.port = payload.port
    if requested_database is not None:
        source.database_name = requested_database
    if payload.username is not None:
        source.username = payload.username.strip()
    if payload.name is not None:
        source.name = payload.name
    if payload.ssl_mode is not None:
        try:
            source.ssl_mode = validate_ssl_mode(source.engine, payload.ssl_mode)
        except DatabaseSourceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    if payload.trusted_private_network is not None:
        source.trusted_private_network = payload.trusted_private_network
    if payload.is_enabled is not None:
        source.is_enabled = payload.is_enabled
    if (
        payload.freshness_mode is not None
        or payload.freshness_interval_minutes is not None
    ):
        try:
            mode, interval = validate_freshness_policy(
                payload.freshness_mode or source.freshness_mode,
                payload.freshness_interval_minutes
                if payload.freshness_interval_minutes is not None
                else source.freshness_interval_minutes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        source.freshness_mode = mode
        source.freshness_interval_minutes = interval
    if payload.semantic_refresh_mode is not None:
        source.semantic_refresh_mode = payload.semantic_refresh_mode

    if action == PASSWORD_REPLACE:
        source.password_cipher = encrypt_secret(payload.password)
        source.has_password = True
    elif action == PASSWORD_CLEAR:
        source.password_cipher = encrypt_secret("")
        source.has_password = False
    # PASSWORD_KEEP leaves both columns unchanged.

    source.status = "idle"
    source.last_error = None
    await db.commit()
    await db.refresh(source)
    body = _enrich_dict(source)
    body["snapshot_counts"] = await _snapshot_counts(db, source.id)
    return body


@router.get("/{source_id}/delete-impact", response_model=dict[str, Any])
async def delete_impact(source_id: int, db: DatabaseSession):
    source = await _load_source_or_404(db, source_id)
    snapshots = list(
        (
            await db.execute(
                select(DatabaseSnapshot).where(DatabaseSnapshot.source_id == source.id)
            )
        )
        .scalars()
        .all()
    )
    document_ids = sorted(
        {
            snapshot.document_id
            for snapshot in snapshots
            if snapshot.document_id is not None
        }
    )
    active = trash = 0
    if document_ids:
        active = int(
            (
                await db.execute(
                    select(func.count(Document.id)).where(
                        Document.id.in_(document_ids),
                        Document.is_deleted.is_(False),
                    )
                )
            ).scalar_one()
            or 0
        )
        trash = int(
            (
                await db.execute(
                    select(func.count(Document.id)).where(
                        Document.id.in_(document_ids),
                        Document.is_deleted.is_(True),
                    )
                )
            ).scalar_one()
            or 0
        )
    return {
        "source_id": source.id,
        "source_name": source.name,
        "snapshot_count": len(snapshots),
        "active_document_count": active,
        "trashed_document_count": trash,
    }


@router.delete("/{source_id}", response_model=dict[str, Any])
async def delete_source(
    source_id: int,
    db: DatabaseSession,
    document_action: Annotated[Literal["keep", "trash"], Query()] = "keep",
):
    source = await _load_source_or_404(db, source_id)
    if document_action not in DOCUMENT_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_document_action",
                "message": "document_action 必须是 keep / trash 之一",
            },
        )
    snapshots = list(
        (
            await db.execute(
                select(DatabaseSnapshot).where(DatabaseSnapshot.source_id == source.id)
            )
        )
        .scalars()
        .all()
    )
    document_ids = sorted(
        {
            snapshot.document_id
            for snapshot in snapshots
            if snapshot.document_id is not None
        }
    )
    documents = (
        list(
            (await db.execute(select(Document).where(Document.id.in_(document_ids))))
            .scalars()
            .all()
        )
        if document_ids
        else []
    )

    if document_action == DOCUMENT_ACTION_TRASH:
        await trash_document_records(
            db,
            documents,
            reason="connector_removed",
            commit=False,
        )
    for document in documents:
        meta = dict(document.meta or {})
        meta["database_source_name"] = source.name
        meta["database_source_id"] = None
        meta["connector_removed"] = True
        document.meta = meta

    # Snapshots cascade-delete via the FK; no need to delete them by hand.
    await db.delete(source)
    await db.commit()
    return {
        "ok": True,
        "affected_documents": len(documents),
        "document_action": document_action,
        "message": (
            "数据库连接器已删除，关联资料已移入回收站"
            if document_action == DOCUMENT_ACTION_TRASH
            else "数据库连接器已删除，已入库资料予以保留"
        ),
    }


@router.post("/{source_id}/test", response_model=dict[str, Any])
async def test_source(source_id: int, db: DatabaseSession):
    source = await _load_source_or_404(db, source_id)
    try:
        info = await test_database_connection(source)
    except DatabaseSourceError as exc:
        source.status = "failed"
        source.last_error = str(exc)
        source.last_tested_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        message = f"连接失败：{exc}"
        source.status = "failed"
        source.last_error = message
        source.last_tested_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(status_code=502, detail=message) from None
    source.status = "idle"
    source.last_error = None
    source.last_tested_at = datetime.now(timezone.utc)
    await db.commit()
    return {
        "ok": True,
        "message": "数据库连接正常",
        "server_version": info.get("server_version", ""),
        "current_database": info.get("current_database", source.database_name),
    }


@router.get("/{source_id}/schemas", response_model=list[str])
async def list_schemas(source_id: int, db: DatabaseSession):
    source = await _load_source_or_404(db, source_id)
    try:
        schemas = await list_database_schemas(source)
    except DatabaseSourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return schemas


@router.get("/{source_id}/catalog", response_model=list[dict[str, Any]])
async def get_catalog(
    source_id: int,
    db: DatabaseSession,
    schema: Annotated[str, Query(min_length=1, max_length=255)],
):
    source = await _load_source_or_404(db, source_id)
    try:
        tables = await fetch_database_catalog(source, schema)
    except DatabaseSourceError as exc:
        if exc.code in {"invalid_identifier", "schema_not_found"}:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        raise HTTPException(status_code=502, detail=str(exc)) from None
    snapshot_rows = (
        await db.execute(
            select(DatabaseSnapshot, Document)
            .join(Document, Document.id == DatabaseSnapshot.document_id)
            .where(
                DatabaseSnapshot.source_id == source.id,
                DatabaseSnapshot.schema_name == schema,
            )
        )
    ).all()
    snapshots = {
        snapshot.table_name: (snapshot, document)
        for snapshot, document in snapshot_rows
    }
    return [
        {
            "schema_name": table.schema_name,
            "table_name": table.table_name,
            "kind": table.kind,
            "imported": table.table_name in snapshots,
            "snapshot": (
                _snapshot_payload(*snapshots[table.table_name])
                if table.table_name in snapshots
                else None
            ),
            "columns": [
                {
                    "name": column.name,
                    "data_type": column.data_type,
                    "nullable": column.nullable,
                    "is_primary_key": column.is_primary_key,
                }
                for column in table.columns
            ],
        }
        for table in tables
    ]


@router.get("/{source_id}/snapshots", response_model=list[dict[str, Any]])
async def list_snapshots(source_id: int, db: DatabaseSession):
    """List imported remote tables, including snapshots no longer in the catalog."""

    source = await _load_source_or_404(db, source_id)
    rows = (
        await db.execute(
            select(DatabaseSnapshot, Document)
            .join(Document, Document.id == DatabaseSnapshot.document_id)
            .where(DatabaseSnapshot.source_id == source.id)
            .order_by(DatabaseSnapshot.schema_name, DatabaseSnapshot.table_name)
        )
    ).all()
    return [_snapshot_payload(snapshot, document) for snapshot, document in rows]


def _snapshot_payload(
    snapshot: DatabaseSnapshot,
    document: Document,
) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "schema_name": snapshot.schema_name,
        "table_name": snapshot.table_name,
        "document_id": snapshot.document_id,
        "document_title": document.title,
        "document_deleted": bool(document.is_deleted),
        "dataset_id": snapshot.dataset_id,
        "row_count": snapshot.row_count,
        "snapshot_at": snapshot.snapshot_at.isoformat()
        if snapshot.snapshot_at
        else None,
        "last_error": snapshot.last_error,
    }


def _storage_root_for(storage: BlobStorage) -> Path:
    candidate = getattr(storage, "_blob_path", None)
    if candidate is None:
        return Path("./storage").resolve()
    return Path(candidate).resolve().parent


@router.post(
    "/{source_id}/tables/import",
    response_model=dict[str, Any],
    status_code=201,
)
async def import_table(
    source_id: int,
    payload: ImportTableRequest,
    db: DatabaseSession,
    storage: StorageDependency,
):
    source = await _load_source_or_404(db, source_id)
    if not source.is_enabled:
        raise HTTPException(status_code=409, detail="数据库连接器已停用，请先启用")
    try:
        result = await import_database_table(
            db,
            source,
            payload.schema_name,
            payload.table,
            storage_root=_storage_root_for(storage),
        )
    except DatabaseSourceError as exc:
        await db.rollback()
        if exc.code in {"invalid_identifier", "schema_not_found", "table_not_found"}:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if exc.code in {"row_limit_exceeded"}:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        if exc.code in {"document_trashed"}:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=502,
            detail=f"导入失败：{exc}",
        ) from exc
    return _import_payload(result)


def _import_payload(result: DatabaseImportResult) -> dict[str, Any]:
    return {
        "ok": True,
        "status": result.status,
        "skip_reason": result.skip_reason,
        "removed_existing": result.removed_existing,
        "document_id": result.document_id,
        "document_version_id": result.document_version_id,
        "dataset_id": result.dataset_id,
        "snapshot_id": result.snapshot_id,
        "version_number": result.version_number,
        "row_count": result.row_count,
        "column_count": result.column_count,
        "fingerprint": result.fingerprint,
        "reused_document": result.reused_document,
        "semantic_refresh_mode": result.semantic_refresh_mode,
        "semantic_reused_fields": result.semantic_reused_fields,
        "semantic_ai_fields": result.semantic_ai_fields,
    }


__all__ = ["router"]
