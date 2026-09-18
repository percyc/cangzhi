"""Stable read-only knowledge API for CLI, Skills, MCP, and agents."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..ai import build_provider_from_db
from ..core.config import settings
from ..core.db import get_db
from ..models.blobs import Blob
from ..models.documents import Document, DocumentSourceType, DocumentVersion
from ..models.exploration_grants import ExplorationGrant
from ..models.processing import ProcessingJob
from ..security.api_auth import (
    APIIdentity,
    extract_token_prefix,
    generate_exploration_grant_token,
    hash_pat_token,
    require_api_identity,
)
from ..security.auth import now_utc
from ..services.ask_history import (
    AskHistoryError,
    get_owned_conversation,
    record_ask_turn,
)
from ..services.dataset_execution import (
    DatasetExecutionError,
    execute_dataset_query,
    get_dataset_schema,
    list_visible_datasets,
    preview_dataset,
)
from ..services.deep_analysis import DeepAnalysisService
from ..services.evidence import EvidenceError, EvidenceService
from ..services.knowledge_read import (
    KnowledgeReadError,
    list_current_documents,
    read_current_chunk,
    read_current_document,
)
from ..services.source_navigation import read_document_map, read_document_block
from ..services.enhancement_read import (
    list_document_enhancements,
    read_enhancement,
    read_overview,
)
from ..services.knowledge_scopes import (
    KnowledgeScopeError,
    KnowledgeScopeResolver,
    list_facet_catalog,
    list_scope_catalog,
)
from ..services.processing_status import (
    ProcessingStatusError,
    compute_processing_status,
)
from ..services.qa import AskError, AskRequest, QAService
from ..services.scope_keys import (
    DocumentSelection,
    candidate_condition,
    list_document_scope_keys,
    normalize_scope_keys,
    replace_document_scope_keys,
)
from ..services.search import DEFAULT_LIMIT, MAX_LIMIT, search_documents
from ..storage import get_storage
from ..storage.base import BlobStorage, BlobTooLargeError
from .files import normalized_filename, validate_file_type
from .schemas import DatasetFilterInput

router = APIRouter(prefix="/v1", tags=["knowledge-v1"])
logger = logging.getLogger(__name__)

_read_identity = require_api_identity("knowledge:read", allow_exploration=True)
_search_identity = require_api_identity("knowledge:search", allow_exploration=True)
_ask_identity = require_api_identity("knowledge:ask", allow_exploration=True)
_write_identity = require_api_identity("documents:write")
MAX_ACTIVE_EXPLORATION_GRANTS = 500


class ScopeKeysPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_keys: list[str] = Field(default_factory=list, max_length=100)


class ScopeKeysBatchItem(ScopeKeysPayload):
    document_id: int | None = Field(default=None, ge=1)
    external_id: str | None = Field(default=None, max_length=64)


class ScopeKeysBatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ScopeKeysBatchItem] = Field(min_length=1, max_length=200)


class DocumentSelectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_keys: list[str] = Field(default_factory=list, max_length=100)
    document_ids: list[int] = Field(default_factory=list, max_length=200)


class ScopeSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_id: int | None = Field(default=None, ge=1)
    scope_slug: str | None = Field(default=None, max_length=128)
    category_ids: list[int] = Field(default_factory=list)
    tag_ids: list[int] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    connector_ids: list[int] = Field(default_factory=list)
    document_selection: DocumentSelectionPayload | None = None


class KnowledgeSearchPayload(ScopeSelector):
    query: str = Field(min_length=1, max_length=512)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(default=0, ge=0, le=10_000)


class KnowledgeAskPayload(ScopeSelector):
    question: str = Field(min_length=1, max_length=500)
    mode: Literal["quick", "deep"] = "quick"
    conversation_id: int | None = Field(default=None, ge=1)
    context_label: str | None = Field(default=None, max_length=512)


class DatasetQueryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: list[DatasetFilterInput] = Field(default_factory=list, max_length=8)
    columns: list[str] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list, max_length=3)
    metric: str = "rows"
    metric_column: str | None = None
    sort_by: str | None = None
    sort_order: str = "asc"
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=1_000_000)


class ExplorationGrantCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_selection: DocumentSelectionPayload
    ttl_seconds: int = Field(default=3600, ge=60, le=86_400)


def _scope_error(exc: KnowledgeScopeError) -> HTTPException:
    status = 404 if exc.code == "scope_not_found" else 400
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": str(exc)},
    )


async def _resolve_scope(db: AsyncSession, payload: ScopeSelector):
    try:
        return await KnowledgeScopeResolver(db).resolve(
            scope_id=payload.scope_id,
            scope_slug=payload.scope_slug,
            category_ids=payload.category_ids,
            tag_ids=payload.tag_ids,
            source_types=payload.source_types,
            connector_ids=payload.connector_ids,
        )
    except KnowledgeScopeError as exc:
        raise _scope_error(exc) from None


def _parse_form_scope_keys(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    if len(values) == 1 and values[0].lstrip().startswith("["):
        try:
            decoded = json.loads(values[0])
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="scope_keys JSON 格式无效") from exc
        if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
            raise HTTPException(status_code=400, detail="scope_keys 必须是字符串数组")
        values = decoded
    try:
        return normalize_scope_keys(values)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _selection(value: DocumentSelectionPayload | None) -> DocumentSelection | None:
    if value is None:
        return None
    try:
        selection = DocumentSelection.coerce(value.scope_keys, value.document_ids)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_document_selection", "message": str(exc)},
        ) from None
    if selection is None or selection.is_empty:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_document_selection",
                "message": "document_selection 至少需要一个 scope_key 或 document_id",
            },
        )
    return selection


def _reject_exploration_history(
    identity: APIIdentity, conversation_id: int | None
) -> None:
    if identity.auth_method == "exploration" and conversation_id is not None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "conversation_history_not_allowed",
                "message": "短期知识探索凭证不允许读取或写入历史对话",
            },
        )


@router.post("/exploration-grants", status_code=201, response_model=dict[str, Any])
async def create_exploration_grant(
    payload: ExplorationGrantCreatePayload,
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Mint a short-lived knowledge credential with an immutable boundary."""

    if identity.auth_method != "pat" or identity.pat_id is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "pat_required",
                "message": "探索凭证必须由工作空间 PAT 创建",
            },
        )
    boundary = _selection(payload.document_selection)
    if boundary is None:
        raise HTTPException(status_code=400, detail="探索范围不能为空")
    workspace_id = db.sync_session.info.get("cangzhi_workspace_id")
    if not isinstance(workspace_id, int):
        raise HTTPException(status_code=400, detail="工作空间上下文不可用")
    now = now_utc()
    active_count = int(
        await db.scalar(
            select(func.count(ExplorationGrant.id)).where(
                ExplorationGrant.created_by_pat_id == identity.pat_id,
                ExplorationGrant.revoked_at.is_(None),
                ExplorationGrant.expires_at > now,
            )
        )
        or 0
    )
    if active_count >= MAX_ACTIVE_EXPLORATION_GRANTS:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "exploration_grant_limit_reached",
                "message": f"单个 PAT 最多保留 {MAX_ACTIVE_EXPLORATION_GRANTS} 个有效探索凭证",
            },
        )
    plaintext = generate_exploration_grant_token()
    grant_scopes = sorted(
        scope
        for scope in identity.scopes
        if scope in {"knowledge:read", "knowledge:search", "knowledge:ask"}
    )
    grant = ExplorationGrant(
        workspace_id=workspace_id,
        admin_id=identity.admin_id,
        created_by_pat_id=identity.pat_id,
        token_hash=hash_pat_token(plaintext),
        token_prefix=extract_token_prefix(plaintext),
        scope_keys=list(boundary.scope_keys),
        document_ids=list(boundary.document_ids),
        scopes=grant_scopes,
        expires_at=now + timedelta(seconds=payload.ttl_seconds),
    )
    db.add(grant)
    await db.commit()
    await db.refresh(grant)
    return {
        "token": plaintext,
        "grant": grant.to_audit_dict(),
        "knowledge_api_path": "/api/v1/knowledge",
        "mcp_path": "/api/mcp",
        "notice": "探索凭证明文只显示这一次，可交给 Skill 或 MCP 客户端。",
    }


