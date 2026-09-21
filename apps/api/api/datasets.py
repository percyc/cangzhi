from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.datasets import DatasetArtifact, DatasetField, KnowledgeDataset
from ..models.documents import Document
from ..models.table_rows import StructuredTableRow
from ..services.dataset_execution import (
    DatasetExecutionError,
    execute_dataset_query,
    preview_dataset,
)
from .schemas import DatasetFilterInput

router = APIRouter(prefix="/datasets", tags=["datasets"])


class DatasetFieldResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    position: int
    name: str
    inferred_type: str
    semantic_role: str | None
    confidence: float | None
    null_count: int
    distinct_count: int | None
    sample_values: list[Any] = Field(default_factory=list)
    statistics: dict[str, Any] = Field(default_factory=dict)


class DatasetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    document_version_id: int
    name: str
    sheet_name: str
    region_index: int
    dataset_kind: str
    status: str
    row_count: int
    column_count: int
    header_row: int | None
    source_row_start: int | None
    source_row_end: int | None
    profile: dict[str, Any] = Field(default_factory=dict)
    fields: list[DatasetFieldResponse] = Field(default_factory=list)
    execution: dict[str, Any] = Field(default_factory=dict)


class DatasetRowsResponse(BaseModel):
    dataset_id: int
    offset: int
    limit: int
    total: int
    columns: list[str]
    rows: list[dict[str, Any]]


async def _dataset_response(
    db: AsyncSession, dataset: KnowledgeDataset
) -> DatasetResponse:
    fields = list(
        (
            await db.scalars(
                select(DatasetField)
                .where(DatasetField.dataset_id == dataset.id)
                .order_by(DatasetField.position)
            )
        ).all()
    )
    artifact = await db.scalar(
        select(DatasetArtifact).where(
            DatasetArtifact.dataset_id == dataset.id,
            DatasetArtifact.is_active.is_(True),
        )
    )
    return DatasetResponse(
        **{
            column.name: getattr(dataset, column.name)
            for column in KnowledgeDataset.__table__.columns
            if column.name
            in {
                "id",
                "document_id",
                "document_version_id",
                "name",
                "sheet_name",
                "region_index",
                "dataset_kind",
                "status",
                "row_count",
                "column_count",
                "header_row",
                "source_row_start",
                "source_row_end",
                "profile",
            }
        },
        fields=[DatasetFieldResponse.model_validate(field) for field in fields],
        execution={
            "backend": "duckdb"
            if artifact and artifact.status == "ready"
            else "postgresql_fallback",
            "artifact_status": artifact.status if artifact else "pending",
            "artifact_version": artifact.version_number if artifact else None,
            "format": artifact.format if artifact else None,
            "byte_size": artifact.byte_size if artifact else 0,
        },
    )


async def _visible_dataset(db: AsyncSession, dataset_id: int) -> KnowledgeDataset:
    dataset = await db.scalar(
        select(KnowledgeDataset)
        .join(Document, Document.id == KnowledgeDataset.document_id)
        .where(
            KnowledgeDataset.id == dataset_id,
            Document.is_deleted.is_(False),
            Document.current_version_id == KnowledgeDataset.document_version_id,
        )
    )
    if dataset is None:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return dataset


@router.get("", response_model=list[DatasetResponse])
async def list_datasets(
    document_id: int | None = Query(None, ge=1),
    db: AsyncSession = Depends(get_db),
) -> list[DatasetResponse]:
    query = (
        select(KnowledgeDataset)
        .join(Document, Document.id == KnowledgeDataset.document_id)
        .where(
            Document.is_deleted.is_(False),
            Document.current_version_id == KnowledgeDataset.document_version_id,
        )
        .order_by(
            KnowledgeDataset.document_id,
            KnowledgeDataset.sheet_name,
            KnowledgeDataset.region_index,
        )
    )
    if document_id is not None:
        query = query.where(KnowledgeDataset.document_id == document_id)
    datasets = list((await db.scalars(query)).all())
    return [await _dataset_response(db, dataset) for dataset in datasets]


