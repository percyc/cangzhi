"""Unify document lifecycle and durable external identities.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-30 23:40:00
"""

from collections.abc import Sequence
import hashlib
import posixpath
from urllib.parse import unquote, urlsplit

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def _external_identity(base_url: str, remote_path: str) -> str:
    parts = urlsplit((base_url or "").strip())
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    port = parts.port
    default_port = (scheme == "https" and port == 443) or (
        scheme == "http" and port == 80
    )
    authority = hostname if port is None or default_port else f"{hostname}:{port}"
    normalized_path = "/" + posixpath.normpath(
        "/" + unquote(remote_path or "").lstrip("/")
    ).lstrip("/")
    value = f"webdav:{scheme}://{authority}{normalized_path}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.add_column("documents", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.add_column("documents", sa.Column("delete_reason", sa.String(length=64)))
    op.add_column("documents", sa.Column("external_identity", sa.String(length=64)))
    op.create_index("ix_documents_deleted_at", "documents", ["deleted_at"])
    op.create_index(
        "ix_documents_external_identity",
        "documents",
        ["external_identity"],
        unique=True,
    )

    op.add_column(
        "webdav_sources",
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_index("ix_webdav_sources_is_enabled", "webdav_sources", ["is_enabled"])

    op.add_column(
        "webdav_entries", sa.Column("external_identity", sa.String(length=64))
    )
    op.add_column("webdav_entries", sa.Column("resume_state", sa.String(length=32)))
    op.add_column("webdav_entries", sa.Column("ignore_reason", sa.String(length=64)))
    op.add_column(
        "webdav_entries", sa.Column("ignored_at", sa.DateTime(timezone=True))
    )
    op.create_index(
        "ix_webdav_entries_external_identity",
        "webdav_entries",
        ["external_identity"],
    )

    op.create_table(
        "external_item_exclusions",
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("external_identity", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("display_path", sa.String(length=2048)),
        sa.Column(
            "source_snapshot",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_external_item_exclusions_external_identity",
        "external_item_exclusions",
        ["external_identity"],
        unique=True,
    )
    op.create_index(
        "ix_external_item_exclusions_id",
        "external_item_exclusions",
        ["id"],
    )

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT e.id, e.document_id, e.remote_path, s.base_url
            FROM webdav_entries AS e
            JOIN webdav_sources AS s ON s.id = e.source_id
            ORDER BY e.id
            """
        )
    ).mappings()
    used_document_identities: set[str] = set()
    for row in rows:
        identity = _external_identity(row["base_url"], row["remote_path"])
        connection.execute(
            sa.text(
                """
                UPDATE webdav_entries
                SET external_identity = :identity,
                    ignore_reason = CASE
                        WHEN state = 'ignored' THEN 'document_trashed'
                        ELSE ignore_reason
                    END,
                    resume_state = CASE
                        WHEN state = 'ignored' THEN 'synced'
                        ELSE resume_state
                    END
                WHERE id = :entry_id
                """
            ),
            {"identity": identity, "entry_id": row["id"]},
        )
        document_id = row["document_id"]
        if document_id is None or identity in used_document_identities:
            continue
        result = connection.execute(
            sa.text(
                """
                UPDATE documents
                SET external_identity = :identity,
                    deleted_at = CASE
                        WHEN is_deleted THEN COALESCE(updated_at, created_at, now())
                        ELSE deleted_at
                    END,
                    delete_reason = CASE
                        WHEN is_deleted THEN 'user'
                        ELSE delete_reason
                    END
                WHERE id = :document_id
                  AND external_identity IS NULL
                """
            ),
            {"identity": identity, "document_id": document_id},
        )
        if result.rowcount:
            used_document_identities.add(identity)

    connection.execute(
        sa.text(
            """
            UPDATE documents
            SET deleted_at = COALESCE(updated_at, created_at, now()),
                delete_reason = COALESCE(delete_reason, 'user')
            WHERE is_deleted = true AND deleted_at IS NULL
            """
        )
    )

    op.drop_constraint(
        "fk_documents_current_version_id_document_versions",
        "documents",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_documents_current_version_id_document_versions",
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint(
        "document_versions_document_id_fkey",
        "document_versions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "document_versions_document_id_fkey",
        "document_versions",
        "documents",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "processing_jobs_document_id_fkey",
        "processing_jobs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "processing_jobs_document_id_fkey",
        "processing_jobs",
        "documents",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "processing_jobs_document_version_id_fkey",
        "processing_jobs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "processing_jobs_document_version_id_fkey",
        "processing_jobs",
        "document_versions",
        ["document_version_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "processing_jobs_document_version_id_fkey",
        "processing_jobs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "processing_jobs_document_version_id_fkey",
        "processing_jobs",
        "document_versions",
        ["document_version_id"],
        ["id"],
    )
    op.drop_constraint(
        "processing_jobs_document_id_fkey",
        "processing_jobs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "processing_jobs_document_id_fkey",
        "processing_jobs",
        "documents",
        ["document_id"],
        ["id"],
    )
    op.drop_constraint(
        "document_versions_document_id_fkey",
        "document_versions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "document_versions_document_id_fkey",
        "document_versions",
        "documents",
        ["document_id"],
        ["id"],
    )
    op.drop_constraint(
        "fk_documents_current_version_id_document_versions",
        "documents",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_documents_current_version_id_document_versions",
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
    )
    op.drop_table("external_item_exclusions")
    op.drop_index(
        "ix_webdav_entries_external_identity", table_name="webdav_entries"
    )
    op.drop_column("webdav_entries", "ignored_at")
    op.drop_column("webdav_entries", "ignore_reason")
    op.drop_column("webdav_entries", "resume_state")
    op.drop_column("webdav_entries", "external_identity")
    op.drop_index("ix_webdav_sources_is_enabled", table_name="webdav_sources")
    op.drop_column("webdav_sources", "is_enabled")
    op.drop_index("ix_documents_external_identity", table_name="documents")
    op.drop_index("ix_documents_deleted_at", table_name="documents")
    op.drop_column("documents", "external_identity")
    op.drop_column("documents", "delete_reason")
    op.drop_column("documents", "deleted_at")
