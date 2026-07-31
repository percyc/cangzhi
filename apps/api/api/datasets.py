from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.datasets import DatasetField, KnowledgeDataset
from ..models.documents import Document
from ..models.table_rows import StructuredTableRow

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