@router.get("/summary", response_model=dict[str, Any])
async def dataset_summary(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Return a lightweight capability hint for the question composer."""

    rows = (
        await db.execute(
            select(
                KnowledgeDataset.id,
                KnowledgeDataset.document_id,
                KnowledgeDataset.name,
                KnowledgeDataset.sheet_name,
                DatasetArtifact.status,
            )
            .join(Document, Document.id == KnowledgeDataset.document_id)
            .outerjoin(
                DatasetArtifact,
                (DatasetArtifact.dataset_id == KnowledgeDataset.id)
                & (DatasetArtifact.is_active.is_(True)),
            )
            .where(
                Document.is_deleted.is_(False),
                Document.current_version_id == KnowledgeDataset.document_version_id,
            )
            .order_by(KnowledgeDataset.id.desc())
        )
    ).all()
    return {
        "dataset_count": len(rows),
        "document_count": len({int(row.document_id) for row in rows}),
        "ready_count": sum(row.status == "ready" for row in rows),
        "examples": [
            {
                "id": int(row.id),
                "name": str(row.name),
                "sheet_name": str(row.sheet_name),
            }
            for row in rows[:3]
        ],
    }


@router.get("/{dataset_id}", response_model=DatasetResponse)
async def get_dataset(
    dataset_id: int, db: AsyncSession = Depends(get_db)
) -> DatasetResponse:
    return await _dataset_response(db, await _visible_dataset(db, dataset_id))


@router.get("/{dataset_id}/rows", response_model=DatasetRowsResponse)
async def preview_dataset_rows(
    dataset_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> DatasetRowsResponse:
    dataset = await _visible_dataset(db, dataset_id)
    fields = list(
        (
            await db.scalars(
                select(DatasetField)
                .where(DatasetField.dataset_id == dataset.id)
                .order_by(DatasetField.position)
            )
        ).all()
    )
    artifact = await db.scalar(
        select(DatasetArtifact).where(
            DatasetArtifact.dataset_id == dataset.id,
            DatasetArtifact.is_active.is_(True),
            DatasetArtifact.status == "ready",
        )
    )
    if artifact is not None:
        try:
            result = await preview_dataset(db, dataset.id, offset=offset, limit=limit)
            return DatasetRowsResponse(
                dataset_id=dataset.id,
                offset=offset,
                limit=limit,
                total=result["matched_row_count"],
                columns=[field.name for field in fields],
                rows=result["rows"],
            )
        except DatasetExecutionError:
            # The catalog remains usable if a local artifact is temporarily
            # missing; the background job can rebuild it without data loss.
            pass
    rows = list(
        (
            await db.scalars(
                select(StructuredTableRow)
                .where(StructuredTableRow.dataset_id == dataset.id)
                .order_by(StructuredTableRow.row_number)
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    total = await db.scalar(
        select(func.count(StructuredTableRow.id)).where(
            StructuredTableRow.dataset_id == dataset.id
        )
    )
    return DatasetRowsResponse(
        dataset_id=dataset.id,
        offset=offset,
        limit=limit,
        total=int(total or 0),
        columns=[field.name for field in fields],
        rows=[{"row_number": row.row_number, **(row.values or {})} for row in rows],
    )


class DatasetQueryPayload(BaseModel):
    filters: list[DatasetFilterInput] = Field(default_factory=list, max_length=8)
    columns: list[str] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list, max_length=3)
    metric: str = "rows"
    metric_column: str | None = None
    sort_by: str | None = None
    sort_order: str = "asc"
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=1_000_000)


@router.post("/{dataset_id}/query", response_model=dict[str, Any])
async def query_dataset(
    dataset_id: int,
    payload: DatasetQueryPayload,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        return await execute_dataset_query(db, dataset_id, payload.model_dump())
    except DatasetExecutionError as exc:
        status = (
            404
            if exc.code == "dataset_not_found"
            else 409
            if exc.code == "artifact_unavailable"
            else 400
        )
        raise HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