@router.post(
    "/exploration-grants/{grant_id}/revoke",
    response_model=dict[str, Any],
)
async def revoke_exploration_grant(
    grant_id: int,
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    if identity.auth_method != "pat" or identity.pat_id is None:
        raise HTTPException(
            status_code=403,
            detail={"code": "pat_required", "message": "必须使用创建凭证的 PAT"},
        )
    grant = await db.scalar(
        select(ExplorationGrant).where(
            ExplorationGrant.id == grant_id,
            ExplorationGrant.created_by_pat_id == identity.pat_id,
        )
    )
    if grant is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "exploration_grant_not_found", "message": "探索凭证不存在"},
        )
    if grant.revoked_at is None:
        grant.revoked_at = now_utc()
        await db.commit()
        await db.refresh(grant)
    return {"grant": grant.to_audit_dict()}


@router.post("/documents", status_code=202, response_model=dict[str, Any])
async def upload_document_v1(
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()] = "",
    external_id: Annotated[str | None, Form(max_length=64)] = None,
    scope_keys: Annotated[list[str] | None, Form()] = None,
    identity: APIIdentity = Depends(_write_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    storage: BlobStorage = Depends(get_storage),  # noqa: B008
) -> dict[str, Any]:
    """Upload or version a document through a workspace-bound API."""

    del identity
    filename = normalized_filename(file.filename)
    content_type = validate_file_type(filename, file.content_type)
    keys = _parse_form_scope_keys(scope_keys)
    cleaned_external_id = (external_id or "").strip() or None
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    try:
        stored = storage.save(file.file, max_bytes=max_bytes)
    except BlobTooLargeError:
        raise HTTPException(status_code=413, detail=f"文件不能超过 {settings.max_upload_size_mb} MB") from None
    finally:
        await file.close()
    if stored.size == 0:
        storage.delete(stored.storage_key)
        raise HTTPException(status_code=400, detail="不能上传空文件")

    cleanup_storage_key: str | None = stored.storage_key
    try:
        document = None
        if cleaned_external_id:
            document = await db.scalar(
                select(Document).where(Document.external_identity == cleaned_external_id)
            )
        current_version = None
        if document is not None and document.current_version_id:
            current_version = await db.get(DocumentVersion, document.current_version_id)
        if document is not None and document.is_deleted:
            raise HTTPException(status_code=409, detail="相同 external_id 的文档已在回收站")

        # Blobs are intentionally global and reference-counted across
        # workspaces; Documents and their scope-key groupings remain isolated.
        existing_blob = await db.scalar(select(Blob).where(Blob.sha256 == stored.sha256))
        if existing_blob is not None:
            storage.delete(stored.storage_key)
            cleanup_storage_key = None
            blob = existing_blob
        else:
            blob = Blob(
                sha256=stored.sha256,
                storage_key=stored.storage_key,
                content_type=content_type,
                file_size=stored.size,
                original_filename=filename,
            )
            db.add(blob)
            await db.flush()

        display_title = (title or "").strip() or Path(filename).stem or filename
        if document is None:
            document = Document(
                title=display_title[:1024],
                source_type=DocumentSourceType.file,
                external_identity=cleaned_external_id,
            )
            db.add(document)
            await db.flush()
            version_number = 1
        elif current_version is not None and current_version.content_hash == stored.sha256:
            if title.strip():
                document.title = display_title[:1024]
            if keys is not None:
                await replace_document_scope_keys(db, document, keys)
            await db.commit()
            return {
                "document_id": document.id,
                "version_id": current_version.id,
                "status": "unchanged",
                "scope_keys": await list_document_scope_keys(db, document.id),
            }
        else:
            if title.strip():
                document.title = display_title[:1024]
            version_number = int(
                await db.scalar(
                    select(func.max(DocumentVersion.version_number)).where(
                        DocumentVersion.document_id == document.id
                    )
                )
                or 0
            ) + 1

        version = DocumentVersion(
            document_id=document.id,
            blob_id=blob.id,
            version_number=version_number,
            content_hash=stored.sha256,
            processing_status="created",
        )
        db.add(version)
        await db.flush()
        document.current_version_id = version.id
        if keys is not None:
            await replace_document_scope_keys(db, document, keys)
        db.add(
            ProcessingJob(
                document_id=document.id,
                document_version_id=version.id,
                stage="stored",
                status="created",
                idempotency_key=f"{version.id}:stored:v1",
                config_version="m1-v1",
            )
        )
        await db.commit()
        cleanup_storage_key = None
        return {
            "document_id": document.id,
            "version_id": version.id,
            "version_number": version.version_number,
            "status": "queued",
            "scope_keys": await list_document_scope_keys(db, document.id),
            "status_url": f"/api/v1/documents/{document.id}/processing-status",
        }
    except Exception:
        await db.rollback()
        if cleanup_storage_key is not None:
            storage.delete(cleanup_storage_key)
        raise


