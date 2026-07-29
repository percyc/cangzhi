"""Add build progress columns to embedding_profiles and link processing_jobs.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-29 14:00:00

This migration implements the data plane for ADR-015 (M3-6b phase 2).
The phase 1 migration (0008) only recorded "tested" profiles; phase 2
introduces the actual build/activate/rollback flow on top of those
profiles.

What this migration adds
========================

* ``embedding_profiles`` gains six progress-tracking columns:

  * ``total_chunks``, ``completed_chunks``, ``failed_chunks`` — the
    live counters that drive the build progress UI and the
    "ready" gate.
  * ``build_started_at``, ``build_finished_at`` — audit-trail
    timestamps stamped by the API when the build is enqueued and by
    the worker when the last job finishes.
  * ``activated_at`` — the moment the profile became ``active`` so
    the operator can audit "which configuration was in effect when?".

  All six are nullable so the columns work for ``tested`` /
  ``failed`` profiles that never entered the build pipeline. A pair
  of CHECK constraints enforces the obvious invariants
  (``completed_chunks <= total_chunks`` and the failure counter
  relationship) so a buggy client cannot poison the progress view.

* ``processing_jobs`` gains nullable ``embedding_profile_id`` and
  ``embedding_chunk_id`` foreign keys
  foreign key. The column is the explicit association the M3-6b
  prompt calls out: a build job is per profile, not per document
  version. The FK is created with ``ON DELETE CASCADE`` so an
  admin who deletes a profile also drops its orphaned build jobs,
  and the index lets the worker find "give me the next pending
  build job for this profile" efficiently.

  The legacy ``document_id`` / ``document_version_id`` columns stay
  populated for backward compatibility. The explicit chunk FK means
  workers never need to parse an idempotency string to find content.

Why an explicit FK rather than reusing a job key
================================================

The prompt asks for a "more concise and reliable" association
than the existing per-document job. The FK is the most reliable
form: a ``SELECT ... WHERE embedding_profile_id = ?`` is a single
indexed read with no ambiguity, and the worker's "claim next
pending job" can be scoped to a profile trivially. Reusing the
``idempotency_key`` string to encode a profile is fragile: it
mixes concerns (idempotency vs ownership), it requires string
parsing, and it ties the FK to a column that has a different
purpose.

Compatibility notes
===================

* The columns are additive. The phase 1 endpoints (``/test`` and
  ``/status``) keep working unchanged because they only read the
  existing columns.
* The default value of every new column is ``NULL`` (or ``0`` for
  the counters, depending on the NOT NULL flag). Existing
  ``tested`` profiles automatically satisfy the new constraints.
* The migration runs unchanged on PostgreSQL and SQLite; the
  CHECK constraints are written in portable SQL.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    # --- embedding_profiles progress columns -----------------------------
    op.add_column(
        "embedding_profiles",
        sa.Column("total_chunks", sa.Integer(), nullable=True),
    )
    op.add_column(
        "embedding_profiles",
        sa.Column("completed_chunks", sa.Integer(), nullable=True),
    )
    op.add_column(
        "embedding_profiles",
        sa.Column("failed_chunks", sa.Integer(), nullable=True),
    )
    op.add_column(
        "embedding_profiles",
        sa.Column("build_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "embedding_profiles",
        sa.Column("build_finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "embedding_profiles",
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_check_constraint(
        "ck_embedding_profiles_counters_nonneg",
        "embedding_profiles",
        "total_chunks IS NULL OR total_chunks >= 0",
    )
    op.create_check_constraint(
        "ck_embedding_profiles_counters_consistent",
        "embedding_profiles",
        (
            "(total_chunks IS NULL AND completed_chunks IS NULL AND failed_chunks IS NULL)"
            " OR (total_chunks IS NOT NULL AND completed_chunks IS NOT NULL AND failed_chunks IS NOT NULL)"
        ),
    )
    op.create_check_constraint(
        "ck_embedding_profiles_counters_bounded",
        "embedding_profiles",
        (
            "(total_chunks IS NULL) OR "
            "(completed_chunks <= total_chunks AND failed_chunks <= total_chunks)"
        ),
    )
    op.create_check_constraint(
        "ck_embedding_profiles_build_order",
        "embedding_profiles",
        (
            "build_started_at IS NULL OR build_finished_at IS NULL "
            "OR build_finished_at >= build_started_at"
        ),
    )

    op.create_index(
        "ix_embedding_profiles_build_started",
        "embedding_profiles",
        ["build_started_at"],
        unique=False,
    )

    # --- processing_jobs.embedding_profile_id ----------------------------
    op.add_column(
        "processing_jobs",
        sa.Column("embedding_profile_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_processing_jobs_embedding_profile_embedding_profiles",
        "processing_jobs",
        "embedding_profiles",
        ["embedding_profile_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_processing_jobs_embedding_profile",
        "processing_jobs",
        ["embedding_profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_processing_jobs_embedding_stage_status",
        "processing_jobs",
        ["embedding_profile_id", "stage", "status"],
        unique=False,
    )
    op.add_column(
        "processing_jobs",
        sa.Column("embedding_chunk_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_processing_jobs_embedding_chunk_document_chunks",
        "processing_jobs",
        "document_chunks",
        ["embedding_chunk_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_processing_jobs_embedding_chunk",
        "processing_jobs",
        ["embedding_chunk_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_processing_jobs_embedding_chunk", table_name="processing_jobs"
    )
    op.drop_constraint(
        "fk_processing_jobs_embedding_chunk_document_chunks",
        "processing_jobs",
        type_="foreignkey",
    )
    op.drop_column("processing_jobs", "embedding_chunk_id")
    op.drop_index(
        "ix_processing_jobs_embedding_stage_status",
        table_name="processing_jobs",
    )
    op.drop_index(
        "ix_processing_jobs_embedding_profile", table_name="processing_jobs"
    )
    op.drop_constraint(
        "fk_processing_jobs_embedding_profile_embedding_profiles",
        "processing_jobs",
        type_="foreignkey",
    )
    op.drop_column("processing_jobs", "embedding_profile_id")

    op.drop_index(
        "ix_embedding_profiles_build_started", table_name="embedding_profiles"
    )
    op.drop_constraint(
        "ck_embedding_profiles_build_order", "embedding_profiles"
    )
    op.drop_constraint(
        "ck_embedding_profiles_counters_bounded", "embedding_profiles"
    )
    op.drop_constraint(
        "ck_embedding_profiles_counters_consistent", "embedding_profiles"
    )
    op.drop_constraint(
        "ck_embedding_profiles_counters_nonneg", "embedding_profiles"
    )
    op.drop_column("embedding_profiles", "activated_at")
    op.drop_column("embedding_profiles", "build_finished_at")
    op.drop_column("embedding_profiles", "build_started_at")
    op.drop_column("embedding_profiles", "failed_chunks")
    op.drop_column("embedding_profiles", "completed_chunks")
    op.drop_column("embedding_profiles", "total_chunks")
