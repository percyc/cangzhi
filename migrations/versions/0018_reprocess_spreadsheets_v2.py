"""Reprocess existing spreadsheets with the two-dimensional schema.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-31 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def _is_legacy_spreadsheet(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("document_type") not in {"xlsx", "xls"}:
        return False
    metadata = payload.get("metadata")
    version = metadata.get("spreadsheet_schema_version") if isinstance(metadata, dict) else None
    return version != 2


def upgrade() -> None:
    documents = sa.table(
        "documents",
        sa.column("id", sa.Integer()),
        sa.column("current_version_id", sa.Integer()),
        sa.column("is_deleted", sa.Boolean()),
    )
    versions = sa.table(
        "document_versions",
        sa.column("id", sa.Integer()),
        sa.column("blob_id", sa.Integer()),
        sa.column("structured_content", sa.JSON()),
        sa.column("processing_status", sa.String()),
    )
    jobs = sa.table(
        "processing_jobs",
        sa.column("id", sa.Integer()),
        sa.column("document_id", sa.Integer()),
        sa.column("document_version_id", sa.Integer()),
        sa.column("stage", sa.String()),
        sa.column("status", sa.String()),
        sa.column("idempotency_key", sa.String()),
        sa.column("retry_count", sa.Integer()),
        sa.column("next_retry_at", sa.DateTime(timezone=True)),
        sa.column("last_error", sa.Text()),
        sa.column("error_details", sa.JSON()),
        sa.column("config_version", sa.String()),
        sa.column("started_at", sa.DateTime(timezone=True)),
        sa.column("finished_at", sa.DateTime(timezone=True)),
    )
    entries = sa.table(
        "webdav_entries",
        sa.column("document_id", sa.Integer()),
        sa.column("state", sa.String()),
        sa.column("last_error", sa.Text()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(
            documents.c.id,
            versions.c.id.label("version_id"),
            versions.c.blob_id,
            versions.c.structured_content,
        )
        .join(versions, versions.c.id == documents.c.current_version_id)
        .where(documents.c.is_deleted.is_(False))
    )
    for document_id, version_id, blob_id, structured_content in rows:
        if not _is_legacy_spreadsheet(structured_content):
            continue
        if blob_id is None:
            connection.execute(
                entries.update()
                .where(
                    entries.c.document_id == document_id,
                    entries.c.state == "synced",
                )
                .values(state="changed", last_error=None)
            )
            continue
        existing_job = connection.execute(
            sa.select(jobs.c.id)
            .where(
                jobs.c.document_version_id == version_id,
                jobs.c.stage.in_(("stored", "parsing")),
            )
            .order_by(jobs.c.id)
            .limit(1)
        ).scalar_one_or_none()
        reset_values = {
            "stage": "stored",
            "status": "created",
            "retry_count": 0,
            "next_retry_at": None,
            "last_error": None,
            "error_details": None,
            "config_version": "2",
            "started_at": None,
            "finished_at": None,
        }
        if existing_job is not None:
            connection.execute(
                jobs.update().where(jobs.c.id == existing_job).values(**reset_values)
            )
        else:
            connection.execute(
                jobs.insert().values(
                    document_id=document_id,
                    document_version_id=version_id,
                    idempotency_key=f"{version_id}:spreadsheet-v2:stored",
                    **reset_values,
                )
            )
        connection.execute(
            versions.update()
            .where(versions.c.id == version_id)
            .values(processing_status="created")
        )


def downgrade() -> None:
    # Reprocessing derives data from unchanged originals. Rolling code back does
    # not need to rewrite already generated content.
    pass
