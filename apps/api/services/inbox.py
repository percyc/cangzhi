"""Workspace-scoped inbox filtering over compact, shared pipeline status."""

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.documents import Document
from ..models.taxonomy import Category, DocumentCategory
from ..models.webdav import WebDAVEntry, WebDAVSource
from .processing_status import load_pipeline_statuses

InboxFilter = Literal[
    "attention",
    "all",
    "processing",
    "failed",
    "needs_organization",
    "source_issue",
    "not_vectorized",
    "completed",
]
FILTERS = (
    "attention",
    "all",
    "processing",
    "failed",
    "needs_organization",
    "source_issue",
    "not_vectorized",
    "completed",
)


async def list_inbox(
    db: AsyncSession, *, filter: InboxFilter, offset: int, limit: int
) -> dict:
    # Only identifiers and source membership are needed before pagination.
    documents = (
        await db.execute(
            select(
                Document.id,
                Document.current_version_id,
                Document.meta["external_source"].as_string().label("external_source"),
                Document.meta["webdav_source_id"].as_integer().label("connector_id"),
            )
            .where(Document.is_deleted.is_(False))
            .order_by(Document.updated_at.desc(), Document.id.desc())
        )
    ).all()
    counts = dict.fromkeys(FILTERS, 0)
    matches = []
    # Bound each status read, including old corpora with many chunk records.
    for start in range(0, len(documents), 100):
        batch = documents[start : start + 100]
        version_ids = [d.current_version_id for d in batch if d.current_version_id]
        pipelines = await load_pipeline_statuses(
            db, version_ids, include_extraction=False
        )
        category_rows = (
            await db.execute(
                select(
                    DocumentCategory.document_version_id,
                    Category.id,
                    Category.slug,
                    Category.name,
                )
                .join(Category, Category.id == DocumentCategory.category_id)
                .where(
                    DocumentCategory.document_version_id.in_(version_ids),
                    DocumentCategory.is_primary.is_(True),
                )
                .order_by(DocumentCategory.id)
            )
        ).all()
        categories = {}
        for row in category_rows:
            categories.setdefault(
                row.document_version_id,
                {"id": row.id, "slug": row.slug, "name": row.name},
            )
        connector_ids = [
            d.connector_id
            for d in batch
            if d.external_source == "webdav" and d.connector_id is not None
        ]
        connectors = (
            dict(
                (
                    await db.execute(
                        select(WebDAVSource.id, WebDAVSource.name).where(
                            WebDAVSource.id.in_(connector_ids)
                        )
                    )
                ).all()
            )
            if connector_ids
            else {}
        )
        webdav_ids = [d.id for d in batch if d.external_source == "webdav"]
        entries = (
            dict(
                (
                    await db.execute(
                        select(WebDAVEntry.document_id, WebDAVEntry.state).where(
                            WebDAVEntry.document_id.in_(webdav_ids)
                        )
                    )
                ).all()
            )
            if webdav_ids
            else {}
        )
        for document in batch:
            pipeline = pipelines.get(document.current_version_id)
            if pipeline is None:
                pipeline = {
                    "document_id": document.id,
                    "overall_status": "processing",
                    "keyword_searchable": False,
                    "vector_searchable": False,
                    "stages": {
                        "parsing": {"status": "pending", "message": "等待正文入库"},
                        "understanding": {
                            "status": "pending",
                            "message": "等待前一阶段完成",
                        },
                        "chunking": {
                            "status": "pending",
                            "message": "等待前一阶段完成",
                            "child_chunks": 0,
                        },
                        "embedding": {
                            "status": "pending",
                            "message": "等待前一阶段完成",
                            "completed": 0,
                            "total": 0,
                            "missing": 0,
                            "failed": 0,
                            "model": None,
                        },
                    },
                }
            category = categories.get(document.current_version_id)
            origin = None
            if document.external_source == "webdav":
                origin = {
                    "kind": "webdav",
                    "label": connectors.get(
                        document.connector_id, "已删除的 WebDAV 连接器"
                    ),
                    "connector_available": document.connector_id in connectors,
                    "source_status": entries.get(document.id),
                }
            source_issue = bool(
                origin
                and (
                    not origin["connector_available"]
                    or origin["source_status"] in {"failed", "missing"}
                )
            )
            flags = {
                "all": True,
                "processing": pipeline["overall_status"] == "processing",
                "failed": pipeline["overall_status"] == "failed",
                "completed": pipeline["overall_status"] == "completed",
                "needs_organization": bool(category and category["slug"] == "inbox"),
                "source_issue": source_issue,
                "not_vectorized": not pipeline["vector_searchable"],
            }
            flags["attention"] = (
                not flags["completed"]
                or flags["not_vectorized"]
                or flags["needs_organization"]
                or source_issue
            )
            for key, included in flags.items():
                counts[key] += int(included)
            if flags[filter]:
                matches.append(
                    {
                        "id": document.id,
                        "primary_category": category,
                        "origin": origin,
                        "pipeline": pipeline,
                    }
                )
    page = matches[offset : offset + limit]
    details = (
        {
            row.id: row
            for row in (
                await db.execute(
                    select(
                        Document.id,
                        Document.title,
                        Document.source_type,
                        Document.updated_at,
                    ).where(Document.id.in_([item["id"] for item in page]))
                )
            ).all()
        }
        if page
        else {}
    )
    for item in page:
        row = details[item["id"]]
        item.update(
            title=row.title,
            source_type=row.source_type.value,
            updated_at=row.updated_at,
        )
    return {
        "items": page,
        "total": len(matches),
        "counts": counts,
        "has_processing": counts["processing"] > 0,
    }
