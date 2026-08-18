"""Version-bound evidence reads for the in-conversation drawer.

The :class:`EvidenceService` is the only place that returns the body
content the drawer renders. It is intentionally separate from
:mod:`apps.api.services.knowledge_read` because the contract is
different:

* A draw open event passes a ``document_version_id`` that the
  citation was bound to. The service refuses to return content from
  any other version, so a model that learnt the citation before a
  re-process can never be shown the new content as the original
  evidence.
* The response is shaped for the drawer. For Markdown notes it
  returns the chapter Markdown plus the highlighted snippet
  locations. For PDF/Word it returns the parsed paragraph text plus
  the page number so the front end can drive ``#page=`` on the
  version-locked preview. For datasets it returns the contributing
  rows and the plan that produced them.

The same code path is used by the REST drawer endpoint and by the
MCP ``knowledge_get_evidence`` tool, so an external agent can ask
for the same evidence the UI is looking at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.chunks import DocumentChunk
from ..models.datasets import DatasetArtifact, DatasetField, KnowledgeDataset
from ..models.documents import Document, DocumentSourceType, DocumentVersion
from ..models.table_rows import StructuredTableRow
from .scope_keys import DocumentSelection, candidate_condition


class EvidenceError(LookupError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class EvidenceContext:
    """Resolved drawer payload for a single evidence reference."""

    evidence_type: str
    document_id: int
    document_version_id: int
    title: str
    heading_path: list[str]
    page: int | None
    paragraph_index: int | None
    source_start: int | None
    source_end: int | None
    snippet: str
    context_markdown: str
    highlight_ranges: list[dict[str, int]]
    table_location: dict[str, Any]
    preview_url: str | None
    original_url: str | None
    document_type: str
    dataset: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "evidence_type": self.evidence_type,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "title": self.title,
            "heading_path": list(self.heading_path),
            "page": self.page,
            "paragraph_index": self.paragraph_index,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "snippet": self.snippet,
            "context_markdown": self.context_markdown,
            "highlight_ranges": list(self.highlight_ranges),
            "table_location": dict(self.table_location),
            "preview_url": self.preview_url,
            "original_url": self.original_url,
            "document_type": self.document_type,
        }
        if self.dataset is not None:
            payload["dataset"] = dict(self.dataset)
        return payload


class EvidenceService:
    """Resolve a citation into the drawer payload.

    The service does not own a database session. Each public method
    receives one so a single instance is safe to share between
    requests.
    """

    async def resolve_chunk(
        self,
        db: AsyncSession,
        *,
        chunk_id: int,
        document_version_id: int,
        document_boundary: DocumentSelection | None = None,
    ) -> EvidenceContext:
        chunk, document, version = await self._load_chunk(
            db,
            chunk_id=chunk_id,
            document_version_id=document_version_id,
            document_boundary=document_boundary,
        )
        snippet = (chunk.content or "").strip()
        table_location = _table_location(chunk.extra)
        if table_location.get("sheet_name") or table_location.get("table_ranges"):
            return await self._dataset_context(
                db,
                chunk=chunk,
                document=document,
                version=version,
                snippet=snippet,
                table_location=table_location,
            )
        evidence_type = _classify(
            source_type=document.source_type,
            document_type=_document_type(version),
            table_location=table_location,
        )
        if evidence_type == "markdown":
            section_chunk = (
                await db.get(DocumentChunk, chunk.parent_id)
                if chunk.parent_id is not None
                else None
            )
            context_markdown, ranges = _render_markdown_context(
                snippet=snippet,
                chunk=chunk,
                section_chunk=section_chunk,
            )
        elif evidence_type == "pdf_word":
            context_markdown, ranges = _render_paragraph_context(snippet=snippet)
        else:
            context_markdown, ranges = _render_paragraph_context(snippet=snippet)
        return EvidenceContext(
            evidence_type=evidence_type,
            document_id=document.id,
            document_version_id=version.id,
            title=document.title,
            heading_path=list(chunk.heading_path or []),
            page=chunk.page,
            paragraph_index=chunk.paragraph_index,
            source_start=chunk.source_start,
            source_end=chunk.source_end,
            snippet=snippet,
            context_markdown=context_markdown,
            highlight_ranges=ranges,
            table_location=table_location,
            preview_url=_preview_url(document.id, version.id, evidence_type),
            original_url=_original_url(document.id, version.id, evidence_type),
            document_type=_document_type(version),
        )

    async def resolve_dataset(
        self,
        db: AsyncSession,
        *,
        dataset_id: int,
        document_version_id: int,
        artifact_version: int | None = None,
        document_boundary: DocumentSelection | None = None,
    ) -> EvidenceContext:
        statement = (
            select(KnowledgeDataset)
            .join(Document, Document.id == KnowledgeDataset.document_id)
            .where(
                KnowledgeDataset.id == dataset_id,
                Document.is_deleted.is_(False),
            )
        )
        boundary_condition = candidate_condition(document_boundary)
        if boundary_condition is not None:
            statement = statement.where(boundary_condition)
        dataset = await db.scalar(statement)
        if dataset is None:
            raise EvidenceError("dataset_not_found", "数据集不存在")
        if dataset.document_version_id != document_version_id:
            raise EvidenceError(
                "version_mismatch",
                "该数据集引用已不再属于当前版本，请改用回答中携带的版本号",
            )
        document = await db.get(Document, dataset.document_id)
        if document is None or document.is_deleted:
            raise EvidenceError("document_not_found", "文档不存在")
        version = await db.get(DocumentVersion, document_version_id)
        if version is None or version.document_id != document.id:
            raise EvidenceError("version_not_found", "文档版本不存在，无法打开引用证据")
        fields = list(
            (
                await db.scalars(
                    select(DatasetField)
                    .where(DatasetField.dataset_id == dataset.id)
                    .order_by(DatasetField.position)
                )
            ).all()
        )
        artifact_query = select(DatasetArtifact).where(
            DatasetArtifact.dataset_id == dataset.id
        )
        artifact_query = (
            artifact_query.where(DatasetArtifact.version_number == artifact_version)
            if artifact_version is not None
            else artifact_query.where(DatasetArtifact.is_active.is_(True))
        )
        artifact = await db.scalar(artifact_query)
        if artifact_version is not None and artifact is None:
            raise EvidenceError("artifact_version_mismatch", "引用的数据索引版本不存在")
        return EvidenceContext(
            evidence_type="dataset",
            document_id=document.id,
            document_version_id=version.id,
            title=document.title,
            heading_path=[
                dataset.sheet_name,
                f"数据区域 {dataset.region_index}",
            ],
            page=None,
            paragraph_index=None,
            source_start=None,
            source_end=None,
            snippet=(
                f"{dataset.name} · 区域 {dataset.region_index} · "
                f"{dataset.row_count} 行 / {dataset.column_count} 列"
            ),
            context_markdown="",
            highlight_ranges=[],
            table_location={
                "sheet_name": dataset.sheet_name,
                "region_index": dataset.region_index,
                "row_start": dataset.source_row_start,
                "row_end": dataset.source_row_end,
                "column_names": [field.name for field in fields],
            },
            preview_url=None,
            original_url=None,
            document_type=_document_type(version),
            dataset={
                "dataset_id": dataset.id,
                "document_id": document.id,
                "document_version_id": version.id,
                "artifact_version": artifact.version_number if artifact else None,
                "name": dataset.name,
                "sheet_name": dataset.sheet_name,
                "region_index": dataset.region_index,
                "row_count": dataset.row_count,
                "column_count": dataset.column_count,
                "fields": [
                    {
                        "name": field.name,
                        "inferred_type": field.inferred_type,
                        "semantic_role": field.semantic_role,
                        "sample_values": list(field.sample_values or []),
                    }
                    for field in fields
                ],
            },
        )

    async def preview_dataset_rows(
        self,
        db: AsyncSession,
        *,
        dataset_id: int,
        document_version_id: int,
        artifact_version: int | None = None,
        source_rows: list[int],
        columns: list[str] | None,
        limit: int = 20,
        document_boundary: DocumentSelection | None = None,
    ) -> dict[str, Any]:
        statement = (
            select(KnowledgeDataset)
            .join(Document, Document.id == KnowledgeDataset.document_id)
            .where(
                KnowledgeDataset.id == dataset_id,
                Document.is_deleted.is_(False),
            )
        )
        boundary_condition = candidate_condition(document_boundary)
        if boundary_condition is not None:
            statement = statement.where(boundary_condition)
        dataset = await db.scalar(statement)
        if dataset is None:
            raise EvidenceError("dataset_not_found", "数据集不存在")
        if dataset.document_version_id != document_version_id:
            raise EvidenceError(
                "version_mismatch",
                "数据集引用版本已变化，无法读取贡献行",
            )
        if artifact_version is not None:
            artifact = await db.scalar(
                select(DatasetArtifact).where(
                    DatasetArtifact.dataset_id == dataset.id,
                    DatasetArtifact.version_number == artifact_version,
                )
            )
            if artifact is None:
                raise EvidenceError(
                    "artifact_version_mismatch", "引用的数据索引版本不存在"
                )
        fields = list(
            (
                await db.scalars(
                    select(DatasetField)
                    .where(DatasetField.dataset_id == dataset.id)
                    .order_by(DatasetField.position)
                )
            ).all()
        )
        requested = list(dict.fromkeys(int(item) for item in source_rows if item > 0))
        bounded = requested[: max(1, min(limit, 200))]
        if not bounded:
            return {
                "dataset_id": dataset.id,
                "document_version_id": dataset.document_version_id,
                "rows": [],
                "columns": columns or [field.name for field in fields],
                "returned": 0,
                "requested": len(requested),
                "truncated": False,
            }
        records = list(
            (
                await db.scalars(
                    select(StructuredTableRow)
                    .where(
                        StructuredTableRow.dataset_id == dataset.id,
                        StructuredTableRow.document_version_id == document_version_id,
                        StructuredTableRow.row_number.in_(bounded),
                    )
                    .order_by(StructuredTableRow.row_number)
                )
            ).all()
        )
        allowed_columns = [field.name for field in fields]
        resolved_columns = (
            [column for column in (columns or []) if column in allowed_columns]
            or allowed_columns
            or list((records[0].values or {}).keys() if records else [])
        )
        rows = [
            {
                "row_number": record.row_number,
                **{
                    column: (record.values or {}).get(column)
                    for column in resolved_columns
                },
            }
            for record in records
        ]
        return {
            "dataset_id": dataset.id,
            "document_version_id": dataset.document_version_id,
            "rows": rows,
            "columns": list(resolved_columns),
            "returned": len(rows),
            "requested": len(requested),
            "truncated": len(requested) > len(bounded),
        }

    async def _dataset_context(
        self,
        db: AsyncSession,
        *,
        chunk: DocumentChunk,
        document: Document,
        version: DocumentVersion,
        snippet: str,
        table_location: dict[str, Any],
    ) -> EvidenceContext:
        dataset = await db.scalar(
            select(KnowledgeDataset).where(
                KnowledgeDataset.document_version_id == version.id,
                KnowledgeDataset.sheet_name == table_location.get("sheet_name", ""),
                KnowledgeDataset.region_index
                == int(table_location.get("region_index") or 1),
            )
        )
        fields: list[DatasetField] = []
        artifact: DatasetArtifact | None = None
        if dataset is not None:
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
        dataset_payload: dict[str, Any] | None = None
        if dataset is not None:
            dataset_payload = {
                "dataset_id": dataset.id,
                "document_id": document.id,
                "document_version_id": version.id,
                "artifact_version": artifact.version_number if artifact else None,
                "name": dataset.name,
                "sheet_name": dataset.sheet_name,
                "region_index": dataset.region_index,
                "row_count": dataset.row_count,
                "column_count": dataset.column_count,
                "fields": [
                    {
                        "name": field.name,
                        "inferred_type": field.inferred_type,
                        "semantic_role": field.semantic_role,
                        "sample_values": list(field.sample_values or []),
                    }
                    for field in fields
                ],
            }
        return EvidenceContext(
            evidence_type="dataset",
            document_id=document.id,
            document_version_id=version.id,
            title=document.title,
            heading_path=list(chunk.heading_path or []),
            page=chunk.page,
            paragraph_index=chunk.paragraph_index,
            source_start=chunk.source_start,
            source_end=chunk.source_end,
            snippet=snippet,
            context_markdown="",
            highlight_ranges=[],
            table_location=table_location,
            preview_url=None,
            original_url=None,
            document_type=_document_type(version),
            dataset=dataset_payload,
        )

    async def _load_chunk(
        self,
        db: AsyncSession,
        *,
        chunk_id: int,
        document_version_id: int,
        document_boundary: DocumentSelection | None = None,
    ) -> tuple[DocumentChunk, Document, DocumentVersion]:
        statement = (
            select(DocumentChunk)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.id == chunk_id, Document.is_deleted.is_(False))
        )
        boundary_condition = candidate_condition(document_boundary)
        if boundary_condition is not None:
            statement = statement.where(boundary_condition)
        chunk = await db.scalar(statement)
        if chunk is None:
            raise EvidenceError("chunk_not_found", "知识片段不存在")
        if chunk.document_version_id != document_version_id:
            raise EvidenceError(
                "version_mismatch",
                "片段已属于更新版本，引用证据不再匹配原始问题",
            )
        document = await db.get(Document, chunk.document_id)
        if document is None or document.is_deleted:
            raise EvidenceError("document_not_found", "文档不存在")
        version = await db.get(DocumentVersion, document_version_id)
        if version is None or version.document_id != document.id:
            raise EvidenceError("version_not_found", "文档版本不存在")
        return chunk, document, version


def _table_location(extra: dict | None) -> dict[str, Any]:
    if not extra:
        return {}
    keys = (
        "sheet_name",
        "region_index",
        "row_start",
        "row_end",
        "header_row",
        "column_names",
        "row_fragmented",
        "table_ranges",
    )
    return {key: extra[key] for key in keys if key in extra}


def _classify(
    *,
    source_type: DocumentSourceType,
    document_type: str,
    table_location: dict[str, Any],
) -> str:
    if table_location.get("sheet_name") or table_location.get("table_ranges"):
        return "dataset"
    if source_type == DocumentSourceType.note or document_type in {"markdown", "note"}:
        return "markdown"
    if document_type in {"pdf", "doc", "docx"}:
        return "pdf_word"
    return "document"


def _preview_url(document_id: int, version_id: int, evidence_type: str) -> str | None:
    if evidence_type == "document":
        return None
    if evidence_type == "markdown":
        return f"/api/v1/knowledge/documents/{document_id}?version_id={version_id}"
    return f"/api/documents/{document_id}/preview?version_id={version_id}"


def _original_url(document_id: int, version_id: int, evidence_type: str) -> str | None:
    if evidence_type != "pdf_word":
        return None
    return f"/api/documents/{document_id}/original?inline=true&version_id={version_id}"


def _render_markdown_context(
    *,
    snippet: str,
    chunk: DocumentChunk,
    section_chunk: DocumentChunk | None = None,
) -> tuple[str, list[dict[str, int]]]:
    """Render the surrounding Markdown context the drawer will show.

    The strategy mirrors ``_expanded_chunk_context`` in the QA
    service so the drawer can reproduce what the model saw: take the
    parent chunk (section) if available, otherwise return the chunk
    itself. The snippet is the highlight target; we tag the matched
    substring as a single character range so the renderer can paint
    it.
    """

    body = ((section_chunk.content if section_chunk else chunk.content) or "").strip()
    text, ranges = _highlight(body, snippet)
    return text, ranges


def _render_paragraph_context(*, snippet: str) -> tuple[str, list[dict[str, int]]]:
    """Render a single parsed paragraph with the highlight region."""

    text, ranges = _highlight(snippet, snippet)
    return text, ranges


def _highlight(body: str, needle: str) -> tuple[str, list[dict[str, int]]]:
    if not body:
        return "", []
    if not needle:
        return body, []
    ranges: list[dict[str, int]] = []
    cursor = 0
    while True:
        index = body.find(needle, cursor)
        if index < 0:
            break
        ranges.append({"start": index, "end": index + len(needle)})
        cursor = index + len(needle) or index + 1
    if not ranges:
        return body, []
    return body, ranges


__all__ = [
    "EvidenceContext",
    "EvidenceError",
    "EvidenceService",
    "StructuredTableRow",
]


def _document_type(version: DocumentVersion) -> str:
    structured = version.structured_content or {}
    if isinstance(structured, dict):
        return str(structured.get("document_type") or "").lower()
    return ""