@router.get("/documents/{document_id}/processing-status", response_model=dict[str, Any])
async def get_document_processing_status_v1(
    document_id: int,
    _identity: APIIdentity = Depends(_write_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Return ingestion progress to the same external uploader."""

    try:
        return await compute_processing_status(db, document_id)
    except ProcessingStatusError as exc:
        message = str(exc)
        status_code = 404 if "不存在" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from exc


@router.get("/documents/{document_id}/scope-keys", response_model=dict[str, Any])
async def get_document_scope_keys_v1(
    document_id: int,
    _identity: APIIdentity = Depends(_write_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    document = await db.scalar(select(Document).where(Document.id == document_id, Document.is_deleted.is_(False)))
    if document is None:
        raise HTTPException(status_code=404, detail="知识文档不存在")
    return {"document_id": document.id, "external_id": document.external_identity, "scope_keys": await list_document_scope_keys(db, document.id)}


@router.put("/documents/{document_id}/scope-keys", response_model=dict[str, Any])
async def put_document_scope_keys_v1(
    document_id: int,
    payload: ScopeKeysPayload,
    _identity: APIIdentity = Depends(_write_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    document = await db.scalar(select(Document).where(Document.id == document_id, Document.is_deleted.is_(False)))
    if document is None:
        raise HTTPException(status_code=404, detail="知识文档不存在")
    try:
        keys = await replace_document_scope_keys(db, document, payload.scope_keys)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await db.commit()
    return {"document_id": document.id, "external_id": document.external_identity, "scope_keys": keys}


@router.put("/document-scope-keys/batch", response_model=dict[str, Any])
async def put_document_scope_keys_batch_v1(
    payload: ScopeKeysBatchPayload,
    _identity: APIIdentity = Depends(_write_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for item in payload.items:
        if (item.document_id is None) == (item.external_id is None):
            raise HTTPException(status_code=400, detail="每项必须且只能提供 document_id 或 external_id")
        condition = Document.id == item.document_id if item.document_id is not None else Document.external_identity == item.external_id
        document = await db.scalar(select(Document).where(condition, Document.is_deleted.is_(False)))
        if document is None:
            raise HTTPException(status_code=404, detail="批量项目中的知识文档不存在")
        try:
            keys = await replace_document_scope_keys(db, document, item.scope_keys)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        results.append({"document_id": document.id, "external_id": document.external_identity, "scope_keys": keys})
    await db.commit()
    return {"items": results}


@router.get("/capabilities", response_model=dict[str, Any])
async def capabilities(
    _identity: APIIdentity = Depends(_read_identity),  # noqa: B008
) -> dict[str, Any]:
    return {
        "api_version": "v1",
        "service": "cangzhi",
        "authentication": [
            "bearer_pat",
            "bearer_exploration_grant",
            "session_cookie",
        ],
        "scopes": [
            "knowledge:read",
            "knowledge:search",
            "knowledge:ask",
            "documents:write",
        ],
        "features": {
            "saved_scopes": True,
            "facets": True,
            "lexical_search": True,
            "vector_search": True,
            "cited_qa": True,
            "structured_table_qa": True,
            "deep_analysis": True,
            "dataset_catalog": True,
            "dataset_query": True,
            "dataset_execution_backend": "duckdb_parquet",
            "mcp_ask": True,
            "document_read": True,
            "chunk_read": True,
            "evidence_viewer": True,
            "version_bound_evidence": True,
            "document_upload": True,
            "document_scope_keys": True,
            "document_selection_union": True,
            "document_catalog": True,
            "mcp_exploration_grants": True,
            "rest_exploration_grants": True,
            "enhancement_read": True,
            "enhancement_overview": True,
            "source_navigation": True,
        },
    }


def _dataset_http_error(exc: DatasetExecutionError) -> HTTPException:
    status = {
        "dataset_not_found": 404,
        "artifact_unavailable": 409,
        "artifact_missing": 409,
        "query_timeout": 408,
    }.get(exc.code, 400)
    return HTTPException(
        status_code=status, detail={"code": exc.code, "message": str(exc)}
    )


def _enhancement_http_error(exc: KnowledgeReadError) -> HTTPException:
    """Map read-side enhancement errors; only built artifacts are served."""
    status = {
        "enhancement_stale": 409,
        "stale": 409,
        "version_mismatch": 409,
    }.get(exc.code, 404 if str(exc.code).endswith("_not_found") else 400)
    return HTTPException(
        status_code=status, detail={"code": exc.code, "message": str(exc)}
    )


def _source_navigation_error(exc: KnowledgeReadError) -> HTTPException:
    status = {"source_changed": 409, "structure_unavailable": 409,
              "structure_too_large": 413}.get(exc.code)
    if status is None:
        return _enhancement_http_error(exc)
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


@router.get("/knowledge/documents/{document_id}/map", response_model=dict[str, Any])
async def get_document_map_v1(
    document_id: int,
    view: Literal["outline", "blocks"] = "outline",
    offset: int = Query(default=0, ge=0, le=1_000_000),
    limit: int = Query(default=20, ge=1, le=100),
    scope_keys: list[str] | None = Query(default=None, max_length=100),
    document_ids: list[int] | None = Query(default=None, max_length=200),
    identity: APIIdentity = Depends(_read_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    selection = _selection(DocumentSelectionPayload(scope_keys=scope_keys or [],
        document_ids=document_ids or [])) if scope_keys is not None or document_ids is not None else None
    try:
        return await read_document_map(db, document_id, view=view, offset=offset, limit=limit,
            document_selection=selection, document_boundary=identity.exploration_boundary)
    except KnowledgeReadError as exc:
        raise _source_navigation_error(exc) from None


@router.get("/knowledge/documents/{document_id}/block", response_model=dict[str, Any])
async def get_document_block_v1(
    document_id: int,
    block_id: str = Query(min_length=1, max_length=160),
    offset: int = Query(default=0, ge=0, le=2_000_000),
    max_chars: int = Query(default=4000, ge=1, le=12000),
    scope_keys: list[str] | None = Query(default=None, max_length=100),
    document_ids: list[int] | None = Query(default=None, max_length=200),
    identity: APIIdentity = Depends(_read_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    selection = _selection(DocumentSelectionPayload(scope_keys=scope_keys or [],
        document_ids=document_ids or [])) if scope_keys is not None or document_ids is not None else None
    try:
        return await read_document_block(db, document_id, block_id=block_id, offset=offset,
            max_chars=max_chars, document_selection=selection,
            document_boundary=identity.exploration_boundary)
    except KnowledgeReadError as exc:
        raise _source_navigation_error(exc) from None


@router.get("/knowledge/documents", response_model=dict[str, Any])
async def list_knowledge_documents(
    scope_keys: list[str] | None = Query(default=None),  # noqa: B008
    document_ids: list[int] | None = Query(default=None),  # noqa: B008
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=10_000),
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """List current document metadata, optionally selected by opaque keys."""

    selection = None
    if scope_keys is not None or document_ids is not None:
        selection = _selection(
            DocumentSelectionPayload(
                scope_keys=scope_keys or [], document_ids=document_ids or []
            )
        )
    return await list_current_documents(
        db,
        limit=limit,
        offset=offset,
        document_selection=selection,
        document_boundary=identity.exploration_boundary,
    )


@router.get("/knowledge/datasets", response_model=dict[str, Any])
async def list_datasets_v1(
    document_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=200),
    scope_keys: list[str] | None = Query(default=None),  # noqa: B008
    document_ids: list[int] | None = Query(default=None),  # noqa: B008
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    selection = None
    if scope_keys is not None or document_ids is not None:
        selection = _selection(
            DocumentSelectionPayload(
                scope_keys=scope_keys or [], document_ids=document_ids or []
            )
        )
    datasets = await list_visible_datasets(
        db,
        document_id=document_id,
        limit=limit,
        document_selection=selection,
        document_boundary=identity.exploration_boundary,
    )
    return {
        "items": datasets,
        "limit": limit,
    }


@router.get("/knowledge/datasets/{dataset_id}/schema", response_model=dict[str, Any])
async def get_dataset_schema_v1(
    dataset_id: int,
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        return await get_dataset_schema(
            db, dataset_id, document_boundary=identity.exploration_boundary
        )
    except DatasetExecutionError as exc:
        raise _dataset_http_error(exc) from None


@router.get("/knowledge/datasets/{dataset_id}/rows", response_model=dict[str, Any])
async def preview_dataset_v1(
    dataset_id: int,
    offset: int = Query(default=0, ge=0, le=1_000_000),
    limit: int = Query(default=50, ge=1, le=200),
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        return await preview_dataset(
            db,
            dataset_id,
            offset=offset,
            limit=limit,
            document_boundary=identity.exploration_boundary,
        )
    except DatasetExecutionError as exc:
        raise _dataset_http_error(exc) from None


@router.post("/knowledge/datasets/{dataset_id}/query", response_model=dict[str, Any])
async def query_dataset_v1(
    dataset_id: int,
    payload: DatasetQueryPayload,
    identity: APIIdentity = Depends(_search_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        return await execute_dataset_query(
            db,
            dataset_id,
            payload.model_dump(),
            document_boundary=identity.exploration_boundary,
        )
    except DatasetExecutionError as exc:
        raise _dataset_http_error(exc) from None


@router.get("/knowledge/scopes", response_model=dict[str, Any])
async def list_knowledge_scopes(
    _identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    return {"items": await list_scope_catalog(db)}


@router.get("/knowledge/facets", response_model=dict[str, Any])
async def list_knowledge_facets(
    scope_keys: list[str] | None = Query(default=None),  # noqa: B008
    document_ids: list[int] | None = Query(default=None),  # noqa: B008
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    selection = None
    if scope_keys is not None or document_ids is not None:
        selection = _selection(
            DocumentSelectionPayload(
                scope_keys=scope_keys or [], document_ids=document_ids or []
            )
        )
    return await list_facet_catalog(
        db,
        document_selection=selection,
        document_boundary=identity.exploration_boundary,
    )


@router.post("/knowledge/search", response_model=dict[str, Any])
async def knowledge_search(
    payload: KnowledgeSearchPayload,
    identity: APIIdentity = Depends(_search_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    selection = _selection(payload.document_selection)
    scope = await _resolve_scope(db, payload)
    result = await search_documents(
        db,
        query=payload.query.strip(),
        limit=payload.limit,
        offset=payload.offset,
        category_ids=scope.category_ids,
        tag_ids=scope.tag_ids,
        source_types=scope.source_types,
        document_ids=scope.document_ids,
        connector_ids=scope.connector_ids,
        document_selection=selection,
        document_boundary=identity.exploration_boundary,
        matches_none=scope.matches_none,
    )
    return {
        **result.to_dict(),
        "scope": {
            "id": scope.scope_id,
            "slug": scope.scope_slug,
            "document_selection": payload.document_selection.model_dump()
            if payload.document_selection
            else None,
            **scope.to_filters_dict(),
        },
    }


@router.post("/knowledge/ask", response_model=dict[str, Any])
async def knowledge_ask(
    payload: KnowledgeAskPayload,
    identity: APIIdentity = Depends(_ask_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    selection = _selection(payload.document_selection)
    _reject_exploration_history(identity, payload.conversation_id)
    scope = await _resolve_scope(db, payload)
    if payload.conversation_id is not None:
        try:
            await get_owned_conversation(db, payload.conversation_id, identity.admin_id)
        except AskHistoryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
    provider = await build_provider_from_db(db)
    service = (
        DeepAnalysisService(provider) if payload.mode == "deep" else QAService(provider)
    )
    try:
        result = await service.ask(
            db,
            AskRequest(
                question=payload.question.strip(),
                category_ids=scope.category_ids,
                tag_ids=scope.tag_ids,
                source_types=scope.source_types,
                document_ids=scope.document_ids,
                connector_ids=scope.connector_ids,
                document_selection=selection,
                document_boundary=identity.exploration_boundary,
                matches_none=scope.matches_none,
            ),
        )
    except AskError as exc:
        status = {
            "empty_question": 400,
            "question_too_long": 400,
            "provider_not_configured": 503,
            "provider_failed": 502,
        }.get(exc.code, 500)
        raise HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    response_payload = {
        **result.to_dict(),
        "scope": {
            "id": scope.scope_id,
            "slug": scope.scope_slug,
            "document_selection": payload.document_selection.model_dump()
            if payload.document_selection
            else None,
            **scope.to_filters_dict(),
        },
    }
    if payload.conversation_id is not None:
        turn = await record_ask_turn(
            db,
            conversation_id=payload.conversation_id,
            admin_id=identity.admin_id,
            question=payload.question.strip(),
            mode=payload.mode,
            context_label=payload.context_label,
            scope_snapshot=response_payload["scope"],
            response_snapshot=response_payload,
        )
        response_payload["history"] = {
            "conversation_id": payload.conversation_id,
            "turn_id": turn.id,
        }
    return response_payload


@router.post("/knowledge/ask/stream")
async def knowledge_ask_stream(
    payload: KnowledgeAskPayload,
    identity: APIIdentity = Depends(_ask_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> StreamingResponse:
    """Stream auditable tool progress and finish with the normal answer DTO."""

    selection = _selection(payload.document_selection)
    _reject_exploration_history(identity, payload.conversation_id)
    scope = await _resolve_scope(db, payload)
    if payload.conversation_id is not None:
        try:
            await get_owned_conversation(db, payload.conversation_id, identity.admin_id)
        except AskHistoryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
    provider = await build_provider_from_db(db)
    request = AskRequest(
        question=payload.question.strip(),
        category_ids=scope.category_ids,
        tag_ids=scope.tag_ids,
        source_types=scope.source_types,
        document_ids=scope.document_ids,
        connector_ids=scope.connector_ids,
        document_selection=selection,
        document_boundary=identity.exploration_boundary,
        matches_none=scope.matches_none,
    )
    scope_payload = {
        "id": scope.scope_id,
        "slug": scope.scope_slug,
        "document_selection": payload.document_selection.model_dump()
        if payload.document_selection
        else None,
        **scope.to_filters_dict(),
    }
    if db.bind is None:
        raise HTTPException(status_code=503, detail="数据库连接不可用")
    stream_session_factory = async_sessionmaker(bind=db.bind, expire_on_commit=False)

    async def events() -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        stream_started = asyncio.get_running_loop().time()

        async def on_progress(event: dict[str, Any]) -> None:
            await queue.put({"type": "progress", **event})

        async def run() -> None:
            try:
                async with stream_session_factory() as stream_db:
                    from ..services.workspaces import bind_workspace_context

                    workspace_id = db.sync_session.info.get("cangzhi_workspace_id")
                    if workspace_id is not None:
                        bind_workspace_context(stream_db.sync_session, workspace_id)
                    service = (
                        DeepAnalysisService(provider)
                        if payload.mode == "deep"
                        else QAService(provider)
                    )
                    if isinstance(service, DeepAnalysisService):
                        result = await service.ask(
                            stream_db, request, on_progress=on_progress
                        )
                    else:
                        await on_progress(
                            {"phase": "synthesis", "message": "正在检索并生成回答"}
                        )
                        result = await service.ask(stream_db, request)
                    response_payload = {**result.to_dict(), "scope": scope_payload}
                    if payload.conversation_id is not None:
                        turn = await record_ask_turn(
                            stream_db,
                            conversation_id=payload.conversation_id,
                            admin_id=identity.admin_id,
                            question=payload.question.strip(),
                            mode=payload.mode,
                            context_label=payload.context_label,
                            scope_snapshot=scope_payload,
                            response_snapshot=response_payload,
                        )
                        response_payload["history"] = {
                            "conversation_id": payload.conversation_id,
                            "turn_id": turn.id,
                        }
                await queue.put({"type": "result", "data": response_payload})
            except AskError as exc:
                await queue.put(
                    {
                        "type": "error",
                        "error": {"code": exc.code, "message": str(exc)},
                    }
                )
            except Exception:
                logger.exception("streaming knowledge ask failed")
                await queue.put(
                    {
                        "type": "error",
                        "error": {
                            "code": "internal_error",
                            "message": "问答请求失败，请稍后重试",
                        },
                    }
                )

        task = asyncio.create_task(run())
        yield _ndjson(
            {
                "type": "progress",
                "phase": "starting",
                "message": "正在分析问题",
                "tool_calls": 0,
                "max_tool_calls": DeepAnalysisService.default_max_tool_calls,
            }
        )
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10)
                except TimeoutError:
                    elapsed = int(asyncio.get_running_loop().time() - stream_started)
                    yield _ndjson(
                        {
                            "type": "progress",
                            "phase": "waiting",
                            "message": f"正在等待模型或数据响应（已 {elapsed} 秒）",
                        }
                    )
                    continue
                yield _ndjson(event)
                if event.get("type") in {"result", "error"}:
                    break
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _ndjson(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str) + "\n"


@router.get("/knowledge/chunks/{chunk_id}", response_model=dict[str, Any])
async def get_knowledge_chunk(
    chunk_id: int,
    version_id: int | None = Query(default=None, ge=1),
    include_context: bool = Query(default=False),
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        chunk = await read_current_chunk(
            db, chunk_id, document_boundary=identity.exploration_boundary
        )
    except KnowledgeReadError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    if version_id is not None and chunk["document_version_id"] != version_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "version_mismatch",
                "message": "片段版本已变更，无法作为原始证据返回",
            },
        )
    if not include_context:
        return chunk
    try:
        evidence = await EvidenceService().resolve_chunk(
            db,
            chunk_id=chunk_id,
            document_version_id=chunk["document_version_id"],
            document_boundary=identity.exploration_boundary,
        )
    except EvidenceError as exc:
        raise HTTPException(
            status_code={
                "version_mismatch": 409,
                "artifact_version_mismatch": 409,
                "chunk_not_found": 404,
                "document_not_found": 404,
                "version_not_found": 404,
            }.get(exc.code, 400),
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    chunk["evidence"] = evidence.to_dict()
    return chunk


@router.get("/knowledge/documents/{document_id}", response_model=dict[str, Any])
async def get_knowledge_document(
    document_id: int,
    version_id: int | None = Query(default=None, ge=1),
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        document = await read_current_document(
            db, document_id, document_boundary=identity.exploration_boundary
        )
    except KnowledgeReadError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    if version_id is not None and document["version"]["id"] != version_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "version_mismatch",
                "message": "文档版本已变更，无法作为原始证据返回",
            },
        )
    return document


@router.get(
    "/knowledge/documents/{document_id}/enhancements",
    response_model=dict[str, Any],
)
async def list_document_enhancements_v1(
    document_id: int,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    scope_keys: list[str] | None = Query(default=None, max_length=100),  # noqa: B008
    document_ids: list[int] | None = Query(default=None, max_length=200),  # noqa: B008
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """List only built enhancement artifacts; no model call is made."""
    selection = None
    if scope_keys is not None or document_ids is not None:
        selection = _selection(
            DocumentSelectionPayload(
                scope_keys=scope_keys or [], document_ids=document_ids or []
            )
        )
    try:
        return await list_document_enhancements(
            db,
            document_id,
            limit=limit,
            offset=offset,
            document_selection=selection,
            document_boundary=identity.exploration_boundary,
        )
    except KnowledgeReadError as exc:
        raise _enhancement_http_error(exc) from None


@router.get("/knowledge/enhancements/{run_id}", response_model=dict[str, Any])
async def get_enhancement_v1(
    run_id: int,
    window_index: int = Query(default=0, ge=0),
    view: Literal[
        "summary", "entities", "relations", "events", "evidence"
    ] = Query(default="summary"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=5, ge=1, le=20),
    scope_keys: list[str] | None = Query(default=None, max_length=100),  # noqa: B008
    document_ids: list[int] | None = Query(default=None, max_length=200),  # noqa: B008
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Read one enhancement window view plus its bounded source evidence."""
    selection = None
    if scope_keys is not None or document_ids is not None:
        selection = _selection(
            DocumentSelectionPayload(
                scope_keys=scope_keys or [], document_ids=document_ids or []
            )
        )
    try:
        return await read_enhancement(
            db,
            run_id,
            window_index=window_index,
            view=view,
            offset=offset,
            limit=limit,
            document_selection=selection,
            document_boundary=identity.exploration_boundary,
        )
    except KnowledgeReadError as exc:
        raise _enhancement_http_error(exc) from None


@router.get("/knowledge/enhancements/{run_id}/overview", response_model=dict[str, Any])
async def get_enhancement_overview_v1(
    run_id: int,
    node_key: str | None = Query(default=None, max_length=40),
    scope_keys: list[str] | None = Query(default=None, max_length=100),
    document_ids: list[int] | None = Query(default=None, max_length=200),
    identity: APIIdentity = Depends(_read_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    selection = None
    if scope_keys is not None or document_ids is not None:
        selection = _selection(DocumentSelectionPayload(scope_keys=scope_keys or [], document_ids=document_ids or []))
    try:
        return await read_overview(db, run_id, node_key=node_key, document_selection=selection,
                                   document_boundary=identity.exploration_boundary)
    except KnowledgeReadError as exc:
        raise _enhancement_http_error(exc) from None


async def _document_blob_response(
    db: AsyncSession,
    storage: BlobStorage,
    document_id: int,
    *,
    preview: bool,
    document_boundary: DocumentSelection | None = None,
) -> StreamingResponse:
    statement = (
        select(Document, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.id == Document.current_version_id)
            .where(Document.id == document_id, Document.is_deleted.is_(False))
    )
    boundary_condition = candidate_condition(document_boundary)
    if boundary_condition is not None:
        statement = statement.where(boundary_condition)
    row = (await db.execute(statement)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="知识文档不存在")
    document, version = row
    blob_id = version.preview_blob_id if preview else version.blob_id
    if blob_id is None:
        raise HTTPException(status_code=404, detail="该版本没有可用的预览文件" if preview else "该版本没有已保存的原文件")
    blob = await db.get(Blob, blob_id)
    if blob is None:
        raise HTTPException(status_code=404, detail="文件记录不存在")
    try:
        stream = storage.open(blob.storage_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="文件内容已丢失") from None
    filename = quote(blob.original_filename or (f"{document.title}.pdf" if preview else "download"))
    disposition = "inline" if preview else "attachment"
    return StreamingResponse(
        stream,
        media_type=blob.content_type,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}",
            "Content-Length": str(blob.file_size),
        },
    )


@router.get("/knowledge/documents/{document_id}/original")
async def download_knowledge_document_original(
    document_id: int,
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    storage: BlobStorage = Depends(get_storage),  # noqa: B008
) -> StreamingResponse:
    return await _document_blob_response(
        db,
        storage,
        document_id,
        preview=False,
        document_boundary=identity.exploration_boundary,
    )


@router.get("/knowledge/documents/{document_id}/preview")
async def preview_knowledge_document_original(
    document_id: int,
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    storage: BlobStorage = Depends(get_storage),  # noqa: B008
) -> StreamingResponse:
    return await _document_blob_response(
        db,
        storage,
        document_id,
        preview=True,
        document_boundary=identity.exploration_boundary,
    )


@router.get("/knowledge/evidence/by-chunk/{chunk_id}", response_model=dict[str, Any])
async def get_evidence_by_chunk(
    chunk_id: int,
    document_version_id: int = Query(..., ge=1),
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        evidence = await EvidenceService().resolve_chunk(
            db,
            chunk_id=chunk_id,
            document_version_id=document_version_id,
            document_boundary=identity.exploration_boundary,
        )
    except EvidenceError as exc:
        raise HTTPException(
            status_code={
                "version_mismatch": 409,
                "artifact_version_mismatch": 409,
                "chunk_not_found": 404,
                "document_not_found": 404,
                "version_not_found": 404,
            }.get(exc.code, 400),
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    return evidence.to_dict()


@router.get(
    "/knowledge/evidence/by-dataset/{dataset_id}",
    response_model=dict[str, Any],
)
async def get_evidence_by_dataset(
    dataset_id: int,
    document_version_id: int = Query(..., ge=1),
    artifact_version: int | None = Query(default=None, ge=1),
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        evidence = await EvidenceService().resolve_dataset(
            db,
            dataset_id=dataset_id,
            document_version_id=document_version_id,
            artifact_version=artifact_version,
            document_boundary=identity.exploration_boundary,
        )
    except EvidenceError as exc:
        raise HTTPException(
            status_code={
                "dataset_not_found": 404,
                "version_mismatch": 409,
                "artifact_version_mismatch": 409,
                "document_not_found": 404,
                "version_not_found": 404,
            }.get(exc.code, 400),
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    return evidence.to_dict()


class EvidenceRowPreviewPayload(BaseModel):
    columns: list[str] = Field(default_factory=list, max_length=64)
    source_rows: list[int] = Field(default_factory=list, max_length=200)
    limit: int = Field(default=20, ge=1, le=200)


@router.post(
    "/knowledge/evidence/by-dataset/{dataset_id}/rows",
    response_model=dict[str, Any],
)
async def preview_evidence_rows(
    dataset_id: int,
    payload: EvidenceRowPreviewPayload,
    document_version_id: int = Query(..., ge=1),
    artifact_version: int | None = Query(default=None, ge=1),
    identity: APIIdentity = Depends(_read_identity),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    try:
        return await EvidenceService().preview_dataset_rows(
            db,
            dataset_id=dataset_id,
            document_version_id=document_version_id,
            artifact_version=artifact_version,
            source_rows=payload.source_rows,
            columns=payload.columns,
            limit=payload.limit,
            document_boundary=identity.exploration_boundary,
        )
    except EvidenceError as exc:
        raise HTTPException(
            status_code={
                "dataset_not_found": 404,
                "version_mismatch": 409,
                "artifact_version_mismatch": 409,
                "document_not_found": 404,
                "version_not_found": 404,
            }.get(exc.code, 400),
            detail={"code": exc.code, "message": str(exc)},
        ) from None
